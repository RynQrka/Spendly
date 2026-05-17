"""Insights and anomaly alert database operations — Phase 7."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Insights ───────────────────────────────────────────────────────────────────


async def save_insight(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    insight_type: str,
    title: str,
    body: str,
    category: str | None = None,
    data_json: dict[str, Any] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> int:
    """Persist a Gemini-generated insight. Returns new row id."""
    now = datetime.now(UTC).isoformat()
    async with conn.execute(
        """
        INSERT INTO insights
            (user_id, insight_type, category, title, body, data_json,
             period_start, period_end, is_read, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            user_id,
            insight_type,
            category,
            title,
            body,
            json.dumps(data_json) if data_json else None,
            period_start,
            period_end,
            now,
        ),
    ) as cur:
        row_id = cur.lastrowid or -1
    await conn.commit()
    log.info("Insight saved", extra={"insight_type": insight_type, "title": title})
    return row_id


async def get_recent_insights(
    conn: aiosqlite.Connection,
    user_id: int,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the most recent N insights for the user."""
    async with conn.execute(
        """
        SELECT insight_type, title, body, category, data_json, period_start, period_end, created_at
        FROM insights
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_insights_read(
    conn: aiosqlite.Connection,
    user_id: int,
) -> None:
    """Mark all unread insights as read."""
    await conn.execute(
        "UPDATE insights SET is_read = 1 WHERE user_id = ? AND is_read = 0",
        (user_id,),
    )
    await conn.commit()


# ── Anomaly alerts ─────────────────────────────────────────────────────────────


async def get_alerted_categories_this_month(
    conn: aiosqlite.Connection,
    user_id: int,
    month: str,
) -> list[str]:
    """Return category names already alerted this month (dedup guard)."""
    async with conn.execute(
        "SELECT category FROM anomaly_alerts WHERE user_id = ? AND month = ?",
        (user_id, month),
    ) as cur:
        rows = await cur.fetchall()
    return [r["category"] for r in rows]


async def record_anomaly_alert(
    conn: aiosqlite.Connection,
    user_id: int,
    category: str,
    threshold: float,
    month: str,
) -> bool:
    """Record that an anomaly alert was sent. Returns False if already recorded (UNIQUE).

    The UNIQUE(user_id, category, month) constraint prevents double-alerts.
    """
    now = datetime.now(UTC).isoformat()
    try:
        await conn.execute(
            """
            INSERT INTO anomaly_alerts (user_id, category, threshold, month, sent_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, category, threshold, month, now),
        )
        await conn.commit()
        return True
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            return False
        raise


# ── Spending data builder for insights ────────────────────────────────────────


async def build_spending_data(
    conn: aiosqlite.Connection,
    user_id: int,
    days: int = 30,
) -> dict[str, Any]:
    """Assemble a rich spending summary for the insight generator prompt.

    Covers the last `days` days, broken down in multiple ways.
    """
    from datetime import date, timedelta

    today = date.today()
    from_date = (today - timedelta(days=days)).isoformat()
    to_date = today.isoformat()

    # All expenses in window
    async with conn.execute(
        """
        SELECT amount, category, merchant, expense_date
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0 AND expense_date BETWEEN ? AND ?
        ORDER BY expense_date
        """,
        (user_id, from_date, to_date),
    ) as cur:
        rows = await cur.fetchall()

    expenses = [dict(r) for r in rows]

    # All incomes in window
    async with conn.execute(
        """
        SELECT amount, source, income_date
        FROM incomes
        WHERE user_id = ? AND income_date BETWEEN ? AND ?
        ORDER BY income_date
        """,
        (user_id, from_date, to_date),
    ) as cur:
        income_rows = await cur.fetchall()

    incomes = [dict(r) for r in income_rows]

    if not expenses and not incomes and days <= 0:
        return {"empty": True, "period_days": days}

    total = sum(float(e["amount"]) for e in expenses)

    # Category breakdown
    by_cat: dict[str, float] = {}
    for e in expenses:
        c = e["category"]
        by_cat[c] = by_cat.get(c, 0.0) + float(e["amount"])

    # Merchant breakdown
    by_merchant: dict[str, float] = {}
    for e in expenses:
        m = e.get("merchant") or "Unknown"
        by_merchant[m] = by_merchant.get(m, 0.0) + float(e["amount"])

    # Day-of-week breakdown
    from datetime import datetime as dt

    dow: dict[str, float] = {}
    for e in expenses:
        try:
            day = dt.fromisoformat(e["expense_date"]).strftime("%A")
            dow[day] = dow.get(day, 0.0) + float(e["amount"])
        except ValueError:
            pass

    # Weekly breakdown (split 30 days into ~4 weeks)
    weekly: dict[str, float] = {}
    for e in expenses:
        try:
            d = dt.fromisoformat(e["expense_date"])
            label = d.strftime("W%W-%Y")
            weekly[label] = weekly.get(label, 0.0) + float(e["amount"])
        except ValueError:
            pass

    # Previous equivalent period for trend comparison
    prev_from = (today - timedelta(days=days * 2)).isoformat()
    prev_to = (today - timedelta(days=days + 1)).isoformat()
    async with conn.execute(
        """
        SELECT SUM(amount) as prev_total
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0 AND expense_date BETWEEN ? AND ?
        """,
        (user_id, prev_from, prev_to),
    ) as cur:
        prev_row = await cur.fetchone()
    prev_total = float(prev_row["prev_total"] or 0.0)

    # Incomes aggregation
    total_income = sum(float(i["amount"]) for i in incomes)
    by_income_source: dict[str, float] = {}
    for i in incomes:
        s = i.get("source") or "Unknown"
        by_income_source[s] = by_income_source.get(s, 0.0) + float(i["amount"])
        
    async with conn.execute(
        """
        SELECT SUM(amount) as prev_total
        FROM incomes
        WHERE user_id = ? AND income_date BETWEEN ? AND ?
        """,
        (user_id, prev_from, prev_to),
    ) as cur:
        prev_i_row = await cur.fetchone()
    prev_total_income = float(prev_i_row["prev_total"] or 0.0)

    net_savings = total_income - total
    savings_rate = round((net_savings / total_income * 100), 1) if total_income > 0 else 0.0

    # Recurring: merchants with 3+ appearances
    merchant_counts: dict[str, int] = {}
    for e in expenses:
        m = e.get("merchant") or ""
        if m:
            merchant_counts[m] = merchant_counts.get(m, 0) + 1
    recurring = {m: c for m, c in merchant_counts.items() if c >= 3}

    return {
        "period_days": days,
        "from_date": from_date,
        "to_date": to_date,
        "total": round(total, 2),
        "total_income": round(total_income, 2),
        "net_savings": round(net_savings, 2),
        "savings_rate": savings_rate,
        "prev_total": round(prev_total, 2),
        "prev_total_income": round(prev_total_income, 2),
        "transaction_count": len(expenses),
        "income_count": len(incomes),
        "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
        "by_merchant": dict(sorted(by_merchant.items(), key=lambda x: -x[1])[:10]),
        "by_income_source": dict(sorted(by_income_source.items(), key=lambda x: -x[1])),
        "by_day_of_week": dow,
        "weekly_totals": weekly,
        "recurring_merchants": recurring,
        "daily_avg": round(total / days, 2),
        "prev_daily_avg": round(prev_total / days, 2) if prev_total else 0.0,
    }


# ── User patterns aggregator ───────────────────────────────────────────────────


async def update_user_patterns(
    conn: aiosqlite.Connection,
    user_id: int,
) -> None:
    """Recompute and persist user_patterns from last 90 days of expenses.

    Called nightly by the scheduler. Patterns are injected into every
    Gemini context call so the AI knows the user's habits.

    Fixes applied:
    - avg_daily_spend uses actual days with data, not always 90
    - avg_monthly_spend uses actual months with data
    - last_logged_at uses created_at timestamp for precision
    - active_logging_hour query fixed (no redundant subquery)
    """
    from datetime import date, timedelta

    today = date.today()
    from_date = (today - timedelta(days=90)).isoformat()

    async with conn.execute(
        """
        SELECT amount, category, merchant, expense_date, created_at
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0 AND expense_date >= ?
        ORDER BY expense_date DESC
        """,
        (user_id, from_date),
    ) as cur:
        rows = await cur.fetchall()

    expenses = [dict(r) for r in rows]

    if not expenses:
        return

    total = sum(float(e["amount"]) for e in expenses)

    # Actual days with at least one expense (not always 90)
    unique_days = len({e["expense_date"] for e in expenses})
    unique_months = len({e["expense_date"][:7] for e in expenses})  # YYYY-MM

    avg_daily = round(total / unique_days, 2) if unique_days else 0.0
    avg_monthly = round(total / unique_months, 2) if unique_months else 0.0

    # Top category by spend
    by_cat: dict[str, float] = {}
    for e in expenses:
        c = e["category"]
        by_cat[c] = by_cat.get(c, 0.0) + float(e["amount"])
    top_category = max(by_cat, key=lambda k: by_cat[k]) if by_cat else None

    # Top merchant by spend
    by_merch: dict[str, float] = {}
    for e in expenses:
        m = e.get("merchant") or ""
        if m:
            by_merch[m] = by_merch.get(m, 0.0) + float(e["amount"])
    top_merchant = max(by_merch, key=lambda k: by_merch[k]) if by_merch else None

    # Active logging hour — hour of day user most commonly sends messages
    # Uses raw_logs.user_id (the internal integer FK, not telegram_id)
    async with conn.execute(
        """
        SELECT strftime('%H', created_at) AS hour, COUNT(*) AS cnt
        FROM raw_logs
        WHERE user_id = ?
        GROUP BY hour
        ORDER BY cnt DESC
        LIMIT 1
        """,
        (user_id,),
    ) as cur:
        hour_row = await cur.fetchone()
    active_hour = int(hour_row["hour"]) if hour_row else None

    # Streak days: consecutive calendar days ending today with at least one expense
    dates_set = {e["expense_date"] for e in expenses}
    streak = 0
    current = today
    while current.isoformat() in dates_set:
        streak += 1
        current = current - timedelta(days=1)

    # last_logged_at: most recent created_at timestamp for precision
    last_logged = expenses[0].get("created_at") or expenses[0]["expense_date"]

    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO user_patterns
            (user_id, avg_daily_spend, avg_monthly_spend, top_category,
             top_merchant, active_logging_hour,
             streak_days, last_logged_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            avg_daily_spend      = excluded.avg_daily_spend,
            avg_monthly_spend    = excluded.avg_monthly_spend,
            top_category         = excluded.top_category,
            top_merchant         = excluded.top_merchant,
            active_logging_hour  = excluded.active_logging_hour,
            streak_days          = excluded.streak_days,
            last_logged_at       = excluded.last_logged_at,
            updated_at           = excluded.updated_at
        """,
        (
            user_id,
            avg_daily,
            avg_monthly,
            top_category,
            top_merchant,
            active_hour,
            streak,
            last_logged,
            now,
        ),
    )
    await conn.commit()
    log.info(
        "User patterns updated",
        extra={
            "user_id": user_id,
            "streak": streak,
            "top_cat": top_category,
            "daily_avg": avg_daily,
            "days_used": unique_days,
        },
    )


async def touch_last_logged(
    conn: aiosqlite.Connection,
    user_id: int,
) -> None:
    """Fast update: refresh streak_days and last_logged_at immediately after a log.

    Called after every successful expense insert so the streak is always
    current without waiting for the nightly full recomputation.
    """
    from datetime import date, timedelta

    today = date.today()

    # Fetch last 30 days of expense dates (enough for streak, cheap query)
    from_date = (today - timedelta(days=30)).isoformat()
    async with conn.execute(
        """
        SELECT DISTINCT expense_date
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0 AND expense_date >= ?
        ORDER BY expense_date DESC
        """,
        (user_id, from_date),
    ) as cur:
        rows = await cur.fetchall()

    dates_set = {r["expense_date"] for r in rows}
    streak = 0
    current = today
    while current.isoformat() in dates_set:
        streak += 1
        current = current - timedelta(days=1)

    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO user_patterns (user_id, streak_days, last_logged_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            streak_days    = excluded.streak_days,
            last_logged_at = excluded.last_logged_at,
            updated_at     = excluded.updated_at
        """,
        (user_id, streak, now, now),
    )
    await conn.commit()
    log.debug("Fast pattern touch", extra={"user_id": user_id, "streak": streak})
