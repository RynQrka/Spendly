"""Expense database operations — Phase 4.

All expense reads and writes go through here.
No SQL lives outside this module for expense operations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Idempotency ────────────────────────────────────────────────────────────────


def make_idempotency_key(
    user_id: int,
    amount: float,
    category: str,
    merchant: str | None,
    expense_date: str,
) -> str:
    """SHA-256 hash of the core expense fields.

    Same expense sent twice produces the same key — blocked at DB UNIQUE constraint.
    """
    raw = f"{user_id}|{amount:.2f}|{category.lower()}|{(merchant or '').lower()}|{expense_date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Write ──────────────────────────────────────────────────────────────────────


async def insert_expense(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    amount: float,
    category: str,
    merchant: str | None,
    note: str | None,
    expense_date: str,
    expense_time: str | None,
    tags: str | None = None,
    mood_rating: str | None = None,
    source: str = "telegram",
) -> int | None:
    """Insert a single expense. Returns new row id, or None if duplicate (idempotency).

    Duplicate = same idempotency_key already exists for this user.
    """
    now = datetime.now(UTC).isoformat()
    idem_key = make_idempotency_key(user_id, amount, category, merchant, expense_date)

    try:
        async with conn.execute(
            """
            INSERT INTO expenses (
                user_id, amount, category, merchant, note,
                expense_date, expense_time,
                tags, mood_rating,
                source, is_deleted, idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                category,
                merchant,
                note,
                expense_date,
                expense_time,
                tags,
                mood_rating,
                source,
                idem_key,
                now,
                now,
            ),
        ) as cur:
            row_id = cur.lastrowid
        await conn.commit()
        log.info(
            "Expense inserted",
            extra={
                "expense_id": row_id,
                "amount": amount,
                "category": category,
                "merchant": merchant,
            },
        )
        return row_id

    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            log.info("Duplicate expense blocked", extra={"idem_key": idem_key[:8]})
            return None
        raise


# ── Merchant memory ────────────────────────────────────────────────────────────


async def upsert_merchant_memory(
    conn: aiosqlite.Connection,
    user_id: int,
    merchant: str,
    category: str,
) -> None:
    """Insert or update merchant->category mapping.

    On conflict: bump occurrence_count, update category and last_seen.
    """
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO merchant_memory
            (user_id, merchant, category,
             confidence, occurrence_count, last_seen, created_at, updated_at)
        VALUES (?, ?, ?, 1.0, 1, ?, ?, ?)
        ON CONFLICT(user_id, merchant) DO UPDATE SET
            category         = excluded.category,
            occurrence_count = occurrence_count + 1,
            confidence       = MIN(1.0, confidence + 0.1),
            last_seen        = excluded.last_seen,
            updated_at       = excluded.updated_at
        """,
        (user_id, merchant.strip(), category, now, now, now),
    )
    await conn.commit()


async def lookup_merchant(
    conn: aiosqlite.Connection,
    user_id: int,
    merchant: str,
) -> str | None:
    """Return trusted category for a merchant, or None if not in memory / below threshold."""
    from spendly.core.constants import MERCHANT_MEMORY_MIN_HITS

    async with conn.execute(
        """
        SELECT category, occurrence_count FROM merchant_memory
        WHERE user_id = ? AND LOWER(merchant) = LOWER(?)
        """,
        (user_id, merchant.strip()),
    ) as cur:
        row = await cur.fetchone()

    if row and row["occurrence_count"] >= MERCHANT_MEMORY_MIN_HITS:
        return row["category"]
    return None


# ── Reads ──────────────────────────────────────────────────────────────────────


async def get_last_expense(
    conn: aiosqlite.Connection,
    user_id: int,
) -> dict[str, Any] | None:
    """Return the most recent non-deleted expense for the user."""
    async with conn.execute(
        """
        SELECT id, amount, category, merchant, expense_date, expense_time,
               note
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return None
    return dict(row)


