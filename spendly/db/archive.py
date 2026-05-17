"""Archival system — Phase 17.

Archive lifecycle:
  - Automatic: job_year_end_rotation fires 31 Dec 23:58
      → renames expense.db to expenseYYYY.db
      → creates fresh expense.db
      → Telegram pings at start and end

  - On-demand: archive_old_records() manually moves expenses older
    than `cutoff_days` from the live DB into a archive sidecar.
    Used sparingly — the automatic rotation is the primary path.

Archived data is read-only. Corrections must happen on the live DB.

Every archive function is synchronous (sqlite3) since they run in
Flask or one-off scripts, not the async bot.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Archive discovery ──────────────────────────────────────────────────────────


def list_archive_years() -> list[int]:
    """Return sorted list of years for which an archive file exists.

    Scans for files named ``expense<YYYY>.db`` next to the main DB.
    """
    parent = settings.db_path.parent
    years: list[int] = []
    for path in parent.glob("expense????.db"):
        name = path.stem  # "expense2024"
        if name.startswith("expense") and name[7:].isdigit():
            years.append(int(name[7:]))
    return sorted(years)


def archive_path(year: int) -> Path:
    """Return the Path object for ``expense<YYYY>.db``."""
    return settings.db_path.parent / f"expense{year}.db"


# ── Archive reader ─────────────────────────────────────────────────────────────


def load_archived_expenses(
    user_id: int,
    year: int,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Read all non-deleted expenses from an archive file.

    Returns an empty list if the archive doesn't exist or is unreadable.
    Optionally filtered by category.
    """
    path = archive_path(year)
    if not path.exists():
        return []

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        sql = """
            SELECT id, amount, category, merchant, note,
                   expense_date, expense_time
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
        """
        params: list[Any] = [user_id]

        if category:
            sql += " AND LOWER(category) = LOWER(?)"
            params.append(category)

        sql += " ORDER BY expense_date DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    except Exception:
        log.warning("Could not read archive", extra={"year": year, "path": str(path)})
        return []


def get_archive_stats(user_id: int, year: int) -> dict[str, Any]:
    """Return summary statistics for an archived year.

    Returns an empty-stats dict if the archive doesn't exist.
    """
    expenses = load_archived_expenses(user_id, year)
    if not expenses:
        return {
            "year": year,
            "exists": archive_path(year).exists(),
            "total": 0.0,
            "count": 0,
            "by_category": {},
            "top_merchant": None,
            "avg_monthly": 0.0,
        }

    total = sum(float(e["amount"]) for e in expenses)

    by_cat: dict[str, float] = {}
    by_merch: dict[str, int] = {}
    for e in expenses:
        c = e.get("category") or "Other"
        m = e.get("merchant") or ""
        by_cat[c] = by_cat.get(c, 0.0) + float(e["amount"])
        if m:
            by_merch[m] = by_merch.get(m, 0) + 1

    top_merchant = max(by_merch, key=lambda k: by_merch[k]) if by_merch else None

    # Count unique months with data
    months = {e["expense_date"][:7] for e in expenses}
    avg_monthly = round(total / len(months), 2) if months else 0.0

    return {
        "year": year,
        "exists": True,
        "total": round(total, 2),
        "count": len(expenses),
        "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
        "top_merchant": top_merchant,
        "avg_monthly": avg_monthly,
        "months_active": len(months),
    }


# ── On-demand archival (live DB → sidecar) ────────────────────────────────────


def archive_old_records(
    user_id: int,
    cutoff_days: int = 365,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move expenses older than ``cutoff_days`` from the live DB to archive files.

    Each year's records go into ``expense<YYYY>.db``.
    If ``dry_run=True``, only count what would be archived — make no changes.

    Returns summary: {"archived": int, "by_year": {year: count}, "dry_run": bool}

    Note: This is a supplementary path. The primary archival mechanism is
    the Dec 31 full-DB rotation (job_year_end_rotation in scheduler.py).
    Use this when you want to free up the live DB mid-year without a full rotation.
    """
    cutoff = (date.today() - timedelta(days=cutoff_days)).isoformat()

    with sqlite3.connect(str(settings.db_path)) as live:
        live.row_factory = sqlite3.Row
        old_rows = live.execute(
            """
            SELECT id, user_id, amount, category, merchant, note,
                   expense_date, expense_time,
                   source, idempotency_key, created_at, updated_at
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0 AND expense_date < ?
            ORDER BY expense_date
            """,
            (user_id, cutoff),
        ).fetchall()

    if not old_rows:
        return {"archived": 0, "by_year": {}, "dry_run": dry_run}

    # Group by year
    by_year: dict[int, list[sqlite3.Row]] = {}
    for row in old_rows:
        try:
            yr = int(row["expense_date"][:4])
        except (ValueError, TypeError):
            yr = date.today().year - 1
        by_year.setdefault(yr, []).append(row)

    if dry_run:
        return {
            "archived": len(old_rows),
            "by_year": {yr: len(rows) for yr, rows in by_year.items()},
            "dry_run": True,
        }

    archived_total = 0

    for yr, rows in by_year.items():
        dest_path = archive_path(yr)
        _ensure_archive_schema(dest_path)

        with sqlite3.connect(str(dest_path)) as arch:
            arch.execute("PRAGMA journal_mode=WAL")
            for row in rows:
                try:
                    arch.execute(
                        """
                        INSERT OR IGNORE INTO expenses (
                            user_id, amount, category, merchant, note,
                            expense_date, expense_time,
                            source, is_deleted, idempotency_key, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,0,?,?,?)
                        """,
                        (
                            row["user_id"],
                            row["amount"],
                            row["category"],
                            row["merchant"],
                            row["note"],
                            row["expense_date"],
                            row["expense_time"],
                            row["source"],
                            row["idempotency_key"],
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                    archived_total += 1
                except Exception:
                    log.warning("Could not insert row into archive", exc_info=True)
            arch.commit()

        # Soft-delete from live DB (preserve idempotency, don't hard-delete)
        now = datetime.now(UTC).isoformat()
        ids = [r["id"] for r in rows]
        with sqlite3.connect(str(settings.db_path)) as live:
            live.executemany(
                "UPDATE expenses SET is_deleted = 1, updated_at = ? WHERE id = ?",
                [(now, rid) for rid in ids],
            )
            live.commit()

        log.info(
            "Archive batch complete",
            extra={"year": yr, "count": len(rows), "dest": str(dest_path)},
        )

    return {
        "archived": archived_total,
        "by_year": {yr: len(rows) for yr, rows in by_year.items()},
        "dry_run": False,
    }


# ── Internal helpers ───────────────────────────────────────────────────────────


def _ensure_archive_schema(path: Path) -> None:
    """Create the expenses table in an archive DB if it doesn't exist yet."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            amount           REAL    NOT NULL,
            category         TEXT    NOT NULL DEFAULT 'Other',
            merchant         TEXT,
            note             TEXT,
            expense_date     TEXT    NOT NULL,
            expense_time     TEXT,
            source           TEXT    NOT NULL DEFAULT 'telegram',
            is_deleted       INTEGER NOT NULL DEFAULT 0,
            idempotency_key  TEXT    UNIQUE,
            created_at       TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
