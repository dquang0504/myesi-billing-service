from datetime import datetime
import logging
import uuid
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import func, text, select
from app.utils.stripe_client import (
    cycle_switch_logic,
    downgrade_subscription_logic,
    upgrade_subscription_logic,
    get_plan,
)
from app.utils.payment_provider import (
    CheckoutContext,
    ProviderError,
    get_payment_provider,
    snapshot_plan,
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
from app.utils.extract_client_info import extract_client_info
from app.db.models import SubscriptionPlan
from sqlalchemy.orm import selectinload
from app.schemas.models import UpdateSubRequest, CancelSubscriptionRequest
from app.services.tax_service import calculate_tax
from app.services.billing_address_service import (
    create_address,
    get_address_by_id,
    get_default_address,
    list_active_addresses,
)
import httpx
from typing import Optional


router = APIRouter(prefix="/api/billing", tags=["Billing"])
logger = logging.getLogger("billing")
FREE_PLAN_ID = getattr(settings, "FREE_PLAN_ID", 0)


async def _resolve_org_id(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        text("SELECT organization_id FROM users WHERE id=:uid"),
        {"uid": user_id},
    )
    row = result.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="User organization not found")
    return row[0]


async def _ensure_subscription_record(
    db: AsyncSession,
    stripe_subscription_id: Optional[str],
    stripe_customer_id: Optional[str],
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Guarantee that a local subscription row exists for the provided Stripe subscription id.
    Returns tuple (actor_id, subscription_id, plan_id).
    """
    if not stripe_subscription_id:
        return None, None, None

    row = await db.execute(
        text(
            "SELECT billing_contact_user_id, id, plan_id FROM subscriptions WHERE stripe_subscription_id=:sid"
        ),
        {"sid": stripe_subscription_id},
    )
    rec = row.fetchone()
    if rec:
        return rec.billing_contact_user_id, rec.id, rec.plan_id

    if not stripe_customer_id:
        return None, None, None

    checkout_row = await db.execute(
        text(
            """
            SELECT actor_id
            FROM checkout_records
            WHERE raw_session->>'customer'=:cust
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"cust": stripe_customer_id},
    )
    checkout = checkout_row.fetchone()
    if not checkout:
        return None, None, None

    actor_id = checkout[0]
    await db.execute(
        text(
            """
            INSERT INTO subscriptions (created_by, billing_contact_user_id, stripe_customer_id,
                                       stripe_subscription_id, status, created_at, updated_at)
            VALUES (:uid, :uid, :cust, :sid, 'active', NOW(), NOW())
            ON CONFLICT (stripe_subscription_id) DO NOTHING
            """
        ),
        {"uid": actor_id, "cust": stripe_customer_id, "sid": stripe_subscription_id},
    )
    await db.commit()

    row = await db.execute(
        text(
            "SELECT billing_contact_user_id, id, plan_id FROM subscriptions WHERE stripe_subscription_id=:sid"
        ),
        {"sid": stripe_subscription_id},
    )
    rec = row.fetchone()
    if rec:
        return rec.billing_contact_user_id, rec.id, rec.plan_id
    return actor_id, None, None


