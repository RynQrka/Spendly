"""APScheduler jobs — Phase 8.

All background jobs live here. The scheduler is started in bot/app.py post_init.

Jobs:
1. anomaly_check            — every 6 hours, checks budget threshold breaches
2. proactive_checkin        — daily at 09:00, fixed-time mini summary + prompt
4. year_end_rotation        — 31 Dec 23:58, rotates expense.db
5. update_patterns          — nightly 02:00, recomputes user_patterns
6. end_of_month_reflection  — last day of month 21:00, sends Telegram summary
7. mindfulness_reflection   — weekly Sunday evening coaching
8. subscription_audit        — quarterly bill audit
9. project_threshold_check — daily budget check

Key design:
- Check-in is a single fixed-time message (09:00)
- FailureNotifier deduplicates error pings (first failure → ping, subsequent silently logged)
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime

import aiosqlite
from telegram.ext import Application

from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Job: anomaly check ─────────────────────────────────────────────────────────


async def job_anomaly_check(application: Application) -> None:
    """Check spending categories against budget threshold. Push alerts if crossed."""
    log.info("Anomaly check job started")
    try:
        from spendly.ai.context import build_context
        from spendly.ai.insights import run_anomaly_check
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            ctx = await build_context(conn, user_id)
            alerts = await run_anomaly_check(conn, user_id, ctx)
            for msg in alerts:
                await application.bot.send_message(
                    chat_id=settings.user_id, text=msg, parse_mode="Markdown"
                )
                log.info("Anomaly alert sent", extra={"preview": msg[:60]})
        finally:
            await conn.close()
    except Exception:
        log.error("Anomaly check job failed", exc_info=True)
        await _notify_error(application, "Anomaly check job failed")


# ── Job: proactive check-in ────────────────────────────────────────────────────


async def job_proactive_checkin(application: Application, slot: str = "morning") -> None:
    """Send a warm proactive check-in message to the owner.

    slot: "daily" (single daily check-in) | "morning" | "evening"

    Detects logging gap (2+ days without an expense) and adjusts period type.
    """
    log.info("Proactive check-in job started", extra={"slot": slot})
    try:
        from spendly.ai.context import build_context
        from spendly.ai.insights import generate_checkin_message
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user

        now = datetime.now(settings.timezone)
        hour = now.hour
        period = _resolve_period(slot, hour)

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)

            # Detect logging gap
            period = await _detect_period(conn, user_id, period)

            ctx = await build_context(conn, user_id)
            msg = await generate_checkin_message(conn, user_id, ctx, period=period)
            if msg:
                await application.bot.send_message(
                    chat_id=settings.user_id, text=msg, parse_mode="Markdown"
                )
                log.info("Check-in sent", extra={"period": period, "slot": slot})
        finally:
            await conn.close()
    except Exception:
        log.error("Proactive check-in job failed", exc_info=True)


def _resolve_period(slot: str, hour: int) -> str:
    """Map slot + current hour to a period label."""
    if slot == "daily":
        return "daily_mini"
    if slot == "morning":
        return "morning"
    if slot == "evening":
        return "evening"
    return "morning" if hour < 14 else "evening"


async def _detect_period(conn: object, user_id: int, default_period: str) -> str:
    """Override period to 'logging_gap' if no expense logged in 2+ days."""
    from datetime import date

    from spendly.db.expenses import get_last_expense

    last = await get_last_expense(conn, user_id)
    if not last:
        return default_period

    last_date_str = last.get("expense_date", "")
    try:
        last_date = date.fromisoformat(last_date_str)
        gap = (date.today() - last_date).days
        if gap >= 2:
            log.info("Logging gap detected", extra={"days": gap})
            return "logging_gap"
    except ValueError:
        pass

    return default_period


# ── Job: nightly user patterns update ─────────────────────────────────────────


async def job_update_patterns(application: Application) -> None:
    """Nightly at 02:00 — recompute user_patterns from last 90 days."""
    log.info("User patterns update job started")
    try:
        from spendly.db.connection import get_connection
        from spendly.db.insights import update_user_patterns
        from spendly.db.queries import upsert_user

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            await update_user_patterns(conn, user_id)
            log.info("User patterns update complete")
        finally:
            await conn.close()
    except Exception:
        log.error("User patterns update job failed", exc_info=True)


# ── Job: subscription reminders ────────────────────────────────────────────────


# ── Job: bill reminder nudges ──────────────────────────────────────────────────


async def job_bill_nudge(application: Application, subscription_id: int) -> None:
    """Send a proactive reminder for a bill due soon.

    Includes a 'Mark as Paid' button.
    """
    log.info("Bill nudge job started", extra={"subscription_id": subscription_id})
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from spendly.db.connection import get_connection

        conn = await get_connection()
        try:
            # We don't know the type, so we union
            async with conn.execute(
                """
                SELECT merchant, amount, category FROM recurring_expenses WHERE id = ?
                UNION ALL
                SELECT source as merchant, amount, 'Income' as category FROM recurring_incomes WHERE id = ?
                """,
                (subscription_id, subscription_id),
            ) as cur:
                row = await cur.fetchone()

            if not row:
                return

            msg = (
                "🔔 *Bill Reminder*\n\n"
                "Due today: *"
                f"{row['merchant']}* — *₹{row['amount']}*.\n\n"
                "Log it now or tap below if you've already paid."
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Mark as Paid",
                        callback_data=f"paid:{row['merchant']}:{row['amount']}",
                    )
                ]
            ]

            await application.bot.send_message(
                chat_id=settings.user_id,
                text=msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

            # Update last_reminded_at
            # Since we unioned, we might update both if IDs overlap, but that's fine for nudges,
            # or we could be smarter, but usually this is just a best effort flag.
            await conn.execute(
                "UPDATE recurring_expenses SET last_reminded_at = ? WHERE id = ?",
                (datetime.now().isoformat(), subscription_id),
            )
            await conn.execute(
                "UPDATE recurring_incomes SET last_reminded_at = ? WHERE id = ?",
                (datetime.now().isoformat(), subscription_id),
            )
            await conn.commit()
            log.info("Bill nudge sent", extra={"merchant": row["merchant"]})
        finally:
            await conn.close()
    except Exception:
        log.error("Bill nudge job failed", exc_info=True)


async def job_reschedule_bill_nudges(application: Application) -> None:
    """Daily at 09:05 — send reminders only for bills due today (once)."""
    from datetime import date

    from spendly.db.connection import get_connection
    from spendly.db.queries import upsert_user
    from spendly.db.recurring import (
        find_logged_expense_for_subscription_period,
        subscription_due_today,
        was_subscription_reminded_today,
    )

    today = date.today()

    conn = await get_connection()
    try:
        user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)

        subs = await subscription_due_today(conn, user_id, today=today)
        for sub in subs:
            txn_type = sub.get("transaction_type", "expense")
            if txn_type == "expense":
                if await find_logged_expense_for_subscription_period(conn, user_id, sub, today=today):
                    continue
            else:
                # Assuming find_logged_income_for_subscription_period exists (we just created it)
                from spendly.db.recurring import find_logged_income_for_subscription_period
                if await find_logged_income_for_subscription_period(conn, user_id, sub, today=today):
                    continue

            if await was_subscription_reminded_today(conn, sub["id"], txn_type, today=today):
                continue
            await job_bill_nudge(application, subscription_id=sub["id"])
    finally:
        await conn.close()


# ── Job: process recurring expenses ──────────────────────────────────────────


# ── Job: process recurring expenses ──────────────────────────────────────────


async def job_process_recurring_expenses(application: Application) -> None:
    """Daily at 00:05 — Check all active recurring items and auto-log if due."""
    log.info("Process recurring expenses job started")
    try:
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user
        from spendly.db.recurring import process_recurring_expenses

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            logged_items = await process_recurring_expenses(conn, user_id)

            if logged_items:
                # Notify the user
                msg = "📝 *Auto-Logged Recurring Expenses*\n\n"
                for item in logged_items:
                    msg += f"• *{item['merchant']}* (₹{item['amount']}) in {item['category']}\n"
                msg += "\nThese have been added to your expenses and reports."

                await application.bot.send_message(
                    chat_id=settings.user_id,
                    text=msg,
                    parse_mode="Markdown",
                )
                log.info("Auto-log notification sent", extra={"count": len(logged_items)})
        finally:
            await conn.close()
    except Exception:
        log.error("Process recurring expenses job failed", exc_info=True)


# ── Job: weekly mindfulness reflection ──────────────────────────────────────


async def job_weekly_mindfulness(application: Application) -> None:
    """Sunday 16:00-19:00 — pick a random time to send the nudge."""
    scheduler = application.bot_data.get("scheduler")
    if not scheduler:
        return

    # Pick a random time for today (Sunday)
    hour = random.randint(16, 18)
    minute = random.randint(0, 59)

    scheduler.add_job(
        _run_mindfulness_nudge,
        trigger="date",
        run_date=datetime.now().replace(hour=hour, minute=minute),
        kwargs={"application": application},
        id="weekly_mindfulness_nudge",
        replace_existing=True,
    )
    log.info("Mindfulness nudge scheduled", extra={"at": f"{hour:02d}:{minute:02d}"})


async def _run_mindfulness_nudge(application: Application) -> None:
    log.info("Running mindfulness nudge")
    try:
        from spendly.ai.context import build_context
        from spendly.ai.reflection import generate_mindfulness_reflection
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            ctx = await build_context(conn, user_id)
            msg = await generate_mindfulness_reflection(conn, user_id, ctx)
            if msg:
                await application.bot.send_message(
                    chat_id=settings.user_id, text=msg, parse_mode="Markdown"
                )
                log.info("Mindfulness nudge sent")
        finally:
            await conn.close()
    except Exception:
        log.error("Mindfulness nudge failed", exc_info=True)


# ── Job: subscription audit ──────────────────────────────────────────────────


async def job_subscription_audit(application: Application) -> None:
    """Every 3 months — check for price hikes and zombie subs."""
    log.info("Subscription audit job started")
    try:
        from spendly.ai.reflection import generate_subscription_audit
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            msg = await generate_subscription_audit(conn, user_id)
            if msg:
                await application.bot.send_message(
                    chat_id=settings.user_id, text=msg, parse_mode="Markdown"
                )
                log.info("Subscription audit sent")
        finally:
            await conn.close()
    except Exception:
        log.error("Subscription audit failed", exc_info=True)


# ── Job: project threshold alert ─────────────────────────────────────────────


async def job_project_threshold_check(application: Application) -> None:
    """Daily — check if any project budget crossed 80%."""
    log.info("Project threshold check job started")
    try:
        from spendly.ai.projects import check_project_thresholds
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            alerts = await check_project_thresholds(conn, user_id)
            for msg in alerts:
                await application.bot.send_message(
                    chat_id=settings.user_id, text=msg, parse_mode="Markdown"
                )
                log.info("Project alert sent")
        finally:
            await conn.close()
    except Exception:
        log.error("Project threshold check failed", exc_info=True)


async def job_prune_deleted_expenses(application: Application) -> None:
    """Weekly — move expenses where is_deleted=1 and updated_at > 30 days old to audit_expenses."""
    log.info("Prune deleted expenses job started")
    try:
        from spendly.db.connection import get_connection
        from spendly.db.queries import emit_event, upsert_user

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)

            # Subquery to move rows
            async with conn.execute(
                """
                INSERT INTO audit_expenses (
                    id, user_id, amount, category, merchant, note,
                    payment_method, expense_date, tags, source,
                    idempotency_key, mood_rating, created_at, updated_at, archived_at
                )
                SELECT
                    id, user_id, amount, category, merchant, note,
                    payment_method, expense_date, tags, source,
                    idempotency_key, mood_rating, created_at, updated_at, datetime('now')
                FROM expenses
                WHERE is_deleted = 1 AND updated_at < datetime('now', '-30 days')
                """
            ) as cur:
                count = cur.rowcount

            if count > 0:
                # Delete the moved rows from active table
                await conn.execute(
                    "DELETE FROM expenses "
                    "WHERE is_deleted = 1 AND updated_at < datetime('now', '-30 days')"
                )
                await conn.commit()
                await emit_event(conn, user_id, "db_prune", {"count": count})
                log.info("Pruned deleted expenses", extra={"count": count})
            else:
                log.info("Nothing to prune")
        finally:
            await conn.close()
    except Exception:
        log.error("Prune deleted expenses job failed", exc_info=True)


# ── Job: end-of-month reflection ───────────────────────────────────────────────


async def job_end_of_month_reflection(application: Application) -> None:
    """Last day of each month at 21:00 - send Telegram summary, save web report."""
    log.info("End-of-month reflection job started")
    try:
        from datetime import date, timedelta

        from spendly.ai.context import build_context
        from spendly.ai.reflection import generate_monthly_reflection, save_monthly_report
        from spendly.db.connection import get_connection
        from spendly.db.queries import upsert_user

        # On the 1st, we reflect on the month that just ended
        today = date.today()
        target_date = today - timedelta(days=5)  # Definitely in the previous month
        target_month = target_date.replace(day=1)

        conn = await get_connection()
        try:
            user_id = await upsert_user(conn, telegram_id=str(settings.user_id), name=None)
            ctx = await build_context(conn, user_id)

            result = await generate_monthly_reflection(
                conn=conn,
                user_id=user_id,
                ctx=ctx,
                target_month=target_month,
            )

            if result:
                if result["telegram_summary"]:
                    await application.bot.send_message(
                        chat_id=settings.user_id,
                        text=result["telegram_summary"],
                        parse_mode="Markdown",
                    )
                await save_monthly_report(
                    conn=conn,
                    user_id=user_id,
                    month_key=result["month_key"],
                    total_spend=result["total_spend"],
                    report_data=result["report_data"],
                    telegram_summary=result["telegram_summary"],
                )
                log.info("End-of-month reflection complete", extra={"month": result["month_key"]})

                # ── Google Drive Backup ─────────────────────────────────────
                await _upload_monthly_to_gdrive(conn, user_id, today.replace(day=1))

            else:
                await _notify_error(application, "Monthly reflection generation failed")

        finally:
            await conn.close()
    except Exception:
        log.error("End-of-month reflection job failed", exc_info=True)
        await _notify_error(application, "End-of-month reflection failed")


# ── Job: year-end DB rotation ──────────────────────────────────────────────────


async def job_year_end_rotation(application: Application) -> None:
    """31 Dec 23:58 — rename expense.db to expenseYYYY.db, create fresh expense.db.

    Steps:
    1. Block all expense logging
    2. Telegram ping: logging paused
    3. Sleep 2s for in-flight writes to complete
    4. Rename expense.db → expenseYYYY.db
    5. Create fresh expense.db with same schema
    6. Re-seed prompt versions
    7. Unblock logging
    8. Telegram ping: logging resumed
    """
    import asyncio

    log.info("Year-end DB rotation starting")
    application.bot_data["logging_blocked"] = True

    year = datetime.now(settings.timezone).year
    db_path = settings.db_path
    archive_path = db_path.parent / f"expense{year}.db"

    try:
        await application.bot.send_message(
            chat_id=settings.user_id,
            text=(f"Year-end rotation starting.\nLogging is paused while I archive {year} data."),
        )
        await asyncio.sleep(2)

        db_path.rename(archive_path)
        log.info("DB archived", extra={"archive": str(archive_path)})

        from spendly.db.connection import get_connection, init_db

        await init_db()

        from spendly.ai.seeder import seed_prompts

        conn = await get_connection()
        try:
            await seed_prompts(conn)
        finally:
            await conn.close()

        log.info("Fresh expense.db ready")

    except Exception:
        log.error("Year-end rotation failed", exc_info=True)
        application.bot_data["logging_blocked"] = False
        await _notify_error(application, "Year-end DB rotation FAILED — logging restored")
        return

    finally:
        application.bot_data["logging_blocked"] = False

    await application.bot.send_message(
        chat_id=settings.user_id,
        text=(f"Done — expense{year}.db archived.\nFresh expense.db is live for {year + 1}."),
    )
    log.info("Year-end rotation complete")


# ── Scheduler builder ──────────────────────────────────────────────────────────


def build_scheduler(application: Application):
    """Build and configure the APScheduler instance.

    Single daily proactive message at 09:00.
    Bill nudges fire at 09:10 only on the due date (already-logged guard prevents spam).
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    # Anomaly check every 6 hours
    scheduler.add_job(
        job_anomaly_check,
        trigger="interval",
        hours=6,
        kwargs={"application": application},
        id="anomaly_check",
        replace_existing=True,
    )

    # Bill nudges: 09:10 each day — only alerts for subscriptions due TODAY (not already paid)
    scheduler.add_job(
        job_reschedule_bill_nudges,
        trigger="cron",
        hour=9,
        minute=10,
        kwargs={"application": application},
        id="reschedule_bill_nudges",
        replace_existing=True,
    )

    scheduler.add_job(
        job_proactive_checkin,
        trigger="cron",
        hour=9,
        minute=0,
        kwargs={"application": application, "slot": "daily"},
        id="checkin_daily",
        replace_existing=True,
    )

    # Nightly user patterns update at 02:00
    scheduler.add_job(
        job_update_patterns,
        trigger="cron",
        hour=2,
        minute=0,
        kwargs={"application": application},
        id="update_patterns",
        replace_existing=True,
    )

    # Monthly reflection: 1st of each month at 09:30 (after checkin at 09:00)
    scheduler.add_job(
        job_end_of_month_reflection,
        trigger="cron",
        day=1,
        hour=9,
        minute=30,
        kwargs={"application": application},
        id="monthly_reflection",
        replace_existing=True,
    )

    # Year-end rotation: 31 Dec at 23:58
    scheduler.add_job(
        job_year_end_rotation,
        trigger="cron",
        month=12,
        day=31,
        hour=23,
        minute=58,
        kwargs={"application": application},
        id="year_end_rotation",
        replace_existing=True,
    )

    # Subscription logic: handled by individual nudge and log jobs
    scheduler.add_job(
        job_process_recurring_expenses,
        trigger="cron",
        hour=0,
        minute=5,
        kwargs={"application": application},
        id="process_recurring_expenses",
        replace_existing=True,
    )

    # Mindfulness reflection: Every Sunday (picked by reschedule job)
    scheduler.add_job(
        job_weekly_mindfulness,
        trigger="cron",
        day_of_week="sun",
        hour=0,
        minute=3,
        kwargs={"application": application},
        id="schedule_mindfulness",
        replace_existing=True,
    )

    # Subscription audit: Every 90 days
    scheduler.add_job(
        job_subscription_audit,
        trigger="interval",
        days=90,
        kwargs={"application": application},
        id="subscription_audit",
        replace_existing=True,
    )

    # Project threshold check: Daily at 10:00
    scheduler.add_job(
        job_project_threshold_check,
        trigger="cron",
        hour=10,
        minute=0,
        kwargs={"application": application},
        id="project_threshold_check",
        replace_existing=True,
    )

    # Prune deleted expenses: Weekly Sunday at 03:00
    scheduler.add_job(
        job_prune_deleted_expenses,
        trigger="cron",
        day_of_week="sun",
        hour=3,
        minute=0,
        kwargs={"application": application},
        id="prune_deleted_expenses",
        replace_existing=True,
    )

    log.info(
        "Scheduler configured — 10 jobs",
        extra={"checkin": "09:00", "bill_nudges": "09:10 (due-date only)"},
    )
    return scheduler


