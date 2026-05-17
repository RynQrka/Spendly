"""Prompt version seeder.

Seeds all prompt templates into the prompt_versions table at startup.
Safe to call on every startup — uses INSERT OR IGNORE.
If a prompt name+version already exists, it is not overwritten.
To update a prompt: bump its version in prompts.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from spendly.ai.prompts import PROMPT_TEMPLATES
from spendly.core.logger import get_logger

log = get_logger(__name__)


async def seed_prompts(conn: aiosqlite.Connection) -> None:
    """Insert all prompt templates that don't already exist in the DB."""
    now = datetime.now(UTC).isoformat()
    seeded = 0

    for template in PROMPT_TEMPLATES:
        await conn.execute(
            """
            INSERT OR IGNORE INTO prompt_versions
                (name, version, content, is_active, notes, created_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (template.name, template.version, template.content, template.notes, now),
        )
        seeded += 1

    await conn.commit()
    log.info("Prompt versions seeded", extra={"count": seeded})


async def load_prompt(conn: aiosqlite.Connection, name: str) -> str | None:
    """Load the active prompt content for a given name from the DB.

    Returns None if no active prompt is found (falls back to in-memory template).
    """
    async with conn.execute(
        "SELECT content FROM prompt_versions "
        "WHERE name = ? AND is_active = 1 "
        "ORDER BY created_at DESC LIMIT 1",
        (name,),
    ) as cur:
        row = await cur.fetchone()
        return row["content"] if row else None
