"""Income database operations."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from spendly.core.logger import get_logger

log = get_logger(__name__)


def make_income_idempotency_key(
    user_id: int,
    amount: float,
    source: str,
    income_date: str,
) -> str:
    raw = f"{user_id}|{amount:.2f}|{source.lower()}|{income_date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def insert_income(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    amount: float,
    source: str,
    note: str | None,
    income_date: str,
) -> int | None:
    now = datetime.now(UTC).isoformat()
    idem_key = make_income_idempotency_key(user_id, amount, source, income_date)

    try:
        async with conn.execute(
            """
            INSERT INTO incomes (
                user_id, amount, source, note, income_date,
                idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                source.strip(),
                note,
                income_date,
                idem_key,
                now,
                now,
            ),
        ) as cur:
            row_id = cur.lastrowid
        await conn.commit()
        log.info("Income inserted", extra={"income_id": row_id, "amount": amount, "source": source})
        return row_id
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            log.info("Duplicate income blocked", extra={"idem_key": idem_key[:8]})
            return None
        raise


async def get_last_income(conn: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    async with conn.execute(
        """
        SELECT id, amount, source, note, income_date, created_at
        FROM incomes
        WHERE user_id = ?
        ORDER BY income_date DESC, id DESC
        LIMIT 1
        """,
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_incomes_in_range(
    conn: aiosqlite.Connection,
    user_id: int,
    date_from: str,
    date_to: str,
    source: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> list[dict[str, Any]]:
    clauses = ["user_id = ?", "income_date BETWEEN ? AND ?"]
    params: list[Any] = [user_id, date_from, date_to]

    if source:
        clauses.append("LOWER(source) LIKE LOWER(?)")
        params.append(f"%{source}%")
    if min_amount is not None:
        clauses.append("amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        clauses.append("amount <= ?")
        params.append(max_amount)

    sql = (
        "SELECT id, amount, source, note, income_date "
        f"FROM incomes WHERE {' AND '.join(clauses)} "
        "ORDER BY income_date DESC, id DESC"
    )
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_income(conn: aiosqlite.Connection, income_id: int, user_id: int) -> bool:
    """Hard delete an income entry."""
    async with conn.execute(
        "DELETE FROM incomes WHERE id = ? AND user_id = ?",
        (income_id, user_id),
    ) as cur:
        affected = cur.rowcount
    await conn.commit()
    return affected > 0