# ── Error notifier ─────────────────────────────────────────────────────────────


async def _notify_error(application: Application, context: str) -> None:
    try:
        await application.bot.send_message(
            chat_id=settings.user_id,
            text=f"Spendly background job error:\n`{context}`",
            parse_mode="Markdown",
        )
    except Exception:
        log.error("Could not send error notification", exc_info=True)


# ── Failure notifier ───────────────────────────────────────────────────────────


class FailureNotifier:
    """Tracks expense logging failure state and deduplicates Telegram pings.

    Rules:
    - First failure after a working period  → send 'logging failed' ping
    - Subsequent failures in same run       → logged only, no ping
    - First success after a failed period   → send 'logging restored' ping
    """

    def __init__(self) -> None:
        self._in_failure: bool = False

    async def on_failure(self, application: Application, reason: str) -> None:
        if not self._in_failure:
            self._in_failure = True
            log.error("Expense logging failure — notifying owner", extra={"reason": reason})
            try:
                await application.bot.send_message(
                    chat_id=settings.user_id,
                    text=f"Spendly couldn't process that:\n`{reason}`",
                    parse_mode="Markdown",
                )
            except Exception:
                log.error("Could not send failure notification", exc_info=True)

    async def on_recovery(self, application: Application) -> None:
        if self._in_failure:
            self._in_failure = False
            log.info("Expense logging recovered — notifying owner")
            try:
                await application.bot.send_message(
                    chat_id=settings.user_id,
                    text="All good — logging is working again.",
                )
            except Exception:
                log.error("Could not send recovery notification", exc_info=True)

    @property
    def in_failure(self) -> bool:
        return self._in_failure


