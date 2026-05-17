"""Patch scheduler.py: fix timing and comments."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

SRC = r"d:\Sandbox\Spendly\spendly\bot\scheduler.py"

with open(SRC, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: docstring
content = content.replace(
    '    """Build and configure the APScheduler instance.\n\n    Initial check-in times are randomised here; they are re-randomised\n    every day at 00:01 by job_reschedule_checkins.\n    """',
    '    """Build and configure the APScheduler instance.\n\n    Single daily proactive message at 09:00.\n    Bill nudges fire at 09:10 only on the due date (already-logged guard prevents spam).\n    """'
)

# Fix 2: bill nudge timing comment + minute
content = content.replace(
    '    # Daily reschedule at 00:01 — picks fresh random times for check-ins and bill nudges\n    scheduler.add_job(\n        job_reschedule_bill_nudges,\n        trigger="cron",\n        hour=9,\n        minute=5,',
    '    # Bill nudges: 09:10 each day — only alerts for subscriptions due TODAY (not already paid)\n    scheduler.add_job(\n        job_reschedule_bill_nudges,\n        trigger="cron",\n        hour=9,\n        minute=10,'
)

# Fix 3: monthly reflection time (avoid collision with 09:00 check-in)
content = content.replace(
    '    # Monthly reflection: 1st of every month at 09:00\n    scheduler.add_job(\n        job_end_of_month_reflection,\n        trigger="cron",\n        day=1,\n        hour=9,\n        minute=0,',
    '    # Monthly reflection: 1st of each month at 09:30 (after checkin at 09:00)\n    scheduler.add_job(\n        job_end_of_month_reflection,\n        trigger="cron",\n        day=1,\n        hour=9,\n        minute=30,'
)

# Fix 4: log count
content = content.replace(
    '    log.info(\n        "Scheduler configured — 11 jobs",\n        extra={"checkin_daily": "09:00"},\n    )',
    '    log.info(\n        "Scheduler configured — 10 jobs",\n        extra={"checkin": "09:00", "bill_nudges": "09:10 (due-date only)"},\n    )'
)

with open(SRC, "w", encoding="utf-8") as f:
    f.write(content)

print("Scheduler patched successfully.")
