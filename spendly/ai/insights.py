"""Insights and anomaly detection — Phase 7.

Two distinct flows:

1. On-demand insights (INSIGHT intent):
   - User asks "any patterns?" or "where is my money going?"
   - Flash analyzes 30-day spending data
   - Returns 2-4 specific, data-backed insights
   - Flash Lite formats warm Telegram reply

2. Proactive anomaly detection (scheduler-triggered):
   - Background job runs periodically
   - Flash checks if any category crossed the budget threshold
   - Dedup via anomaly_alerts table (one alert per category per month)
   - Sends direct Telegram push if threshold breached

3. Proactive check-in (scheduler-triggered):
   - Runs twice daily at random times between 09:00-23:00
   - Flash Lite generates a short, conversational nudge
   - Sent directly to Telegram
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.config import settings
from spendly.core.constants import DEFAULT_TONE, normalize_tone
from spendly.core.logger import get_logger
from spendly.db.expenses import get_monthly_category_totals
from spendly.db.insights import (
    build_spending_data,
    get_alerted_categories_this_month,
    get_recent_insights,
    mark_insights_read,
    record_anomaly_alert,
    save_insight,
)

log = get_logger(__name__)


# ── On-demand insight generation ───────────────────────────────────────────────


async def process_insight_request(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Handle INSIGHT intent — user asked for patterns/analysis.

    Returns reply string for Telegram.
    """
    # Build rich spending summary
    spending_data = await build_spending_data(conn, user_id, days=30)

    if spending_data.get("empty"):
        return "Nothing logged in the last 30 days - no patterns to surface yet."

    # Flash generates insights
    insight_req = GatewayRequest(
        task="insight_generate",
        prompt_name="insight_generate",
        user_message=user_message,
        context={
            **ctx,
            "spending_data": json.dumps(spending_data, ensure_ascii=False),
            "user_patterns": ctx.get("user_patterns", "{}"),
        },
        use_lite=False,
    )
    insight_resp = await gateway.call(insight_req, db_conn=conn)

    if not insight_resp.ok:
        log.error("Insight generation failed", extra={"error": insight_resp.error})
        return "Couldn't pull insights right now - try again in a moment."

    insights: list[dict[str, Any]] = insight_resp.data.get("insights", [])

    if not insights:
        return "Everything looks pretty steady - no major patterns jumping out right now."

    # Persist all insights to DB
    for ins in insights:
        await save_insight(
            conn,
            user_id,
            insight_type=ins.get("insight_type", "general"),
            title=ins.get("title", ""),
            body=ins.get("body", ""),
            category=ins.get("category"),
            data_json=ins.get("data"),
            period_start=spending_data.get("from_date"),
            period_end=spending_data.get("to_date"),
        )

    # Flash Lite formats the reply
    reply = await _format_insights_reply(
        conn=conn,
        insights=insights,
        spending_data=spending_data,
        user_message=user_message,
        ctx=ctx,
        bot_data=bot_data,
    )

    await mark_insights_read(conn, user_id)
    return reply


# ── Anomaly detection (proactive, scheduler-triggered) ─────────────────────────


