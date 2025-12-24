import asyncio
from typing import Any, Dict, Optional

from app.utils.payment_provider.base import (
    CheckoutContext,
    CheckoutResult,
    PaymentProvider,
    PlanSnapshot,
    ProviderError,
)
from app.utils.stripe_client import create_new_subscription_session


class StripePaymentProvider(PaymentProvider):
    name = "stripe"

    async def create_checkout(self, ctx: CheckoutContext) -> CheckoutResult:
        price_id = self._resolve_price_id(ctx.plan, ctx.interval)
        if not ctx.actor_email:
            raise ProviderError("Customer email is required for checkout", 400)
        if not price_id:
            raise ProviderError("Stripe price id is not configured for this plan", 400)

        tax_line = self._build_tax_line(ctx)
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(
            None,
            lambda: create_new_subscription_session(
                customer_email=ctx.actor_email,
                price_id=price_id,
                idempotency_key=ctx.idempotency_key,
                tax_line=tax_line,
            ),
        )
        payload: Dict[str, Any] = dict(session)
        payload["tax_breakdown"] = ctx.tax_details
        payload["provider"] = self.name
        payload["plan_id"] = ctx.plan.id
        return CheckoutResult(
            session_id=session["id"],
            checkout_url=session["url"],
            raw_session=payload,
        )

    def _resolve_price_id(self, plan: PlanSnapshot, interval: str) -> Optional[str]:
        if interval == "monthly":
            return plan.stripe_price_id_monthly
        return plan.stripe_price_id_yearly

    def _build_tax_line(self, ctx: CheckoutContext) -> Optional[Dict[str, Any]]:
        tax_cents = ctx.tax_details.get("tax_cents", 0)
        if tax_cents <= 0:
            return None
        stripe_interval = "month" if ctx.interval == "monthly" else "year"
        percent = ctx.tax_details.get("tax_rate_percent")
        label = "Digital Services Tax"
        if percent is not None:
            label = f"{percent:.3f}% Digital Services Tax"
        return {
            "amount_cents": tax_cents,
            "label": label,
            "interval": stripe_interval,
            "currency": ctx.currency,
        }
