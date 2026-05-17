from spendly.web.db import get_history_data, get_sankey_data, get_user_id


def main():
    print("Verifying Chart Data Aggregation...")

    uid = get_user_id()
    if not uid:
        print("FAIL: User not found in DB.")
        return

    print(f"Internal User ID: {uid}")

    history = get_history_data(uid, days=30)
    print(f"History Data Points: {len(history)}")
    if history:
        print(f"Sample: {history[0]}")

    sankey = get_sankey_data(uid)
    print(f"Sankey Links: {len(sankey)}")
    if sankey:
        print(f"Sample: {sankey[0]}")


if __name__ == "__main__":
    main()