# Module-level singleton
# Module-level singleton
failure_notifier = FailureNotifier()


# ── GDrive Helper ──────────────────────────────────────────────────────────────


async def _upload_monthly_to_gdrive(
    conn: aiosqlite.Connection,
    user_id: int,
    month_start: date,
) -> None:
    """Generate CSV/PDF for the month and upload to Google Drive."""
    try:
        import calendar
        from pathlib import Path

        from spendly.db.expenses import get_expenses_in_range
        from spendly.db.queries import emit_event
        from spendly.utils.export import generate_csv
        from spendly.utils.gdrive import upload_to_gdrive

        _, last_day = calendar.monthrange(month_start.year, month_start.month)
        month_end = month_start.replace(day=last_day)

        expenses = await get_expenses_in_range(
            conn,
            user_id=user_id,
            date_from=month_start.isoformat(),
            date_to=month_end.isoformat(),
        )

        if not expenses:
            log.info("No expenses for month, skipping GDrive upload")
            return

        month_label = month_start.strftime("%B %Y")
        temp_dir = Path("scratch/reports")
        temp_dir.mkdir(parents=True, exist_ok=True)

        # 1. CSV
        csv_bytes = generate_csv(expenses, month_label=month_label)
        csv_path = temp_dir / f"Spendly_{month_start.strftime('%Y-%m')}.csv"
        csv_path.write_bytes(csv_bytes)

        # Run synchronous upload in a thread to avoid blocking the event loop
        await asyncio.to_thread(upload_to_gdrive, csv_path, mime_type="text/csv")

        csv_path.unlink()
        await emit_event(conn, user_id, "gdrive_backup", {"file": csv_path.name})
        log.info("Monthly CSV report uploaded to Google Drive")
    except Exception:
        log.error("Google Drive monthly upload failed", exc_info=True)
