"""Telegram message and command handlers — Phase 3.

Phase 3 changes:
  - handle_message now routes every message through the AI intent classifier
  - Session state (last_intent, last_filters, last_tone, awaiting_clarification)
    is stored in application.bot_data keyed by user_id
  - Conversation history saved to DB on every turn
  - Phase 1 echo removed
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime

from telegram import Update
from telegram.error import Conflict, NetworkError
from telegram.ext import ContextTypes

from spendly.bot.auth import is_authorized
from spendly.core.constants import DEFAULT_TONE, TONES, normalize_tone
from spendly.core.logger import get_logger
from spendly.db.connection import get_connection
from spendly.db.queries import (
    emit_event,
    get_user_by_id,
    get_user_id,
    insert_raw_log,
    update_user_settings,
    upsert_user,
)

log = get_logger(__name__)

_WELCOME = (
    "Hey! Spendly is live 👋\n\n"
    "Just talk to me normally — tell me what you spent, ask about your money, "
    "or anything else.\n\n"
    "Try: *Swiggy 340* or *how much did I spend this week?*"
)


# ── /start ─────────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return

    user = update.effective_user
    conn = await get_connection()
    try:
        await upsert_user(conn, telegram_id=str(user.id), name=user.first_name or user.username)
    finally:
        await conn.close()

    await update.message.reply_text(_WELCOME, parse_mode="Markdown")
    log.info("Start command", extra={"user_id": user.id})


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return

    user = update.effective_user
    conn = await get_connection()
    try:
        uid = await get_user_id(conn, str(user.id))
        user_row = await get_user_by_id(conn, uid)
    finally:
        await conn.close()

    if not user_row:
        return

    from spendly.core.config import settings

    budget = user_row["monthly_budget"] or settings.monthly_budget
    currency = user_row["currency"] or "INR"
    tone = normalize_tone(user_row["tone"] if user_row and user_row["tone"] else DEFAULT_TONE)

    text = (
        "⚙️ *Your Settings*\n\n"
        f"💰 Monthly Budget: `{currency} {budget:,.0f}`\n"
        f"💱 Currency: `{currency}`\n"
        f"🎭 Personality: `{tone.replace('_', ' ').capitalize()}`\n\n"
        "To change these, use:\n"
        "`/set_budget [amount]`\n"
        "`/set_currency [code]`\n"
        "`/set_tone [tone_name]`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_set_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide an amount. Example: `/set_budget 15000`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(context.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid number.")
        return

    user = update.effective_user
    conn = await get_connection()
    try:
        uid = await get_user_id(conn, str(user.id))
        await update_user_settings(conn, uid, monthly_budget=amount)
    finally:
        await conn.close()

    await update.message.reply_text(f"✅ Budget updated to *{amount:,.0f}*.", parse_mode="Markdown")


async def cmd_set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide a currency code. Example: `/set_currency USD`",
            parse_mode="Markdown",
        )
        return

    code = context.args[0].upper().strip()
    if len(code) != 3:
        await update.message.reply_text("Currency code should be 3 letters (e.g., INR, USD, EUR).")
        return

    user = update.effective_user
    conn = await get_connection()
    try:
        uid = await get_user_id(conn, str(user.id))
        await update_user_settings(conn, uid, currency=code)
    finally:
        await conn.close()

    await update.message.reply_text(f"✅ Currency updated to *{code}*.", parse_mode="Markdown")


async def cmd_set_tone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return

    available = ", ".join(f"`{t}`" for t in TONES)
    if not context.args:
        await update.message.reply_text(
            f"Please provide a personality name.\n\nAvailable:\n{available}", parse_mode="Markdown"
        )
        return

    tone = context.args[0].lower().strip()
    if tone not in TONES:
        await update.message.reply_text(
            f"Unknown personality. Pick from:\n{available}", parse_mode="Markdown"
        )
        return

    user = update.effective_user
    conn = await get_connection()
    try:
        uid = await get_user_id(conn, str(user.id))
        await update_user_settings(conn, uid, tone=tone)
        await emit_event(conn, uid, "set_tone", {"tone": tone})
    finally:
        await conn.close()

    await update.message.reply_text(
        f"✅ Tone updated to *{tone.replace('_', ' ').capitalize()}*.", parse_mode="Markdown"
    )


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a detailed system pulse report."""
    if not is_authorized(update):
        return

    from spendly.core.config import settings
    from spendly.db.connection import get_connection

    # ── 1. Uptime ──
    startup_time = context.application.bot_data.get("startup_time", datetime.now(UTC))
    uptime = datetime.now(UTC) - startup_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"

    # ── 2. DB Stats ──
    db_size_mb = os.path.getsize(settings.db_path) / (1024 * 1024)
    disc_usage = shutil.disk_usage(os.path.dirname(settings.db_path))
    disk_free_gb = disc_usage.free / (1024**3)

    # ── 3. DB Queries (Latency, Counts, Events) ──
    conn = await get_connection()
    try:
        # Total Expenses
        async with conn.execute("SELECT count(*) FROM expenses WHERE is_deleted = 0") as cur:
            row = await cur.fetchone()
            total_expenses = row[0] if row else 0

        # User Count
        async with conn.execute("SELECT count(*) FROM users") as cur:
            row = await cur.fetchone()
            total_users = row[0] if row else 0

        # AI Latency (last 50)
        async with conn.execute(
            "SELECT AVG(latency_ms) FROM "
            "(SELECT latency_ms FROM ai_logs ORDER BY created_at DESC LIMIT 50)"
        ) as cur:
            row = await cur.fetchone()
            avg_latency = row[0] if row and row[0] is not None else 0

        # Last Prune
        async with conn.execute(
            "SELECT created_at FROM events "
            "WHERE event_type = 'db_prune' ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            last_prune = row[0] if row else "Never"

        # Last Backup
        async with conn.execute(
            "SELECT created_at FROM events "
            "WHERE event_type = 'gdrive_backup' ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            last_backup = row[0] if row else "Never"

        # WAL Mode
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
            wal_mode = row[0].upper() if row else "UNKNOWN"

    finally:
        await conn.close()

    # ── 4. Scheduler ──
    scheduler = context.application.bot_data.get("scheduler")
    job_count = len(scheduler.get_jobs()) if scheduler else 0

    # ── 5. Format Reply ──
    pulse_msg = (
        "💓 *Spendly System Pulse*\n\n"
        f"⏱ *Uptime:* `{uptime_str}`\n"
        f"🤖 *Gemini Latency:* `{avg_latency:.0f}ms` (avg 50)\n"
        f"📈 *Scheduler:* `{job_count} active jobs`\n\n"
        f"💾 *Database Status*\n"
        f"• Size: `{db_size_mb:.2f} MB`\n"
        f"• WAL Mode: `{wal_mode}`\n"
        f"• Free Disk: `{disk_free_gb:.1f} GB`\n"
        f"• Total Data: `{total_expenses:,} expenses` | `{total_users} users`\n\n"
        f"🧹 *Maintenance*\n"
        f"• Last Prune: `{last_prune}`\n"
        f"• Last Backup: `{last_backup}`\n\n"
        "🌐 *Environment:* `Production`"
    )

    await update.message.reply_text(pulse_msg, parse_mode="Markdown")


# ── Main message handler ───────────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route every text message through Gemini intent classification."""
    if not is_authorized(update):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return

    log.info("Message received", extra={"user_id": user.id, "length": len(text)})

    # Block logging during year-end DB rotation
    if context.application.bot_data.get("logging_blocked"):
        await update.message.reply_text(
            "Expense logging is briefly paused for year-end database rotation. "
            "Please try again in a moment."
        )
        return

    # ── Upsert user + log raw message ─────────────────────────────────────────
    conn = await get_connection()
    try:
        user_id = await upsert_user(
            conn,
            telegram_id=str(user.id),
            name=user.first_name or user.username,
        )
        await insert_raw_log(conn, user_id=user_id, raw_message=text)

        # ── Per-user session state stored in bot_data ──────────────────────────
        # Each user gets their own namespace to avoid cross-user state pollution
        session_key = f"session_{user.id}"
        if session_key not in context.application.bot_data:
            context.application.bot_data[session_key] = {}
        bot_data = context.application.bot_data[session_key]

        # ── Route through AI ───────────────────────────────────────────────────
        from spendly.ai.router import route

        reply, markup = await route(
            conn=conn,
            user_id=user_id,
            user_message=text,
            bot_data=bot_data,
            telegram_context=context,
        )

        # Notify recovery if coming back from a prior failure
        from spendly.bot.scheduler import failure_notifier

        if failure_notifier.in_failure:
            await failure_notifier.on_recovery(context.application)

    except Exception as exc:
        log.error("Error handling message", exc_info=True)
        # Notify owner on first failure, suppress duplicates
        from spendly.bot.scheduler import failure_notifier

        await failure_notifier.on_failure(
            context.application,
            reason=f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        reply = "Something went wrong on my end — couldn't process that right now. I've flagged it."
        markup = None
    finally:
        await conn.close()

    await update.message.reply_text(reply, reply_markup=markup, parse_mode="Markdown")


# ── Voice message handler ──────────────────────────────────────────────────────


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming voice notes by transcribing and parsing via Gemini."""
    if not is_authorized(update):
        return

    user = update.effective_user
    voice = update.message.voice
    if not voice:
        return

    log.info("Voice note received", extra={"user_id": user.id, "duration": voice.duration})

    # Notify processing
    status_msg = await update.message.reply_text("Transcribing your voice note... 🎙️")

    try:
        # Download the file
        file = await context.bot.get_file(voice.file_id)

        from pathlib import Path

        temp_dir = Path("scratch/voice")
        temp_dir.mkdir(parents=True, exist_ok=True)
        file_path = temp_dir / f"{voice.file_unique_id}.ogg"

        await file.download_to_drive(str(file_path))

        # Process via Gemini
        from spendly.ai.voice import process_voice_expense

        # We need a connection and user_id for context
        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, str(user.id), user.first_name or user.username)

            session_key = f"session_{user.id}"
            if session_key not in context.application.bot_data:
                context.application.bot_data[session_key] = {}
            bot_data = context.application.bot_data[session_key]

            reply, markup = await process_voice_expense(
                conn=conn,
                user_id=user_id,
                audio_path=file_path,
                bot_data=bot_data,
                telegram_context=context,
            )
        finally:
            await conn.close()

        # Update status message with the reply
        await status_msg.edit_text(reply, reply_markup=markup, parse_mode="Markdown")

    except Exception:
        log.error("Error handling voice message", exc_info=True)
        await status_msg.edit_text("Sorry, I couldn't process that voice note. 😔")
    finally:
        # Clean up temp file
        if "file_path" in locals() and file_path.exists():
            file_path.unlink()


# ── Callback handler ───────────────────────────────────────────────────────────


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks for confirmations, receipts, and undos."""
    query = update.callback_query
    data = query.data or ""
    user = update.effective_user

    log.info("Callback received", extra={"user_id": user.id, "data": data})

    conn = await get_connection()
    try:
        from spendly.db.queries import upsert_user
        internal_user_id = await upsert_user(conn, telegram_id=str(user.id), name=user.first_name)
        
        session_key = f"session_{user.id}"
        if session_key not in context.application.bot_data:
            context.application.bot_data[session_key] = {}
        bot_data = context.application.bot_data[session_key]

        if data.startswith("mood:"):
            parts = data.split(":")
            if len(parts) == 3:
                expense_id = int(parts[1])
                rating = parts[2]
                from spendly.core.constants import MOODS
                from spendly.db.expenses import get_expense_by_id, update_expense_field
                
                affected = await update_expense_field(conn, expense_id, internal_user_id, "mood_rating", rating)
                if affected:
                    mood_label = MOODS.get(rating, rating.capitalize())
                    await query.answer(f"Rating: {mood_label}")
                    new_text = f"{query.message.text}\n\n*Mood:* {mood_label}"
                    await query.edit_message_text(new_text, parse_mode="Markdown")

        elif data.startswith("confirm:"):
            from spendly.ai.router import route
            reply, markup = await route(conn, internal_user_id, "Yes", bot_data, context)
            await query.answer("Confirmed!")
            await query.edit_message_text(reply, reply_markup=markup, parse_mode="Markdown")

        elif data.startswith("cancel:"):
            from spendly.ai.router import route
            reply, markup = await route(conn, internal_user_id, "No", bot_data, context)
            await query.answer("Cancelled")
            await query.edit_message_text(reply, reply_markup=markup, parse_mode="Markdown")

        elif data.startswith("undo:"):
            expense_id = int(data.split(":")[1])
            from spendly.db.expenses import delete_expense
            await delete_expense(conn, expense_id, internal_user_id)
            await query.answer("Undone")
            await query.edit_message_text("🗑️ Transaction removed.")

        elif data.startswith("undo_income:"):
            income_id = int(data.split(":")[1])
            from spendly.db.incomes import delete_income
            await delete_income(conn, income_id, internal_user_id)
            await query.answer("Undone")
            await query.edit_message_text("🗑️ Income entry removed.")

        elif data.startswith("paid:"):
            # Format: paid:{merchant}:{amount}
            parts = data.split(":")
            if len(parts) >= 3:
                merchant, amount = parts[1], float(parts[2])
                from spendly.db.expenses import insert_expense
                await insert_expense(
                    conn, user_id=internal_user_id, amount=amount, category="Bills", 
                    merchant=merchant, note="Auto-logged from reminder", payment_method="upi",
                    expense_date=datetime.now().date().isoformat()
                )
                await query.answer(f"Logged {merchant}!")
                await query.edit_message_text(f"✅ *{merchant}* (₹{amount:,.0f}) has been logged.")

    except Exception:
        log.error("Callback error", exc_info=True)
        await query.answer("Something went wrong.")
    finally:
        await conn.close()


# ── Global error handler ───────────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every unhandled exception and ping the owner on Telegram."""
    if isinstance(context.error, Conflict):
        log.warning(
            "Telegram polling conflict detected; another getUpdates request is active",
            extra={"error": str(context.error)},
        )
        return

    if isinstance(context.error, NetworkError):
        log.warning(
            "Telegram polling network issue detected; retrying automatically",
            extra={"error": str(context.error)},
        )
        return

    log.error("Unhandled exception", exc_info=context.error)

    owner_id = context.application.bot_data.get("owner_chat_id")
    if owner_id:
        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=(f"⚠️ Spendly error:\n\n`{type(context.error).__name__}: {context.error}`"),
                parse_mode="Markdown",
            )
        except Exception:
            log.error("Could not send error ping to owner", exc_info=True)
