"""What-If Scenario handler.

Analyzes hypothetical expenses against current budget and spending velocity
to give immediate financial feedback.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

import aiosqlite

from spendly.ai.gateway import gateway
from spendly.ai.models import GatewayRequest
from spendly.core.config import settings
from spendly.core.logger import get_logger

log = get_logger(__name__)


async def process_whatif(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
) -> str:
    """Evaluate a hypothetical scenario ("what if I buy X?")."""
    today = date.today()
    month_start = today.replace(day=1).isoformat()

    # 1. Gather real budget data
    budget = settings.monthly_budget

    async with conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM expenses
        WHERE user_id = ? AND is_deleted = 0
          AND expense_date BETWEEN ? AND ?
        """,
        (user_id, month_start, today.isoformat()),
    ) as cur:
        row = await cur.fetchone()
        spent_this_month = float(row[0]) if row else 0.0

    # Calculate runout date before scenario
    current_runout_date = "N/A"
    if budget > 0 and spent_this_month > 0:
        days_elapsed = today.day
        velocity = spent_this_month / days_elapsed
        days_until_empty = int(budget / velocity) if velocity > 0 else 0

        _, days_in_month = calendar.monthrange(today.year, today.month)
        if (velocity * days_in_month) > budget:
            rd = today.replace(day=1) + timedelta(days=days_until_empty - 1)
            current_runout_date = f"{rd.strftime('%B')} {rd.day}"

    # 2. Ask Gemini to evaluate the impact
    prompt_ctx = {
        **ctx,
        "monthly_budget": str(budget),
        "spent_this_month": f"{spent_this_month:,.0f}",
        "current_runout_date": current_runout_date,
    }

    req = GatewayRequest(
        task="what_if_scenario",
        prompt_name="what_if_scenario",
        user_message=user_message,
        context=prompt_ctx,
    )

    resp = await gateway.call(req, db_conn=conn)
    if not resp.ok:
        log.error("What-if failed", extra={"error": resp.error})
        return "I had trouble analyzing that scenario. Can you simplify it?"

    data = resp.data
    reply_msg = data.get("reply")

    if not reply_msg:
        return "Not sure about that scenario."

    # We could theoretically save this scenario to DB, but for now we just return the reply
    return reply_msg
