from datetime import datetime
import stripe


def extract_subscription_period(stripe_sub_id: str):
    """
    Retrieve current_period_start and current_period_end from the first subscription item.
    Compatible with Stripe API >= 2025-03-31.
    """
    if not stripe_sub_id:
        return None, None

    sub_obj = stripe.Subscription.retrieve(stripe_sub_id, expand=["items.data.price"])
    sub_item = (
        sub_obj["items"]["data"][0] if sub_obj.get("items", {}).get("data") else None
    )
    if not sub_item:
        return None, None

    start_ts = sub_item.get("current_period_start")
    end_ts = sub_item.get("current_period_end")
    return (
        datetime.fromtimestamp(start_ts) if start_ts else None,
        datetime.fromtimestamp(end_ts) if end_ts else None,
    )


def extract_card_info(payment_intent_id: str):
    """
    Retrieve card brand and last4 from PaymentIntent → Charge → payment_method_details.
    """
    if not payment_intent_id:
        return None, None

    pi = stripe.PaymentIntent.retrieve(payment_intent_id)
    if not pi.charges.data:
        return None, None

    charge = pi.charges.data[0]
    card_details = getattr(charge.payment_method_details, "card", None)
    if not card_details:
        return None, None

    return card_details.brand, card_details.last4


def extract_invoice_data(invoice_data: dict):
    """
    Extract all key invoice + subscription info for DB storage.
    Includes amount, currency, pdf URL, period, and card info.
    """
    stripe_sub_id = invoice_data.get("subscription")
    period_start, period_end = extract_subscription_period(stripe_sub_id)
    card_brand, last4 = extract_card_info(invoice_data.get("payment_intent"))

    return {
        "stripe_subscription_id": stripe_sub_id,
        "period_start": period_start,
        "period_end": period_end,
        "amount_due_cents": int(invoice_data.get("amount_due", 0)),
        "amount_paid_cents": int(invoice_data.get("amount_paid", 0)),
        "currency": invoice_data.get("currency", "usd"),
        "invoice_pdf_url": invoice_data.get("invoice_pdf"),
        "hosted_invoice_url": invoice_data.get("hosted_invoice_url"),
        "status": invoice_data.get("status"),
        "card_brand": card_brand,
        "last4": last4,
    }
