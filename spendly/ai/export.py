"""Export handler — Phase 9.

Handles EXPORT intent. No Gemini needed for file generation — Gemini is only
used to parse the user's natural language export request into structured filters
(which month, which format, which category filter).

Flow:
1. Use query_build prompt (Flash) to extract date range + optional filters
2. Fetch expenses from DB with those filters
3. Generate CSV and/or PDF (hardcoded, no Gemini)
4. Send file(s) via Telegram as document attachments
5. Flash Lite sends a short confirmation reply
"""

from __future__ import annotations

import io
from datetime import date
from typing import Any

import aiosqlite
from telegram import InputFile
from telegram.ext import ContextTypes

from spendly.ai.gateway import gateway
from spendly.ai.models import QUERY_SCHEMA, GatewayRequest
from spendly.core.config import settings
from spendly.core.logger import get_logger
from spendly.db.expenses import get_expenses_in_range
from spendly.utils.export import generate_csv, generate_pdf

log = get_logger(__name__)


# ── Main entry point ───────────────────────────────────────────────────────────


async def process_export(
    conn: aiosqlite.Connection,
    user_id: int,
    user_message: str,
    ctx: dict[str, str],
    bot_data: dict[str, Any],
    telegram_context: ContextTypes.DEFAULT_TYPE,
) -> str:
    """Parse export request, generate files, send via Telegram.

    Returns a short reply string confirming what was sent.
    """
    # ── Step 1: Flash extracts date range + format from NL ────────────────────
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
        log.error("Export query build failed", extra={"error": query_resp.error})
        return "Couldn't figure out which period to export — try: *export april as CSV*"

    filters = query_resp.data.get("filters", {})
    output_format = query_resp.data.get("output_format", "csv")

    # Resolve date range — default to current month
    date_from = filters.get("date_from") or ctx.get("month_start") or ctx["today"]
    date_to = filters.get("date_to") or ctx["today"]

    # Determine formats to generate
    wants_pdf = "pdf" in user_message.lower() or output_format == "pdf"
    wants_csv = "csv" in user_message.lower() or output_format in ("csv", "list", "summary")

    # Default: always generate CSV; add PDF if requested
    generate_both = "both" in user_message.lower()
    if generate_both:
        wants_csv = wants_pdf = True
    elif not wants_csv and not wants_pdf:
        wants_csv = True  # default to CSV

    log.info(
        "Export request",
        extra={
            "date_from": date_from,
            "date_to": date_to,
            "csv": wants_csv,
            "pdf": wants_pdf,
        },
    )

    # ── Step 2: Fetch expenses ────────────────────────────────────────────────
    expenses = await get_expenses_in_range(
        conn,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        category=filters.get("category"),
        merchant=filters.get("merchant"),
    )

    if not expenses:
        period_label = _period_label(date_from, date_to)
        return f"Nothing logged {period_label} to export."

    # ── Step 3: Generate files ────────────────────────────────────────────────
    month_label = _month_label(date_from, date_to)
    files_sent = []

    if wants_csv:
        csv_bytes = generate_csv(expenses, month_label=month_label)
        filename = _csv_filename(date_from, date_to, filters.get("category"))
        await telegram_context.bot.send_document(
            chat_id=settings.user_id,
            document=InputFile(io.BytesIO(csv_bytes), filename=filename),
            caption=f"CSV export — {month_label} ({len(expenses)} transactions)",
        )
        files_sent.append("CSV")
        log.info("CSV sent", extra={"filename": filename, "rows": len(expenses)})

    if wants_pdf:
        pdf_bytes = generate_pdf(
            expenses,
            month_label=month_label,
            monthly_budget=settings.monthly_budget,
        )
        filename = _pdf_filename(date_from, date_to, filters.get("category"))
        await telegram_context.bot.send_document(
            chat_id=settings.user_id,
            document=InputFile(io.BytesIO(pdf_bytes), filename=filename),
            caption=f"PDF report — {month_label} ({len(expenses)} transactions)",
        )
        files_sent.append("PDF")
        log.info("PDF sent", extra={"filename": filename, "rows": len(expenses)})

    # ── Step 4: Confirmation reply ────────────────────────────────────────────
    total = sum(float(e.get("amount", 0)) for e in expenses)
    fmt_label = " + ".join(files_sent)
    return (
        f"{fmt_label} sent — {len(expenses)} transactions, ₹{total:,.0f} total for {month_label}."
    )


# ── File naming ────────────────────────────────────────────────────────────────


def _csv_filename(date_from: str, date_to: str, category: str | None) -> str:
    suffix = f"_{category.lower()}" if category else ""
    label = _compact_label(date_from, date_to)
    return f"spendly_{label}{suffix}.csv"


def _pdf_filename(date_from: str, date_to: str, category: str | None) -> str:
    suffix = f"_{category.lower()}" if category else ""
    label = _compact_label(date_from, date_to)
    return f"spendly_{label}{suffix}.pdf"


def _compact_label(date_from: str, date_to: str) -> str:
    """e.g. '2025-04' for a full month, '2025-04-01_2025-04-15' for a range."""
    try:
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to)
        # Full month
        if d0.day == 1 and d1.month == d0.month and d1.day >= 28:
            return d0.strftime("%Y-%m")
        return f"{date_from}_{date_to}"
    except ValueError:
        return f"{date_from}_{date_to}"


def _month_label(date_from: str, date_to: str) -> str:
    try:
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to)
        if d0.month == d1.month and d0.year == d1.year:
            return d0.strftime("%B %Y")
        return f"{d0.strftime('%d %b')} - {d1.strftime('%d %b %Y')}"
    except ValueError:
        return f"{date_from} to {date_to}"


def _period_label(date_from: str, date_to: str) -> str:
    try:
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to)
        if d0 == d1:
            return f"on {d0.strftime('%d %b')}"
        if d0.month == d1.month:
            return f"in {d0.strftime('%B %Y')}"
        return f"from {d0.strftime('%d %b')} to {d1.strftime('%d %b %Y')}"
    except ValueError:
        return "in that period"