async def _apply_scheduled_downgrade(
    db: AsyncSession,
    stripe_subscription_id: str,
    subscription_db_id: Optional[int],
    actor_id: Optional[int],
    subscription_payload: dict,
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    """
    Apply a pending scheduled downgrade when a new billing cycle starts.
    """
    if not subscription_db_id:
        return

    row = await db.execute(
        text(
            """
            SELECT id, target_price_id
            FROM scheduled_downgrades
            WHERE subscription_id=:sid
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"sid": subscription_db_id},
    )
    pending = row.fetchone()
    if not pending:
        return

    if subscription_payload.get("cancel_at_period_end"):
        # still waiting for current cycle to end; keep schedule
        logger.debug(
            "Scheduled downgrade pending for subscription %s but cancel_at_period_end=True; skipping until renewal",
            stripe_subscription_id,
        )
        return

    items = subscription_payload.get("items", {}).get("data") or []
    if not items:
        logger.warning(
            "Unable to apply scheduled downgrade for %s: missing items payload",
            stripe_subscription_id,
        )
        return

    target_price = pending.target_price_id
    subscription_item_id = items[0]["id"]

    try:
        stripe.Subscription.modify(
            stripe_subscription_id,
            cancel_at_period_end=False,
            proration_behavior="none",
            items=[{"id": subscription_item_id, "price": target_price}],
        )
    except Exception as exc:
        logger.error(
            "Failed to apply scheduled downgrade for %s: %s",
            stripe_subscription_id,
            exc,
        )
        return

    await db.execute(
        text("DELETE FROM scheduled_downgrades WHERE id=:id"),
        {"id": pending.id},
    )
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="scheduled_downgrade_applied",
            session_id=stripe_subscription_id,
            details={
                "subscription_id": subscription_db_id,
                "target_price_id": target_price,
            },
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()

    logger.info(
        "Scheduled downgrade applied",
        extra={
            "stripe_subscription_id": stripe_subscription_id,
            "target_price_id": target_price,
        },
    )


def _stripe_obj_to_dict(obj):
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return obj


async def _fetch_subscription_for_org(db: AsyncSession, org_id: int):
    result = await db.execute(
        text(
            """
            SELECT
                s.id AS subscription_id,
                s.billing_contact_user_id,
                s.plan_id,
                s.stripe_subscription_id,
                s.status,
                s.current_period_start,
                s.current_period_end,
                s.cancel_at_period_end,
                s.stripe_customer_id
            FROM organizations o
            JOIN subscriptions s ON s.id = o.subscription_id
            WHERE o.id = :oid
            LIMIT 1
            """
        ),
        {"oid": org_id},
    )
    return result.fetchone()


def _extract_payment_intent_id(invoice: Optional[dict]) -> Optional[str]:
    if not invoice:
        return None
    payment_intent = invoice.get("payment_intent")
    if isinstance(payment_intent, dict):
        return payment_intent.get("id")
    return payment_intent


def _calculate_prorated_amount(
    amount_paid_cents: int,
    period_start: Optional[datetime],
    period_end: Optional[datetime],
) -> int:
    if not amount_paid_cents or not period_start or not period_end:
        return 0
    total_seconds = (period_end - period_start).total_seconds()
    if total_seconds <= 0:
        return 0
    remaining_seconds = (period_end - datetime.utcnow()).total_seconds()
    if remaining_seconds <= 0:
        return 0
    prorated = int(amount_paid_cents * (remaining_seconds / total_seconds))
    return min(max(prorated, 0), amount_paid_cents)


def _get_latest_paid_invoice(stripe_subscription_id: str) -> Optional[dict]:
    invoices = []
    try:
        invoice_list = stripe.Invoice.list(
            subscription=stripe_subscription_id, limit=1, status="paid"
        )
        invoices = getattr(invoice_list, "data", None) or invoice_list.get("data", [])
    except Exception:
        try:
            invoice_list = stripe.Invoice.list(
                subscription=stripe_subscription_id, limit=1
            )
            invoices = getattr(invoice_list, "data", None) or invoice_list.get(
                "data", []
            )
        except Exception:
            invoices = []

    if not invoices:
        return None

    invoice = _stripe_obj_to_dict(invoices[0])
    if invoice.get("status") != "paid":
        return None
    return invoice


async def _cancel_subscription_cycle_end(
    db: AsyncSession,
    subscription_row,
    actor_id: int,
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> dict:
    stripe_sub_id = subscription_row.stripe_subscription_id
    if subscription_row.cancel_at_period_end:
        return {
            "success": True,
            "mode": "cycle_end",
            "cancel_at_period_end": True,
            "current_period_end": subscription_row.current_period_end,
        }

    try:
        stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=True)
    except Exception as exc:
        logger.error(
            "Failed to set cancel_at_period_end for subscription %s: %s",
            stripe_sub_id,
            exc,
        )
        raise HTTPException(
            status_code=400,
            detail="Unable to update subscription in Stripe. Please try again later.",
        )

    await db.execute(
        text(
            """
            UPDATE subscriptions
            SET cancel_at_period_end=TRUE, updated_at=NOW()
            WHERE id=:sid
            """
        ),
        {"sid": subscription_row.subscription_id},
    )
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="cancel_at_cycle_end",
            session_id=stripe_sub_id,
            details={"subscription_id": subscription_row.subscription_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()

    return {
        "success": True,
        "mode": "cycle_end",
        "cancel_at_period_end": True,
        "current_period_end": subscription_row.current_period_end,
    }


async def _cancel_subscription_immediately(
    db: AsyncSession,
    subscription_row,
    actor_id: int,
    refund_mode: Optional[str],
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> dict:
    stripe_sub_id = subscription_row.stripe_subscription_id

    existing = await db.execute(
        text(
            """
            SELECT refund_amount_cents, refund_currency, refund_mode, stripe_refund_id
            FROM cancellation_requests
            WHERE subscription_id=:sid AND mode='immediate'
            LIMIT 1
            """
        ),
        {"sid": subscription_row.subscription_id},
    )
    existing_row = existing.fetchone()
    if existing_row:
        return {
            "success": True,
            "mode": "immediate",
            "canceled": True,
            "refunded": (existing_row.refund_amount_cents or 0) > 0,
            "refund_amount_cents": existing_row.refund_amount_cents or 0,
            "currency": existing_row.refund_currency or "usd",
            "refund_mode": existing_row.refund_mode,
            "stripe_refund_id": existing_row.stripe_refund_id,
        }

    invoice_data = _get_latest_paid_invoice(stripe_sub_id)
    amount_paid = invoice_data.get("amount_paid", 0) if invoice_data else 0
    currency = invoice_data.get("currency", "usd") if invoice_data else "usd"
    invoice_id = invoice_data.get("id") if invoice_data else None
    payment_intent_id = (
        _extract_payment_intent_id(invoice_data) if invoice_data else None
    )

    normalized_mode = refund_mode or "none"
    if normalized_mode not in {"full", "prorated", "none"}:
        normalized_mode = "none"

    refund_amount = 0
    if normalized_mode == "full":
        refund_amount = amount_paid
    elif normalized_mode == "prorated":
        refund_amount = _calculate_prorated_amount(
            amount_paid,
            subscription_row.current_period_start,
            subscription_row.current_period_end,
        )

    try:
        stripe.Subscription.delete(stripe_sub_id)
    except stripe.error.InvalidRequestError as exc:
        if getattr(exc, "code", "") == "resource_missing":
            logger.info(
                "Stripe subscription %s already canceled: %s",
                stripe_sub_id,
                exc.user_message or str(exc),
            )
        else:
            logger.error(
                "Failed to cancel subscription %s immediately: %s",
                stripe_sub_id,
                exc,
            )
            raise HTTPException(
                status_code=400,
                detail="Unable to cancel subscription immediately. Please try again later.",
            )

    refund_id = None

    if refund_amount > 0 and payment_intent_id:
        try:
            refund_resp = stripe.Refund.create(
                payment_intent=payment_intent_id,
                amount=refund_amount,
                reason="requested_by_customer",
                idempotency_key=f"cancel-{subscription_row.subscription_id}-{invoice_id or 'none'}-{refund_amount}",
            )
            refund_id = refund_resp.get("id") if refund_resp else None
        except Exception as exc:
            logger.error(
                "Failed to create refund for subscription %s: %s",
                stripe_sub_id,
                exc,
            )
            raise HTTPException(
                status_code=400,
                detail="Refund failed while canceling subscription. Please try again.",
            )
    elif refund_amount > 0 and not payment_intent_id:
        logger.warning(
            "Unable to refund subscription %s due to missing payment intent",
            stripe_sub_id,
        )
        refund_amount = 0

    cancelled_at = datetime.utcnow()

    await db.execute(
        text(
            """
            UPDATE subscriptions
            SET status='canceled',
                cancel_at_period_end=FALSE,
                current_period_end=:now,
                updated_at=:now
            WHERE id=:sid
            """
        ),
        {"sid": subscription_row.subscription_id, "now": cancelled_at},
    )

    await db.execute(
        text(
            """
            INSERT INTO cancellation_requests
                (subscription_id, stripe_subscription_id, mode, refund_mode,
                 refund_amount_cents, refund_currency, stripe_refund_id,
                 stripe_invoice_id, payment_intent_id)
            VALUES
                (:sid, :stripe_sid, 'immediate', :refund_mode, :amount, :currency,
                 :refund_id, :invoice_id, :payment_intent_id)
            """
        ),
        {
            "sid": subscription_row.subscription_id,
            "stripe_sid": stripe_sub_id,
            "refund_mode": normalized_mode,
            "amount": refund_amount,
            "currency": currency,
            "refund_id": refund_id,
            "invoice_id": invoice_id,
            "payment_intent_id": payment_intent_id,
        },
    )

    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="cancel_immediately",
            session_id=stripe_sub_id,
            details={
                "refund_mode": normalized_mode,
                "refund_amount_cents": refund_amount,
                "currency": currency,
            },
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )

    if refund_id:
        db.add(
            PaymentAudit(
                actor_id=actor_id,
                action="refund_created",
                session_id=refund_id,
                details={
                    "invoice_id": invoice_id,
                    "payment_intent_id": payment_intent_id,
                    "amount_cents": refund_amount,
                    "currency": currency,
                },
                ip_address=client_ip,
                user_agent=user_agent,
            )
        )

    await db.commit()

    return {
        "success": True,
        "mode": "immediate",
        "canceled": True,
        "refunded": refund_amount > 0,
        "refund_amount_cents": refund_amount,
        "currency": currency,
        "refund_mode": normalized_mode,
        "stripe_refund_id": refund_id,
    }


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


async def _create_checkout_session_for_provider(
    provider_name: str,
    payload: dict,
    request: Request,
    db: AsyncSession,
):
    plan_id = payload.get("planId")
    interval = payload.get("interval", "monthly").lower()

    if not plan_id:
        raise HTTPException(status_code=400, detail="Missing planId")
    if interval not in ["monthly", "yearly"]:
        raise HTTPException(status_code=400, detail="Invalid interval value")

    user_data = payload.get("user", {})
    actor_id = user_data.get("id")
    actor_email = user_data.get("email")
    if not actor_email:
        raise HTTPException(status_code=400, detail="Customer email is required")
    if provider_name == "paddle" and not actor_id:
        raise HTTPException(status_code=400, detail="Missing user context for Paddle")

    result = await db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.is_active:
        raise HTTPException(status_code=400, detail="Plan is not active")

    amount_cents = (
        plan.monthly_price_cents if interval == "monthly" else plan.annual_price_cents
    )
    tax_details = calculate_tax(amount_cents)
    total_amount_cents = tax_details["total_cents"]

    idempotency_key = str(uuid.uuid4())
    client_ip, user_agent = extract_client_info(request)
    provider = get_payment_provider(provider_name)
    org_id = None
    billing_address = None

    metadata = {"plan_id": plan.id, "provider": provider_name}
    if provider_name == "paddle":
        org_id = await _resolve_org_id(db, int(actor_id))
        org_row = await db.execute(
            text("SELECT paddle_customer_id FROM organizations WHERE id=:oid"),
            {"oid": org_id},
        )
        org_rec = org_row.fetchone()
        paddle_customer_id = org_rec.paddle_customer_id if org_rec else None
        billing_address_id = payload.get("billing_address_id")
        billing_address_payload = payload.get("billing_address") or {}

        if billing_address_id:
            billing_address = await get_address_by_id(
                db, org_id, int(billing_address_id)
            )
            if not billing_address:
                raise HTTPException(status_code=404, detail="Billing address not found")
        elif billing_address_payload:
            billing_address = await create_address(
                db,
                org_id,
                label=billing_address_payload.get("label"),
                country_code=billing_address_payload.get("country_code"),
                postal_code=billing_address_payload.get("postal_code"),
                make_default=bool(billing_address_payload.get("make_default", False)),
                created_by=actor_id,
            )
            db.add(
                PaymentAudit(
                    actor_id=actor_id,
                    action="billing_address_created",
                    details={
                        "address_id": billing_address["id"],
                        "organization_id": org_id,
                        "source": "paddle_checkout",
                    },
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
            )
            await db.commit()
        else:
            billing_address = await get_default_address(db, org_id)

        if not billing_address:
            existing_addresses = await list_active_addresses(db, org_id)
            raise HTTPException(
                status_code=400,
                detail={
                    "detail": "billing_address_required",
                    "required_fields": ["country_code", "postal_code"],
                    "existing_addresses": existing_addresses,
                },
            )

        metadata.update(
            {
                "org_id": org_id,
                "billing_address_id": billing_address["id"],
                "country_code": billing_address["country_code"],
                "postal_code": billing_address["postal_code"],
                "paddle_customer_id": paddle_customer_id,
            }
        )

    ctx = CheckoutContext(
        plan=snapshot_plan(plan),
        interval=interval,
        actor_id=actor_id,
        actor_email=actor_email,
        subtotal_cents=amount_cents,
        total_cents=total_amount_cents,
        currency=plan.currency,
        tax_details=tax_details,
        idempotency_key=idempotency_key,
        metadata=metadata,
    )

    try:
        checkout = await provider.create_checkout(ctx)
        session_payload = dict(checkout.raw_session)
        session_payload.setdefault("tax_breakdown", tax_details)
        session_payload.setdefault("provider", provider_name)

        record = CheckoutRecord(
            actor_id=actor_id,
            session_id=checkout.session_id,
            customer_email=actor_email,
            amount=total_amount_cents,
            currency=plan.currency,
            status="created",
            idempotency_key=idempotency_key,
            raw_session=session_payload,
        )
        db.add(record)

        audit = PaymentAudit(
            actor_id=actor_id,
            action=f"create_{provider_name}_checkout",
            session_id=checkout.session_id,
            details={
                "plan": plan.name,
                "interval": interval,
                "subtotal_cents": amount_cents,
                "tax_cents": tax_details["tax_cents"],
                "total_cents": total_amount_cents,
                "tax_rate_percent": tax_details["tax_rate_percent"],
            },
            ip_address=client_ip,
            user_agent=user_agent,
        )
        db.add(audit)
        if provider_name == "paddle" and org_id:
            paddle_customer_id = session_payload.get("paddle_customer_id")
            if paddle_customer_id:
                await db.execute(
                    text(
                        """
                        UPDATE organizations
                        SET paddle_customer_id = COALESCE(paddle_customer_id, :pid)
                        WHERE id = :org_id
                        """
                    ),
                    {"pid": paddle_customer_id, "org_id": org_id},
                )

        await db.commit()

        logger.info(
            "%s checkout created",
            provider_name.capitalize(),
            extra={
                "session_id": checkout.session_id,
                "plan": plan.name,
                "interval": interval,
            },
        )
        return {
            "success": True,
            "url": checkout.checkout_url,
            "session_id": checkout.session_id,
            "tax": tax_details,
        }
    except ProviderError as exc:
        logger.exception("Checkout creation failed via provider")
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except Exception as exc:
        logger.exception("Checkout creation failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/create-checkout-session")
async def create_new_subscription_session_route(
    payload: dict, request: Request, db: AsyncSession = Depends(get_db)
):
    """
    Create a Stripe checkout session.
    """
    return await _create_checkout_session_for_provider("stripe", payload, request, db)


@router.post("/paddle/create-checkout")
async def create_paddle_checkout_route(
    payload: dict, request: Request, db: AsyncSession = Depends(get_db)
):
    """
    Create a Paddle checkout session.
    """
    return await _create_checkout_session_for_provider("paddle", payload, request, db)


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
    except Exception as e:
        # Stripe signature verification error (compatible across stripe versions)
        if e.__class__.__name__ == "SignatureVerificationError":
            raise HTTPException(status_code=400, detail="Invalid signature")
        raise

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
                        INSERT INTO subscriptions (created_by, billing_contact_user_id, stripe_customer_id,
                                                   stripe_subscription_id, status, created_at, updated_at)
                        VALUES (:uid, :uid, :cust, :sid, 'active', NOW(), NOW())
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
    # 4️⃣ customer.subscription.* events
    # -------------------------
    elif event_type.startswith("customer.subscription."):
        stripe_sub_id = data.get("id")
        ensured_actor_id, ensured_sub_db_id, ensured_plan_id = (
            await _ensure_subscription_record(db, stripe_sub_id, data.get("customer"))
        )
        if ensured_actor_id:
            actor_id = ensured_actor_id
        if ensured_sub_db_id:
            sub_db_id = ensured_sub_db_id
        if ensured_plan_id is not None:
            old_plan_id = ensured_plan_id

        if event_type == "customer.subscription.updated" and stripe_sub_id:
            await _apply_scheduled_downgrade(
                db,
                stripe_sub_id,
                sub_db_id,
                actor_id,
                data,
                client_ip,
                user_agent,
            )

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
                    "SELECT billing_contact_user_id, id FROM subscriptions WHERE stripe_subscription_id=:sid"
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
                    INSERT INTO subscriptions (created_by, billing_contact_user_id, stripe_customer_id,
                                               stripe_subscription_id, status, created_at, updated_at)
                    VALUES (:uid, :uid, :cust, :sid, 'active', NOW(), NOW())
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

        subtotal_cents = data.get("subtotal")
        tax_cents = data.get("tax")
        total_cents = data.get("total")
        if subtotal_cents is None:
            subtotal_cents = data.get("amount_due", 0)
            if tax_cents:
                subtotal_cents = max(subtotal_cents - tax_cents, 0)

        if total_cents is None:
            total_cents = (subtotal_cents or 0) + (tax_cents or 0)

        if tax_cents is None and subtotal_cents not in (None, 0):
            tax_cents = max(total_cents - subtotal_cents, 0)

        tax_rate_percent = data.get("tax_percent")
        if tax_rate_percent is None and subtotal_cents:
            if subtotal_cents > 0:
                tax_rate_percent = (tax_cents or 0) / subtotal_cents * 100
        tax_code = settings.TAX_DEFAULT_CODE
        tax_jurisdiction = settings.TAX_DEFAULT_JURISDICTION

        # If subscription found → upsert invoice
        if actor_id and sub_db_id:
            await db.execute(
                text(
                    """
                    INSERT INTO invoices (
                        user_id, subscription_id, stripe_invoice_id,
                        amount_due_cents, amount_paid_cents, currency,
                        invoice_pdf_url, hosted_invoice_url,
                        status, period_start, period_end,
                        subtotal_cents, tax_cents, total_cents,
                        tax_rate_percent, tax_code, tax_jurisdiction,
                        created_at
                    ) VALUES (
                        :uid, :subid, :iid,
                        :due, :paid, :currency,
                        :pdf, :hosted,
                        :status, :pstart, :pend,
                        :subtotal, :tax, :total,
                        :tax_rate, :tax_code, :tax_jurisdiction,
                        NOW()
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
                        period_end = EXCLUDED.period_end,
                        subtotal_cents = EXCLUDED.subtotal_cents,
                        tax_cents = EXCLUDED.tax_cents,
                        total_cents = EXCLUDED.total_cents,
                        tax_rate_percent = EXCLUDED.tax_rate_percent,
                        tax_code = EXCLUDED.tax_code,
                        tax_jurisdiction = EXCLUDED.tax_jurisdiction
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
                    "subtotal": subtotal_cents,
                    "tax": tax_cents,
                    "total": total_cents,
                    "tax_rate": tax_rate_percent,
                    "tax_code": tax_code,
                    "tax_jurisdiction": tax_jurisdiction,
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
                        WHERE billing_contact_user_id = :uid
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
        .where(Subscription.billing_contact_user_id == user_id)
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
                       sp.annual_price_cents, sp.currency, s.billing_contact_user_id, s.stripe_subscription_id,
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


@router.post("/subscription/cancel")
async def cancel_subscription(
    payload: CancelSubscriptionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id_header = request.headers.get("X-User-ID")
    if not user_id_header:
        raise HTTPException(status_code=400, detail="Missing X-User-ID header")

    try:
        actor_id = int(user_id_header)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid X-User-ID header")

    client_ip, user_agent = extract_client_info(request)
    org_id = await _resolve_org_id(db, actor_id)
    subscription_row = await _fetch_subscription_for_org(db, org_id)

    if not subscription_row:
        raise HTTPException(
            status_code=404, detail="Subscription not found for organization"
        )

    if not subscription_row.stripe_subscription_id:
        raise HTTPException(
            status_code=400, detail="Subscription is not linked to a Stripe record"
        )

    if payload.mode == "cycle_end":
        return await _cancel_subscription_cycle_end(
            db, subscription_row, actor_id, client_ip, user_agent
        )
    if payload.mode == "immediate":
        return await _cancel_subscription_immediately(
            db, subscription_row, actor_id, payload.refund, client_ip, user_agent
        )

    raise HTTPException(status_code=400, detail="Invalid cancellation mode")


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
    if req.planId == FREE_PLAN_ID:
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


@router.get("/usage")
async def get_billing_usage_overview(
    request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-ID header")
    user_id = int(user_id)

    org_id = await _resolve_org_id(db, user_id)

    plan_result = await db.execute(
        text(
            """
            SELECT sp.id, sp.name, sp.description,
                   sp.sbom_limit, sp.user_limit,
                   sp.project_scan_limit, sp.scan_rate_limit,
                   sp.monthly_price_cents, sp.annual_price_cents, sp.currency,
                   s.interval, s.current_period_start, s.current_period_end
            FROM organizations o
            JOIN subscriptions s ON s.id = o.subscription_id
            JOIN subscription_plans sp ON sp.id = s.plan_id
            WHERE o.id = :oid
            """
        ),
        {"oid": org_id},
    )
    plan_row = plan_result.fetchone()
    if not plan_row:
        raise HTTPException(status_code=404, detail="Subscription plan not found")

    usage_rows = await db.execute(
        text(
            """
            SELECT usage_key, used, period_start, period_end
            FROM usage_counters
            WHERE organization_id = :oid
              AND usage_key IN ('sbom_upload', 'project_scan', 'api_requests')
            """
        ),
        {"oid": org_id},
    )
    usage_map = {row.usage_key: row for row in usage_rows.fetchall()}

    def build_usage(key: str, limit: int | None):
        entry = usage_map.get(key)
        used = entry.used if entry else 0
        next_reset = (
            entry.period_end.isoformat() if entry and entry.period_end else None
        )
        remaining = None
        percent = None
        if limit is not None and limit >= 0:
            remaining = max(limit - used, 0)
            percent = min(100, round((used / limit) * 100, 2)) if limit > 0 else 0
        return {
            "used": int(used),
            "limit": limit,
            "remaining": remaining,
            "nextReset": next_reset,
            "percent": percent,
        }

    sbom_usage = build_usage("sbom_upload", getattr(plan_row, "sbom_limit", None))
    scan_usage = build_usage(
        "project_scan", getattr(plan_row, "project_scan_limit", None)
    )
    api_limit = getattr(plan_row, "scan_rate_limit", None)
    api_usage = build_usage("api_requests", api_limit)

    seats_result = await db.execute(
        text(
            """
            SELECT COUNT(*) FROM users
            WHERE organization_id = :oid AND COALESCE(is_active, TRUE) = TRUE
            """
        ),
        {"oid": org_id},
    )
    seats_used = seats_result.scalar() or 0
    seat_limit = getattr(plan_row, "user_limit", None)
    seat_usage = {
        "used": seats_used,
        "limit": seat_limit,
        "remaining": (
            (seat_limit - seats_used)
            if seat_limit is not None and seat_limit >= 0
            else None
        ),
        "percent": (
            min(100, round((seats_used / seat_limit) * 100, 2))
            if seat_limit and seat_limit > 0
            else None
        ),
    }

    history_rows = await db.execute(
        text(
            """
            WITH dates AS (
                SELECT generate_series(date_trunc('day', NOW()) - INTERVAL '13 days',
                                       date_trunc('day', NOW()),
                                       INTERVAL '1 day')::date AS day
            ),
            sbom_counts AS (
                SELECT DATE(s.created_at) AS day, COUNT(*) AS uploads
                FROM sboms s
                JOIN projects p ON p.id = s.project_id
                WHERE p.organization_id = :oid
                  AND s.created_at >= NOW() - INTERVAL '14 days'
                GROUP BY DATE(s.created_at)
            ),
            scan_counts AS (
                SELECT DATE(s.created_at) AS day, COUNT(*) AS scans
                FROM sboms s
                JOIN projects p ON p.id = s.project_id
                WHERE p.organization_id = :oid
                  AND s.source IN ('auto-code-scan','project_scan')
                  AND s.created_at >= NOW() - INTERVAL '14 days'
                GROUP BY DATE(s.created_at)
            )
            SELECT d.day,
                   COALESCE(sb.uploads, 0) AS sbom_uploads,
                   COALESCE(sc.scans, 0) AS project_scans
            FROM dates d
            LEFT JOIN sbom_counts sb ON sb.day = d.day
            LEFT JOIN scan_counts sc ON sc.day = d.day
            ORDER BY d.day
            """
        ),
        {"oid": org_id},
    )
    history = [
        {
            "date": row.day.isoformat(),
            "sbom_uploads": int(row.sbom_uploads),
            "project_scans": int(row.project_scans),
        }
        for row in history_rows.fetchall()
    ]

    invoice_stats_row = await db.execute(
        text(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN inv.status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
                SUM(CASE WHEN inv.status != 'paid' THEN inv.amount_due_cents ELSE 0 END) AS outstanding_cents,
                SUM(inv.amount_paid_cents) AS total_paid_cents
            FROM invoices inv
            JOIN subscriptions s ON inv.subscription_id = s.id
            JOIN organizations o ON o.subscription_id = s.id
            WHERE o.id = :oid
            """
        ),
        {"oid": org_id},
    )
    invoice_stats = invoice_stats_row.fetchone()

    return {
        "plan": {
            "id": plan_row.id,
            "name": plan_row.name,
            "description": plan_row.description,
            "interval": plan_row.interval,
            "nextRenewal": (
                plan_row.current_period_end.isoformat()
                if plan_row.current_period_end
                else None
            ),
            "cycleStart": (
                plan_row.current_period_start.isoformat()
                if plan_row.current_period_start
                else None
            ),
            "limits": {
                "sbom": plan_row.sbom_limit,
                "users": plan_row.user_limit,
                "project_scans": plan_row.project_scan_limit,
                "api_calls_per_minute": plan_row.scan_rate_limit,
            },
        },
        "usage": {
            "sbomUploads": sbom_usage,
            "projectScans": scan_usage,
            "seats": seat_usage,
            "apiCalls": api_usage,
        },
        "history": history,
        "invoiceStats": {
            "total": invoice_stats.total if invoice_stats else 0,
            "paid": invoice_stats.paid_count if invoice_stats else 0,
            "outstanding_cents": invoice_stats.outstanding_cents or 0,
            "total_paid_cents": invoice_stats.total_paid_cents or 0,
        },
    }


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
        .where(Subscription.billing_contact_user_id == user_id)
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
            "id": inv.id,
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

    try:
        client_ip, user_agent = extract_client_info(request)
        audit = PaymentAudit(
            actor_id=user_id,
            action="view_invoices",
            details={"page": page, "limit": limit, "returned": len(items)},
            ip_address=client_ip,
            user_agent=user_agent,
        )
        db.add(audit)
        await db.commit()
    except Exception as audit_err:
        logger.warning(f"Failed to log invoice view: {audit_err}")

    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": (total + limit - 1) // limit,
    }
