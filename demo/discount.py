PREMIUM_DISCOUNT = 0.10
PREMIUM_MINIMUM_SPEND = 50.0


def calculate_total(price: float, is_premium: bool = False) -> float:
    if is_premium and price >= PREMIUM_MINIMUM_SPEND:
        return round(price * (1 - PREMIUM_DISCOUNT), 2)

    return price