async def get_recent_expenses(
    conn: aiosqlite.Connection,
    user_id: int,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the most recent N non-deleted expenses."""
    async with conn.execute(
        """
        SELECT id, amount, category, merchant, expense_date, expense_time,
               note, created_at
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_monthly_category_totals(
    conn: aiosqlite.Connection,
    user_id: int,
    year: int,
    month: int,
) -> dict[str, float]:
    """Return {category: total_spend} for the given month."""
    month_str = f"{year:04d}-{month:02d}"
    async with conn.execute(
        """
        SELECT category, SUM(amount) as total
        FROM expenses
        WHERE user_id = ?
          AND is_deleted = 0
          AND strftime('%Y-%m', expense_date) = ?
        GROUP BY category
        """,
        (user_id, month_str),
    ) as cur:
        rows = await cur.fetchall()
    return {r["category"]: float(r["total"]) for r in rows}


async def get_expenses_in_range(
    conn: aiosqlite.Connection,
    user_id: int,
    date_from: str,
    date_to: str,
    category: str | None = None,
    merchant: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> list[dict[str, Any]]:
    """Flexible query used by Phase 5 (query handler)."""
    clauses = [
        "user_id = ?",
        "is_deleted = 0",
        "expense_date BETWEEN ? AND ?",
    ]
    params: list[Any] = [user_id, date_from, date_to]

    if category:
        clauses.append("LOWER(category) = LOWER(?)")
        params.append(category)
    if merchant:
        clauses.append("LOWER(merchant) LIKE LOWER(?)")
        params.append(f"%{merchant}%")
    if min_amount is not None:
        clauses.append("amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        clauses.append("amount <= ?")
        params.append(max_amount)

    sql = (
        "SELECT id, amount, category, merchant, expense_date, expense_time, "
        "note FROM expenses "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY expense_date DESC, created_at DESC"
    )

    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Events ─────────────────────────────────────────────────────────────────────


async def emit_event(
    conn: aiosqlite.Connection,
    user_id: int,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Write an event to the events table."""
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO events (user_id, event_type, payload, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (user_id, event_type, json.dumps(payload), now),
    )
    await conn.commit()


# ── Correction operations ──────────────────────────────────────────────────────


async def soft_delete_expense(
    conn: aiosqlite.Connection,
    expense_id: int,
    user_id: int,
) -> bool:
    """Soft-delete an expense. Returns True if a row was affected."""
    now = datetime.now(UTC).isoformat()
    async with conn.execute(
        """
        UPDATE expenses
        SET is_deleted = 1, updated_at = ?
        WHERE id = ? AND user_id = ? AND is_deleted = 0
        """,
        (now, expense_id, user_id),
    ) as cur:
        affected = cur.rowcount
    await conn.commit()
    return affected > 0


async def update_expense_field(
    conn: aiosqlite.Connection,
    expense_id: int,
    user_id: int,
    field: str,
    new_value: str | float,
) -> bool:
    """Update a single field on an expense. Returns True if a row was affected.

    Allowed fields: amount, category, merchant, note, expense_date,
    mood_rating, tags
    """
    allowed = {
        "amount",
        "category",
        "merchant",
        "note",
        "expense_date",
        "mood_rating",
        "tags",
    }
    if field not in allowed:
        raise ValueError(f"Field '{field}' is not updatable")

    now = datetime.now(UTC).isoformat()
    sql = (
        f"UPDATE expenses SET {field} = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND is_deleted = 0"
    )
    async with conn.execute(sql, (new_value, now, expense_id, user_id)) as cur:
        affected = cur.rowcount
    await conn.commit()
    return affected > 0


async def get_expense_by_id(
    conn: aiosqlite.Connection,
    expense_id: int,
    user_id: int,
) -> dict[str, Any] | None:
    """Fetch a single non-deleted expense by id."""
    async with conn.execute(
        """
        SELECT id, amount, category, merchant, note,
               expense_date, expense_time,
               mood_rating, tags, created_at
        FROM expenses
        WHERE id = ? AND user_id = ? AND is_deleted = 0
        """,
        (expense_id, user_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None
