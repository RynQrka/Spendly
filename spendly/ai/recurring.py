"""Recurring subscription handler - Phase 13 (Enhanced)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import aiosqlite

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Schema Safety ──────────────────────────────────────────────────────────────


async def _ensure_recurring_columns(conn: aiosqlite.Connection) -> None:
    """Ensure required columns exist (runtime migration safety)."""
    for table in ("recurring_expenses", "recurring_incomes"):
        # We assume tables exist since schema.py is updated and DB handles them at startup,
        # but just in case, this runtime check ensures columns.
        try:
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                cols = [row[1] for row in await cur.fetchall()]
        except Exception:
            continue

        stmts: list[str] = []

        if "frequency" not in cols:
            stmts.append(
                f"ALTER TABLE {table} "
                "ADD COLUMN frequency TEXT NOT NULL DEFAULT 'monthly'"
            )
        if "billing_day" not in cols:
            stmts.append(
                f"ALTER TABLE {table} ADD COLUMN billing_day INTEGER NOT NULL DEFAULT 1"
            )
        if "billing_month" not in cols:
            stmts.append(f"ALTER TABLE {table} ADD COLUMN billing_month INTEGER")
        if "created_at" not in cols:
            stmts.append(
                f"ALTER TABLE {table} ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
            )
        if "updated_at" not in cols:
            stmts.append(
                f"ALTER TABLE {table} ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )

        for stmt in stmts:
            log.info("Applying runtime migration", extra={"stmt": stmt})
            await conn.execute(stmt)

        if stmts:
            await conn.commit()


# ── Main Handler ───────────────────────────────────────────────────────────────


async def process_recurring_manage(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],  # reserved for future use
) -> str:
    """Parse and apply recurring subscription change."""

    await _ensure_recurring_columns(conn)

    # Fetch subscriptions
    query = (
        "SELECT id, merchant, amount, category, frequency, "
        "billing_day, billing_month, 'expense' as transaction_type, is_active "
        "FROM recurring_expenses WHERE user_id = ? "
        "UNION ALL "
        "SELECT id, source as merchant, amount, 'Income' as category, frequency, "
        "billing_day, billing_month, 'income' as transaction_type, is_active "
        "FROM recurring_incomes WHERE user_id = ?"
    )
    async with conn.execute(query, (user_id, user_id)) as cur:
        rows = await cur.fetchall()
        subs = [dict(r) for r in rows]

    today = date.today()

    # AI parsing context
    req = GatewayRequest(
        task="recurring_manage",
        prompt_name="recurring_manage",
        user_message=user_message,
        context={
            **ctx,
            "active_subscriptions": json.dumps(subs, default=str),
            "today_day": str(today.day),
        },
    )

    resp = await gateway.call(req, db_conn=conn)

    if not resp.ok:
        log.error("Recurring parse failed", extra={"error": resp.error})
        return "I had trouble understanding that. Try rephrasing."

    data = resp.data
    action = data.get("action")
    merchant = data.get("merchant", "")
    now = datetime.now(UTC).isoformat()

    # ── ADD ────────────────────────────────────────────────────────────────────

    if action == "ADD":
        amount = data.get("amount")
        freq = data.get("frequency", "monthly")
        day = data.get("billing_day", today.day)
        month = data.get("billing_month")
        txn_type = data.get("transaction_type", "expense")
        category = data.get("category", "Subscription" if txn_type == "expense" else "Income")

        if not amount or amount <= 0:
            return "I couldn't understand the amount. Try *add Netflix 649 monthly*."

        import sqlite3
        try:
            if txn_type == "expense":
                await conn.execute(
                    """
                    INSERT INTO recurring_expenses
                        (user_id, merchant, amount, category, frequency,
                         billing_day, billing_month, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (user_id, merchant, amount, category, freq, day, month, now, now),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO recurring_incomes
                        (user_id, source, amount, frequency,
                         billing_day, billing_month, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (user_id, merchant, amount, freq, day, month, now, now),
                )
            await conn.commit()
        except sqlite3.IntegrityError:
            return f"You already have a recurring item for *{merchant}*. Try asking me to *update* it instead."

        log.info("Subscription added", extra={"merchant": merchant})

        return (
            f"Added *{merchant}* ₹{amount} "
            f"{_format_schedule(freq, day, month)}.\n"
            f"I'll remind you when it's due."
        )

    # ── DELETE ─────────────────────────────────────────────────────────────────

    elif action == "DELETE":
        target_id = data.get("target_id")

        if not target_id:
            return f"I couldn't find *{merchant}*. Try *subscriptions*."

        target = next((s for s in subs if s["id"] == target_id), None)
        txn_type = target["transaction_type"] if target else "expense"
        table = "recurring_expenses" if txn_type == "expense" else "recurring_incomes"

        # HARD DELETE (current)
        await conn.execute(
            f"DELETE FROM {table} WHERE id = ? AND user_id = ?",
            (target_id, user_id),
        )

        # SOFT DELETE (optional instead of above)
        # await conn.execute(
        #     "UPDATE recurring_subscriptions SET is_active = 0, updated_at = ? "
        #     "WHERE id = ? AND user_id = ?",
        #     (now, target_id, user_id),
        # )

        await conn.commit()

        return f"Removed *{merchant}* from your subscriptions."

    # ── UPDATE ─────────────────────────────────────────────────────────────────

    elif action == "UPDATE":
        target_id = data.get("target_id")

        if not target_id:
            return f"I couldn't find *{merchant}*. Try *subscriptions*."

        target = next((s for s in subs if s["id"] == target_id), None)
        txn_type = target["transaction_type"] if target else "expense"
        table = "recurring_expenses" if txn_type == "expense" else "recurring_incomes"

        sets: list[str] = []
        params: list[Any] = []

        if (amt := data.get("amount")) is not None:
            if amt <= 0:
                return "Amount must be greater than ₹0."
            sets.append("amount = ?")
            params.append(amt)

        if (day := data.get("billing_day")) is not None:
            sets.append("billing_day = ?")
            params.append(day)

        if (month := data.get("billing_month")) is not None:
            sets.append("billing_month = ?")
            params.append(month)

        if (freq := data.get("frequency")) is not None:
            sets.append("frequency = ?")
            params.append(freq)

        if not sets:
            return "Nothing to update."

        sets.append("updated_at = ?")
        params.append(now)
        params.extend([target_id, user_id])

        await conn.execute(
            f"UPDATE {table} SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            params,
        )
        await conn.commit()

        return f"Updated *{merchant}* successfully."

    # ── LIST ───────────────────────────────────────────────────────────────────

    elif action == "LIST":
        active = [s for s in subs if s.get("is_active")]

        if not active:
            return "No recurring items yet.\nTry *add Netflix 649 monthly* or *add Salary 80000 monthly*."

        lines = ["Your recurring items:\n"]
        total_exp = 0.0
        total_inc = 0.0

        for sub in active:
            amt = float(sub.get("amount", 0))
            freq = sub.get("frequency", "monthly")
            day = sub.get("billing_day")
            month = sub.get("billing_month")
            txn_type = sub.get("transaction_type", "expense")
            icon = "🔻" if txn_type == "expense" else "🟢"

            schedule = _format_schedule(freq, day, month)
            lines.append(f"• {icon} *{sub['merchant']}* — ₹{amt:,.0f} ({schedule})")

            if freq == "monthly":
                if txn_type == "expense":
                    total_exp += amt
                else:
                    total_inc += amt

        if total_exp > 0:
            lines.append(f"\nTotal monthly expenses: ₹{total_exp:,.0f}")
        if total_inc > 0:
            lines.append(f"Total monthly incomes: ₹{total_inc:,.0f}")

        return "\n".join(lines)

    # ── FALLBACK ───────────────────────────────────────────────────────────────

    return "Try *add*, *update*, *cancel*, or *subscriptions*."


# ── Helpers ────────────────────────────────────────────────────────────────────


def _ordinal(n: int | None) -> str:
    if n is None:
        return "?"
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _format_schedule(freq: str, day: int | None, month: int | None) -> str:
    if freq == "monthly":
        return f"every month on the {_ordinal(day)}"
    if freq == "yearly":
        if month:
            return f"every year on {_ordinal(day)} month {month}"
        return f"every year on {_ordinal(day)}"
    if freq == "weekly":
        return f"every week (day {day})"
    return f"every {freq}"
