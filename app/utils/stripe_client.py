from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
import stripe
from app.core.config import settings

stripe.api_key = settings.STRIPE_SECRET_KEY


def create_new_subscription_session(
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
    print("This is the price_id: ", price_id)
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


async def get_plan(db, plan_id: int):
    row = await db.execute(
        text("SELECT * FROM subscription_plans WHERE id=:pid AND is_active=TRUE"),
        {"pid": plan_id},
    )
    plan = row.fetchone()
    if not plan:
        raise HTTPException(404, "Plan not found")

    return plan


async def upgrade_subscription_logic(db, current_sub, new_price_id):
    """
    Perform immediate upgrade with proration and return invoice/payment info.
    Returns a dict with keys:
      - success: bool
      - paid: bool (True if invoice was paid)
      - hosted_invoice_url: str | None
      - payment_intent_client_secret: str | None
      - message: str
    """
    try:
        item_id = current_sub["items"]["data"][0]["id"]

        # Modify subscription and create invoice immediately (attempt to pay)
        updated = stripe.Subscription.modify(
            current_sub["id"],
            items=[{"id": item_id, "price": new_price_id}],
            proration_behavior="always_invoice",
        )

        # If modify returned a latest_invoice id, fetch it (expand payment_intent)
        latest_invoice_id = updated.get("latest_invoice")
        invoice = None
        if latest_invoice_id:
            invoice = stripe.Invoice.retrieve(
                latest_invoice_id, expand=["payment_intent"]
            )

        # Persist DB: update subscriptions.plan_id based on new_price_id
        await db.execute(
            text(
                """
                UPDATE subscriptions
                SET plan_id = (SELECT id FROM subscription_plans 
                               WHERE stripe_price_id_monthly=:pid OR stripe_price_id_yearly=:pid),
                    updated_at = NOW()
                WHERE stripe_subscription_id=:sid
            """
            ),
            {"pid": new_price_id, "sid": current_sub["id"]},
        )
        await db.commit()

        # Decide response based on invoice/payment_intent
        resp = {
            "success": True,
            "paid": False,
            "hosted_invoice_url": None,
            "payment_intent_client_secret": None,
            "message": "Upgrade processed",
        }

        if invoice:
            # If invoice is paid already
            if invoice.get("status") == "paid" or invoice.get("paid", False):
                resp["paid"] = True
                resp["message"] = (
                    "Invoice paid; subscription upgraded and billed immediately."
                )
                return resp

            # If hosted_invoice_url present (Stripe-hosted invoice page)
            hosted = invoice.get("hosted_invoice_url")
            if hosted:
                resp["hosted_invoice_url"] = hosted
                resp["message"] = (
                    "Invoice created and requires payment. Redirect user to hosted invoice."
                )
                return resp

            # If there's a payment_intent that requires action, return client_secret
            pi = invoice.get("payment_intent") if invoice else None
            if pi:
                # if needs action, front-end can use client_secret to finish payment with Stripe.js
                client_secret = pi.get("client_secret")
                if client_secret:
                    resp["payment_intent_client_secret"] = client_secret
                    resp["message"] = "Payment requires action via Stripe.js."
                    return resp

        # fallback: no invoice created, return success but warn
        resp["message"] = (
            "Upgrade applied; no invoice created or invoice already handled by Stripe."
        )
        return resp

    except stripe.error.CardError as e:
        # card declined etc
        raise HTTPException(402, f"Card error: {str(e.user_message or e)}")
    except Exception as e:
        raise HTTPException(500, f"Upgrade failed: {str(e)}")


async def downgrade_subscription_logic(db, current_sub, new_price_id):
    try:
        # lookup subscription_id
        row = await db.execute(
            text(
                "SELECT id, user_id FROM subscriptions WHERE stripe_subscription_id=:sid"
            ),
            {"sid": current_sub["id"]},
        )
        rec = row.fetchone()
        if not rec:
            raise HTTPException(400, "Subscription not found in DB")

        sub_db_id, user_id = rec

        # find organization
        r = await db.execute(
            text("SELECT organization_id FROM users WHERE id=:uid"), {"uid": user_id}
        )
        org = r.fetchone()
        if not org or not org[0]:
            raise HTTPException(400, "User has no organization")
        org_id = org[0]

        # check if existing scheduled downgrade
        row = await db.execute(
            text("SELECT id FROM scheduled_downgrades WHERE subscription_id=:sid"),
            {"sid": sub_db_id},
        )
        exists = row.fetchone()

        if exists:
            # UPDATE existing
            await db.execute(
                text(
                    """
                    UPDATE scheduled_downgrades
                    SET target_price_id=:pid, created_at=NOW()
                    WHERE subscription_id=:sid
                """
                ),
                {"pid": new_price_id, "sid": sub_db_id},
            )
        else:
            # INSERT new
            await db.execute(
                text(
                    """
                    INSERT INTO scheduled_downgrades (subscription_id, organization_id, target_price_id)
                    VALUES (:sid, :oid, :pid)
                """
                ),
                {"sid": sub_db_id, "oid": org_id, "pid": new_price_id},
            )

        await db.commit()

        return {
            "success": True,
            "message": "Downgrade scheduled for next billing cycle",
        }

    except Exception as e:
        raise HTTPException(500, f"Downgrade scheduling failed: {str(e)}")


# ---------------------------
# CYCLE SWITCH (delegated to upgrade/downgrade)
# ---------------------------
async def cycle_switch_logic(db, current_sub, new_price_id):
    """
    Monthly -> Yearly = upgrade
    Yearly -> Monthly = downgrade
    """
    try:
        old_price = current_sub["items"]["data"][0]["price"]["id"]

        # fetch old plan to compare price
        old_plan_res = await db.execute(
            text(
                """
                SELECT monthly_price_cents, annual_price_cents,
                       stripe_price_id_monthly, stripe_price_id_yearly
                FROM subscription_plans
                WHERE stripe_price_id_monthly=:pid OR stripe_price_id_yearly=:pid
            """
            ),
            {"pid": old_price},
        )
        old = old_plan_res.fetchone()
        if not old:
            raise HTTPException(400, "Old plan not found")

        # determine upgrade or downgrade by price
        old_amount = (
            old.monthly_price_cents
            if old_price == old.stripe_price_id_monthly
            else old.annual_price_cents
        )

        new_plan_res = await db.execute(
            text(
                """
                SELECT monthly_price_cents, annual_price_cents,
                       stripe_price_id_monthly, stripe_price_id_yearly
                FROM subscription_plans
                WHERE stripe_price_id_monthly=:pid OR stripe_price_id_yearly=:pid
            """
            ),
            {"pid": new_price_id},
        )
        new = new_plan_res.fetchone()
        new_amount = (
            new.monthly_price_cents
            if new_price_id == new.stripe_price_id_monthly
            else new.annual_price_cents
        )

        if new_amount > old_amount:
            return await upgrade_subscription_logic(db, current_sub, new_price_id)
        else:
            return await downgrade_subscription_logic(db, current_sub, new_price_id)

    except Exception as e:
        raise HTTPException(500, f"Cycle switch failed: {str(e)}")
