import stripe
from app.core.config import settings

stripe.api_key = settings.STRIPE_SECRET_KEY

def create_checkout_session(customer_email: str, amount: int, currency="usd"):
    """
    Creates a Stripe Checkout session.
    """
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        customer_email=customer_email,
        line_items=[{
            "price_data": {
                "currency": currency,
                "product_data": {"name": "MyESI Subscription"},
                "unit_amount": amount,  # Amount in cents
            },
            "quantity": 1,
        }],
        success_url="http://localhost:3000/success",
        cancel_url="http://localhost:3000/cancel",
    )
    return session
