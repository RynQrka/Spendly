"""Monthly financial mood reflection — Phase 11.

Two completely different outputs from one Gemini Flash call:

1. Telegram output (SHORT):
   - 2-3 warm, human sentences
   - Conversational, like a friend summing up the month
   - No bullet points, no numbers overload
   - Tone-rotated as usual

2. Web app output (FULL):
   - Written summary paragraph
   - Total spend + vs last month %
   - Category breakdown with amounts and percentages
   - Week-by-week totals
   - Top merchants ranked
   - Biggest single expense
   - Recurring expenses detected
   - Day-of-week heatmap data
   - Gemini's 2-3 specific observations

Triggers:
- Automatic: scheduler fires on last day of month at 21:00
- On-demand: user asks "how was last month?" or "monthly summary"
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiosqlite

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.logger import get_logger
from spendly.db.expenses import get_expenses_in_range
from spendly.db.insights import build_spending_data

log = get_logger(__name__)


# ── Main generation function ───────────────────────────────────────────────────


async def generate_monthly_reflection(
    conn: aiosqlite.Connection,
    user_id: int,
    ctx: dict[str, str],
    target_month: date | None = None,
) -> dict[str, Any] | None:
    """Generate the monthly reflection for a given month.

    target_month: first day of the month to reflect on.
                  Defaults to the current month.

    Returns dict with keys:
        telegram_summary  — short warm Telegram message
        report_data       — full structured data for web app
        month_key         — 'YYYY-MM'
        total_spend       — float
    Returns None if generation fails.
    """
    if target_month is None:
        today = date.today()
        target_month = today.replace(day=1)

    month_label = target_month.strftime("%B %Y")
    month_key = target_month.strftime("%Y-%m")

    # Last day of the target month
    next_month = (target_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_day = next_month - timedelta(days=1)
    date_from_str = target_month.isoformat()
    date_to_str = last_day.isoformat()

    log.info("Generating monthly reflection", extra={"month": month_key})

    # Fetch this month's expenses
    expenses = await get_expenses_in_range(
        conn,
        user_id=user_id,
        date_from=date_from_str,
        date_to=date_to_str,
    )

    # Build rich spending summary
    days_in_month = (next_month - target_month).days
    spending_data = await build_spending_data(conn, user_id, days=days_in_month)

    # Fetch previous month for comparison
    prev_month_end = target_month - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    prev_expenses = await get_expenses_in_range(
        conn,
        user_id=user_id,
        date_from=prev_month_start.isoformat(),
        date_to=prev_month_end.isoformat(),
    )
    prev_total = sum(float(e["amount"]) for e in prev_expenses)
    curr_total = sum(float(e["amount"]) for e in expenses)

    vs_last_month_pct = 0.0
    if prev_total > 0:
        vs_last_month_pct = round((curr_total - prev_total) / prev_total * 100, 1)

    # Enrich spending_data with comparison
    spending_data["vs_last_month_pct"] = vs_last_month_pct
    spending_data["prev_month_total"] = round(prev_total, 2)
    spending_data["month_label"] = month_label

    # Flash generates the reflection
    reflection_req = GatewayRequest(
        task="monthly_reflection",
        prompt_name="monthly_reflection",
        user_message="",
        context={
            **ctx,
            "month_label": month_label,
            "monthly_data": json.dumps(spending_data, ensure_ascii=False, default=str),
            "user_patterns": ctx.get("user_patterns", "{}"),
        },
        use_lite=False,
    )
    resp = await gateway.call(reflection_req, db_conn=conn)

    if not resp.ok:
        log.error("Monthly reflection failed", extra={"error": resp.error, "month": month_key})
        return None

    telegram_summary = resp.data.get("telegram_summary", "")
    report_data = resp.data.get("report_data", {})

    # Ensure report_data has all required fields even if Gemini omits some
    report_data = _ensure_report_fields(report_data, spending_data, expenses)

    return {
        "telegram_summary": telegram_summary,
        "report_data": report_data,
        "month_key": month_key,
        "total_spend": curr_total,
    }


# ── Save to DB ─────────────────────────────────────────────────────────────────


async def save_monthly_report(
    conn: aiosqlite.Connection,
    user_id: int,
    month_key: str,
    total_spend: float,
    report_data: dict[str, Any],
    telegram_summary: str,
) -> None:
    """Persist the monthly report. Upserts on conflict."""
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO monthly_reports
            (user_id, month, total_spend, report_json,
             telegram_summary, generated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, month) DO UPDATE SET
            total_spend      = excluded.total_spend,
            report_json      = excluded.report_json,
            telegram_summary = excluded.telegram_summary,
            generated_at     = excluded.generated_at
        """,
        (
            user_id,
            month_key,
            total_spend,
            json.dumps(report_data, default=str),
            telegram_summary,
            now,
        ),
    )
    await conn.commit()
    log.info("Monthly report saved", extra={"month": month_key, "total": total_spend})


# ── Retrieve from DB ───────────────────────────────────────────────────────────


