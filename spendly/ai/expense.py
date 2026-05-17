"""Expense logging handler — Updated for Receipts and Inline Buttons.

Full flow:
1. Check merchant memory
2. Parse NL via Gemini
3. Validate items
4. If multi-item: show Receipt Summary + "Confirm All" button
5. If single-item: log immediately or ask for confirmation via buttons
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.config import settings
from spendly.core.constants import (
    CATEGORIES,
    DEFAULT_TONE,
    LOW_CONFIDENCE_CUTOFF,
    MAX_EXPENSE_AMOUNT,
    MIN_EXPENSE_AMOUNT,
    normalize_tone,
)
from spendly.core.logger import get_logger
from spendly.db.expenses import (
    emit_event,
    insert_expense,
    lookup_merchant,
    upsert_merchant_memory,
)
from spendly.db.recurring import (
    find_logged_expense_for_subscription_period,
    get_subscription_by_merchant,
)

log = get_logger(__name__)


# ── Main entry point ───────────────────────────────────────────────────────────


async def process_expense_log(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Parse, validate, and process expenses with Receipt Summary support."""
    # 1. Quick merchant lookup
    quick = await _quick_merchant_lookup(conn, user_id, user_message)
    if quick:
        ctx["merchant_memory_hint"] = json.dumps(quick)

    # 2. Parse via Gemini
    parse_req = GatewayRequest(
        task="expense_parse",
        prompt_name="expense_parse",
        user_message=user_message,
        context=ctx,
        use_lite=False,
    )
    parse_resp = await gateway.call(parse_req, db_conn=conn)

    if not parse_resp.ok:
        return (
            "Hmm, I couldn't make sense of that one. "
            "Mind trying again? Something like *Swiggy 340* works great."
        ), None

    data = parse_resp.data

    # 3. Clarification?
    if data.get("needs_clarification"):
        q = data.get("clarification_question", "Could you give me a bit more detail?")
        bot_data["awaiting_clarification"] = "EXPENSE_LOG"
        bot_data["pending_parse_data"] = data
        return q, None

    items: list[dict[str, Any]] = data.get("items", [])
    if not items:
        return "I didn't catch an expense in there. What did you spend on?", None

    # 4. Validate all items first
    processed_items = []
    for item in items:
        result = await _process_single_item_validation(conn, user_id, item)
        if result["status"] == "invalid":
            return f"Sorry, {result['reason']}.", None
        processed_items.append(result)

    # 5. Handle Confirmations (Priority)
    # If any item is a duplicate or needs high-value confirm, handle it immediately
    for res in processed_items:
        if res["status"] == "recurring_duplicate_confirm":
            reply, markup = await _format_recurring_duplicate_reply(
                conn, user_id, res["context"], user_message, ctx, bot_data
            )
            bot_data["awaiting_clarification"] = "RECURRING_DUPLICATE_CONFIRM"
            bot_data["pending_recurring_duplicate"] = res["item"]
            bot_data["recurring_duplicate_context"] = res["context"]
            return reply, markup
        
        if res["status"] == "needs_confirm":
            bot_data["awaiting_clarification"] = "HIGH_VALUE_CONFIRM"
            bot_data["pending_high_value"] = res["item"]
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Log It", callback_data="confirm:high_value"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel:high_value")
            ]])
            return res["reply"], markup

    # 6. Success Path: Single vs Multi-item (Receipt)
    if len(processed_items) > 1:
        # Multi-item: Show Receipt Summary
        receipt_text = "🧾 *Receipt Summary*\n" + "─" * 15 + "\n"
        total = 0.0
        for res in processed_items:
            item = res["item"]
            amt = item["amount"]
            merchant = item.get("merchant") or item.get("category", "Other")
            receipt_text += f"• *{merchant}*: ₹{amt:,.0f}\n"
            total += amt
        receipt_text += "─" * 15 + f"\n*Total: ₹{total:,.0f}*"
        receipt_text += "\n\nLog all of these?"

        bot_data["pending_receipt"] = [r["item"] for r in processed_items]
        bot_data["awaiting_clarification"] = "RECEIPT_CONFIRM"
        
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm All", callback_data="confirm:receipt"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel:receipt")
        ]])
        return receipt_text, markup

    # Single-item: Log immediately
    res = processed_items[0]
    stored_result = await _store_validated_item(conn, user_id, res["item"])
    
    reply, _ = await _format_confirmation_reply(
        conn, user_id, [stored_result], [], user_message, ctx, bot_data
    )
    
    # Add an Undo button for the single log
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{stored_result['expense_id']}")
    ]])
    
    await _emit_stored_events(conn, user_id, [stored_result])
    return reply, markup


# ── Confirmation Handlers ──────────────────────────────────────────────────────


async def process_high_value_confirmation(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup | None]:
    pending = bot_data.pop("pending_high_value", None)
    if not pending: return "What was I logging?", None

    if user_message.lower() in ["yes", "yeah", "yep", "log it"]:
        res = await _store_validated_item(conn, user_id, pending)
        await _emit_stored_events(conn, user_id, [res])
        reply, _ = await _format_confirmation_reply(conn, user_id, [res], [], user_message, ctx, bot_data)
        return reply, None
    return "Skipped that one.", None


