"""Database schema — all CREATE TABLE statements.

Single source of truth. Applied at startup via db/connection.py.
All tables use TEXT for timestamps (ISO-8601 UTC strings).
"""

from __future__ import annotations

# Each entry is a complete CREATE TABLE IF NOT EXISTS statement.
# Order matters — referenced tables must come before referencing ones.

SCHEMA_STATEMENTS: list[str] = [
    # ── users ──────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id     TEXT    UNIQUE NOT NULL,
        name            TEXT,
        timezone        TEXT    NOT NULL DEFAULT 'Asia/Kolkata',
        currency        TEXT    NOT NULL DEFAULT 'INR',
        monthly_budget  REAL             DEFAULT 0,
        tone            TEXT    NOT NULL DEFAULT 'financial_advisor',
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL
    )
    """,
    # ── expenses ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           INTEGER NOT NULL REFERENCES users(id),
        amount            REAL    NOT NULL,
        category          TEXT    NOT NULL,
        merchant          TEXT,
        note              TEXT,
        expense_date      TEXT    NOT NULL,
        expense_time      TEXT,
        source            TEXT    NOT NULL DEFAULT 'telegram',
        is_deleted        INTEGER NOT NULL DEFAULT 0,
        idempotency_key   TEXT    UNIQUE,
        mood_rating       TEXT,
        tags              TEXT,
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, expense_date)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_category  ON expenses(user_id, category)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_merchant  ON expenses(user_id, merchant)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_deleted   ON expenses(user_id, is_deleted)",
    # ── incomes ────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS incomes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES users(id),
        amount          REAL    NOT NULL,
        source          TEXT    NOT NULL,
        note            TEXT,
        income_date     TEXT    NOT NULL,
        idempotency_key TEXT    UNIQUE,
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_incomes_user_date ON incomes(user_id, income_date)",
    "CREATE INDEX IF NOT EXISTS idx_incomes_source    ON incomes(user_id, source)",
    # ── category_budgets ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS category_budgets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        category    TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        UNIQUE(user_id, category)
    )
    """,
    # ── merchant_memory ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS merchant_memory (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL REFERENCES users(id),
        merchant         TEXT    NOT NULL,
        category         TEXT    NOT NULL,
        confidence       REAL    NOT NULL DEFAULT 1.0,
        occurrence_count INTEGER NOT NULL DEFAULT 1,
        last_seen        TEXT    NOT NULL,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        UNIQUE(user_id, merchant)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_merchant_memory ON merchant_memory(user_id, merchant)",
    # ── raw_logs ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS raw_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        source      TEXT    NOT NULL DEFAULT 'telegram',
        raw_message TEXT    NOT NULL,
        intent      TEXT,
        processed   INTEGER NOT NULL DEFAULT 0,
        expense_id  INTEGER REFERENCES expenses(id),
        created_at  TEXT    NOT NULL
    )
    """,
    # ── ai_logs ────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ai_logs (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id            INTEGER REFERENCES users(id),
        model              TEXT    NOT NULL,
        prompt_version     TEXT,
        input_tokens       INTEGER,
        output_tokens      INTEGER,
        latency_ms         INTEGER,
        intent             TEXT,
        raw_input          TEXT,
        raw_output         TEXT,
        parsed_output      TEXT,
        is_valid           INTEGER NOT NULL DEFAULT 1,
        retry_count        INTEGER NOT NULL DEFAULT 0,
        error              TEXT,
        hallucination_flag INTEGER NOT NULL DEFAULT 0,
        disagreement_flag  INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_logs_user  ON ai_logs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_logs_flags "
    "ON ai_logs(hallucination_flag, disagreement_flag)",
    # ── prompt_versions ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS prompt_versions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        version    TEXT    NOT NULL,
        content    TEXT    NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 1,
        notes      TEXT,
        created_at TEXT    NOT NULL,
        UNIQUE(name, version)
    )
    """,
    # ── events ─────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER REFERENCES users(id),
        event_type   TEXT    NOT NULL,
        payload      TEXT,
        status       TEXT    NOT NULL DEFAULT 'pending',
        created_at   TEXT    NOT NULL,
        processed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_user   ON events(user_id, event_type)",
    # ── insights ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS insights (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        insight_type TEXT    NOT NULL,
        category     TEXT,
        title        TEXT    NOT NULL,
        body         TEXT    NOT NULL,
        data_json    TEXT,
        period_start TEXT,
        period_end   TEXT,
        is_read      INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_insights_user ON insights(user_id, is_read, created_at)",
    # ── anomaly_alerts ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS anomaly_alerts (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id   INTEGER NOT NULL REFERENCES users(id),
        category  TEXT    NOT NULL,
        threshold REAL    NOT NULL,
        month     TEXT    NOT NULL,
        sent_at   TEXT    NOT NULL,
        UNIQUE(user_id, category, month)
    )
    """,
    # ── monthly_reports ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS monthly_reports (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL REFERENCES users(id),
        month            TEXT    NOT NULL,
        total_spend      REAL,
        report_json      TEXT,
        telegram_summary TEXT,
        generated_at     TEXT    NOT NULL,
        is_archived      INTEGER NOT NULL DEFAULT 0,
        archived_at      TEXT,
        UNIQUE(user_id, month)
    )
    """,
    # ── user_patterns ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS user_patterns (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id              INTEGER UNIQUE NOT NULL REFERENCES users(id),
        avg_daily_spend      REAL,
        avg_monthly_spend    REAL,
        top_category         TEXT,
        top_merchant         TEXT,
        active_logging_hour  INTEGER,
        streak_days          INTEGER NOT NULL DEFAULT 0,
        last_logged_at       TEXT,
        updated_at           TEXT    NOT NULL
    )
    """,
    # ── conversation_history ───────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS conversation_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        role       TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        intent     TEXT,
        created_at TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_convo_user ON conversation_history(user_id, created_at)",
    # ── archives ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS archives (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        year          INTEGER NOT NULL,
        data_json     TEXT    NOT NULL,
        expense_count INTEGER,
        total_spend   REAL,
        archived_at   TEXT    NOT NULL,
        UNIQUE(user_id, year)
    )
    """,
    # ── recurring_expenses ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS recurring_expenses (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           INTEGER NOT NULL REFERENCES users(id),
        merchant          TEXT    NOT NULL,
        amount            REAL    NOT NULL,
        category          TEXT    NOT NULL DEFAULT 'Subscription',
        frequency         TEXT    NOT NULL DEFAULT 'monthly',
        billing_day       INTEGER NOT NULL,  -- 1 to 31 (month day) or 0 to 6 (week day)
        billing_month     INTEGER,            -- 1 to 12 (for yearly)
        is_active         INTEGER NOT NULL DEFAULT 1,
        last_logged_date  TEXT,               -- ISO-8601 date of last auto-log
        last_reminded_at  TEXT,               -- ISO-8601 date of last reminder nudge
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL,
        UNIQUE(user_id, merchant)
    )
    """,
    # ── recurring_incomes ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS recurring_incomes (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           INTEGER NOT NULL REFERENCES users(id),
        source            TEXT    NOT NULL,
        amount            REAL    NOT NULL,
        frequency         TEXT    NOT NULL DEFAULT 'monthly',
        billing_day       INTEGER NOT NULL,
        billing_month     INTEGER,
        is_active         INTEGER NOT NULL DEFAULT 1,
        last_logged_date  TEXT,
        last_reminded_at  TEXT,
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL,
        UNIQUE(user_id, source)
    )
    """,
    # ── project_budgets ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS project_budgets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        tag         TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        is_active   INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        UNIQUE(user_id, tag)
    )
    """,
    # ── audit_expenses ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS audit_expenses (
        id                INTEGER PRIMARY KEY,
        user_id           INTEGER NOT NULL,
        amount            REAL    NOT NULL,
        category          TEXT    NOT NULL,
        merchant          TEXT,
        note              TEXT,
        expense_date      TEXT    NOT NULL,
        tags              TEXT,
        source            TEXT,
        idempotency_key   TEXT,
        mood_rating       TEXT,
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL,
        archived_at       TEXT    NOT NULL
    )
    """,
]