async def run_anomaly_check(
    conn: aiosqlite.Connection,
    user_id: int,
    ctx: dict[str, str],
) -> list[str]:
    """Check if any spending category has crossed the alert threshold.

    Returns list of alert messages to push to Telegram.
    Dedup enforced via anomaly_alerts table.
    """
    if settings.monthly_budget <= 0:
        return []

    from datetime import date

    today = date.today()
    month_str = today.strftime("%Y-%m")

    # Current month category totals
    category_totals = await get_monthly_category_totals(conn, user_id, today.year, today.month)
    if not category_totals:
        return []

    # Already alerted categories this month
    already_alerted = await get_alerted_categories_this_month(conn, user_id, month_str)

    # Flash checks which categories crossed threshold
    anomaly_req = GatewayRequest(
        task="anomaly_check",
        prompt_name="anomaly_check",
        user_message="",
        context={
            **ctx,
            "monthly_budget": str(settings.monthly_budget),
            "anomaly_pct": str(int(settings.anomaly_budget_pct * 100)),
            "category_totals": json.dumps(category_totals, ensure_ascii=False),
            "already_alerted": json.dumps(already_alerted),
        },
        use_lite=False,
    )
    anomaly_resp = await gateway.call(anomaly_req, db_conn=conn)

    if not anomaly_resp.ok:
        log.error("Anomaly check failed", extra={"error": anomaly_resp.error})
        return []

    alerts_data: list[dict[str, Any]] = anomaly_resp.data.get("alerts", [])
    messages: list[str] = []

    for alert in alerts_data:
        category = alert.get("category", "")
        spent = float(alert.get("spent", 0))
        message = alert.get("message", "")

        if not category or not message:
            continue

        # Record alert (UNIQUE constraint prevents double-send)
        recorded = await record_anomaly_alert(conn, user_id, category, spent, month_str)
        if recorded:
            messages.append(f"🔔 {message}")
            log.info("Anomaly alert triggered", extra={"category": category, "spent": spent})

    return messages


# ── Proactive check-in (scheduler-triggered, twice daily) ──────────────────────


async def generate_checkin_message(
    conn: aiosqlite.Connection,
    user_id: int,
    ctx: dict[str, str],
    period: str = "general",
) -> str | None:
    """Generate a short, warm proactive check-in message via Flash Lite.

    period: "morning" | "evening" | "general" | "end_of_month" | "logging_gap"

    Returns the message string, or None if nothing worth surfacing.
    """
    data_payload: dict[str, Any]
    if period == "daily_mini":
        from datetime import date, timedelta

        today = date.today()
        yesterday = today - timedelta(days=1)
        month_start = today.replace(day=1)

        async with conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0 AND expense_date = ?
            """,
            (user_id, yesterday.isoformat()),
        ) as cur:
            row = await cur.fetchone()
            y_total = float(row["total"]) if row else 0.0
            y_count = int(row["count"]) if row else 0

        async with conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND is_deleted = 0 AND expense_date BETWEEN ? AND ?
            """,
            (user_id, month_start.isoformat(), today.isoformat()),
        ) as cur:
            row = await cur.fetchone()
            m_total = float(row["total"]) if row else 0.0

        data_payload = {
            "period": "daily_mini",
            "yesterday": yesterday.strftime("%d %b"),
            "yesterday_total": round(y_total, 2),
            "yesterday_count": y_count,
            "month_total": round(m_total, 2),
            "month_label": today.strftime("%b %Y"),
            "call_to_action": "Ask the user to log today's expenses (short, one line).",
        }
    else:
        recent = await get_recent_insights(conn, user_id, limit=3)
        spending_summary = await build_spending_data(conn, user_id, days=7)
        data_payload = {
            "period": period,
            "spending_7_days": spending_summary,
            "recent_insights": recent,
        }

    checkin_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message="",
        context={
            **ctx,
            "task_type": f"proactive_checkin_{period}",
            "data": json.dumps(data_payload, ensure_ascii=False, default=str),
            "last_tone": ctx.get("last_tone", "none"),
        },
        use_lite=True,
    )
    reply_resp = await gateway.call(checkin_req, db_conn=conn)

    if reply_resp.ok:
        msg = reply_resp.data.get("message", "").strip()
        return msg if msg else None

    return None


# ── Insight reply formatter ────────────────────────────────────────────────────


async def _format_insights_reply(
    conn: aiosqlite.Connection,
    insights: list[dict[str, Any]],
    spending_data: dict[str, Any],
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Flash Lite formats the insight list into a warm, conversational reply."""
    data_payload = {
        "insights": insights,
        "spending_data": spending_data,
    }

    reply_req = GatewayRequest(
        task="reply_format",
        prompt_name="reply_format",
        user_message=user_message,
        context={
            **ctx,
            "task_type": "insights",
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

    # Plain fallback - render top 2 insights as text
    lines = []
    for ins in insights[:2]:
        lines.append(f"*{ins.get('title', '')}*")
        lines.append(ins.get("body", ""))
    return "\n\n".join(lines) if lines else "No significant patterns found."
