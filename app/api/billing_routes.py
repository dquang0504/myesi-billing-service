from datetime import datetime
import logging
import uuid
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import func, text, select
from app.utils.stripe_client import (
    create_new_subscription_session,
    cycle_switch_logic,
    downgrade_subscription_logic,
    upgrade_subscription_logic,
    get_plan,
)
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
from app.schemas.models import UpdateSubRequest
import httpx


router = APIRouter(prefix="/api/billing", tags=["Billing"])
logger = logging.getLogger("billing")


async def notify_payment(
    org_id: int,
    status: str,
    amount_cents: int,
    currency: str,
    plan_name: str,
    hosted_invoice_url: str = None,
):
    """
    Send payment notification to notification-service via internal event ingress.
    status: 'success' | 'failed'
    """
    if not org_id or not settings.NOTIFICATION_SERVICE_URL:
        return
    event = {
        "type": f"payment.{status}",
        "organization_id": org_id,
        "severity": "info" if status == "success" else "critical",
        "payload": {
            "amount": amount_cents,
            "currency": currency or "usd",
            "plan_name": plan_name,
            "invoice_url": hosted_invoice_url,
            "status": status,
        },
    }
    headers = {}
    if settings.NOTIFICATION_SERVICE_TOKEN:
        headers["X-Service-Token"] = settings.NOTIFICATION_SERVICE_TOKEN
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{settings.NOTIFICATION_SERVICE_URL}/api/notification/events",
                json=event,
                headers=headers,
            )
    except Exception as e:
        logger.warning(f"Failed to notify payment event: {e}")


