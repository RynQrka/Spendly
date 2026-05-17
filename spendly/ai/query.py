"""Conversational query and summary handler — Phase 5.

Full flow:
1. Call Gemini Flash (query_build) to extract filters from NL question
2. Execute DB query with those filters
3. Disaggrement / hallucination check on result
4. Format output_format: summary | list | comparison | csv
5. Call Flash Lite (reply_format) to produce warm human reply
6. Persist filters to bot_data for follow-up context
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import aiosqlite

from spendly.ai.gateway import gateway
from spendly.ai.models import QUERY_SCHEMA, GatewayRequest
from spendly.ai.validator import check_disagreement
from spendly.core.constants import DEFAULT_TONE, normalize_tone
from spendly.core.logger import get_logger
from spendly.db.expenses import get_expenses_in_range
from spendly.db.incomes import get_incomes_in_range

log = get_logger(__name__)


# ── Main entry point ───────────────────────────────────────────────────────────


async def process_query(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Handle QUERY and SUMMARY intents.

    Returns reply string for Telegram.
    """
    # ── Step 1: Flash extracts filters ────────────────────────────────────────
    query_req = GatewayRequest(
        task="query_build",
        prompt_name="query_build",
        user_message=user_message,
        context=ctx,
        use_lite=False,
        schema=QUERY_SCHEMA,
    )
    query_resp = await gateway.call(query_req, db_conn=conn)

    if not query_resp.ok:
        log.error("Query build failed", extra={"error": query_resp.error})
        return "Couldn't figure out what you're asking — try rephrasing?"

    qdata = query_resp.data
    filters = qdata.get("filters", {})
    output_format = qdata.get("output_format", "summary")
    query_intent = qdata.get("intent", "general")
    transaction_type = (filters.get("transaction_type") or "expense").lower()
    if transaction_type not in {"expense", "income", "both"}:
        transaction_type = "expense"

    log.info(
        "Query filters extracted",
        extra={
            "intent": query_intent,
            "format": output_format,
            "date_from": filters.get("date_from"),
            "date_to": filters.get("date_to"),
        },
    )

    # Persist filters for follow-up queries ("and last month?")
    bot_data["last_filters"] = filters

    # ── Step 2: Execute DB query ───────────────────────────────────────────────
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")

    if not date_from or not date_to:
        # No date range — default to this month
        date_from = ctx.get("month_start", ctx["today"])
        date_to = ctx["today"]

    if transaction_type == "expense":
        expenses = await get_expenses_in_range(
            conn,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            category=filters.get("category"),
            merchant=filters.get("merchant"),
            min_amount=filters.get("min_amount"),
            max_amount=filters.get("max_amount"),
        )
        income_rows: list[dict[str, Any]] = []
    elif transaction_type == "income":
        income_rows = await get_incomes_in_range(
            conn,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            source=filters.get("merchant"),
            min_amount=filters.get("min_amount"),
            max_amount=filters.get("max_amount"),
        )
        expenses = [
            {
                "id": r.get("id"),
                "amount": r.get("amount"),
                "category": "Income",
                "merchant": r.get("source"),
                "note": r.get("note"),
                "expense_date": r.get("income_date"),
                "expense_time": None,
                "source": "income",
            }
            for r in income_rows
        ]
    else:
        if output_format != "summary":
            output_format = "summary"
        expenses = await get_expenses_in_range(
            conn,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            category=filters.get("category"),
            merchant=filters.get("merchant"),
            min_amount=filters.get("min_amount"),
            max_amount=filters.get("max_amount"),
        )
        income_rows = await get_incomes_in_range(
            conn,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            source=None,
            min_amount=None,
            max_amount=None,
        )

    # ── Step 3: Disagreement check ────────────────────────────────────────────
    if check_disagreement(qdata, expenses):
        log.warning("Disagreement detected in query response")

    # ── Step 4: Build analytics payload ──────────────────────────────────────
    analytics = _compute_analytics(expenses, date_from, date_to)
    if transaction_type == "both":
        expense_total = analytics.get("total", 0.0)
        income_total = sum(float(r["amount"]) for r in income_rows)
        analytics["income_total"] = round(income_total, 2)
        analytics["expense_total"] = round(expense_total, 2)
        analytics["net_total"] = round(income_total - expense_total, 2)
        analytics["transaction_type"] = "both"
    else:
        analytics["transaction_type"] = transaction_type

    # ── Step 5: Route to output formatter ────────────────────────────────────
    if output_format == "csv":
        return await _format_as_csv_reply(expenses, analytics, date_from, date_to)

    if output_format == "comparison":
        # For comparison: fetch previous period too
        prev = await _fetch_comparison_period(conn, user_id, filters, ctx)
        return await _format_comparison_reply(
            conn=conn,
            expenses=expenses,
            prev_expenses=prev,
            analytics=analytics,
            user_message=user_message,
            ctx=ctx,
            bot_data=bot_data,
            filters=filters,
        )

    # summary or list — both go through Flash Lite
    return await _format_reply(
        conn=conn,
        expenses=expenses,
        analytics=analytics,
        output_format=output_format,
        user_message=user_message,
        ctx=ctx,
        bot_data=bot_data,
        filters=filters,
        date_from=date_from,
        date_to=date_to,
    )


