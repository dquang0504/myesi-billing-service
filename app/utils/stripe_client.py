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
    tax_line: Optional[dict] = None,
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

    line_items = [{"price": price_id, "quantity": quantity}]
    if tax_line and tax_line.get("amount_cents", 0) > 0:
        interval = tax_line.get("interval", "month")
        if isinstance(interval, str):
            interval_map = {
                "monthly": "month",
                "yearly": "year",
                "month": "month",
                "year": "year",
            }
            interval = interval_map.get(interval.lower(), interval)
        line_items.append(
            {
                "price_data": {
                    "currency": tax_line.get("currency", "usd"),
                    "unit_amount": tax_line["amount_cents"],
                    "product_data": {
                        "name": tax_line.get("label", "Digital Services Tax")
                    },
                    "recurring": {"interval": interval or "month"},
                },
                "quantity": 1,
            }
        )

    session = stripe.checkout.Session.create(
        customer_email=customer_email,
        payment_method_types=["card"],
        mode=mode,  # 'subscription' by default
        line_items=line_items,
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
                "SELECT id, billing_contact_user_id FROM subscriptions WHERE stripe_subscription_id=:sid"
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
    Switching between billing cycles follows explicit rules:
      - Monthly -> Yearly: immediate upgrade with potential prorated charge.
      - Yearly -> Monthly: scheduled downgrade at the end of the paid term.
    """
    try:
        current_item = current_sub["items"]["data"][0]
        current_price = current_item["price"]["id"]

        # Fetch plan definition once so we know both price ids
        plan_res = await db.execute(
            text(
                """
                SELECT stripe_price_id_monthly, stripe_price_id_yearly
                FROM subscription_plans
                WHERE stripe_price_id_monthly=:pid OR stripe_price_id_yearly=:pid
                """
            ),
            {"pid": current_price},
        )
        plan = plan_res.fetchone()
        if not plan:
            raise HTTPException(400, "Unable to locate current plan for cycle switch")

        if current_price == plan.stripe_price_id_monthly:
            current_interval = "monthly"
        elif current_price == plan.stripe_price_id_yearly:
            current_interval = "yearly"
        else:
            raise HTTPException(400, "Unsupported current billing interval")

        if new_price_id == plan.stripe_price_id_monthly:
            requested_interval = "monthly"
        elif new_price_id == plan.stripe_price_id_yearly:
            requested_interval = "yearly"
        else:
            raise HTTPException(
                400, "Requested billing interval is not part of the current plan"
            )

        if current_interval == requested_interval:
            raise HTTPException(
                400, f"Subscription already uses the {requested_interval} interval."
            )

        if current_interval == "monthly" and requested_interval == "yearly":
            resp = await upgrade_subscription_logic(db, current_sub, new_price_id)
            resp.setdefault(
                "message",
                "Switched to yearly billing immediately. Prorated charges may apply.",
            )
            resp["cycle_switch"] = {
                "from": "monthly",
                "to": "yearly",
                "mode": "immediate_upgrade",
            }
            return resp

        resp = await downgrade_subscription_logic(db, current_sub, new_price_id)
        resp.setdefault(
            "message",
            "Switch to monthly billing scheduled at the end of the current term.",
        )
        resp["cycle_switch"] = {
            "from": "yearly",
            "to": "monthly",
            "mode": "scheduled_downgrade",
        }
        return resp

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Cycle switch failed: {str(e)}")