async def get_monthly_report(
    conn: aiosqlite.Connection,
    user_id: int,
    month_key: str,
) -> dict[str, Any] | None:
    """Fetch a saved monthly report. Returns None if not found."""
    async with conn.execute(
        """
        SELECT month, total_spend, report_json, telegram_summary,
               generated_at, is_archived
        FROM monthly_reports
        WHERE user_id = ? AND month = ?
        """,
        (user_id, month_key),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return None

    report_json = row["report_json"]
    try:
        report_data = json.loads(report_json) if report_json else {}
    except (json.JSONDecodeError, TypeError):
        report_data = {}

    return {
        "month": row["month"],
        "total_spend": row["total_spend"],
        "report_data": report_data,
        "telegram_summary": row["telegram_summary"],
        "generated_at": row["generated_at"],
        "is_archived": bool(row["is_archived"]),
    }


async def get_all_monthly_reports(
    conn: aiosqlite.Connection,
    user_id: int,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Return summary list of all monthly reports (newest first).

    Used by web app timeline view.
    """
    query = """
        SELECT month, total_spend, telegram_summary, generated_at, is_archived
        FROM monthly_reports
        WHERE user_id = ?
    """
    if not include_archived:
        query += " AND is_archived = 0"
    query += " ORDER BY month DESC"

    async with conn.execute(query, (user_id,)) as cur:
        rows = await cur.fetchall()

    return [
        {
            "month": r["month"],
            "total_spend": r["total_spend"],
            "telegram_summary": r["telegram_summary"],
            "generated_at": r["generated_at"],
            "is_archived": bool(r["is_archived"]),
        }
        for r in rows
    ]


# ── On-demand trigger ──────────────────────────────────────────────────────────


async def process_reflection_request(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Handle on-demand reflection request.

    User says: "how was last month?", "give me a monthly summary",
               "reflect on april", etc.

    Returns Telegram-formatted short reply.
    """
    # Determine which month — default last completed month
    today = date.today()
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)

    # Check if we already have a cached report
    cached = await get_monthly_report(conn, user_id, last_month.strftime("%Y-%m"))
    if cached and cached.get("telegram_summary"):
        log.info("Returning cached reflection", extra={"month": last_month.strftime("%Y-%m")})
        return cached["telegram_summary"]

    # Generate fresh
    result = await generate_monthly_reflection(
        conn=conn,
        user_id=user_id,
        ctx=ctx,
        target_month=last_month,
    )

    if not result:
        return "Couldn't generate the monthly summary right now — try again in a moment."

    # Save to DB
    await save_monthly_report(
        conn=conn,
        user_id=user_id,
        month_key=result["month_key"],
        total_spend=result["total_spend"],
        report_data=result["report_data"],
        telegram_summary=result["telegram_summary"],
    )

    return result["telegram_summary"] or "Nothing notable stood out this month."


# ── Report field enforcement ───────────────────────────────────────────────────


def _ensure_report_fields(
    report_data: dict[str, Any],
    spending_data: dict[str, Any],
    expenses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Guarantee all required report_data fields exist with sensible defaults.

    Gemini may omit optional fields. This ensures the web app always has
    a consistent structure to render.
    """
    total = spending_data.get("total", 0.0)

    # Category breakdown
    if "category_breakdown" not in report_data:
        by_cat = spending_data.get("by_category", {})
        report_data["category_breakdown"] = [
            {
                "category": cat,
                "amount": round(amt, 2),
                "pct": round(amt / total * 100, 1) if total else 0,
            }
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])
        ]

    # Weekly totals
    if "weekly_totals" not in report_data:
        report_data["weekly_totals"] = [
            {"week": k, "amount": round(v, 2)}
            for k, v in spending_data.get("weekly_totals", {}).items()
        ]

    # Top merchants
    if "top_merchants" not in report_data:
        by_merch = spending_data.get("by_merchant", {})
        merch_counts: dict[str, int] = {}
        for e in expenses:
            m = e.get("merchant") or ""
            if m:
                merch_counts[m] = merch_counts.get(m, 0) + 1
        report_data["top_merchants"] = [
            {
                "merchant": m,
                "amount": round(amt, 2),
                "count": merch_counts.get(m, 0),
            }
            for m, amt in sorted(by_merch.items(), key=lambda x: -x[1])[:5]
        ]

    # Biggest expense
    if "biggest_expense" not in report_data and expenses:
        biggest = max(expenses, key=lambda e: float(e.get("amount", 0)))
        report_data["biggest_expense"] = {
            "amount": float(biggest.get("amount", 0)),
            "merchant": biggest.get("merchant") or "",
            "date": biggest.get("expense_date", ""),
            "category": biggest.get("category", ""),
        }
        
    # Incomes breakdown
    if "income_breakdown" not in report_data:
        by_source = spending_data.get("by_income_source", {})
        total_income = spending_data.get("total_income", 0.0)
        report_data["income_breakdown"] = [
            {
                "source": src,
                "amount": round(amt, 2),
                "pct": round(amt / total_income * 100, 1) if total_income else 0,
            }
            for src, amt in sorted(by_source.items(), key=lambda x: -x[1])
        ]

    # Recurring detected
    if "recurring_detected" not in report_data:
        recurring = spending_data.get("recurring_merchants", {})
        report_data["recurring_detected"] = [
            {"merchant": m, "count": c} for m, c in recurring.items()
        ]

    # Day-of-week totals
    if "day_of_week_totals" not in report_data:
        report_data["day_of_week_totals"] = spending_data.get("by_day_of_week", {})

    # Comparison data
    report_data.setdefault("total_spend", total)
    report_data.setdefault("total_income", spending_data.get("total_income", 0.0))
    report_data.setdefault("net_savings", spending_data.get("net_savings", 0.0))
    report_data.setdefault("savings_rate", spending_data.get("savings_rate", 0.0))
    report_data.setdefault("vs_last_month_pct", spending_data.get("vs_last_month_pct", 0.0))
    report_data.setdefault("prev_month_total", spending_data.get("prev_month_total", 0.0))
    report_data.setdefault("gemini_observations", [])

    return report_data
