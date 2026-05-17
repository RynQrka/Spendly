"""Database query helpers.

Phase 0/1: user upsert and raw_log insert.
More queries added in later phases.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from spendly.core.logger import get_logger

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def upsert_user(conn: aiosqlite.Connection, telegram_id: str, name: str | None) -> int:
    """Insert user if not exists, update name if provided. Returns user row id."""
    now = _now_iso()
    await conn.execute(
        """
        INSERT INTO users (telegram_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            name       = COALESCE(excluded.name, name),
            updated_at = excluded.updated_at
        """,
        (telegram_id, name, now, now),
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
        row = await cur.fetchone()
        return row["id"] if row else -1


async def insert_raw_log(
    conn: aiosqlite.Connection,
    *,
    user_id: int | None,
    raw_message: str,
    source: str = "telegram",
    intent: str | None = None,
) -> int:
    """Insert a raw message log. Returns the new row id."""
    now = _now_iso()
    async with conn.execute(
        """
        INSERT INTO raw_logs (user_id, source, raw_message, intent, processed, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (user_id, source, raw_message, intent, now),
    ) as cur:
        row_id = cur.lastrowid or -1
    await conn.commit()
    return row_id


async def update_raw_log_processed(
    conn: aiosqlite.Connection,
    raw_log_id: int,
    intent: str,
) -> None:
    """Mark a raw_log entry as processed and set its classified intent."""
    await conn.execute(
        "UPDATE raw_logs SET intent = ?, processed = 1 WHERE id = ?",
        (intent, raw_log_id),
    )
    await conn.commit()


async def emit_event(
    conn: aiosqlite.Connection, user_id: int, event_type: str, data: dict | None = None
) -> None:
    """Log a system event."""
    await conn.execute(
        "INSERT INTO events (user_id, event_type, data, created_at) VALUES (?, ?, ?, ?)",
        (user_id, event_type, json.dumps(data) if data else None, _now_iso()),
    )
    await conn.commit()


async def get_user_id(conn: aiosqlite.Connection, telegram_id: str) -> int | None:
    """Return internal user.id for a given Telegram user ID string."""
    async with conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
        row = await cur.fetchone()
        return row["id"] if row else None


async def get_user_by_id(conn: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    """Return full user row as a dict."""
    async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_user_settings(
    conn: aiosqlite.Connection,
    user_id: int,
    monthly_budget: float | None = None,
    currency: str | None = None,
    tone: str | None = None,
) -> None:
    """Update user-specific settings in the database."""
    now = _now_iso()
    updates = []
    params = []

    if monthly_budget is not None:
        updates.append("monthly_budget = ?")
        params.append(monthly_budget)
    if currency is not None:
        updates.append("currency = ?")
        params.append(currency)
    if tone is not None:
        updates.append("tone = ?")
        params.append(tone)

    if not updates:
        return

    params.append(now)
    params.append(user_id)

    sql = f"UPDATE users SET {', '.join(updates)}, updated_at = ? WHERE id = ?"
    await conn.execute(sql, params)
    await conn.commit()
