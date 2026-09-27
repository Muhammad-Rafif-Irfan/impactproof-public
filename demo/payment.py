from invoice import create_invoice


def charge(final_amount: float, original_amount: float) -> dict:
    invoice = create_invoice(
        original_amount=original_amount,
        final_amount=final_amount,
    )

    return {
        "original_amount": original_amount,
        "final_amount": final_amount,
        "invoice": invoice,
    }
