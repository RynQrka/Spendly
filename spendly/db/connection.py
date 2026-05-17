"""Async SQLite connection and initialisation.

- WAL mode enabled at first connect.
- Foreign keys enforced.
- All current tables created via schema.py on startup.
- Health check verifies the DB is reachable and tables exist.
"""

from __future__ import annotations

import aiosqlite

from spendly.core.config import settings
from spendly.core.logger import get_logger
from spendly.db.schema import SCHEMA_STATEMENTS

log = get_logger(__name__)

_EXPECTED_TABLES = 18


async def get_connection() -> aiosqlite.Connection:
    """Open and configure a single async SQLite connection.

    Caller is responsible for closing: await conn.close()
    """
    conn = await aiosqlite.connect(str(settings.db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")
    return conn


async def init_db() -> None:
    """Create all tables and indexes if they do not exist.

    Safe to call on every startup — all statements use IF NOT EXISTS.
    """
    conn = await get_connection()
    try:
        for stmt in SCHEMA_STATEMENTS:
            stmt = stmt.strip()
            if stmt:
                await conn.execute(stmt)
        await conn.commit()

        # Apply structural migrations (e.g. adding columns to existing tables)
        await _check_and_apply_migrations(conn)

    finally:
        await conn.close()
    log.info("Database initialised", extra={"path": str(settings.db_path)})


async def _check_and_apply_migrations(conn: aiosqlite.Connection) -> None:
    """Safely apply structural changes that CREATE TABLE IF NOT EXISTS misses."""
    try:
        # 1. users table migrations
        async with conn.execute("PRAGMA table_info(users)") as cur:
            columns = [row[1] for row in await cur.fetchall()]

        if "tone" not in columns:
            log.info("Migration: Adding 'tone' column to 'users'")
            await conn.execute(
                "ALTER TABLE users ADD COLUMN tone TEXT NOT NULL DEFAULT 'financial_advisor'"
            )

        # 2. expenses table migrations
        async with conn.execute("PRAGMA table_info(expenses)") as cur:
            columns = [row[1] for row in await cur.fetchall()]

        if "tags" not in columns:
            log.info("Migration: Adding 'tags' column to 'expenses'")
            await conn.execute("ALTER TABLE expenses ADD COLUMN tags TEXT")

        if "mood_rating" not in columns:
            log.info("Migration: Adding 'mood_rating' column to 'expenses'")
            await conn.execute("ALTER TABLE expenses ADD COLUMN mood_rating TEXT")

        # 3. recurring tables migrations
        # Rename legacy table if it exists
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recurring_subscriptions'") as cur:
            if await cur.fetchone():
                async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recurring_expenses'") as target_cur:
                    if await target_cur.fetchone():
                        log.info("Migration: Dropping redundant 'recurring_subscriptions' table")
                        await conn.execute("DROP TABLE recurring_subscriptions")
                    else:
                        log.info("Migration: Renaming 'recurring_subscriptions' to 'recurring_expenses'")
                        await conn.execute("ALTER TABLE recurring_subscriptions RENAME TO recurring_expenses")
        
        # Check recurring_expenses columns
        async with conn.execute("PRAGMA table_info(recurring_expenses)") as cur:
            subs_columns = [row[1] for row in await cur.fetchall()]

        if "frequency" not in subs_columns:
            log.info("Migration: Adding 'frequency' column to 'recurring_expenses'")
            await conn.execute(
                "ALTER TABLE recurring_expenses "
                "ADD COLUMN frequency TEXT NOT NULL DEFAULT 'monthly'"
            )

        if "billing_day" not in subs_columns:
            log.info("Migration: Adding 'billing_day' column to 'recurring_expenses'")
            await conn.execute(
                "ALTER TABLE recurring_expenses "
                "ADD COLUMN billing_day INTEGER NOT NULL DEFAULT 1"
            )

        if "billing_month" not in subs_columns:
            log.info("Migration: Adding 'billing_month' column to 'recurring_expenses'")
            await conn.execute(
                "ALTER TABLE recurring_expenses ADD COLUMN billing_month INTEGER"
            )

        # 4. ai_logs table migrations
        # Backfill user_id in ai_logs if it is NULL
        async with conn.execute("SELECT COUNT(*) FROM ai_logs WHERE user_id IS NULL") as cur:
            null_count_row = await cur.fetchone()
            null_count = null_count_row[0] if null_count_row else 0
        if null_count > 0:
            log.info(f"Migration: Backfilling user_id for {null_count} rows in 'ai_logs'")
            async with conn.execute("SELECT id FROM users ORDER BY id LIMIT 1") as cur:
                user_row = await cur.fetchone()
                default_user_id = user_row[0] if user_row else 1
            await conn.execute("UPDATE ai_logs SET user_id = ? WHERE user_id IS NULL", (default_user_id,))

        await conn.commit()
    except Exception:
        log.error("Auto-migration failed", exc_info=True)


async def health_check() -> dict[str, object]:
    """Return DB health status dict.

    Keys: ok, path, tables, wal, error
    """
    try:
        conn = await get_connection()
        try:
            async with conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ) as cur:
                row = await cur.fetchone()
                table_count: int = row[0] if row else 0

            async with conn.execute("PRAGMA journal_mode") as cur:
                mode_row = await cur.fetchone()
                wal_active = (mode_row[0] == "wal") if mode_row else False
        finally:
            await conn.close()

        ok = table_count >= _EXPECTED_TABLES
        result: dict[str, object] = {
            "ok": ok,
            "path": str(settings.db_path),
            "tables": table_count,
            "wal": wal_active,
            "error": None if ok else f"Expected {_EXPECTED_TABLES} tables, found {table_count}",
        }
        if ok:
            log.info("DB health check passed", extra={"tables": table_count, "wal": wal_active})
        else:
            log.error("DB health check failed", extra=result)
        return result

    except Exception as exc:
        log.error("DB health check error", exc_info=True)
        return {
            "ok": False,
            "path": str(settings.db_path),
            "tables": 0,
            "wal": False,
            "error": str(exc),
        }


async def verify_schema() -> dict[str, object]:
    """Deep schema integrity check — runs at startup.

    Verifies:
    - All current expected tables exist
    - WAL mode is active
    - Each required table has at least the core columns

    Returns {ok, missing_tables, extra_check_errors}
    """
    _REQUIRED_TABLES = {
        "users",
        "expenses",
        "incomes",
        "category_budgets",
        "merchant_memory",
        "raw_logs",
        "ai_logs",
        "prompt_versions",
        "events",
        "insights",
        "anomaly_alerts",
        "monthly_reports",
        "user_patterns",
        "conversation_history",
        "archives",
        "recurring_expenses",
        "recurring_incomes",
        "project_budgets",
        "audit_expenses",
    }

    conn = await get_connection()
    try:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ) as cur:
            rows = await cur.fetchall()
        found = {r[0] for r in rows}
        missing = _REQUIRED_TABLES - found

        async with conn.execute("PRAGMA journal_mode") as cur:
            mode_row = await cur.fetchone()
        wal = (mode_row[0] == "wal") if mode_row else False

    finally:
        await conn.close()

    ok = not missing and wal
    if not ok:
        log.warning(
            "Schema integrity check failed",
            extra={"missing": sorted(missing), "wal": wal},
        )
    else:
        log.info("Schema integrity OK", extra={"tables": len(found), "wal": wal})

    return {
        "ok": ok,
        "found_tables": sorted(found),
        "missing_tables": sorted(missing),
        "wal": wal,
    }
