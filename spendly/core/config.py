"""Typed configuration loaded from environment variables.

Every user-facing variable lives in .env and is loaded here.
System internals (model names, cutoffs, history length) stay in constants.py.
Fails immediately on startup if any required variable is missing.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        print(f"[CONFIG ERROR] Required env var '{key}' is missing or empty.", file=sys.stderr)
        sys.exit(1)
    return val


def _require_int(key: str) -> int:
    raw = _require(key)
    try:
        return int(raw)
    except ValueError:
        print(f"[CONFIG ERROR] '{key}' must be an integer, got: {raw!r}", file=sys.stderr)
        sys.exit(1)


def _optional_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        msg = f"[CONFIG WARN] '{key}' must be a float, got: {raw!r} -- using default {default}"
        print(msg, file=sys.stderr)
        return default


def _optional_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        msg = f"[CONFIG WARN] '{key}' must be an int, got: {raw!r} -- using default {default}"
        print(msg, file=sys.stderr)
        return default


@dataclass(frozen=True)
class Config:
    # ── Required ──────────────────────────────────────────────────────────────
    telegram_token: str
    user_id: int
    gemini_api_key: str

    # ── Localisation ──────────────────────────────────────────────────────────
    timezone: ZoneInfo
    db_path: Path

    # ── Budget & alerts ───────────────────────────────────────────────────────
    monthly_budget: float  # 0 = disabled
    high_value_threshold: float  # INR — ask before logging above this
    anomaly_budget_pct: float  # 0.0-1.0 -- alert when category hits this % of budget

    # ── AI ────────────────────────────────────────────────────────────────────
    ai_max_retries: int

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str

    # ── GCP / Backups ─────────────────────────────────────────────────────────
    gcs_bucket_name: str | None  # If set, enables nightly cloud backups
    web_base_url: str
    gdrive_oauth_client_path: str | None
    gdrive_oauth_token_path: str | None
    gdrive_folder_id: str | None

    # ── Monitoring ────────────────────────────────────────────────────────────
    sentry_dsn: str | None

    @classmethod
    def from_env(cls) -> Config:
        tz_name = os.getenv("TIMEZONE", "Asia/Kolkata")
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            print(
                f"[CONFIG WARN] Unknown timezone '{tz_name}', falling back to Asia/Kolkata",
                file=sys.stderr,
            )
            tz = ZoneInfo("Asia/Kolkata")

        db_path = Path(os.getenv("DB_PATH", "expense.db")).resolve()

        return cls(
            telegram_token=_require("TELEGRAM_TOKEN"),
            user_id=_require_int("MY_USER_ID"),
            gemini_api_key=_require("GEMINI_API_KEY"),
            timezone=tz,
            db_path=db_path,
            monthly_budget=_optional_float("MONTHLY_BUDGET", 0.0),
            high_value_threshold=_optional_float("HIGH_VALUE_THRESHOLD", 5000.0),
            anomaly_budget_pct=_optional_float("ANOMALY_BUDGET_PCT", 0.80),
            ai_max_retries=_optional_int("AI_MAX_RETRIES", 3),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            gcs_bucket_name=os.getenv("GCS_BUCKET_NAME"),
            web_base_url=os.getenv("WEB_BASE_URL", "http://localhost:8080"),
            gdrive_oauth_client_path=os.getenv("GDRIVE_OAUTH_CLIENT_PATH"),
            gdrive_oauth_token_path=os.getenv("GDRIVE_OAUTH_TOKEN_PATH"),
            gdrive_folder_id=os.getenv("GDRIVE_FOLDER_ID"),
            sentry_dsn=os.getenv("SENTRY_DSN"),
        )


# Module-level singleton — instantiated once at import time.
# Any missing required var exits the process immediately with a clear message.
settings = Config.from_env()
