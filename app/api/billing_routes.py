from fastapi import APIRouter, Request, HTTPException
from app.utils.stripe_client import create_checkout_session
from app.core.config import settings
import stripe

router = APIRouter(prefix="/api/billing", tags=["Billing"])

@router.post("/checkout")
async def create_checkout(request: Request):
    body = await request.json()
    email = body.get("email")
    amount = body.get("amount")

    if not email or not amount:
        raise HTTPException(status_code=400, detail="Email and amount required")

    try:
        session = create_checkout_session(email, int(amount))
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        print(f"✅ Payment received for: {session['customer_email']}")

    return {"status": "success"}
