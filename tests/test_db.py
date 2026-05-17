import pytest

from spendly.db.connection import get_connection, init_db


@pytest.mark.asyncio
async def test_init_db():
    """Verify that the database initialization successfully creates the current schema."""
    await init_db()

    conn = await get_connection()
    try:
        async with conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ) as cur:
            row = await cur.fetchone()
            count = row[0]
            assert count >= 18, f"Expected at least 18 tables, found {count}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_recurring_subscription_columns_present():
    """Verify recurring_expenses has the required columns after init_db."""
    await init_db()

    conn = await get_connection()
    try:
        async with conn.execute("PRAGMA table_info(recurring_expenses)") as cur:
            rows = await cur.fetchall()
            columns = {row[1] for row in rows}
            assert "frequency" in columns
            assert "billing_day" in columns
            assert "billing_month" in columns
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_wal_mode_enabled():
    """Verify database creates connections with WAL mode enabled."""
    await init_db()
    conn = await get_connection()
    try:
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
            assert row[0] == "wal", "WAL mode not activated"
    finally:
        await conn.close()
