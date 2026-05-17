"""Synchronous chat handler for the Flask web app — Phase 14.

Bridges Flask (sync) to the async Gemini query pipeline.
Runs each request in a fresh asyncio event loop — acceptable for
a single-user personal tool, no need for production-grade async serving.

The web chat is READ-ONLY. It can answer questions about spending data
but cannot log, correct, or export expenses. Those stay on Telegram.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)

# Questions the chat UI declines to act on
_READ_ONLY_TRIGGERS = [
    "log",
    "add",
    "spent",
    "bought",
    "paid",
    "delete",
    "remove",
    "undo",
    "correct",
    "change",
    "edit",
    "fix",
    "update",
    "export",
    "download",
]

_READ_ONLY_REPLY = (
    "Web chat is read-only — I can answer questions about your spending data, "
    "but logging, editing, or exporting expenses happens on Telegram. "
    "Try: *how much did I spend this week?* or *what's my top category?*"
)


def handle_chat(message: str, history: list[dict[str, str]]) -> dict[str, Any]:
    """Process a web chat message and return a structured reply.

    Returns:
        {
          "reply":    str   — formatted markdown reply
          "intent":   str   — classified intent
          "ok":       bool  — whether query succeeded
          "error":    str | None
        }
    """
    message = message.strip()
    if not message:
        return {"reply": "What would you like to know?", "intent": "UNKNOWN", "ok": True}

    # Guard against write operations
    lower = message.lower()
    if _is_write_intent(lower):
        return {
            "reply": _READ_ONLY_REPLY,
            "intent": "BLOCKED",
            "ok": True,
        }

    try:
        return asyncio.run(_async_handle(message, history))
    except RuntimeError:
        # Already inside a running loop (shouldn't happen in Flask/sync, but guard)
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _async_handle(message, history))
            return future.result(timeout=30)
    except Exception as exc:
        log.error("Web chat handler failed", exc_info=True)
        return {
            "reply": f"Something went wrong: {type(exc).__name__}. Try again.",
            "intent": "ERROR",
            "ok": False,
            "error": str(exc),
        }


# ── Async core ─────────────────────────────────────────────────────────────────


async def _async_handle(message: str, history: list[dict[str, str]]) -> dict[str, Any]:
    """Run the query pipeline asynchronously."""
    from spendly.ai.context import build_context
    from spendly.ai.router import _is_reflection_request
    from spendly.db.connection import get_connection
    from spendly.db.queries import upsert_user

    conn = await get_connection()
    try:
        user_id = await upsert_user(
            conn,
            telegram_id=str(settings.user_id),
            name=None,
        )

        # Build a lightweight bot_data with conversation history from the web UI
        bot_data: dict[str, Any] = {
            "last_intent": None,
            "last_filters": None,
            "last_tone": "financial_advisor",  # web chat always uses advisor tone
        }

        # Inject web history into conversation history table temporarily
        # (We don't persist web chat to the main conversation_history table
        #  to keep Telegram context clean, but we inject last few turns as
        #  context for follow-up question handling)
        ctx = await build_context(
            conn,
            user_id,
            last_intent=bot_data["last_intent"],
            last_filters=bot_data["last_filters"],
            last_tone=bot_data["last_tone"],
        )

        # Add web UI history to context for follow-up awareness
        if history:
            ctx["conversation_history"] = json.dumps(history[-10:], ensure_ascii=False)

        # Reflection request?
        if _is_reflection_request(message):
            from spendly.ai.reflection import process_reflection_request

            reply = await process_reflection_request(conn, user_id, message, ctx, bot_data)
            return {"reply": reply, "intent": "SUMMARY", "ok": True}

        # What-If scenario?
        if (
            message.lower().startswith("what if")
            or "if i buy" in message.lower()
            or "if i spend" in message.lower()
        ):
            from spendly.ai.whatif import process_whatif

            reply = await process_whatif(conn, user_id, message, ctx, bot_data)
            return {"reply": reply, "intent": "WHAT_IF", "ok": True}

        # Route through query handler (QUERY/SUMMARY)
        from spendly.ai.query import process_query

        reply = await process_query(conn, user_id, message, ctx, bot_data)
        return {"reply": reply, "intent": "QUERY", "ok": True}

    finally:
        await conn.close()


def _is_write_intent(lower: str) -> bool:
    """Return True if the message looks like a write operation.

    Blocks:
    - Merchant+amount patterns: "swiggy 340", "lunch 85"
    - Messages whose first word is a write-action trigger
    """
    parts = lower.split()
    # Short messages ending with a number are likely log attempts: "swiggy 340"
    if 1 <= len(parts) <= 3:
        try:
            float(parts[-1].replace(",", "").replace("₹", ""))
            return True
        except (ValueError, IndexError):
            pass

    # First word is a write-action trigger
    first_word = parts[0] if parts else ""
    return first_word in _READ_ONLY_TRIGGERS
