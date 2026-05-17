"""Correction handler - Phase 6.

Handles natural undo and edit flow. No commands needed.

Supported:
  - "that was wrong" / "remove that" / "undo"  → soft delete last expense
  - "change amount to 420"                       → update amount field
  - "that was Transport not Food"                → update category
  - "change merchant to Zomato"                  → update merchant
  - "delete the ₹340 Swiggy"                    → target specific expense by DB anchor

Flow:
  1. Fetch last expense + recent 5 for context
  2. Flash parses correction intent into structured action JSON
  3. If ambiguous → ask one clarifying question
  4. Apply action to DB (soft delete or field update)
  5. Emit expense_updated / expense_deleted event
  6. Flash Lite formats warm confirmation reply
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.constants import CATEGORIES, DEFAULT_TONE, normalize_tone
from spendly.core.logger import get_logger
from spendly.db.expenses import (
    emit_event,
    get_expense_by_id,
    get_last_expense,
    soft_delete_expense,
    update_expense_field,
    upsert_merchant_memory,
)

log = get_logger(__name__)

# Fields that can be updated and their validation rules
_UPDATABLE_FIELDS: dict[str, str] = {
    "amount": "number",
    "category": "enum",
    "merchant": "string",
    "payment_method": "string",
    "note": "string",
    "expense_date": "date",
}


# ── Main entry point ───────────────────────────────────────────────────────────


async def process_correction(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Parse and apply a correction to an existing expense.

    Returns reply string for Telegram.
    """
    # ── Step 1: Fetch context for Flash ───────────────────────────────────────
    last_expense = await get_last_expense(conn, user_id)

    if not last_expense and not ctx.get("recent_100_expenses"):
        return "Nothing logged yet - nothing to correct."

    # ── Step 2: Flash parses the correction ───────────────────────────────────
    correction_req = GatewayRequest(
        task="correction_parse",
        prompt_name="correction_parse",
        user_message=user_message,
        context={
            **ctx,
            "last_expense": json.dumps(last_expense or {}, default=str),
        },
    )
    correction_resp = await gateway.call(correction_req, db_conn=conn)

    if not correction_resp.ok:
        log.error("Correction parse failed", extra={"error": correction_resp.error})
        return "Couldn't work out what you want to change - could you be more specific?"

    cdata = correction_resp.data

    # ── Step 3: Clarification needed? ─────────────────────────────────────────
    if cdata.get("needs_clarification"):
        q = cdata.get("clarification_question", "Which entry did you want to change?")
        bot_data["awaiting_clarification"] = "CORRECTION"
        bot_data["pending_correction_context"] = {
            "last_expense": last_expense,
        }
        return q

    action = cdata.get("action", "delete")
    target_expense_id = cdata.get("target_expense_id")
    field = cdata.get("field")
    new_value = cdata.get("new_value")
    confidence = float(cdata.get("confidence", 1.0))

    # ── Step 4: Resolve target expense ───────────────────────────────────────
    if target_expense_id:
        target = await get_expense_by_id(conn, target_expense_id, user_id)
    else:
        # Default to last expense
        target = last_expense

    if not target:
        return "I couldn't find that expense - it may have already been deleted."

    # Low confidence on target → confirm before acting
    if confidence < 0.75:
        amt = target.get("amount", "?")
        cat = target.get("category", "?")
        merch = target.get("merchant") or cat
        bot_data["awaiting_clarification"] = "CORRECTION_CONFIRM"
        bot_data["pending_correction"] = {
            "action": action,
            "target": target,
            "field": field,
            "new_value": new_value,
        }
        return f"Do you mean the ₹{amt:,.0f} {merch} entry? Just say yes to confirm."

    # ── Step 5: Apply correction ──────────────────────────────────────────────
    return await _apply_correction(
        conn=conn,
        user_id=user_id,
        action=action,
        target=target,
        field=field,
        new_value=new_value,
        user_message=user_message,
        ctx=ctx,
        bot_data=bot_data,
    )


# ── Correction confirmation flow ───────────────────────────────────────────────


