"""Fix recurring.py: add user_id filter in process_recurring_expenses + add blank line before functions."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

SRC = r"d:\Sandbox\Spendly\spendly\db\recurring.py"

with open(SRC, "r", encoding="utf-8") as f:
    content = f.read()

# Bug fix 1: process_recurring_expenses was missing user_id filter in the query
OLD_QUERY = '''    # Fetch active subscriptions that haven't been logged today
    async with conn.execute(
        """
        SELECT * FROM recurring_subscriptions
        WHERE is_active = 1
        AND (last_logged_date IS NULL OR last_logged_date != ?)
        """,
        (today_iso,),
    ) as cur:
        subs = await cur.fetchall()'''

NEW_QUERY = '''    # Fetch active subscriptions that haven't been logged today (scoped to user_id)
    async with conn.execute(
        """
        SELECT * FROM recurring_subscriptions
        WHERE user_id = ? AND is_active = 1
        AND (last_logged_date IS NULL OR last_logged_date != ?)
        """,
        (user_id, today_iso),
    ) as cur:
        subs = await cur.fetchall()'''

if OLD_QUERY in content:
    content = content.replace(OLD_QUERY, NEW_QUERY)
    print("Bug fix 1 applied: user_id filter added to process_recurring_expenses query")
else:
    print("WARNING: Bug fix 1 target not found — may have already been patched")

# Fix 2: add missing blank lines between top-level functions (PEP 8)
OLD_BOUNDARY = "def _subscription_period_bounds"
NEW_BOUNDARY = "\n\ndef _subscription_period_bounds"
if "    def _subscription_period_bounds" not in content and "\n\ndef _subscription_period_bounds" not in content:
    content = content.replace("\ndef _subscription_period_bounds", "\n\n\ndef _subscription_period_bounds")
    print("Fix 2 applied: blank lines before _subscription_period_bounds")

with open(SRC, "w", encoding="utf-8") as f:
    f.write(content)

print("Done patching recurring.py")
