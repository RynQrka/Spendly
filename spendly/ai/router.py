"""Intent router — Updated for UI support.

Classifies every incoming message via Gemini Flash and routes
to the appropriate handler. Handlers return (text, markup).
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from spendly.ai.context import build_context, save_conversation_turn
from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.constants import Intent
from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Main router ────────────────────────────────────────────────────────────────


async def route(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    bot_data: dict[str, Any],
    telegram_context: Any | None = None,
) -> tuple[str, Any]:
    """Classify intent then dispatch to the correct handler.

    Returns the (reply string, reply_markup) to send back to the user.
    """
    last_intent = bot_data.get("last_intent")
    last_filters = bot_data.get("last_filters")
    last_tone = bot_data.get("last_tone")

    ctx = await build_context(
        conn,
        user_id,
        last_intent=last_intent,
        last_filters=last_filters,
        last_tone=last_tone,
    )

    # Save user turn
    await save_conversation_turn(conn, user_id, role="user", message=user_message)

    # Classify intent
    intent_req = GatewayRequest(
        task="intent_classify",
        prompt_name="intent_classify",
        user_message=user_message,
        context=ctx,
        use_lite=False,
    )
    intent_resp = await gateway.call(intent_req, db_conn=conn)

    if not intent_resp.ok:
        log.error("Intent classification failed", extra={"error": intent_resp.error})
        return (
            "Something went sideways on my end - couldn't process that. Try again in a moment?"
        ), None

    intent = intent_resp.data.get("intent", Intent.UNKNOWN)
    confidence = intent_resp.data.get("confidence", 0.0)

    log.info(
        "Intent classified",
        extra={"intent": intent, "confidence": confidence, "user_id": user_id},
    )

    bot_data["last_intent"] = intent
    await _update_raw_log_intent(conn, user_id, user_message, intent)

    reply, markup = await _dispatch(
        intent=intent,
        conn=conn,
        user_id=user_id,
        user_message=user_message,
        ctx=ctx,
        bot_data=bot_data,
        intent_data=intent_resp.data,
        telegram_context=telegram_context,
    )

    await save_conversation_turn(conn, user_id, role="assistant", message=reply, intent=intent)
    return reply, markup


# ── Dispatcher ─────────────────────────────────────────────────────────────────


async def _dispatch(
    *,
    intent: str,
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
    intent_data: dict[str, Any],
    telegram_context: Any | None = None,
) -> tuple[str, Any | None]:
    match intent:
        case Intent.EXPENSE_LOG:
            return await handle_expense_log(conn, user_id, user_message, ctx, bot_data)

        case Intent.INCOME_LOG:
            return await handle_income_log(conn, user_id, user_message, ctx, bot_data)

        case Intent.QUERY | Intent.SUMMARY:
            return await handle_query(conn, user_id, user_message, ctx, bot_data)

        case Intent.ACKNOWLEDGEMENT:
            return await handle_acknowledgement(conn, user_id, user_message, ctx, bot_data)

        case Intent.CORRECTION:
            return await handle_correction(conn, user_id, user_message, ctx, bot_data)

        case Intent.INSIGHT:
            return await handle_insight(conn, user_id, user_message, ctx, bot_data)

        case Intent.EXPORT:
            return await handle_export(conn, user_id, user_message, ctx, bot_data, telegram_context)

        case Intent.CLARIFICATION:
            return await handle_clarification(conn, user_id, user_message, ctx, bot_data)

        case Intent.WHAT_IF:
            return await handle_whatif(conn, user_id, user_message, ctx, bot_data)

        case Intent.RECURRING_MANAGE:
            from spendly.ai.recurring import process_recurring_manage

            reply = await process_recurring_manage(conn, user_id, user_message, ctx, bot_data)
            return reply, None

        case _:
            return await handle_unknown(intent_data), None


# ── Handlers ───────────────────────────────────────────────────────────────────


async def handle_expense_log(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    from spendly.ai.expense import process_expense_log

    return await process_expense_log(conn, user_id, user_message, ctx, bot_data)


async def handle_income_log(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    from spendly.ai.income import process_income_log

    return await process_income_log(conn, user_id, user_message, ctx, bot_data)


async def handle_clarification(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    awaiting = bot_data.get("awaiting_clarification")
    log.info("CLARIFICATION received", extra={"user_id": user_id, "awaiting": awaiting})

    if awaiting == "EXPENSE_LOG":
        bot_data.pop("awaiting_clarification", None)
        from spendly.ai.expense import process_expense_log

        return await process_expense_log(conn, user_id, user_message, ctx, bot_data)

    if awaiting == "INCOME_LOG":
        bot_data.pop("awaiting_clarification", None)
        from spendly.ai.income import process_income_log

        return await process_income_log(conn, user_id, user_message, ctx, bot_data)

    if awaiting == "HIGH_VALUE_CONFIRM":
        from spendly.ai.expense import process_high_value_confirmation

        return await process_high_value_confirmation(conn, user_id, user_message, ctx, bot_data)

    if awaiting == "RECURRING_DUPLICATE_CONFIRM":
        from spendly.ai.expense import process_recurring_duplicate_confirmation

        return await process_recurring_duplicate_confirmation(
            conn, user_id, user_message, ctx, bot_data
        )

    if awaiting == "RECEIPT_CONFIRM":
        from spendly.ai.expense import process_receipt_confirmation

        return await process_receipt_confirmation(conn, user_id, user_message, ctx, bot_data)

    if awaiting == "CORRECTION":
        bot_data.pop("awaiting_clarification", None)
        from spendly.ai.correction import process_correction

        return await process_correction(conn, user_id, user_message, ctx, bot_data)

    if awaiting == "CORRECTION_CONFIRM":
        from spendly.ai.correction import process_correction_confirm

        return await process_correction_confirm(conn, user_id, user_message, ctx, bot_data)

    return await handle_unknown({}), None


def _is_reflection_request(msg: str) -> bool:
    lower = msg.lower()
    reflection_keywords = [
        "last month",
        "monthly summary",
        "monthly reflection",
        "how was last month",
        "month recap",
        "month summary",
        "monthly report",
    ]
    return any(kw in lower for kw in reflection_keywords)


async def handle_query(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    if _is_reflection_request(user_message):
        from spendly.ai.reflection import process_reflection_request

        return await process_reflection_request(conn, user_id, user_message, ctx, bot_data), None

    from spendly.ai.query import process_query

    return await process_query(conn, user_id, user_message, ctx, bot_data), None


async def handle_correction(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    from spendly.ai.correction import process_correction

    return await process_correction(conn, user_id, user_message, ctx, bot_data)


async def handle_insight(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    if _is_reflection_request(user_message):
        from spendly.ai.reflection import process_reflection_request

        return await process_reflection_request(conn, user_id, user_message, ctx, bot_data), None

    from spendly.ai.insights import process_insight_request

    return await process_insight_request(conn, user_id, user_message, ctx, bot_data), None


async def handle_export(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
    telegram_context: Any | None = None,
) -> tuple[str, Any | None]:
    if telegram_context is None:
        return "Export is only available through Telegram.", None
    from spendly.ai.export import process_export

    return await process_export(conn, user_id, user_message, ctx, bot_data, telegram_context)


async def handle_acknowledgement(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    acknowledgements = [
        "Okay, got it - I'm here when you need me.",
        "Alright, thanks for the update. Ping me anytime.",
        "Okay great - I'll remember this for later.",
        "Sure, I'm here whenever you want to continue.",
        "Got it, thanks!",
    ]
    import random

    return random.choice(acknowledgements), None


async def handle_whatif(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> tuple[str, Any | None]:
    from spendly.ai.whatif import process_whatif

    return await process_whatif(conn, user_id, user_message, ctx, bot_data), None


async def handle_unknown(intent_data: dict[str, Any]) -> str:
    q = intent_data.get("clarification_question")
    return q if q else "Not sure what you mean - could you rephrase that?"


# ── DB helpers ─────────────────────────────────────────────────────────────────


async def _update_raw_log_intent(
    conn: aiosqlite.Connection,
    user_id: int,
    raw_message: str,
    intent: str,
) -> None:
    await conn.execute(
        "UPDATE raw_logs SET intent = ?, processed = 1 WHERE id = ("
        "SELECT id FROM raw_logs WHERE raw_message = ? AND intent IS NULL "
        "ORDER BY created_at DESC LIMIT 1)",
        (intent, raw_message),
    )
    await conn.commit()
