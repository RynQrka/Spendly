"""Export generators — Phase 9.

CSV and PDF are fully hardcoded. No Gemini involved.
These functions take a list of expense records and produce bytes.

CSV: clean, flat, sorted by date.
PDF: formatted report — header, summary table, expense list, category breakdown.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

# ── CSV ────────────────────────────────────────────────────────────────────────


def generate_csv(
    expenses: list[dict[str, Any]],
    month_label: str = "",
) -> bytes:
    """Generate a UTF-8 CSV of expenses, sorted by date ascending.

    Columns: Date, Merchant, Category, Amount (INR), Note
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    # Header row
    writer.writerow(["Date", "Merchant", "Category", "Amount (INR)", "Note"])

    # Data rows — sorted by expense_date ascending
    sorted_exp = sorted(expenses, key=lambda e: e.get("expense_date", ""))
    for e in sorted_exp:
        writer.writerow(
            [
                e.get("expense_date", ""),
                e.get("merchant") or "",
                e.get("category", ""),
                f"{float(e.get('amount', 0)):,.2f}",
                e.get("note") or "",
            ]
        )

    return buf.getvalue().encode("utf-8")


# ── PDF ────────────────────────────────────────────────────────────────────────


def generate_pdf(
    expenses: list[dict[str, Any]],
    month_label: str = "",
    monthly_budget: float = 0.0,
) -> bytes:
    """Generate a formatted PDF monthly report.

    Sections:
    1. Header — title, month, generated date
    2. Summary — total, count, daily avg, top category, budget bar
    3. Category breakdown table
    4. Full expense list table
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    elements = []
    W = A4[0] - 4 * cm  # usable width

    # ── Palette ───────────────────────────────────────────────────────────────
    COL_DARK = colors.HexColor("#1a1a2e")
    COL_MID = colors.HexColor("#16213e")
    COL_ACCENT = colors.HexColor("#0f3460")
    COL_TEXT = colors.HexColor("#e0e0e0")
    COL_WHITE = colors.white
    COL_LIGHT = colors.HexColor("#f5f5f5")
    COL_ALT = colors.HexColor("#eeeeee")

    # ── Analytics ─────────────────────────────────────────────────────────────
    total = sum(float(e.get("amount", 0)) for e in expenses)
    count = len(expenses)

    by_cat: dict[str, float] = {}
    for e in expenses:
        cat = e.get("category", "Other")
        by_cat[cat] = by_cat.get(cat, 0.0) + float(e.get("amount", 0))


    sorted_exp = sorted(expenses, key=lambda e: e.get("expense_date", ""))
    days_span = 1
    if sorted_exp:
        try:
            d0 = date.fromisoformat(sorted_exp[0]["expense_date"])
            d1 = date.fromisoformat(sorted_exp[-1]["expense_date"])
            days_span = max((d1 - d0).days + 1, 1)
        except (ValueError, KeyError):
            pass

    daily_avg = total / days_span if days_span else 0
    top_cat = max(by_cat, key=lambda k: by_cat[k]) if by_cat else "-"
    top_cat_v = by_cat.get(top_cat, 0.0)

    # ── 1. Title header ───────────────────────────────────────────────────────
    title_data = [[f"SPENDLY — {month_label.upper() or 'EXPENSE REPORT'}"]]
    title_tbl = Table(title_data, colWidths=[W])
    title_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COL_DARK),
                ("TEXTCOLOR", (0, 0), (-1, -1), COL_WHITE),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 16),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [COL_DARK]),
            ]
        )
    )
    elements.append(title_tbl)
    elements.append(Spacer(1, 0.4 * cm))

    generated = date.today().strftime("%d %B %Y")
    sub_data = [[f"Generated: {generated}"]]
    sub_tbl = Table(sub_data, colWidths=[W])
    sub_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.grey),
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    elements.append(sub_tbl)
    elements.append(Spacer(1, 0.6 * cm))

    # ── 2. Summary cards ──────────────────────────────────────────────────────
    budget_str = f"₹{monthly_budget:,.0f}" if monthly_budget > 0 else "—"
    budget_pct = f"{min(total / monthly_budget * 100, 100):.0f}%" if monthly_budget > 0 else "—"

    summary_data = [
        ["TOTAL SPEND", "TRANSACTIONS", "DAILY AVG", "TOP CATEGORY", "BUDGET"],
        [
            f"₹{total:,.0f}",
            str(count),
            f"₹{daily_avg:,.0f}",
            f"{top_cat}\n₹{top_cat_v:,.0f}",
            f"{budget_str}\n{budget_pct} used",
        ],
    ]
    col_w = W / 5
    sum_tbl = Table(summary_data, colWidths=[col_w] * 5)
    sum_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), COL_MID),
                ("TEXTCOLOR", (0, 0), (-1, 0), COL_TEXT),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("BACKGROUND", (0, 1), (-1, 1), COL_LIGHT),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (-1, 1), 11),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, 1), [COL_LIGHT]),
            ]
        )
    )
    elements.append(sum_tbl)
    elements.append(Spacer(1, 0.6 * cm))

    # ── 3. Category breakdown ─────────────────────────────────────────────────
    elements.append(_section_header("CATEGORY BREAKDOWN", W, COL_ACCENT, COL_WHITE))
    elements.append(Spacer(1, 0.2 * cm))

    cat_rows = [["Category", "Amount (₹)", "% of Total", "Transactions"]]
    cat_txn: dict[str, int] = {}
    for e in expenses:
        c = e.get("category", "Other")
        cat_txn[c] = cat_txn.get(c, 0) + 1

    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        pct = (amt / total * 100) if total else 0
        cat_rows.append([cat, f"{amt:,.0f}", f"{pct:.1f}%", str(cat_txn.get(cat, 0))])

    cat_tbl = Table(cat_rows, colWidths=[W * 0.4, W * 0.25, W * 0.2, W * 0.15])
    cat_tbl.setStyle(_table_style(COL_ACCENT, COL_WHITE, COL_LIGHT, COL_ALT))
    elements.append(cat_tbl)
    elements.append(Spacer(1, 0.6 * cm))


    # ── 5. Full expense list ──────────────────────────────────────────────────
    elements.append(_section_header("ALL TRANSACTIONS", W, COL_DARK, COL_WHITE))
    elements.append(Spacer(1, 0.2 * cm))

    exp_rows = [["Date", "Merchant", "Category", "Amount (₹)"]]
    for e in sorted_exp:
        exp_rows.append(
            [
                e.get("expense_date", ""),
                (e.get("merchant") or "")[:22],
                e.get("category", ""),
                f"{float(e.get('amount', 0)):,.0f}",
            ]
        )

    # Total row
    exp_rows.append(["", "TOTAL", "", f"{total:,.0f}"])

    exp_tbl = Table(
        exp_rows,
        colWidths=[W * 0.18, W * 0.35, W * 0.22, W * 0.25],
    )
    style = _table_style(COL_DARK, COL_WHITE, COL_LIGHT, COL_ALT)
    # Bold total row
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ddeeff"))
    exp_tbl.setStyle(style)
    elements.append(exp_tbl)

    doc.build(elements)
    return buf.getvalue()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _section_header(text: str, width: float, bg: Any, fg: Any) -> Any:
    from reportlab.platypus import Table, TableStyle

    tbl = Table([[text]], colWidths=[width])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("TEXTCOLOR", (0, 0), (-1, -1), fg),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return tbl


def _table_style(header_bg: Any, header_fg: Any, row1_bg: Any, row2_bg: Any) -> Any:
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), header_fg),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [row1_bg, row2_bg]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ]
    )