# ── Analytics ─────────────────────────────────────────────────────────────────


def _compute_analytics(
    expenses: list[dict[str, Any]],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Compute summary statistics from a list of expense records."""
    if not expenses:
        return {
            "total": 0.0,
            "count": 0,
            "by_category": {},
            "by_merchant": {},
            "average": 0.0,
            "largest": None,
            "date_from": date_from,
            "date_to": date_to,
        }

    total = sum(float(e["amount"]) for e in expenses)

    by_category: dict[str, float] = {}
    by_merchant: dict[str, float] = {}
    largest = max(expenses, key=lambda e: float(e["amount"]))

    for e in expenses:
        amt = float(e["amount"])
        cat = e.get("category", "Other")
        merchant = e.get("merchant") or "Unknown"

        by_category[cat] = by_category.get(cat, 0.0) + amt
        by_merchant[merchant] = by_merchant.get(merchant, 0.0) + amt

    return {
        "total": round(total, 2),
        "count": len(expenses),
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
        "by_merchant": dict(sorted(by_merchant.items(), key=lambda x: -x[1])[:10]),
        "average": round(total / len(expenses), 2),
        "largest": {
            "amount": float(largest["amount"]),
            "merchant": largest.get("merchant"),
            "category": largest.get("category"),
            "date": largest.get("expense_date"),
        },
        "date_from": date_from,
        "date_to": date_to,
    }


# ── Grouped list formatter ────────────────────────────────────────────────────


def _group_expenses(expenses: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group expenses by category, sorted by category total descending."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in expenses:
        cat = e.get("category", "Other")
        groups.setdefault(cat, []).append(e)

    # Sort categories by their total spend
    return dict(
        sorted(
            groups.items(),
            key=lambda kv: sum(float(e["amount"]) for e in kv[1]),
            reverse=True,
        )
    )


# ── CSV reply ─────────────────────────────────────────────────────────────────


async def _format_as_csv_reply(
    expenses: list[dict[str, Any]],
    analytics: dict[str, Any],
    date_from: str,
    date_to: str,
) -> str:
    """Return a plain-text CSV block."""
    if not expenses:
        return f"Nothing logged between {_fmt_date(date_from)} and {_fmt_date(date_to)}."

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["date", "merchant", "category", "amount"],
        lineterminator="\n",
    )
    writer.writeheader()
    for e in sorted(expenses, key=lambda x: x.get("expense_date", "")):
        writer.writerow(
            {
                "date": e.get("expense_date", ""),
                "merchant": e.get("merchant") or "",
                "category": e.get("category", ""),
                "amount": e.get("amount", 0),
            }
        )

    csv_text = buf.getvalue()
    header = (
        f"*{_fmt_date(date_from)} to {_fmt_date(date_to)}* — "
        f"{analytics['count']} transactions, ₹{analytics['total']:,.0f} total\n\n"
    )
    return f"{header}```\n{csv_text}```"


# ── Comparison period fetcher ─────────────────────────────────────────────────


async def _fetch_comparison_period(
    conn: aiosqlite.Connection,
    user_id: int,
    filters: dict[str, Any],
    ctx: dict[str, str],
) -> list[dict[str, Any]]:
    """Fetch the previous equivalent period for comparison queries."""
    from datetime import date, timedelta

    try:
        # 1. AI Explicit Comparison Range
        ai_from = filters.get("compare_date_from")
        ai_to = filters.get("compare_date_to")
        if ai_from and ai_to:
            return await get_expenses_in_range(
                conn,
                user_id=user_id,
                date_from=ai_from,
                date_to=ai_to,
                category=filters.get("category"),
                merchant=filters.get("merchant"),
            )

        # 2. Heuristic fallback: equivalent previous period
        date_from = date.fromisoformat(filters.get("date_from") or ctx["month_start"])
        date_to = date.fromisoformat(filters.get("date_to") or ctx["today"])
        delta = (date_to - date_from).days + 1
        prev_to = date_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=delta - 1)

        return await get_expenses_in_range(
            conn,
            user_id=user_id,
            date_from=prev_from.isoformat(),
            date_to=prev_to.isoformat(),
            category=filters.get("category"),
            merchant=filters.get("merchant"),
        )
    except Exception:
        return []


# ── Flash Lite reply formatters ───────────────────────────────────────────────


async def _format_reply(
    conn: aiosqlite.Connection,
    expenses: list[dict[str, Any]],
    analytics: dict[str, Any],
    output_format: str,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
    filters: dict[str, Any],
    date_from: str,
    date_to: str,
) -> str:
    """Flash Lite formats the query result as a human reply."""
    if not expenses:
        period = _period_label(date_from, date_to, ctx)
        ttype = (analytics.get("transaction_type") or "expense").lower()
        if ttype == "income":
            return f"No income logged {period}."
        if ttype == "both":
            return f"No transactions logged {period}."
        return f"Nothing logged {period}."

    grouped = _group_expenses(expenses) if output_format == "list" else {}

    data_payload = {
        "analytics": analytics,
        "output_format": output_format,
        "grouped": grouped if output_format == "list" else {},
        "period_label": _period_label(date_from, date_to, ctx),
        "filters": filters,
        "sample_rows": expenses[:10],  # cap to avoid token overflow
    }

    reply_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message=user_message,
        context={
            **ctx,
            "task_type": f"query_{output_format}",
            "data": json.dumps(data_payload, ensure_ascii=False, default=str),
            "last_tone": bot_data.get("last_tone", "none"),
        },
        use_lite=True,
    )
    reply_resp = await gateway.call(reply_req, db_conn=conn)

    if reply_resp.ok:
        msg = reply_resp.data.get("message", "")
        bot_data["last_tone"] = normalize_tone(reply_resp.data.get("tone_used", DEFAULT_TONE))
        if msg:
            return msg

    # Fallback: plain text summary
    return _plain_summary(analytics, date_from, date_to, ctx)


async def _format_comparison_reply(
    conn: aiosqlite.Connection,
    expenses: list[dict[str, Any]],
    prev_expenses: list[dict[str, Any]],
    analytics: dict[str, Any],
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
    filters: dict[str, Any],
) -> str:
    """Flash Lite formats a comparison between two periods."""
    prev_analytics = _compute_analytics(
        prev_expenses,
        filters.get("date_from", ""),
        filters.get("date_to", ""),
    )

    pct_change = 0.0
    if prev_analytics["total"] > 0:
        pct_change = (
            (analytics["total"] - prev_analytics["total"]) / prev_analytics["total"]
        ) * 100

    data_payload = {
        "current": analytics,
        "previous": prev_analytics,
        "pct_change": round(pct_change, 1),
        "direction": "up" if pct_change > 0 else "down",
    }

    reply_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message=user_message,
        context={
            **ctx,
            "task_type": "query_comparison",
            "data": json.dumps(data_payload, ensure_ascii=False, default=str),
            "last_tone": bot_data.get("last_tone", "none"),
        },
        use_lite=True,
    )
    reply_resp = await gateway.call(reply_req, db_conn=conn)

    if reply_resp.ok:
        msg = reply_resp.data.get("message", "")
        bot_data["last_tone"] = normalize_tone(reply_resp.data.get("tone_used", DEFAULT_TONE))
        if msg:
            return msg

    # Fallback
    direction = "up" if pct_change > 0 else "down"
    return (
        f"₹{analytics['total']:,.0f} this period vs "
        f"₹{prev_analytics['total']:,.0f} last — "
        f"{abs(pct_change):.1f}% {direction}."
    )


# ── Plain text fallbacks ──────────────────────────────────────────────────────


def _plain_summary(
    analytics: dict[str, Any],
    date_from: str,
    date_to: str,
    ctx: dict[str, str],
) -> str:
    """Minimal fallback when Flash Lite fails."""
    period = _period_label(date_from, date_to, ctx)
    total = analytics["total"]
    count = analytics["count"]
    lines = [f"*₹{total:,.0f}* {period} — {count} transactions."]

    top_cats = list(analytics["by_category"].items())[:3]
    if top_cats:
        cat_lines = ", ".join(f"{c}: ₹{v:,.0f}" for c, v in top_cats)
        lines.append(cat_lines)

    return "\n".join(lines)


# ── Date helpers ──────────────────────────────────────────────────────────────


def _fmt_date(iso: str) -> str:
    """Convert YYYY-MM-DD to '9 Apr' style."""
    try:
        from datetime import date

        d = date.fromisoformat(iso)
        return d.strftime("%-d %b")
    except Exception:
        return iso


def _period_label(date_from: str, date_to: str, ctx: dict[str, str]) -> str:
    """Return a human period label like 'this week', 'in April', or a date range."""
    today = ctx.get("today", "")
    week_start = ctx.get("week_start", "")
    month_start = ctx.get("month_start", "")

    if date_from == week_start and date_to == today:
        return "this week"
    if date_from == month_start and date_to == today:
        return "this month"
    if date_from == ctx.get("last_month_start") and date_to == ctx.get("last_month_end"):
        return "last month"
    if date_from == date_to:
        return f"on {_fmt_date(date_from)}"

    return f"from {_fmt_date(date_from)} to {_fmt_date(date_to)}"