async def process_receipt_confirmation(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup | None]:
    items = bot_data.pop("pending_receipt", [])
    if not items: return "Receipt expired.", None

    if user_message.lower() in ["yes", "yeah", "confirm", "log them"]:
        stored = []
        for item in items:
            res = await _store_validated_item(conn, user_id, item)
            stored.append(res)
        
        await _emit_stored_events(conn, user_id, stored)
        reply, _ = await _format_confirmation_reply(conn, user_id, stored, [], "batch log", ctx, bot_data)
        return f"✅ Done!\n\n{reply}", None
    
    return "Cancelled the batch log.", None


async def process_recurring_duplicate_confirmation(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup | None]:
    pending = bot_data.pop("pending_recurring_duplicate", None)
    if not pending: return "What was I logging?", None

    if user_message.lower() in ["yes", "yeah", "confirm", "log it"]:
        res = await _store_validated_item(conn, user_id, pending, skip_recurring_check=True)
        await _emit_stored_events(conn, user_id, [res])
        reply, _ = await _format_confirmation_reply(conn, user_id, [res], [], user_message, ctx, bot_data)
        return reply, None
    
    return "Skipped the duplicate.", None


# ── Internal Helpers ──────────────────────────────────────────────────────────


async def _process_single_item_validation(
    conn: aiosqlite.Connection,
    user_id: int,
    item: dict[str, Any],
) -> dict[str, Any]:
    amount = item.get("amount")
    category = item.get("category", "Other")
    merchant = item.get("merchant")
    confidence = float(item.get("confidence", 1.0))

    if not isinstance(amount, (int, float)) or amount <= 0:
        return {"status": "invalid", "reason": "Amount is missing or invalid"}
    if amount < MIN_EXPENSE_AMOUNT:
        return {"status": "invalid", "reason": f"₹{amount} is too small to log"}
    
    # Merchant Memory override
    if merchant:
        memory_cat = await lookup_merchant(conn, user_id, merchant)
        if memory_cat: category = memory_cat

    if category not in CATEGORIES: category = "Other"
    validated = {**item, "category": category, "amount": float(amount)}

    # Check recurring duplicate
    if merchant:
        sub = await get_subscription_by_merchant(conn, user_id, merchant)
        if sub:
            existing = await find_logged_expense_for_subscription_period(
                conn, user_id, sub, today=date.today()
            )
            if existing:
                return {
                    "status": "recurring_duplicate_confirm",
                    "item": validated,
                    "context": {"original": existing, "subscription": sub}
                }

    # Check high-value / low confidence
    if amount >= settings.high_value_threshold and confidence < 0.85:
        merchant_label = f" at {merchant}" if merchant else ""
        return {
            "status": "needs_confirm",
            "reply": f"That's ₹{amount:,.0f}{merchant_label} — log under *{category}*?",
            "item": validated
        }

    return {"status": "valid", "item": validated}


async def _store_validated_item(
    conn: aiosqlite.Connection,
    user_id: int,
    item: dict[str, Any],
    skip_recurring_check: bool = False,
) -> dict[str, Any]:
    expense_id = await insert_expense(
        conn,
        user_id=user_id,
        amount=float(item["amount"]),
        category=item.get("category", "Other"),
        merchant=item.get("merchant"),
        note=item.get("note"),
        expense_date=item.get("expense_date") or date.today().isoformat(),
        expense_time=item.get("expense_time"),
    )
    if item.get("merchant"):
        await upsert_merchant_memory(conn, user_id, item["merchant"], item["category"])
    
    return {**item, "status": "stored", "expense_id": expense_id}


async def _emit_stored_events(conn: aiosqlite.Connection, user_id: int, stored: list[dict[str, Any]]):
    for s in stored:
        await emit_event(conn, user_id, "expense_logged", {
            "expense_id": s["expense_id"], "amount": s["amount"], "category": s["category"]
        })
    from spendly.db.insights import touch_last_logged
    await touch_last_logged(conn, user_id)


async def _format_confirmation_reply(
    conn: aiosqlite.Connection,
    user_id: int,
    stored: list[dict[str, Any]],
    skipped: list[str],
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, None]:
    reply_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message=user_message,
        context={**ctx, "task_type": "expense_confirm", "data": json.dumps({"stored": stored, "skipped": skipped})},
        use_lite=True,
    )
    resp = await gateway.call(reply_req, db_conn=conn)
    if resp.ok: return resp.data.get("message", "Logged!"), None
    return "Logged!", None


async def _format_recurring_duplicate_reply(
    conn, user_id, dup_ctx, user_message, ctx, bot_data
) -> tuple[str, InlineKeyboardMarkup]:
    # Short circuit for brevity
    merchant = dup_ctx["subscription"].get("merchant", "this")
    reply = f"Looks like *{merchant}* is already logged for this period. Log again?"
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="confirm:recurring"),
        InlineKeyboardButton("❌ No", callback_data="cancel:recurring")
    ]])
    return reply, markup


async def _quick_merchant_lookup(conn, user_id, message):
    # Simplified for this stub
    return None
