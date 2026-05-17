"""Income logging handler — Updated for UI support."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.constants import DEFAULT_TONE, MAX_EXPENSE_AMOUNT, normalize_tone
from spendly.core.logger import get_logger
from spendly.db.incomes import insert_income

log = get_logger(__name__)


async def process_income_log(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Parse, validate, store income and return reply + optional undo markup."""
    parse_req = GatewayRequest(
        task="income_parse",
        prompt_name="income_parse",
        user_message=user_message,
        context=ctx,
        use_lite=False,
    )
    parse_resp = await gateway.call(parse_req, db_conn=conn)

    if not parse_resp.ok:
        return "Couldn't read that credit. Try *salary 80000* or *refund 200*.", None

    data = parse_resp.data
    if data.get("needs_clarification"):
        q = data.get("clarification_question", "How much was credited?")
        bot_data["awaiting_clarification"] = "INCOME_LOG"
        bot_data["pending_parse_data"] = data
        return q, None

    items: list[dict[str, Any]] = data.get("items", [])
    if not items:
        return "I didn't catch an income in there. What got credited?", None

    stored: list[dict[str, Any]] = []
    skipped: list[str] = []

    for item in items:
        amount = item.get("amount")
        if not isinstance(amount, (int, float)) or amount <= 0:
            skipped.append("Invalid amount")
            continue
        if amount > MAX_EXPENSE_AMOUNT:
            skipped.append(f"₹{amount:,.0f} is too large to log")
            continue

        source = (item.get("source") or "Other").strip() or "Other"
        income_date = (item.get("income_date") or ctx.get("today") or "").strip()
        if not income_date:
            skipped.append(f"{source}: missing date")
            continue

        note = item.get("note")

        row_id = await insert_income(
            conn,
            user_id=user_id,
            amount=float(amount),
            source=source,
            note=note,
            income_date=income_date,
        )
        if row_id is None:
            skipped.append(f"₹{amount:,.0f} {source} (already logged)")
            continue

        stored.append(
            {
                "income_id": row_id,
                "amount": float(amount),
                "source": source,
                "income_date": income_date,
            }
        )

    if not stored and not skipped:
        return "Couldn't log that credit. Try again?", None

    # Format reply
    reply_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message=user_message,
        context={
            **ctx,
            "task_type": "income_logged",
            "data": json.dumps({"stored_incomes": stored, "skipped": skipped}, ensure_ascii=False),
            "last_tone": bot_data.get("last_tone", "none"),
        },
        use_lite=True,
    )
    reply_resp = await gateway.call(reply_req, db_conn=conn)

    msg = "Logged!"
    if reply_resp.ok:
        msg = (reply_resp.data.get("message") or "").strip() or msg
        bot_data["last_tone"] = normalize_tone(reply_resp.data.get("tone_used", DEFAULT_TONE))
    elif len(stored) == 1:
        s = stored[0]
        msg = f"Logged ₹{s['amount']:,.0f} income — {s['source']}."
    else:
        total = sum(s["amount"] for s in stored)
        msg = f"Logged {len(stored)} credits — ₹{total:,.0f} total."

    # Add Undo button if single income
    markup = None
    if len(stored) == 1:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Undo", callback_data=f"undo_income:{stored[0]['income_id']}")
        ]])

    return msg, markup
