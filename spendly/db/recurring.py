"""Recurring expense processing logic."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import aiosqlite

from spendly.core.logger import get_logger
from spendly.db.expenses import insert_expense
from spendly.db.incomes import insert_income

log = get_logger(__name__)

def _subscription_period_bounds(sub: dict[str, Any], today: date) -> tuple[str, str]:
    from datetime import timedelta

    freq = sub.get("frequency", "monthly")

    if freq == "daily":
        iso = today.isoformat()
        return iso, iso

    if freq == "weekly":
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        return week_start.isoformat(), week_end.isoformat()

    if freq == "biweekly":
        # Check window: 14 days around the expected due date
        # For simple de-dup, just use a 14-day trailing window
        start = today - timedelta(days=13)
        return start.isoformat(), today.isoformat()

    if freq == "monthly" or freq == "last_day_of_month":
        month_start = today.replace(day=1)
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        return month_start.isoformat(), month_end.isoformat()

    if freq == "yearly":
        return date(today.year, 1, 1).isoformat(), date(today.year, 12, 31).isoformat()

    iso = today.isoformat()
    return iso, iso


async def find_logged_expense_for_subscription_period(
    conn: aiosqlite.Connection,
    user_id: int,
    subscription: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    target = today or date.today()
    merchant = (subscription.get("merchant") or "").strip()
    if not merchant:
        return None

    date_from, date_to = _subscription_period_bounds(subscription, target)
    async with conn.execute(
        """
        SELECT id, amount, expense_date, expense_time, category
        FROM expenses
        WHERE user_id = ?
          AND is_deleted = 0
          AND LOWER(merchant) = LOWER(?)
          AND expense_date BETWEEN ? AND ?
        ORDER BY expense_date DESC, id DESC
        LIMIT 1
        """,
        (user_id, merchant, date_from, date_to),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None

async def find_logged_income_for_subscription_period(
    conn: aiosqlite.Connection,
    user_id: int,
    subscription: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    target = today or date.today()
    source = (subscription.get("source") or "").strip()
    if not source:
        return None

    date_from, date_to = _subscription_period_bounds(subscription, target)
    async with conn.execute(
        """
        SELECT id, amount, income_date, source
        FROM incomes
        WHERE user_id = ?
          AND LOWER(source) = LOWER(?)
          AND income_date BETWEEN ? AND ?
        ORDER BY income_date DESC, id DESC
        LIMIT 1
        """,
        (user_id, source, date_from, date_to),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None

async def subscription_due_today(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    target = today or date.today()

    due: list[dict[str, Any]] = []

    # Fetch expenses
    async with conn.execute(
        "SELECT *, 'expense' as transaction_type FROM recurring_expenses WHERE user_id = ? AND is_active = 1",
        (user_id,),
    ) as cur:
        for r in await cur.fetchall():
            sub = dict(r)
            if _check_if_due(sub, target):
                due.append(sub)
                
    # Fetch incomes
    async with conn.execute(
        "SELECT *, 'income' as transaction_type FROM recurring_incomes WHERE user_id = ? AND is_active = 1",
        (user_id,),
    ) as cur:
        for r in await cur.fetchall():
            sub = dict(r)
            # Normalize source to merchant for generic handling downstream if needed, but better to keep it clean.
            sub["merchant"] = sub["source"]
            if _check_if_due(sub, target):
                due.append(sub)

    return due


async def get_subscription_by_merchant(
    conn: aiosqlite.Connection,
    user_id: int,
    merchant: str,
) -> dict[str, Any] | None:
    m = merchant.strip()
    if not m:
        return None
    async with conn.execute(
        """
        SELECT *, 'expense' as transaction_type FROM recurring_expenses
        WHERE user_id = ? AND is_active = 1 AND LOWER(merchant) = LOWER(?)
        LIMIT 1
        """,
        (user_id, m),
    ) as cur:
        row = await cur.fetchone()
        
    if row:
        return dict(row)
        
    async with conn.execute(
        """
        SELECT *, source as merchant, 'income' as transaction_type FROM recurring_incomes
        WHERE user_id = ? AND is_active = 1 AND LOWER(source) = LOWER(?)
        LIMIT 1
        """,
        (user_id, m),
    ) as cur:
        row = await cur.fetchone()
        
    return dict(row) if row else None


async def was_subscription_reminded_today(
    conn: aiosqlite.Connection,
    subscription_id: int,
    transaction_type: str,
    *,
    today: date | None = None,
) -> bool:
    target = today or date.today()
    table = "recurring_expenses" if transaction_type == "expense" else "recurring_incomes"
    async with conn.execute(
        f"SELECT last_reminded_at FROM {table} WHERE id = ?",
        (subscription_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    last = row["last_reminded_at"]
    if not last:
        return False
    try:
        last_date = datetime.fromisoformat(last).date()
    except ValueError:
        return False
    return last_date == target



async def process_recurring_expenses(
    conn: aiosqlite.Connection,
    user_id: int,
) -> list[dict[str, Any]]:
    """Check all active recurring subscriptions/incomes and log them if due today.

    Returns a list of logged expenses for notification.
    """
    today = date.today()
    today_iso = today.isoformat()
    logged_items = []

    # Fetch active expenses
    async with conn.execute(
        """
        SELECT *, 'expense' as transaction_type FROM recurring_expenses
        WHERE user_id = ? AND is_active = 1
        AND (last_logged_date IS NULL OR last_logged_date != ?)
        """,
        (user_id, today_iso),
    ) as cur:
        subs = [dict(r) for r in await cur.fetchall()]

    # Fetch active incomes
    async with conn.execute(
        """
        SELECT *, 'income' as transaction_type FROM recurring_incomes
        WHERE user_id = ? AND is_active = 1
        AND (last_logged_date IS NULL OR last_logged_date != ?)
        """,
        (user_id, today_iso),
    ) as cur:
        incomes = [dict(r) for r in await cur.fetchall()]
        for inc in incomes:
            inc["merchant"] = inc["source"]
        subs.extend(incomes)

    for sub in subs:
        is_due = _check_if_due(sub, today)
        if is_due:
            try:
                txn_type = sub.get("transaction_type", "expense")
                if txn_type == "expense":
                    await insert_expense(
                        conn,
                        user_id=user_id,
                        amount=sub["amount"],
                        category=sub["category"],
                        merchant=sub["merchant"],
                        note=f"Auto-logged recurring {sub['frequency']}",
                        expense_date=today_iso,
                        expense_time=None,
                        source="system",
                    )
                else:
                    await insert_income(
                        conn,
                        user_id=user_id,
                        amount=sub["amount"],
                        source=sub["merchant"],
                        note=f"Auto-logged recurring {sub['frequency']}",
                        income_date=today_iso,
                    )

                # Update last_logged_date
                table = "recurring_expenses" if txn_type == "expense" else "recurring_incomes"
                await conn.execute(
                    f"UPDATE {table} "
                    "SET last_logged_date = ?, updated_at = ? WHERE id = ?",
                    (today_iso, datetime.now().isoformat(), sub["id"]),
                )
                await conn.commit()

                logged_items.append(
                    {
                        "merchant": sub["merchant"],
                        "amount": sub["amount"],
                        "category": sub["category"],
                        "transaction_type": sub.get("transaction_type", "expense"),
                    }
                )
                log.info(
                    "Auto-logged recurring item",
                    extra={"sub_id": sub["id"], "merchant": sub["merchant"]},
                )
            except Exception:
                log.error(
                    "Failed to auto-log recurring item",
                    extra={"sub_id": sub["id"]},
                    exc_info=True,
                )

    return logged_items


def _check_if_due(sub: dict[str, Any], today: date) -> bool:
    """Check if a recurring subscription is due today based on frequency."""
    freq = sub["frequency"]
    day = sub["billing_day"]
    month = sub["billing_month"]

    if freq == "daily":
        return True

    if freq == "weekly":
        # Python weekday: 0=Mon, 6=Sun. We aligned to this in expense.py
        return today.weekday() == day

    if freq == "monthly":
        # If day is 31 and month has 30 days, log on the last day?
        # For simplicity, just exact match or last day if it exceeds.
        import calendar

        _, last_day = calendar.monthrange(today.year, today.month)
        effective_day = min(day, last_day)
        return today.day == effective_day

    if freq == "yearly":
        if month is None:
            return False  # Invalid yearly subscription without month
        return today.month == month and today.day == day

    if freq == "biweekly":
        # Use created_at as the reference start date
        try:
            created_at = datetime.fromisoformat(sub["created_at"]).date()
            delta = (today - created_at).days
            return delta % 14 == 0
        except (ValueError, TypeError):
            return False

    if freq == "last_day_of_month":
        import calendar
        _, last_day = calendar.monthrange(today.year, today.month)
        return today.day == last_day