async def process_correction_confirm(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Handle yes/no to a correction confirmation.

    Only pops pending_correction on a definitive yes/no.
    Ambiguous responses re-ask without losing state.
    """
    pending = bot_data.get("pending_correction")
    if not pending:
        return "Not sure what you're confirming - what did you want to change?"

    msg_lower = user_message.lower().strip()
    affirmative = any(w in msg_lower for w in ["yes", "yeah", "yep", "correct", "right", "sure"])
    negative = any(w in msg_lower for w in ["no", "nope", "wrong", "cancel"])

    if negative:
        bot_data.pop("pending_correction", None)
        bot_data.pop("awaiting_clarification", None)
        return "Got it - left it unchanged."

    if affirmative:
        bot_data.pop("pending_correction", None)
        bot_data.pop("awaiting_clarification", None)
        return await _apply_correction(
            conn=conn,
            user_id=user_id,
            action=pending["action"],
            target=pending["target"],
            field=pending["field"],
            new_value=pending["new_value"],
            user_message=user_message,
            ctx=ctx,
            bot_data=bot_data,
        )

    # Ambiguous response - re-ask, pending stays in bot_data
    amt = pending.get("target", {}).get("amount", "?")
    merch = pending.get("target", {}).get("merchant") or pending.get("target", {}).get(
        "category", "that entry"
    )
    action_label = pending["action"].replace("_", " ")
    return f"Just say yes or no - should I {action_label} ₹{amt} {merch}?"


# ── Apply action ──────────────────────────────────────────────────────────────


async def _apply_correction(
    conn: aiosqlite.Connection,
    user_id: int,
    action: str,
    target: dict[str, Any],
    field: str | None,
    new_value: Any,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Execute the correction and return a warm Flash Lite reply."""
    expense_id = target["id"]

    if action == "delete":
        return await _do_delete(conn, user_id, expense_id, target, user_message, ctx, bot_data)

    if action in (
        "update_amount",
        "update_category",
        "update_merchant",
        "update_payment_method",
        "update_note",
        "update_date",
    ):
        db_field = _action_to_field(action)
        return await _do_update(
            conn,
            user_id,
            expense_id,
            target,
            db_field,
            new_value,
            user_message,
            ctx,
            bot_data,
        )

    return "Not sure what correction to make - try something like 'delete that' or 'change to 420'."


async def _do_delete(
    conn: aiosqlite.Connection,
    user_id: int,
    expense_id: int,
    target: dict[str, Any],
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Soft-delete an expense and confirm."""
    affected = await soft_delete_expense(conn, expense_id, user_id)

    if not affected:
        return "Couldn't delete that - it may already be gone."

    await emit_event(
        conn,
        user_id,
        "expense_deleted",
        {
            "expense_id": expense_id,
            "amount": target.get("amount"),
            "category": target.get("category"),
        },
    )

    log.info("Expense deleted", extra={"expense_id": expense_id, "user_id": user_id})

    return await _format_correction_reply(
        conn=conn,
        action="delete",
        target=target,
        field=None,
        old_value=None,
        new_value=None,
        user_message=user_message,
        ctx=ctx,
        bot_data=bot_data,
    )


async def _do_update(
    conn: aiosqlite.Connection,
    user_id: int,
    expense_id: int,
    target: dict[str, Any],
    field: str,
    new_value: Any,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Update a single field on an expense and confirm."""
    # Validate the new value
    validated = _validate_field_value(field, new_value)
    if validated is None:
        return f"'{new_value}' doesn't look right for {field}. Try again?"

    old_value = target.get(field)
    affected = await update_expense_field(conn, expense_id, user_id, field, validated)

    if not affected:
        return "Couldn't update that - the expense may have been deleted already."

    # If category changed, update merchant memory too
    if field == "category" and target.get("merchant"):
        await upsert_merchant_memory(conn, user_id, target["merchant"], str(validated))

    await emit_event(
        conn,
        user_id,
        "expense_updated",
        {
            "expense_id": expense_id,
            "field": field,
            "old_value": old_value,
            "new_value": validated,
        },
    )

    log.info(
        "Expense updated",
        extra={"expense_id": expense_id, "field": field, "new_value": validated},
    )

    return await _format_correction_reply(
        conn=conn,
        action="update",
        target=target,
        field=field,
        old_value=old_value,
        new_value=validated,
        user_message=user_message,
        ctx=ctx,
        bot_data=bot_data,
    )


# ── Validation ────────────────────────────────────────────────────────────────


def _validate_field_value(field: str, raw: Any) -> Any | None:
    """Return the validated/coerced value, or None if invalid."""
    if raw is None:
        return None

    if field == "amount":
        try:
            val = float(str(raw).replace(",", "").replace("₹", "").strip())
            return val if val > 0 else None
        except (ValueError, TypeError):
            return None

    if field == "category":
        val = str(raw).strip().capitalize()
        return val if val in CATEGORIES else None

    if field == "expense_date":
        import re

        date_str = str(raw).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return date_str
        return None

    # String fields
    if field in ("merchant", "payment_method", "note"):
        val = str(raw).strip()
        return val if val else None

    return None


def _action_to_field(action: str) -> str:
    """Map action string to DB column name."""
    mapping = {
        "update_amount": "amount",
        "update_category": "category",
        "update_merchant": "merchant",
        "update_payment_method": "payment_method",
        "update_note": "note",
        "update_date": "expense_date",
    }
    return mapping.get(action, action)


# ── Reply formatter ───────────────────────────────────────────────────────────


async def _format_correction_reply(
    conn: aiosqlite.Connection,
    action: str,
    target: dict[str, Any],
    field: str | None,
    old_value: Any,
    new_value: Any,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Use Flash Lite to produce a warm, tone-aware correction confirmation."""
    data_payload = {
        "action": action,
        "target": target,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
    }

    reply_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message=user_message,
        context={
            **ctx,
            "task_type": f"correction_{action}",
            "data": json.dumps(data_payload, default=str),
            "last_tone": bot_data.get("last_tone", "none"),
        },
    )
    reply_resp = await gateway.call(reply_req, db_conn=conn)

    if reply_resp.ok:
        msg = reply_resp.data.get("message", "")
        bot_data["last_tone"] = normalize_tone(reply_resp.data.get("tone_used", DEFAULT_TONE))
        if msg:
            return msg

    # Plain fallback
    if action == "delete":
        amt = target.get("amount", "?")
        merch = target.get("merchant") or target.get("category", "")
        return f"Removed - ₹{amt:,.0f} {merch} is gone."

    if field and new_value is not None:
        return f"Updated - {field} changed to {new_value}."

    return "Done."
