import asyncio
from datetime import date

from spendly.bot.scheduler import _upload_monthly_to_gdrive
from spendly.core.config import settings
from spendly.db.connection import get_connection
from spendly.db.expenses import insert_expense


async def main():
    print("Starting GDrive Integration Verification...")

    from spendly.db.queries import upsert_user

    conn = await get_connection()
    try:
        # 1. Ensure user exists
        internal_user_id = await upsert_user(conn, str(settings.user_id), "Test User")
        print(f"User synced: {internal_user_id}")

        # 2. Insert test data for April 2026
        test_date = "2026-04-19"
        print(f"Inserting test expense for {test_date}...")

        expense_id = await insert_expense(
            conn,
            user_id=internal_user_id,
            amount=750.0,
            category="Food",
            merchant="Test Merchant",
            note="Integration verification test",
            payment_method="upi",
            expense_date=test_date,
            expense_time="14:00",
        )

        if not expense_id:
            print("Expense insertion failed (likely duplicate). Continuing with verification...")
        else:
            print(f"Test expense created: {expense_id}")

        # 2. Trigger GDrive upload for current month
        print("Triggering Google Drive upload...")
        # _upload_monthly_to_gdrive internally generates CSV/PDF and uploads
        await _upload_monthly_to_gdrive(conn, internal_user_id, date(2026, 4, 1))

        print("\nVerification procedure finished!")
        print("Check your terminal logs for the 'File uploaded' message and your GDrive folder.")

    except Exception as e:
        print(f"Verification failed: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # 3. Cleanup: Remove the test data
        print("\nCleaning up test data...")
        await conn.execute("DELETE FROM expenses WHERE note = 'Integration verification test'")
        await conn.commit()
        await conn.close()
        print("Cleanup complete.")


if __name__ == "__main__":
    asyncio.run(main())