@router.post("/create-checkout-session")
async def create_new_subscription_session_route(
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
            lambda: create_new_subscription_session(
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
            action="create_new_subscription_session",
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

    # Verify Stripe signature
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_id = event.get("id")
    event_type = event.get("type")
    data = event.get("data", {}).get("object", {})

    # Idempotency check
    existing_event = await db.execute(
        text("SELECT 1 FROM billing_events WHERE event_id=:eid"),
        {"eid": event_id},
    )
    if existing_event.scalar():
        return {"status": "already_processed"}
    db.add(BillingEvent(event_id=event_id, payload=event))
    await db.commit()

    actor_id, org_id, sub_db_id, old_plan_id, plan_id, interval, stripe_sub_id = (
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    plan_name = None

    # -------------------------
    # 0️⃣ charge.succeeded
    # -------------------------
    if event_type == "charge.succeeded":
        cust_id = data.get("customer")
        pm_id = data.get("payment_method")

        if not cust_id or not pm_id:
            logger.warning(
                "charge.succeeded missing customer/payment_method → skipping"
            )
            return {"status": "ok"}

        # ===== Lấy info payment method từ Stripe =====
        try:
            pm = stripe.PaymentMethod.retrieve(pm_id)
        except Exception as e:
            logger.error(f"Failed to fetch PaymentMethod from Stripe: {e}")
            return {"status": "ok"}

        brand = pm.card.brand if pm.card else None
        last4 = pm.card.last4 if pm.card else None
        exp_month = pm.card.exp_month if pm.card else None
        exp_year = pm.card.exp_year if pm.card else None

        # ===== Check duplicate =====
        row = await db.execute(
            text("SELECT id FROM payment_methods WHERE stripe_payment_method_id=:pmid"),
            {"pmid": pm_id},
        )
        exists = row.fetchone()

        if exists:
            logger.info(f"Payment method {pm_id} already exists → skip insert.")
            return {"status": "ok"}

        # ===== Insert =====
        await db.execute(
            text(
                """
                INSERT INTO payment_methods
                    (stripe_customer_id, stripe_payment_method_id, brand, last4, exp_month, exp_year, is_default)
                VALUES
                    (:cid, :pmid, :brand, :last4, :exp_month, :exp_year, TRUE)
            """
            ),
            {
                "cid": cust_id,
                "pmid": pm_id,
                "brand": brand,
                "last4": last4,
                "exp_month": exp_month,
                "exp_year": exp_year,
            },
        )

        await db.commit()

    # -------------------------
    # 1️⃣ payment_method.attached
    # -------------------------
    elif event_type == "payment_method.attached":
        logger.info("Nothing to process here at payment_method.attached...")
        pass

    # -------------------------
    # 2 checkout.session.completed → tạo subscription và payment method
    # -------------------------
    elif event_type == "checkout.session.completed":
        cs_id = data.get("id")
        # Lấy actor_id từ checkout record
        row = await db.execute(
            text("SELECT actor_id FROM checkout_records WHERE session_id=:sid"),
            {"sid": cs_id},
        )
        rec = row.fetchone()
        if rec:
            actor_id = rec[0]

        stripe_sub_id = data.get("subscription")
        if stripe_sub_id and actor_id:
            # Insert subscription nếu chưa tồn tại
            row = await db.execute(
                text("SELECT id FROM subscriptions WHERE stripe_subscription_id=:sid"),
                {"sid": stripe_sub_id},
            )
            if not row.fetchone():
                await db.execute(
                    text(
                        """
                        INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id,
                                                   status, created_at, updated_at)
                        VALUES (:uid, :cust, :sid, 'active', NOW(), NOW())
                        ON CONFLICT (stripe_subscription_id) DO NOTHING
                    """
                    ),
                    {
                        "uid": actor_id,
                        "cust": data.get("customer"),
                        "sid": stripe_sub_id,
                    },
                )
                await db.commit()

            # Lấy sub_db_id mới tạo
            sub_db_id = (
                await db.execute(
                    text(
                        "SELECT id FROM subscriptions WHERE stripe_subscription_id=:sid"
                    ),
                    {"sid": stripe_sub_id},
                )
            ).fetchone()[0]

    # -------------------------
    # 4️⃣ customer.subscription.created / updated / deleted
    # -------------------------
    elif event_type.startswith("customer.subscription."):
        stripe_sub_id = data.get("id")
        row = await db.execute(
            text(
                "SELECT user_id, id, plan_id FROM subscriptions WHERE stripe_subscription_id=:sid"
            ),
            {"sid": stripe_sub_id},
        )
        rec = row.fetchone()
        if rec:
            actor_id, sub_db_id, old_plan_id = rec
        else:
            # create subscription if missing
            cust_id = data.get("customer")
            if cust_id:
                row2 = await db.execute(
                    text(
                        "SELECT actor_id FROM checkout_records WHERE raw_session->>'customer'=:cust ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"cust": cust_id},
                )
                rec2 = row2.fetchone()
                if rec2:
                    actor_id = rec2[0]
                    await db.execute(
                        text(
                            """
                            INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id,
                                                       status, created_at, updated_at)
                            VALUES (:uid, :cust, :sid, 'active', NOW(), NOW())
                            ON CONFLICT (stripe_subscription_id) DO NOTHING
                        """
                        ),
                        {"uid": actor_id, "cust": cust_id, "sid": stripe_sub_id},
                    )
                    await db.commit()
                    sub_db_id = (
                        await db.execute(
                            text(
                                "SELECT id FROM subscriptions WHERE stripe_subscription_id=:sid"
                            ),
                            {"sid": stripe_sub_id},
                        )
                    ).fetchone()[0]

    elif event_type == "customer.subscription.updated":
        stripe_sub_id = data["id"]

        # 1) Lookup subscription (kept for later blocks)
        row = await db.execute(
            text("SELECT id FROM subscriptions WHERE stripe_subscription_id=:sid"),
            {"sid": stripe_sub_id},
        )
        rec = row.fetchone()
        if rec:
            sub_db_id = rec[0]
        else:
            sub_db_id = (
                None  # do NOT return → allow invoice handler below to still work
            )

        # 2) Check for scheduled downgrade
        if sub_db_id:
            row = await db.execute(
                text(
                    """
                    SELECT id, target_price_id
                    FROM scheduled_downgrades
                    WHERE subscription_id=:sid
                    ORDER BY created_at DESC LIMIT 1
                """
                ),
                {"sid": sub_db_id},
            )
            pending = row.fetchone()

            if pending:
                downgrade_id = pending.id
                target_price = pending.target_price_id

                # 3) Apply only when cycle renewed
                if data.get("status") == "active" and not data.get(
                    "cancel_at_period_end"
                ):
                    try:
                        item_id = data["items"]["data"][0]["id"]

                        stripe.Subscription.modify(
                            stripe_sub_id,
                            cancel_at_period_end=False,
                            items=[{"id": item_id, "price": target_price}],
                            proration_behavior="none",
                        )

                        await db.execute(
                            text("DELETE FROM scheduled_downgrades WHERE id=:id"),
                            {"id": downgrade_id},
                        )
                        await db.commit()

                    except Exception as e:
                        logger.error(f"Failed applying scheduled downgrade: {str(e)}")

    # -------------------------
    # 5️⃣ payment_intent.succeeded
    # -------------------------
    elif event_type.startswith("payment_intent.succeeded"):
        logger.info("Nothing to process here at payment_method.attached...")
        pass

    # -------------------------
    # 6️⃣ invoice.* events (idempotent, no duplicate insert)
    # -------------------------
    elif event_type.startswith("invoice."):
        invoice_id = data.get("id")
        stripe_sub_id = data.get("subscription")

        actor_id = None
        sub_db_id = None
        customer_id = data.get("customer")

        # Lookup subscription → get actor_id + subscription DB ID
        if stripe_sub_id:
            row = await db.execute(
                text(
                    "SELECT user_id, id FROM subscriptions WHERE stripe_subscription_id=:sid"
                ),
                {"sid": stripe_sub_id},
            )
            rec = row.fetchone()
            if rec:
                actor_id, sub_db_id = rec

        # If actor_id missing, try to recover from checkout record for the customer
        if not actor_id and customer_id:
            row = await db.execute(
                text(
                    "SELECT actor_id FROM checkout_records WHERE raw_session->>'customer'=:cust ORDER BY created_at DESC LIMIT 1"
                ),
                {"cust": customer_id},
            )
            rec = row.fetchone()
            if rec:
                actor_id = rec[0]

        # Ensure subscription exists when invoice precedes checkout or customer.subscription events
        if stripe_sub_id and actor_id and not sub_db_id:
            await db.execute(
                text(
                    """
                    INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id,
                                               status, created_at, updated_at)
                    VALUES (:uid, :cust, :sid, 'active', NOW(), NOW())
                    ON CONFLICT (stripe_subscription_id) DO NOTHING
                """
                ),
                {"uid": actor_id, "cust": customer_id, "sid": stripe_sub_id},
            )
            await db.commit()
            row = await db.execute(
                text("SELECT id FROM subscriptions WHERE stripe_subscription_id=:sid"),
                {"sid": stripe_sub_id},
            )
            rec = row.fetchone()
            if rec:
                sub_db_id = rec[0]
                logger.info(
                    "Auto-created subscription record during invoice webhook",
                    extra={
                        "stripe_subscription_id": stripe_sub_id,
                        "invoice_id": invoice_id,
                        "actor_id": actor_id,
                    },
                )

        # If subscription found → upsert invoice
        if actor_id and sub_db_id:
            await db.execute(
                text(
                    """
                    INSERT INTO invoices (
                        user_id, subscription_id, stripe_invoice_id,
                        amount_due_cents, amount_paid_cents, currency,
                        invoice_pdf_url, hosted_invoice_url,
                        status, period_start, period_end, created_at
                    ) VALUES (
                        :uid, :subid, :iid,
                        :due, :paid, :currency,
                        :pdf, :hosted,
                        :status, :pstart, :pend, NOW()
                    )
                    ON CONFLICT (stripe_invoice_id) DO UPDATE
                    SET
                        status = EXCLUDED.status,
                        amount_paid_cents = EXCLUDED.amount_paid_cents,
                        amount_due_cents = EXCLUDED.amount_due_cents,
                        currency = EXCLUDED.currency,
                        invoice_pdf_url = EXCLUDED.invoice_pdf_url,
                        hosted_invoice_url = EXCLUDED.hosted_invoice_url,
                        period_start = EXCLUDED.period_start,
                        period_end = EXCLUDED.period_end
                """
                ),
                {
                    "uid": actor_id,
                    "subid": sub_db_id,
                    "iid": invoice_id,
                    "due": data.get("amount_due", 0),
                    "paid": data.get("amount_paid", 0),
                    "currency": data.get("currency", "usd"),
                    "pdf": data.get("invoice_pdf"),
                    "hosted": data.get("hosted_invoice_url"),
                    "status": data.get("status"),
                    "pstart": (
                        datetime.fromtimestamp(data.get("period_start"))
                        if data.get("period_start")
                        else None
                    ),
                    "pend": (
                        datetime.fromtimestamp(data.get("period_end"))
                        if data.get("period_end")
                        else None
                    ),
                },
            )
        else:
            logger.warning(
                f"Skipping invoice {invoice_id} — subscription {stripe_sub_id} not linked yet"
            )

        # Always commit once (safe)
        await db.commit()

        # Plan name for notification (best-effort)
        plan_name = None
        if sub_db_id:
            row = await db.execute(
                text(
                    """
                    SELECT sp.name
                    FROM subscriptions s
                    JOIN subscription_plans sp ON sp.id = s.plan_id
                    WHERE s.id = :sid
                    """
                ),
                {"sid": sub_db_id},
            )
            plan_name = row.scalar()

    # -------------------------
    # Attach PaymentAudit
    # -------------------------
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="WEBHOOK_RECEIVED",
            session_id=data.get("id"),
            details={"event_type": event_type, "event_id": event_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()

    # -------------------------
    # Lookup organization
    # -------------------------
    if actor_id:
        r = await db.execute(
            text("SELECT organization_id FROM users WHERE id=:uid"),
            {"uid": actor_id},
        )
        row = r.fetchone()
        if row:
            org_id = row[0]

    # -------------------------
    # Payment notifications (org-wide)
    # -------------------------
    payment_status = None
    if event_type in {"invoice.payment_succeeded", "invoice.paid"}:
        payment_status = "success"
    elif event_type in {"invoice.payment_failed", "invoice.payment_action_required"}:
        payment_status = "failed"

    if payment_status and org_id:
        amount_cents = data.get("amount_paid") or data.get("amount_due") or 0
        await notify_payment(
            org_id=org_id,
            status=payment_status,
            amount_cents=amount_cents,
            currency=data.get("currency", "usd"),
            plan_name=plan_name,
            hosted_invoice_url=data.get("hosted_invoice_url"),
        )

    # -------------------------
    # Subscription update, plan upgrade, reset usage
    # -------------------------
    if (
        event_type
        in {
            "checkout.session.completed",
            "invoice.paid",
            "invoice.finalized",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }
        and sub_db_id
    ):
        try:
            sub_obj = stripe.Subscription.retrieve(
                stripe_sub_id, expand=["items.data.price"]
            )
            sub_item = sub_obj["items"]["data"][0]
            period_start = datetime.fromtimestamp(sub_item.get("current_period_start"))
            period_end = datetime.fromtimestamp(sub_item.get("current_period_end"))
            price_id = sub_item["price"]["id"]

            # Determine plan_id and interval
            result = await db.execute(
                text(
                    """
                    SELECT id, stripe_price_id_monthly, stripe_price_id_yearly
                    FROM subscription_plans
                    WHERE stripe_price_id_monthly=:pid OR stripe_price_id_yearly=:pid
                """
                ),
                {"pid": price_id},
            )
            row = result.fetchone()
            if row:
                plan_id = row.id
                interval = (
                    "monthly" if price_id == row.stripe_price_id_monthly else "yearly"
                )

            # Update subscription in DB
            await db.execute(
                text(
                    """
                    UPDATE subscriptions
                    SET plan_id=:pid, status=:status, interval=:interval,
                        current_period_start=:cps, current_period_end=:cpe,
                        cancel_at_period_end=:cape, trial_end=:te, updated_at=NOW()
                    WHERE stripe_subscription_id=:sid
                """
                ),
                {
                    "sid": stripe_sub_id,
                    "pid": plan_id,
                    "status": sub_obj["status"],
                    "interval": interval,
                    "cps": period_start,
                    "cpe": period_end,
                    "cape": sub_obj.get("cancel_at_period_end", False),
                    "te": (
                        datetime.fromtimestamp(sub_obj["trial_end"])
                        if sub_obj.get("trial_end")
                        else None
                    ),
                },
            )
            await db.commit()

            # ------------------------------------------------------------------
            # ⭐ NEW LOGIC: Deactivate older subscriptions
            # ------------------------------------------------------------------
            if actor_id:
                await db.execute(
                    text(
                        """
                        UPDATE subscriptions
                        SET status = 'inactive'
                        WHERE user_id = :uid
                        AND stripe_subscription_id != :current_sid
                    """
                    ),
                    {"uid": actor_id, "current_sid": stripe_sub_id},
                )
                await db.commit()

            # Update organization
            if org_id and sub_db_id:
                await db.execute(
                    text(
                        """
                        UPDATE organizations
                        SET subscription_id = :sid
                        WHERE id = :org_id
                    """
                    ),
                    {"sid": sub_db_id, "org_id": org_id},
                )
                await db.commit()

            # Reset usage on upgrade
            if org_id and plan_id and (old_plan_id is None or old_plan_id != plan_id):
                old_limits = await db.execute(
                    text(
                        "SELECT sbom_limit, project_scan_limit FROM subscription_plans WHERE id=:pid"
                    ),
                    {"pid": old_plan_id},
                )
                old_row = old_limits.fetchone()
                old_sbom, old_scan = (
                    (old_row[0], old_row[1]) if old_row else (None, None)
                )

                new_limits = await db.execute(
                    text(
                        "SELECT sbom_limit, project_scan_limit FROM subscription_plans WHERE id=:pid"
                    ),
                    {"pid": plan_id},
                )
                new_row = new_limits.fetchone()
                new_sbom, new_scan = new_row[0], new_row[1]

                if old_sbom is None or new_sbom > old_sbom:
                    await db.execute(
                        text(
                            """
                            UPDATE usage_counters
                            SET used=0, period_start=date_trunc('day', NOW()),
                                period_end=date_trunc('day', NOW()) + INTERVAL '1 day'
                            WHERE organization_id=:org_id AND usage_key='sbom_upload'
                        """
                        ),
                        {"org_id": org_id},
                    )

                if old_scan is None or new_scan > old_scan:
                    await db.execute(
                        text(
                            """
                            UPDATE usage_counters
                            SET used=0, period_start=date_trunc('day', NOW()),
                                period_end=date_trunc('day', NOW()) + INTERVAL '1 day'
                            WHERE organization_id=:org_id AND usage_key='project_scan'
                        """
                        ),
                        {"org_id": org_id},
                    )
                await db.commit()

        except Exception as e:
            logger.error(f"Error updating subscription: {str(e)}")

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

    user_id = int(user_id)

    # Lấy subscription mới nhất của user
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

    # Lấy invoice mới nhất
    latest_invoice = subscription.invoices[-1] if subscription.invoices else None

    # Lấy default payment method dựa vào stripe_customer_id
    stripe_customer_id = subscription.stripe_customer_id

    pm_result = await db.execute(
        select(PaymentMethod)
        .where(PaymentMethod.stripe_customer_id == stripe_customer_id)
        .order_by(PaymentMethod.created_at.desc())
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
        # Payment method info
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
                    sbom_limit, user_limit, project_scan_limit, currency, is_active,
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
                "project_scan_limit": row.project_scan_limit,
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


# ============================================================
# GET CURRENT SUBSCRIPTION  (MERGED)
# ============================================================
@router.get("/subscription")
async def get_current_subscription(
    request: Request, db: AsyncSession = Depends(get_db)
):
    """
    Return the active subscription for the user's organization.
    """
    try:
        # ---- Extract X-User-ID ----
        user_id = request.headers.get("X-User-ID")
        if not user_id:
            raise HTTPException(status_code=400, detail="Missing X-User-ID header")

        # ---- Query user ----
        user_query = await db.execute(
            text(
                """
                SELECT id, organization_id
                FROM users
                WHERE id = :uid
                """
            ),
            {"uid": int(user_id)},
        )
        user = user_query.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # ---- Query organization ----
        org_query = await db.execute(
            text(
                """
                SELECT id, subscription_id
                FROM organizations
                WHERE id = :oid
                """
            ),
            {"oid": user.organization_id},
        )
        org = org_query.fetchone()

        if not org or not org.subscription_id:
            raise HTTPException(
                status_code=404, detail="Organization has no subscription"
            )

        # ---- Query subscription with plan info ----
        sub_query = await db.execute(
            text(
                """
                SELECT s.id, s.status, s.current_period_end, s.plan_id,
                       sp.name, sp.description, sp.sbom_limit,
                       sp.user_limit, sp.project_scan_limit, sp.monthly_price_cents,
                       sp.annual_price_cents, sp.currency, s.user_id, s.stripe_subscription_id,
                       s.stripe_customer_id, s.interval
                FROM subscriptions s
                JOIN subscription_plans sp ON sp.id = s.plan_id
                WHERE s.id = :sid
                """
            ),
            {"sid": org.subscription_id},
        )

        subscription = sub_query.fetchone()

        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")

        # ---- Response ----
        return {
            "id": str(subscription.id),
            "status": subscription.status,
            "currentPeriodEnd": subscription.current_period_end,
            "plan": {
                "id": subscription.plan_id,
                "name": subscription.name,
                "description": subscription.description,
                "sbom_limit": subscription.sbom_limit,
                "user_limit": subscription.user_limit,
                "project_scan_limit": subscription.project_scan_limit,
                "monthly_price_cents": subscription.monthly_price_cents,
                "annual_price_cents": subscription.annual_price_cents,
                "currency": subscription.currency,
            },
            "stripe_customer_id": subscription.stripe_customer_id,
            "stripeSubscriptionId": subscription.stripe_subscription_id,
            "interval": subscription.interval,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Billing service error fetching subscription: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/subscription")
async def update_subscription(
    req: UpdateSubRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    """
    Upgrade / downgrade / cycle switch.
    Free users or invalid Stripe subscriptions must re-start checkout.
    This version calls the REAL endpoint /create-checkout-session (not the helper),
    so DB records, audit, and webhook actor_id work correctly.
    """

    # Load target plan
    target_plan = await get_plan(db, req.targetPlanId)

    # Helper: call the REAL endpoint internally
    async def call_internal_checkout():
        payload = {
            "planId": req.targetPlanId,
            "interval": req.interval,
            "user": {
                "id": req.customerId if hasattr(req, "customerId") else None,
                "email": req.customerEmail,
            },
        }
        # Important: reuse same request object for extracting IP, UA
        return await create_new_subscription_session_route(payload, request, db)

    # 1️⃣ If current plan is free → ALWAYS checkout
    if req.planId == 1:
        return await call_internal_checkout()

    # 2️⃣ Must have stripeSubscriptionId for paid-plan upgrades
    if not req.stripeSubscriptionId:
        return await call_internal_checkout()

    # 3️⃣ Load Stripe subscription
    try:
        current = stripe.Subscription.retrieve(
            req.stripeSubscriptionId, expand=["items.data"]
        )
    except Exception:
        # Stripe does not know it → restart checkout
        return await call_internal_checkout()

    # 4️⃣ Determine new price
    new_price_id = (
        target_plan.stripe_price_id_monthly
        if req.interval == "monthly"
        else target_plan.stripe_price_id_yearly
    )

    # 5️⃣ Switch action
    if req.action == "upgrade":
        return await upgrade_subscription_logic(db, current, new_price_id)

    if req.action == "downgrade":
        return await downgrade_subscription_logic(db, current, new_price_id)

    if req.action == "cycle":
        return await cycle_switch_logic(db, current, new_price_id)

    raise HTTPException(400, "Invalid subscription action")


@router.get("/payment-method")
async def get_payment_method(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Return user's default payment method.
    Now resolved by going through subscription -> stripe_customer_id.
    """
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_id = int(user_id)
    # 1) Find subscription of user to get stripe_customer_id
    sub_res = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(Subscription.status == "active")
        .order_by(Subscription.current_period_end.desc())
        .limit(1)
    )
    subscription = sub_res.scalars().first()

    if not subscription or not subscription.stripe_customer_id:
        return {"payment_method": None}

    cust_id = subscription.stripe_customer_id

    # 2) Fetch default payment method for that customer
    pm_res = await db.execute(
        select(PaymentMethod)
        .where(PaymentMethod.stripe_customer_id == cust_id)
        .order_by(PaymentMethod.created_at.desc())
    )
    pm = pm_res.scalars().first()

    if not pm:
        return {"payment_method": None}

    return {
        "id": pm.id,
        "brand": pm.brand,
        "last4": pm.last4,
        "exp_month": pm.exp_month,
        "exp_year": pm.exp_year,
        "is_default": pm.is_default,
    }


@router.get("/invoices")
async def get_invoices(
    request: Request, db: AsyncSession = Depends(get_db), page: int = 1, limit: int = 6
):
    """
    Paginated invoice history for authenticated user.
    """
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_id = int(user_id)

    # --- Count total invoices ---
    total_result = await db.execute(
        select(func.count()).select_from(Invoice).where(Invoice.user_id == user_id)
    )
    total = total_result.scalar() or 0
    # --- Pagination ---
    offset = (page - 1) * limit
    query = (
        select(Invoice)
        .where(Invoice.user_id == user_id)
        .order_by(Invoice.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(query)
    invoices = result.scalars().all()

    # --- Build response items ---
    items = [
        {
            "id": inv.stripe_invoice_id,
            "amount_due_cents": inv.amount_due_cents,
            "amount_paid_cents": inv.amount_paid_cents,
            "currency": inv.currency,
            "status": inv.status,
            "invoice_pdf_url": inv.invoice_pdf_url,
            "hosted_invoice_url": inv.hosted_invoice_url,
            "created": inv.created_at.isoformat() if inv.created_at else None,
            "period_start": inv.period_start.isoformat() if inv.period_start else None,
            "period_end": inv.period_end.isoformat() if inv.period_end else None,
        }
        for inv in invoices
    ]

    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": (total + limit - 1) // limit,
    }
