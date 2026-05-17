"""Synchronous DB helpers for the Flask web app — Phase 12.

The Telegram bot uses aiosqlite (async). Flask runs synchronously, so we use
the stdlib sqlite3 module here — same WAL-mode DB, just accessed differently.

All functions are blocking/synchronous. Keep them fast — no heavy aggregation
queries; those are pre-computed by the nightly scheduler and stored in tables.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)


# ── Connection ─────────────────────────────────────────────────────────────────


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a WAL-mode SQLite connection."""
    conn = sqlite3.connect(
        str(settings.db_path),
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


# ── User ───────────────────────────────────────────────────────────────────────


def get_user_id(telegram_id: str | None = None) -> int | None:
    """Return the internal user_id for the given telegram_id or the default owner."""
    if not telegram_id:
        from spendly.core.config import settings

        telegram_id = settings.user_id

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (str(telegram_id),)
        ).fetchone()
        return row["id"] if row else None


def get_token_usage(user_id: int) -> dict[str, int]:
    """Return total tokens consumed by user's AI requests this month."""
    today = date.today()
    month_start = today.replace(day=1).isoformat()

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0) AS input,
                   COALESCE(SUM(output_tokens), 0) AS output
            FROM ai_logs
            WHERE user_id = ? AND created_at >= ?
            """,
            (user_id, month_start),
        ).fetchone()

    return {
        "input": int(row["input"]),
        "output": int(row["output"]),
        "total": int(row["input"] + row["output"]),
    }


# ── Dashboard summary ──────────────────────────────────────────────────────────


def get_dashboard_summary(user_id: int) -> dict[str, Any]:
    """Return all data needed for the main dashboard view.

    Keeps queries simple — heavy aggregation is pre-computed by nightly jobs.
    """
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    week_start = (today - timedelta(days=today.weekday())).isoformat()

    with get_db() as conn:
        # Monthly total
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
              AND expense_date BETWEEN ? AND ?
            """,
            (user_id, month_start, today.isoformat()),
        ).fetchone()
        monthly_total = float(row["total"])
        monthly_count = int(row["count"])

        income_monthly_total = 0.0
        income_monthly_count = 0
        try:
            row_i = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
                FROM incomes
                WHERE user_id = ? AND income_date BETWEEN ? AND ?
                """,
                (user_id, month_start, today.isoformat()),
            ).fetchone()
            income_monthly_total = float(row_i["total"])
            income_monthly_count = int(row_i["count"])
        except sqlite3.OperationalError:
            income_monthly_total = 0.0
            income_monthly_count = 0

        income_totals = []
        income_distribution = []
        try:
            inc_rows = conn.execute(
                """
                SELECT source, SUM(amount) AS total
                FROM incomes
                WHERE user_id = ? AND income_date BETWEEN ? AND ?
                GROUP BY source
                ORDER BY total DESC
                """,
                (user_id, month_start, today.isoformat()),
            ).fetchall()
            income_totals = [{"source": r["source"], "total": float(r["total"])} for r in inc_rows]
            
            for inc in income_totals:
                src = inc["source"]
                top_rows = conn.execute(
                    """
                    SELECT COALESCE(note, '—') AS note, SUM(amount) AS total
                    FROM incomes
                    WHERE user_id = ? AND income_date BETWEEN ? AND ?
                      AND LOWER(source) = LOWER(?)
                    GROUP BY COALESCE(note, '—')
                    ORDER BY total DESC
                    LIMIT 2
                    """,
                    (user_id, month_start, today.isoformat(), src),
                ).fetchall()
                where_clause = ", ".join(
                    f"{r['note']} ₹{float(r['total']):,.0f}"
                    for r in top_rows
                    if float(r["total"] or 0) > 0
                )
                income_distribution.append(
                    {"source": src, "total": float(inc["total"]), "where": where_clause or "—"}
                )
        except sqlite3.OperationalError:
            pass

        # Weekly total
        row_w = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
              AND expense_date BETWEEN ? AND ?
            """,
            (user_id, week_start, today.isoformat()),
        ).fetchone()
        weekly_total = float(row_w["total"])

        # Category breakdown this month
        cats = conn.execute(
            """
            SELECT category, SUM(amount) AS total
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
              AND expense_date BETWEEN ? AND ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (user_id, month_start, today.isoformat()),
        ).fetchall()
        category_totals = [{"category": r["category"], "total": float(r["total"])} for r in cats]

        category_distribution: list[dict[str, Any]] = []
        for c in category_totals:
            cat = c["category"]
            top_rows = conn.execute(
                """
                SELECT COALESCE(merchant, '—') AS merchant, SUM(amount) AS total
                FROM expenses
                WHERE user_id = ? AND is_deleted = 0
                  AND expense_date BETWEEN ? AND ?
                  AND LOWER(category) = LOWER(?)
                GROUP BY COALESCE(merchant, '—')
                ORDER BY total DESC
                LIMIT 2
                """,
                (user_id, month_start, today.isoformat(), cat),
            ).fetchall()
            where = ", ".join(
                f"{r['merchant']} ₹{float(r['total']):,.0f}"
                for r in top_rows
                if float(r["total"] or 0) > 0
            )
            category_distribution.append(
                {"category": cat, "total": float(c["total"]), "where": where or "—"}
            )

        # User patterns
        pat = conn.execute(
            "SELECT * FROM user_patterns WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        patterns = dict(pat) if pat else {}

        # Recent 10 transactions (unified)
        recent = conn.execute(
            """
            SELECT id, amount, 'expense' AS type, category, merchant, expense_date AS date, note, created_at
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
            UNION ALL
            SELECT id, amount, 'income' AS type, 'Income' AS category, source AS merchant, income_date AS date, note, created_at
            FROM incomes
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (user_id, user_id),
        ).fetchall()
        recent_expenses = [dict(r) for r in recent]

        # User settings override
        row_u = conn.execute(
            "SELECT monthly_budget, currency FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        db_budget = (
            row_u["monthly_budget"]
            if row_u and row_u["monthly_budget"] is not None
            else settings.monthly_budget
        )
        db_currency = row_u["currency"] if row_u and row_u["currency"] is not None else "INR"

        # Budget info and Burn Rate Forecast
        budget = db_budget
        budget_pct = (monthly_total / budget * 100) if budget > 0 else None

        # Burn Rate Forecast
        forecast_message = None
        if budget > 0 and monthly_total > 0:
            import calendar

            days_elapsed = today.day
            _, days_in_month = calendar.monthrange(today.year, today.month)
            velocity = monthly_total / days_elapsed
            projected_total = velocity * days_in_month

            if projected_total > budget:
                # When will it run out?
                days_until_empty = int(budget / velocity)
                runout_date = today.replace(day=1) + timedelta(days=days_until_empty - 1)
                month_name = today.strftime("%B")
                forecast_message = (
                    f"Burn-Rate Forecast: Budget hits zero on {month_name} {runout_date.day}"
                )
            else:
                forecast_message = (
                    f"Burn-Rate Forecast: On track to save ₹{(budget - projected_total):,.0f}"
                )
        mood_data = get_mood_summary(user_id, conn=conn)
        net_total = income_monthly_total - monthly_total
        return {
            "month_label": today.strftime("%B %Y"),
            "monthly_total": round(monthly_total, 2),
            "monthly_count": monthly_count,
            "income_monthly_total": round(income_monthly_total, 2),
            "income_monthly_count": income_monthly_count,
            "net_monthly_total": round(net_total, 2),
            "weekly_total": round(weekly_total, 2),
            "category_totals": category_totals,
            "category_distribution": category_distribution,
            "income_totals": income_totals,
            "income_distribution": income_distribution,
            "patterns": patterns,
            "recent_expenses": recent_expenses,
            "budget": budget,
            "currency": db_currency,
            "budget_pct": round(budget_pct, 1) if budget_pct is not None else None,
            "forecast_message": forecast_message,
            "mood_data": mood_data,
            "token_usage": get_token_usage(user_id),
        }


