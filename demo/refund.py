def refund(transaction: dict) -> float:
    # Intentional regression for the demo:
    # after a discount, this should use final_amount.
    return transaction["original_amount"]
