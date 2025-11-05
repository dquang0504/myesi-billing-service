from typing import Optional
import stripe
from app.core.config import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def create_checkout_session(
    customer_email: str,
    price_id: str,
    quantity: int = 1,
    idempotency_key: Optional[str] = None,
    mode: str = "subscription",
):
    """
    Creates a Stripe Checkout session for a subscription plan.
    Uses Stripe Price ID instead of manual amount.
    """

    # Basic request options
    opts = {}
    if idempotency_key:
        opts["idempotency_key"] = idempotency_key

    session = stripe.checkout.Session.create(
        customer_email=customer_email,
        payment_method_types=["card"],
        mode=mode,  # 'subscription' by default
        line_items=[{"price": price_id, "quantity": quantity}],
        allow_promotion_codes=True,
        success_url="https://localhost:3000/admin/subscription/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url="https://localhost:3000/admin/subscription/cancel",
        **opts,
    )

    # Return minimal safe info
    return {
        "id": session.id,
        "url": session.url,
        "mode": session.mode,
        "currency": session.currency,
    }
