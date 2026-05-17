"""Context builder for Gemini calls.

Assembles everything that gets passed to Gemini on every call:
- Today's date and day of week
- Conversation history (last N turns)
- Merchant memory
- User patterns
- Last intent and last filters
- Resolved date shortcuts (yesterday, this week, etc.)

All date resolution happens here — Gemini receives resolved ISO dates,
never raw relative strings.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import aiosqlite

from spendly.core.config import settings
from spendly.core.constants import (
    CONVERSATION_HISTORY_N,
    DEFAULT_TONE,
    TONE_PROMPTS,
    normalize_tone,
)
from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Date resolver ──────────────────────────────────────────────────────────────


def resolve_dates(anchor: date | None = None) -> dict[str, str]:
    """Return a dict of all pre-resolved date strings relative to anchor (default: today).

    These are injected into every Gemini prompt so Gemini never has to guess.
    """
    today = anchor or date.today()

    # Start of this ISO week (Monday)
    week_start = today - timedelta(days=today.weekday())
    # Last week Monday → Sunday
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start - timedelta(days=1)

    # This month
    month_start = today.replace(day=1)
    # Last month
    if today.month == 1:
        last_month_start = date(today.year - 1, 12, 1)
        last_month_end = date(today.year - 1, 12, 31)
    else:
        last_month_start = date(today.year, today.month - 1, 1)
        # Last day of last month = day before this month's 1st
        last_month_end = month_start - timedelta(days=1)

    # Last Friday
    days_since_friday = (today.weekday() - 4) % 7
    last_friday = today - timedelta(days=days_since_friday if days_since_friday else 7)

    # Indian financial year start (April 1)
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    financial_year_start = date(fy_start_year, 4, 1)

    def iso(d: date) -> str:
        return d.isoformat()

    return {
        "today": iso(today),
        "yesterday": iso(today - timedelta(days=1)),
        "day_of_week": today.strftime("%A"),
        "week_start": iso(week_start),
        "last_week_start": iso(last_week_start),
        "last_week_end": iso(last_week_end),
        "month_start": iso(month_start),
        "last_month_start": iso(last_month_start),
        "last_month_end": iso(last_month_end),
        "seven_days_ago": iso(today - timedelta(days=7)),
        "thirty_days_ago": iso(today - timedelta(days=30)),
        "last_friday": iso(last_friday),
        "financial_year_start": iso(financial_year_start),
        "current_month_label": today.strftime("%B %Y"),
        "timezone": str(settings.timezone),
    }


# ── DB helpers ─────────────────────────────────────────────────────────────────


async def get_conversation_history(
    conn: aiosqlite.Connection,
    user_id: int,
    limit: int = CONVERSATION_HISTORY_N,
) -> list[dict[str, str]]:
    """Return last N conversation turns for a user, oldest first."""
    async with conn.execute(
        """
        SELECT role, message, intent FROM conversation_history
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()

    # Reverse so oldest is first (chronological order for Gemini)
    turns = [{"role": r["role"], "message": r["message"]} for r in reversed(rows)]
    return turns


async def save_conversation_turn(
    conn: aiosqlite.Connection,
    user_id: int,
    role: str,
    message: str,
    intent: str | None = None,
) -> None:
    """Append one conversation turn. Prunes old turns beyond 2x the history limit."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO conversation_history (user_id, role, message, intent, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, role, message, intent, now),
    )

    # Prune: keep only the most recent 1000 rows per user
    keep = 1000
    await conn.execute(
        """
        DELETE FROM conversation_history
        WHERE user_id = ? AND id NOT IN (
            SELECT id FROM conversation_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        )
        """,
        (user_id, user_id, keep),
    )
    await conn.commit()


async def get_merchant_memory(
    conn: aiosqlite.Connection,
    user_id: int,
) -> dict[str, str]:
    """Return merchant → category mapping for the user."""
    async with conn.execute(
        """
        SELECT merchant, category FROM merchant_memory
        WHERE user_id = ?
        ORDER BY occurrence_count DESC
        """,
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {r["merchant"]: r["category"] for r in rows}


async def get_user_patterns(
    conn: aiosqlite.Connection,
    user_id: int,
) -> dict[str, Any]:
    """Return stored user patterns for context injection."""
    async with conn.execute(
        "SELECT * FROM user_patterns WHERE user_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return {}

    return {
        "avg_daily_spend": row["avg_daily_spend"],
        "avg_monthly_spend": row["avg_monthly_spend"],
        "top_category": row["top_category"],
        "top_merchant": row["top_merchant"],
        "active_logging_hour": row["active_logging_hour"],
        "streak_days": row["streak_days"],
        "last_logged_at": row["last_logged_at"],
    }


# ── Context assembler ──────────────────────────────────────────────────────────


async def build_context(
    conn: aiosqlite.Connection,
    user_id: int,
    last_intent: str | None = None,
    last_filters: dict[str, Any] | None = None,
    last_tone: str | None = None,
) -> dict[str, str]:
    """Assemble the full context dict passed to every Gemini call.

    Returns flat string-valued dict so template substitution is simple.
    """
    dates = resolve_dates()
    history = await get_conversation_history(conn, user_id)
    merchant = await get_merchant_memory(conn, user_id)
    patterns = await get_user_patterns(conn, user_id)

    query = (
        "SELECT id, amount, merchant, category, expense_date, expense_time "
        "FROM expenses WHERE user_id = ? "
        "ORDER BY expense_date DESC, id DESC LIMIT 100"
    )
    async with conn.execute(query, (user_id,)) as cur:
        ex_rows = await cur.fetchall()
    recent_100_expenses = [dict(r) for r in ex_rows]

    # User settings
    settings_query = "SELECT monthly_budget, currency, tone FROM users WHERE id = ?"
    async with conn.execute(settings_query, (user_id,)) as cur:
        user_row = await cur.fetchone()

    # Use DB values if present, else fallback to defaults
    db_budget = (
        user_row["monthly_budget"]
        if user_row and user_row["monthly_budget"] is not None
        else settings.monthly_budget
    )
    db_currency = user_row["currency"] if user_row and user_row["currency"] is not None else "INR"
    db_tone = user_row["tone"] if user_row and user_row["tone"] else DEFAULT_TONE
    db_tone = normalize_tone(db_tone)
    tone_instruction = TONE_PROMPTS.get(db_tone, TONE_PROMPTS[DEFAULT_TONE])

    ctx: dict[str, str] = {
        **dates,
        "user_id": str(user_id),
        "monthly_budget": str(db_budget),
        "currency": db_currency,
        "conversation_history": json.dumps(history, ensure_ascii=False),
        "merchant_memory": json.dumps(merchant, ensure_ascii=False),
        "user_patterns": json.dumps(patterns, ensure_ascii=False),
        "recent_100_expenses": json.dumps(recent_100_expenses, ensure_ascii=False),
        "last_intent": last_intent or "none",
        "last_filters": json.dumps(last_filters or {}, ensure_ascii=False),
        "last_tone": last_tone or "none",
        "system_tone": db_tone,
        "tone_instruction": tone_instruction,
    }

    log.debug(
        "Context built",
        extra={
            "user_id": user_id,
            "history_turns": len(history),
            "merchants": len(merchant),
            "has_patterns": bool(patterns),
        },
    )
    return ctx
