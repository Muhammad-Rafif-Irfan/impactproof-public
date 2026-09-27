from checkout import checkout
from refund import refund


def main():
    price = 100
    tx = checkout(price, is_premium=True)
    expected_refund = tx["final_amount"]
    actual_refund = refund(tx)
    print(f"Expected refund: ${expected_refund:.2f}")
    print(f"Actual refund:   ${actual_refund:.2f}")
    if actual_refund != expected_refund:
        print("REGRESSION FOUND")
        raise SystemExit(1)
    print("No regression found.")


if __name__ == "__main__":
    main()
