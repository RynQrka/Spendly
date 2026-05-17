"""Time utilities — all datetimes are timezone-aware."""

from __future__ import annotations

from datetime import UTC, datetime

from spendly.core.config import settings


def now_local() -> datetime:
    """Current time in the user's configured timezone."""
    return datetime.now(settings.timezone)


def now_iso_utc() -> str:
    """Current UTC time as ISO-8601 string, for DB storage."""
    return datetime.now(UTC).isoformat()


def format_local(dt: datetime, fmt: str = "%d %b %Y %H:%M") -> str:
    """Format a datetime in the user's local timezone."""
    local = dt.astimezone(settings.timezone)
    return local.strftime(fmt)
