from datetime import datetime
import logging
import uuid
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import text, select
from app.utils.stripe_client import create_checkout_session
from app.core.config import settings
from app.db.session import get_db
from app.db.models import (
    CheckoutRecord,
    PaymentAudit,
    BillingEvent,
    PaymentMethod,
    Invoice,
)
import stripe
from app.db.models import Subscription
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio
from app.utils.extract_client_info import extract_client_info
from app.db.models import SubscriptionPlan
from sqlalchemy.orm import selectinload


router = APIRouter(prefix="/api/billing", tags=["Billing"])
logger = logging.getLogger("billing")


@router.post("/create-checkout-session")
async def create_checkout_session_route(
    payload: dict, request: Request, db: AsyncSession = Depends(get_db)
):
    """
    Create a Stripe Checkout Session for a given plan.
    Frontend sends: { "planId": 1, "interval": "monthly" | "yearly" }
    """
    plan_id = payload.get("planId")
    interval = payload.get("interval", "monthly").lower()

    if not plan_id:
        raise HTTPException(status_code=400, detail="Missing planId")
    if interval not in ["monthly", "yearly"]:
        raise HTTPException(status_code=400, detail="Invalid interval value")

    # Get user from middleware or fallback mock
    user_data = payload.get("user", {})
    print("This is user_data: ", user_data)
    actor_id = user_data.get("id")
    actor_email = user_data.get("email")

    # 1. Fetch plan info
    result = await db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.is_active:
        raise HTTPException(status_code=400, detail="Plan is not active")

    # 2. Choose Stripe price id depending on interval
    price_id = (
        plan.stripe_price_id_monthly
        if interval == "monthly"
        else plan.stripe_price_id_yearly
    )
    amount_cents = (
        plan.monthly_price_cents if interval == "monthly" else plan.annual_price_cents
    )

    # 3. Create Stripe session
    idempotency_key = str(uuid.uuid4())
    client_ip, user_agent = extract_client_info(request)

    try:
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(
            None,
            lambda: create_checkout_session(
                customer_email=actor_email,
                price_id=price_id,
                idempotency_key=idempotency_key,
            ),
        )

        # 4. Save record & audit
        record = CheckoutRecord(
            actor_id=actor_id,
            session_id=session["id"],
            customer_email=actor_email,
            amount=amount_cents,
            currency=plan.currency,
            status="created",
            idempotency_key=idempotency_key,
            raw_session=session,
        )
        db.add(record)

        audit = PaymentAudit(
            actor_id=actor_id,
            action="CREATE_CHECKOUT_SESSION",
            session_id=session["id"],
            details={"plan": plan.name, "interval": interval, "amount": amount_cents},
            ip_address=client_ip,
            user_agent=user_agent,
        )
        db.add(audit)
        await db.commit()

        logger.info(
            "Stripe checkout created",
            extra={
                "session_id": session["id"],
                "plan": plan.name,
                "interval": interval,
            },
        )

        return {"success": True, "url": session["url"], "session_id": session["id"]}

    except Exception as e:
        logger.exception("Checkout creation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing signature header")

    client_ip, user_agent = extract_client_info(request)

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_id = event.get("id")

    # Idempotency check
    existing = await db.execute(
        text("SELECT 1 FROM billing_events WHERE event_id = :eid"), {"eid": event_id}
    )
    if existing.scalar() is not None:
        return {"status": "already_processed"}

    # Persist raw event
    db.add(BillingEvent(event_id=event_id, payload=event))
    await db.commit()

    event_type = event.get("type")
    data = event.get("data", {}).get("object", {})
    session_id = data.get("id")
    actor_id = None

    # Lookup actor_id via checkout_records if session_id exists
    if session_id:
        res = await db.execute(
            text("SELECT actor_id FROM checkout_records WHERE session_id = :sid"),
            {"sid": session_id},
        )
        row = res.fetchone()
        if row:
            actor_id = row[0]

    # Persist audit
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="WEBHOOK_RECEIVED",
            session_id=session_id,
            details={"event_type": event_type, "event_id": event_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()

    # -----------------------------
    # Handle subscription-related events
    # -----------------------------
    subscription_events = [
        "checkout.session.completed",
        "invoice.paid",
        "invoice.finalized",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ]

    if event_type in subscription_events:
        stripe_sub_id = data.get("subscription") or data.get("id")
        if stripe_sub_id:
            sub_obj = stripe.Subscription.retrieve(
                stripe_sub_id, expand=["items.data.price"]
            )
            sub_item = (
                sub_obj["items"]["data"][0]
                if sub_obj.get("items", {}).get("data")
                else None
            )

            # Get subscription period from subscription item
            period_start = (
                datetime.fromtimestamp(sub_item.get("current_period_start"))
                if sub_item and sub_item.get("current_period_start")
                else None
            )
            period_end = (
                datetime.fromtimestamp(sub_item.get("current_period_end"))
                if sub_item and sub_item.get("current_period_end")
                else None
            )

            # Get plan_id from price_id
            price_id = sub_item["price"]["id"] if sub_item else None
            plan_res = await db.execute(
                text(
                    """
                    SELECT id, name, stripe_price_id_monthly, stripe_price_id_yearly
                    FROM subscription_plans
                    WHERE stripe_price_id_monthly = :pid OR stripe_price_id_yearly = :pid
                """
                ),
                {"pid": price_id},
            )
            plan_row = plan_res.fetchone()
            plan_id = plan_row.id if plan_row else None
            interval = (
                "monthly"
                if plan_row and price_id == plan_row.stripe_price_id_monthly
                else "yearly"
            )

            # Update or insert subscription
            existing_sub = await db.execute(
                text(
                    "SELECT id FROM subscriptions WHERE stripe_subscription_id = :sid"
                ),
                {"sid": stripe_sub_id},
            )
            row_sub = existing_sub.fetchone()
            if row_sub:
                await db.execute(
                    text(
                        """
                        UPDATE subscriptions
                        SET current_period_start = :cps,
                            current_period_end = :cpe,
                            trial_end = :te,
                            status = :status,
                            cancel_at_period_end = :cape,
                            plan_id = :pid,
                            interval = :interval
                        WHERE stripe_subscription_id = :sid
                    """
                    ),
                    {
                        "cps": period_start,
                        "cpe": period_end,
                        "te": (
                            datetime.fromtimestamp(sub_obj["trial_end"])
                            if sub_obj.get("trial_end")
                            else None
                        ),
                        "status": sub_obj["status"],
                        "cape": sub_obj.get("cancel_at_period_end", False),
                        "sid": stripe_sub_id,
                        "pid": plan_id,
                        "interval": interval,
                    },
                )
            else:
                db.add(
                    Subscription(
                        user_id=actor_id,
                        plan_id=plan_id,
                        stripe_customer_id=sub_obj["customer"],
                        stripe_subscription_id=sub_obj["id"],
                        status=sub_obj["status"],
                        current_period_start=period_start,
                        current_period_end=period_end,
                        cancel_at_period_end=sub_obj.get("cancel_at_period_end", False),
                        trial_end=(
                            datetime.fromtimestamp(sub_obj["trial_end"])
                            if sub_obj.get("trial_end")
                            else None
                        ),
                        interval=interval,
                    )
                )
            await db.commit()

        # -----------------------------
        # Handle invoice info (amount, pdf, currency)
        # -----------------------------
        if "invoice" in event_type or event_type.startswith("invoice."):
            inv_id = data.get("id")
            amount_paid_cents = int(data.get("amount_paid", 0))
            currency = data.get("currency", "usd")
            invoice_pdf_url = data.get("invoice_pdf")
            hosted_invoice_url = data.get("hosted_invoice_url")
            period_start_inv = (
                datetime.fromtimestamp(data.get("period_start"))
                if data.get("period_start")
                else period_start
            )
            period_end_inv = (
                datetime.fromtimestamp(data.get("period_end"))
                if data.get("period_end")
                else period_end
            )

            existing_inv = await db.execute(
                text("SELECT id FROM invoices WHERE stripe_invoice_id = :iid"),
                {"iid": inv_id},
            )
            if existing_inv.scalar() is None:
                sub_id_in_db = None
                if stripe_sub_id:
                    res_sub = await db.execute(
                        text(
                            "SELECT id FROM subscriptions WHERE stripe_subscription_id = :sid"
                        ),
                        {"sid": stripe_sub_id},
                    )
                    row_sub = res_sub.fetchone()
                    if row_sub:
                        sub_id_in_db = row_sub[0]

                db.add(
                    Invoice(
                        user_id=actor_id,
                        stripe_invoice_id=inv_id,
                        subscription_id=sub_id_in_db,
                        amount_due_cents=int(data.get("amount_due", 0)),
                        amount_paid_cents=amount_paid_cents,
                        currency=currency,
                        invoice_pdf_url=invoice_pdf_url,
                        hosted_invoice_url=hosted_invoice_url,
                        status=data.get("status"),
                        period_start=period_start_inv,
                        period_end=period_end_inv,
                    )
                )
                await db.commit()

    # -----------------------------
    # Handle payment method attached
    # -----------------------------
    elif event_type == "payment_method.attached":
        pm = data
        existing_pm = await db.execute(
            text(
                "SELECT id FROM payment_methods WHERE stripe_payment_method_id = :pid"
            ),
            {"pid": pm["id"]},
        )
        if existing_pm.scalar() is None:
            db.add(
                PaymentMethod(
                    user_id=actor_id,
                    stripe_payment_method_id=pm["id"],
                    brand=pm["card"]["brand"],
                    last4=pm["card"]["last4"],
                    exp_month=pm["card"]["exp_month"],
                    exp_year=pm["card"]["exp_year"],
                    is_default=True,
                )
            )
            await db.commit()

    logger.info(f"Processed Stripe event: {event_type}")
    return {"status": "success"}


@router.get("/latest-subscription")
async def get_latest_subscription(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Return the user's latest subscription with plan, invoice, and payment method.
    Uses ORM relationships instead of raw SQL.
    """
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Convert user_id sang int
    user_id = int(user_id)

    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.invoices),
        )
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalars().first()
    if not subscription:
        raise HTTPException(status_code=404, detail="No active subscription found")

    # Find latest invoice
    latest_invoice = subscription.invoices[-1] if subscription.invoices else None

    # Find default payment method
    pm_result = await db.execute(
        select(PaymentMethod).where(
            PaymentMethod.user_id == user_id, PaymentMethod.is_default
        )
    )
    payment_method = pm_result.scalars().first()

    return {
        "plan_name": subscription.plan.name if subscription.plan else None,
        "interval": subscription.interval,
        "status": subscription.status,
        "amount_paid_cents": (
            latest_invoice.amount_paid_cents if latest_invoice else None
        ),
        "currency": latest_invoice.currency if latest_invoice else "usd",
        "invoice_pdf_url": latest_invoice.invoice_pdf_url if latest_invoice else None,
        "period_end": subscription.current_period_end,
        "card_brand": payment_method.brand if payment_method else None,
        "last4": payment_method.last4 if payment_method else None,
    }


# ----- GET SUBSCRIPTION PLANS -----
@router.get("/plans")
async def get_subscription_plans(db: AsyncSession = Depends(get_db)):
    """
    Return all active subscription plans.
    Used by frontend UI to display available plans (Basic, Pro, Enterprise, etc).
    """
    try:
        result = await db.execute(
            text(
                """
                SELECT id, name, description,
                    monthly_price_cents, annual_price_cents,
                    sbom_limit, user_limit, currency, is_active,
                    stripe_price_id_monthly, stripe_price_id_yearly, stripe_product_id
                FROM subscription_plans
                WHERE is_active = TRUE
                ORDER BY monthly_price_cents ASC
            """
            )
        )
        plans = [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "monthly_price_cents": row.monthly_price_cents,
                "annual_price_cents": row.annual_price_cents,
                "sbom_limit": row.sbom_limit,
                "user_limit": row.user_limit,
                "currency": row.currency,
                "stripe_price_id_monthly": row.stripe_price_id_monthly,
                "stripe_price_id_yearly": row.stripe_price_id_yearly,
                "stripe_product_id": row.stripe_product_id,
            }
            for row in result.fetchall()
        ]

        return {"success": True, "data": plans}
    except Exception as e:
        logger.exception("Failed to fetch subscription plans")
        raise HTTPException(status_code=500, detail=str(e))
