from discount import calculate_total
from payment import charge


def checkout(price: float, is_premium: bool = False) -> dict:
    final_amount = calculate_total(price, is_premium)
    transaction = charge(final_amount, original_amount=price)

    return transaction