# ── Expense list ──────────────────────────────────────────────────────────────


def get_expenses(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Paginated expense list with optional filters."""
    today = date.today()
    if not date_from:
        date_from = today.replace(day=1).isoformat()
    if not date_to:
        date_to = today.isoformat()

    clauses = ["user_id = ?", "is_deleted = 0", "expense_date BETWEEN ? AND ?"]
    params: list[Any] = [user_id, date_from, date_to]

    if category:
        clauses.append("LOWER(category) = LOWER(?)")
        params.append(category)
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")

    sql = (
        "SELECT id, amount, category, merchant, expense_date, "
        "expense_time, note, created_at "
        f"FROM expenses WHERE {' AND '.join(clauses)} "
        "ORDER BY expense_date DESC, created_at DESC "
        "LIMIT ? OFFSET ?"
    )
    params += [limit, offset]

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_expense_count(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    tag: str | None = None,
) -> int:
    """Count expenses matching the given filters."""
    today = date.today()
    if not date_from:
        date_from = today.replace(day=1).isoformat()
    if not date_to:
        date_to = today.isoformat()

    clauses = ["user_id = ?", "is_deleted = 0", "expense_date BETWEEN ? AND ?"]
    params: list[Any] = [user_id, date_from, date_to]

    if category:
        clauses.append("LOWER(category) = LOWER(?)")
        params.append(category)
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")

    sql = f"SELECT COUNT(*) FROM expenses WHERE {' AND '.join(clauses)}"
    with get_db() as conn:
        return conn.execute(sql, params).fetchone()[0]


def get_transactions(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    txn_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Get a consolidated list of expenses and incomes (transactions)."""
    today = date.today()
    if not date_from:
        date_from = today.replace(day=1).isoformat()
    if not date_to:
        date_to = today.isoformat()

    exp_clauses = ["user_id = ?", "is_deleted = 0", "expense_date BETWEEN ? AND ?"]
    exp_params = [user_id, date_from, date_to]
    if category:
        exp_clauses.append("LOWER(category) = LOWER(?)")
        exp_params.append(category)
    if tag:
        exp_clauses.append("tags LIKE ?")
        exp_params.append(f"%{tag}%")

    inc_clauses = ["user_id = ?", "income_date BETWEEN ? AND ?"]
    inc_params = [user_id, date_from, date_to]
    if category and category.lower() != "income":
        inc_clauses.append("1 = 0")
    if tag:
        inc_clauses.append("1 = 0")

    subqueries = []
    params = []

    if not txn_type or txn_type == "all" or txn_type == "expense":
        subqueries.append(
            f"""
            SELECT id, amount, 'expense' AS type, category, merchant, note,
                   expense_date AS date, expense_time AS time, created_at, tags
            FROM expenses
            WHERE {' AND '.join(exp_clauses)}
            """
        )
        params.extend(exp_params)

    if not txn_type or txn_type == "all" or txn_type == "income":
        subqueries.append(
            f"""
            SELECT id, amount, 'income' AS type, 'Income' AS category, source AS merchant, note,
                   income_date AS date, NULL AS time, created_at, NULL AS tags
            FROM incomes
            WHERE {' AND '.join(inc_clauses)}
            """
        )
        params.extend(inc_params)

    if not subqueries:
        return []

    sql = f"""
        SELECT * FROM (
            {' UNION ALL '.join(subqueries)}
        )
        ORDER BY date DESC, created_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_transaction_count(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    txn_type: str | None = None,
) -> int:
    """Count consolidated transactions matching the given filters."""
    today = date.today()
    if not date_from:
        date_from = today.replace(day=1).isoformat()
    if not date_to:
        date_to = today.isoformat()

    exp_clauses = ["user_id = ?", "is_deleted = 0", "expense_date BETWEEN ? AND ?"]
    exp_params = [user_id, date_from, date_to]
    if category:
        exp_clauses.append("LOWER(category) = LOWER(?)")
        exp_params.append(category)
    if tag:
        exp_clauses.append("tags LIKE ?")
        exp_params.append(f"%{tag}%")

    inc_clauses = ["user_id = ?", "income_date BETWEEN ? AND ?"]
    inc_params = [user_id, date_from, date_to]
    if category and category.lower() != "income":
        inc_clauses.append("1 = 0")
    if tag:
        inc_clauses.append("1 = 0")

    subqueries = []
    params = []

    if not txn_type or txn_type == "all" or txn_type == "expense":
        subqueries.append(f"SELECT COUNT(*) FROM expenses WHERE {' AND '.join(exp_clauses)}")
        params.extend(exp_params)

    if not txn_type or txn_type == "all" or txn_type == "income":
        subqueries.append(f"SELECT COUNT(*) FROM incomes WHERE {' AND '.join(inc_clauses)}")
        params.extend(inc_params)

    if not subqueries:
        return 0

    sql = f"SELECT ({') + ('.join(subqueries)})"
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0


# ── Monthly reports ────────────────────────────────────────────────────────────


def get_monthly_reports_list(user_id: int) -> list[dict[str, Any]]:
    """Return summary list of monthly reports, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT month, total_spend, telegram_summary, generated_at, is_archived
            FROM monthly_reports
            WHERE user_id = ? AND is_archived = 0
            ORDER BY month DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_monthly_report_detail(user_id: int, month: str) -> dict[str, Any] | None:
    """Return full monthly report including parsed JSON."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT month, total_spend, report_json, telegram_summary,
                   generated_at, is_archived
            FROM monthly_reports
            WHERE user_id = ? AND month = ?
            """,
            (user_id, month),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["report_data"] = json.loads(result.pop("report_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        result["report_data"] = {}
    return result


# ── Insights ───────────────────────────────────────────────────────────────────


def get_recent_insights(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent insights for the insights tab."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT insight_type, title, body, category, created_at
            FROM insights
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Anomaly alerts ─────────────────────────────────────────────────────────────


def get_anomaly_alerts(user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Return recent anomaly alerts for the anomaly panel."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT category, threshold, month, sent_at
            FROM anomaly_alerts
            WHERE user_id = ?
            ORDER BY sent_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Merchant memory ────────────────────────────────────────────────────────────


def get_merchant_memory(user_id: int) -> list[dict[str, Any]]:
    """Return merchant memory for the read-only viewer."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT merchant, category, occurrence_count, last_seen
            FROM merchant_memory
            WHERE user_id = ?
            ORDER BY occurrence_count DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Archives ────────────────────────────────────────────────────────────────────


def get_archived_expenses(user_id: int, year: int) -> list[dict[str, Any]]:
    """Load archived expenses from expenseYYYY.db if it exists."""
    archive_path = settings.db_path.parent / f"expense{year}.db"
    if not archive_path.exists():
        return []

    try:
        conn = sqlite3.connect(str(archive_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, amount, category, merchant, expense_date,
                   note
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
            ORDER BY expense_date DESC
            """,
            (user_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        log.warning("Could not read archive", extra={"year": year, "path": str(archive_path)})
        return []


def get_archive_years() -> list[int]:
    """Return list of years for which an archive DB file exists."""
    from spendly.db.archive import list_archive_years

    return list_archive_years()


def get_archive_summary(user_id: int, year: int) -> dict[str, Any]:
    """Return summary stats for an archived year (total, count, by_category, etc.)."""
    from spendly.db.archive import get_archive_stats

    return get_archive_stats(user_id, year)


def get_category_detail(
    user_id: int,
    category: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return detailed stats for a single spending category.

    Used by the category deep-dive page.
    """
    from datetime import date, timedelta

    today = date.today()
    if not date_from:
        date_from = (today - timedelta(days=90)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    with get_db() as conn:
        # All expenses in this category within the range
        rows = conn.execute(
            """
            SELECT id, amount, merchant, expense_date, note, created_at
            FROM expenses
            WHERE user_id = ?
              AND LOWER(category) = LOWER(?)
              AND is_deleted = 0
              AND expense_date BETWEEN ? AND ?
            ORDER BY expense_date DESC
            """,
            (user_id, category, date_from, date_to),
        ).fetchall()

        expenses = [dict(r) for r in rows]
        total = sum(float(e["amount"]) for e in expenses)
        count = len(expenses)

        # Monthly breakdown
        monthly: dict[str, float] = {}
        for e in expenses:
            m = e["expense_date"][:7]  # YYYY-MM
            monthly[m] = monthly.get(m, 0.0) + float(e["amount"])

        # Merchant breakdown within category
        by_merch: dict[str, dict[str, Any]] = {}
        for e in expenses:
            m = e.get("merchant") or "Unknown"
            if m not in by_merch:
                by_merch[m] = {"amount": 0.0, "count": 0}
            by_merch[m]["amount"] += float(e["amount"])
            by_merch[m]["count"] += 1

        merch_list = sorted(
            [{"merchant": k, **v} for k, v in by_merch.items()],
            key=lambda x: x["amount"],
            reverse=True,
        )

        # Daily average based on actual days with data
        unique_days = len({e["expense_date"] for e in expenses})
        daily_avg = round(total / unique_days, 2) if unique_days else 0.0

        # Biggest expense
        biggest = max(expenses, key=lambda e: float(e["amount"])) if expenses else None

    return {
        "category": category,
        "date_from": date_from,
        "date_to": date_to,
        "total": round(total, 2),
        "count": count,
        "daily_avg": daily_avg,
        "monthly": dict(sorted(monthly.items())),
        "merchants": merch_list,
        "biggest": biggest,
        "expenses": expenses,
    }


def get_timeline_summary(user_id: int) -> list[dict[str, Any]]:
    """Return 12-month timeline for the financial timeline page.

    Each entry has the month key, total spend, category breakdown,
    and transaction count.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m', expense_date) AS month,
                SUM(amount)                      AS total,
                COUNT(*)                          AS count,
                GROUP_CONCAT(DISTINCT category)  AS categories
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
            """,
            (user_id,),
        ).fetchall()

        income_rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m', income_date) AS month,
                SUM(amount)                      AS total_income,
                COUNT(*)                          AS income_count
            FROM incomes
            WHERE user_id = ?
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
            """,
            (user_id,),
        ).fetchall()

        incomes_by_month = {r["month"]: r for r in income_rows}
        all_months = sorted(list(set([r["month"] for r in rows] + [r["month"] for r in income_rows])), reverse=True)[:12]

        exp_by_month = {r["month"]: r for r in rows}
        months = []
        for month_key in all_months:
            r = exp_by_month.get(month_key, {"total": 0, "count": 0})
            i_row = incomes_by_month.get(month_key, {"total_income": 0, "income_count": 0})

            # Per-month category breakdown
            cat_rows = conn.execute(
                """
                SELECT category, SUM(amount) AS total
                FROM expenses
                WHERE user_id = ?
                  AND is_deleted = 0
                  AND strftime('%Y-%m', expense_date) = ?
                GROUP BY category
                ORDER BY total DESC
                """,
                (user_id, month_key),
            ).fetchall()

            inc_src_rows = conn.execute(
                """
                SELECT source, SUM(amount) AS total
                FROM incomes
                WHERE user_id = ?
                  AND strftime('%Y-%m', income_date) = ?
                GROUP BY source
                ORDER BY total DESC
                """,
                (user_id, month_key),
            ).fetchall()

            months.append(
                {
                    "month": month_key,
                    "total": round(float(r["total"] or 0), 2),
                    "total_income": round(float(i_row["total_income"] or 0), 2),
                    "count": r["count"],
                    "income_count": i_row["income_count"],
                    "categories": [
                        {"category": c["category"], "amount": round(float(c["total"]), 2)}
                        for c in cat_rows
                    ],
                    "income_sources": [
                        {"source": s["source"], "amount": round(float(s["total"]), 2)}
                        for s in inc_src_rows
                    ],
                }
            )

    return months




def get_history_data(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return spending and income history grouped by day or month depending on range."""
    from datetime import date, timedelta
    import calendar

    today = date.today()
    if not date_to:
        date_to = today.isoformat()
    if not date_from:
        d_from = today - timedelta(days=30)
        date_from = d_from.isoformat()

    try:
        start_dt = date.fromisoformat(date_from)
        end_dt = date.fromisoformat(date_to)
    except ValueError:
        start_dt = today - timedelta(days=30)
        end_dt = today

    diff_days = (end_dt - start_dt).days
    group_by_month = diff_days > 185

    with get_db() as conn:
        exp_rows = conn.execute(
            """
            SELECT expense_date, SUM(amount) AS total
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
              AND expense_date BETWEEN ? AND ?
            GROUP BY expense_date
            """,
            (user_id, start_dt.isoformat(), end_dt.isoformat()),
        ).fetchall()
        
        inc_rows = conn.execute(
            """
            SELECT income_date, SUM(amount) AS total
            FROM incomes
            WHERE user_id = ?
              AND income_date BETWEEN ? AND ?
            GROUP BY income_date
            """,
            (user_id, start_dt.isoformat(), end_dt.isoformat()),
        ).fetchall()

    exp_map = {r["expense_date"]: float(r["total"]) for r in exp_rows}
    inc_map = {r["income_date"]: float(r["total"]) for r in inc_rows}

    history = []

    if group_by_month:
        curr = start_dt.replace(day=1)
        while curr <= end_dt:
            month_str = curr.strftime("%Y-%m")
            month_exp = sum(v for k, v in exp_map.items() if k.startswith(month_str))
            month_inc = sum(v for k, v in inc_map.items() if k.startswith(month_str))
            
            history.append({
                "label": curr.strftime("%B %Y"),
                "expense": round(month_exp, 2),
                "income": round(month_inc, 2)
            })
            
            _, days_in_month = calendar.monthrange(curr.year, curr.month)
            curr += timedelta(days=days_in_month)
    else:
        curr = start_dt
        while curr <= end_dt:
            dt_str = curr.isoformat()
            history.append({
                "label": curr.strftime("%d %b"),
                "expense": round(exp_map.get(dt_str, 0.0), 2),
                "income": round(inc_map.get(dt_str, 0.0), 2)
            })
            curr += timedelta(days=1)

    return history


def get_sankey_data(user_id: int) -> list[dict[str, Any]]:
    """Return Sankey nodes and links for consolidated money management cashflow."""
    from datetime import date
    today = date.today()
    month_start = today.replace(day=1).isoformat()

    with get_db() as conn:
        inc_rows = conn.execute(
            """
            SELECT source, SUM(amount) AS total
            FROM incomes
            WHERE user_id = ? AND income_date >= ?
            GROUP BY source
            HAVING total > 0
            """,
            (user_id, month_start),
        ).fetchall()

        exp_rows = conn.execute(
            """
            SELECT category, SUM(amount) AS total
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0
              AND expense_date >= ?
            GROUP BY category
            HAVING total > 0
            """,
            (user_id, month_start),
        ).fetchall()

    links = []
    total_income = 0.0
    total_expense = 0.0

    # 1. Income Sources -> Wallet
    for r in inc_rows:
        amount = float(r["total"])
        total_income += amount
        links.append({
            "from": r["source"],
            "to": "Wallet (Available)",
            "flow": amount
        })

    # 2. Wallet -> Expense Categories
    for r in exp_rows:
        amount = float(r["total"])
        total_expense += amount
        links.append({
            "from": "Wallet (Available)",
            "to": r["category"],
            "flow": amount
        })

    # 3. Surplus Wallet -> Net Savings
    if total_income > total_expense:
        links.append({
            "from": "Wallet (Available)",
            "to": "Net Savings",
            "flow": round(total_income - total_expense, 2)
        })

    return links


def get_mood_summary(user_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Calculate happiness distribution and 'value per rupee' metrics."""
    query = """
        SELECT mood_rating, COUNT(*) as count, SUM(amount) as total
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0 AND mood_rating IS NOT NULL
        GROUP BY mood_rating
    """

    def _process(c: sqlite3.Connection):
        rows = c.execute(query, (user_id,)).fetchall()
        data = {r["mood_rating"]: {"count": r["count"], "total": float(r["total"])} for r in rows}

        # Calculate 'Happiness per Rupee' (Simplified: weight great=1, neutral=0.5, regret=0)
        # Higher score = more value for money
        weights = {"great": 1.0, "neutral": 0.5, "regret": 0.0}
        total_spent_rated = sum(d["total"] for d in data.values())

        if total_spent_rated > 0:
            weighted_score = sum(data[m]["total"] * weights.get(m, 0.5) for m in data)
            efficiency = (weighted_score / total_spent_rated) * 100
        else:
            efficiency = 0

        return {
            "distribution": data,
            "efficiency_score": round(efficiency, 1),
            "total_rated": total_spent_rated,
        }

    if conn:
        return _process(conn)
    with get_db() as conn:
        return _process(conn)


def get_recurring_subscriptions(user_id: int) -> list[dict[str, Any]]:
    """Fetch all recurring items."""
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, merchant, amount, category, frequency,
                   billing_day, billing_month, 'expense' as transaction_type, is_active
            FROM recurring_expenses
            WHERE user_id = ?
            UNION ALL
            SELECT id, source as merchant, amount, 'Income' as category, frequency,
                   billing_day, billing_month, 'income' as transaction_type, is_active
            FROM recurring_incomes
            WHERE user_id = ?
            ORDER BY is_active DESC, transaction_type ASC, amount DESC
            """,
            (user_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]

def get_projects_summary(user_id: int) -> list[dict[str, Any]]:
    """Return summary of expenses tagged with hashtags, along with budgets."""
    with get_db() as conn:
        # Get budgets first
        rows_b = conn.execute(
            "SELECT tag, amount FROM project_budgets WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchall()
        budgets = {r["tag"]: float(r["amount"]) for r in rows_b}

        # Get tag spend
        rows = conn.execute(
            """
            SELECT tags, amount
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0 AND tags IS NOT NULL
            """,
            (user_id,),
        ).fetchall()

    projects: dict[str, dict[str, Any]] = {}
    for row in rows:
        tags = [t.strip() for t in row["tags"].split(",") if t.strip()]
        for tag in tags:
            if tag not in projects:
                projects[tag] = {"total": 0.0, "count": 0, "budget": budgets.get(tag)}
            projects[tag]["total"] += float(row["amount"])
            projects[tag]["count"] += 1

    # Add budgets that have no expenses yet
    for tag, budget in budgets.items():
        if tag not in projects:
            projects[tag] = {"total": 0.0, "count": 0, "budget": budget}

    return sorted(
        [{"tag": k, **v} for k, v in projects.items()], key=lambda x: x["total"], reverse=True
    )
