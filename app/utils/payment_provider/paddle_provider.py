# app/utils/payment_provider/paddle_provider.py
from typing import Any, Dict, Optional

from paddle_billing.Resources.Transactions.Operations import CreateTransaction

from app.core.config import settings
from app.utils.paddle_client import PaddleClient
from app.utils.payment_provider.base import (
    CheckoutContext,
    CheckoutResult,
    PaymentProvider,
    ProviderError,
)


class PaddlePaymentProvider(PaymentProvider):
    name = "paddle"

    def __init__(self) -> None:
        self.client = PaddleClient()

    async def create_checkout(self, ctx: CheckoutContext) -> CheckoutResult:
        if not settings.PADDLE_API_KEY:
            raise ProviderError("Paddle API key is not configured", status_code=500)

        price_id = self._resolve_price_id(ctx)
        if not ctx.actor_email:
            raise ProviderError(
                "Customer email is required for checkout", status_code=400
            )
        if not price_id:
            raise ProviderError(
                "Paddle price id is not configured for this plan", status_code=400
            )

        # Minimum address required for Billing checkout flows
        country_code = (ctx.metadata or {}).get("country_code")
        postal_code = (ctx.metadata or {}).get("postal_code")
        if not country_code or not postal_code:
            raise ProviderError(
                "Missing billing address (country_code, postal_code) required for Paddle checkout.",
                status_code=400,
            )

        tax_line_attempted = False
        try:
            customer_id = (ctx.metadata or {}).get("paddle_customer_id")
            if not customer_id:
                customer = await self.client.create_or_get_customer(
                    ctx.actor_email,
                    custom_data={
                        "actor_id": ctx.actor_id,
                        "org_id": (ctx.metadata or {}).get("org_id"),
                        "source": "myesi",
                    },
                )
                customer_id = customer.id

            address = await self.client.create_address(
                customer_id, country_code, postal_code
            )
            address_id = address.id

            operation = self._build_transaction_operation(
                ctx, price_id, customer_id, address_id, include_tax_line=True
            )
            tax_line_attempted = True
            txn = await self.client.create_transaction(operation)

        except Exception as exc:
            if tax_line_attempted:
                try:
                    operation = self._build_transaction_operation(
                        ctx, price_id, customer_id, address_id, include_tax_line=False
                    )
                    txn = await self.client.create_transaction(operation)
                    tax_line_attempted = False
                except Exception as retry_exc:
                    raise ProviderError(
                        f"Paddle request failed: {retry_exc}", status_code=502
                    )
            else:
                raise ProviderError(f"Paddle request failed: {exc}", status_code=502)

        # SDK entity shape: txn.checkout.url
        checkout_url = getattr(getattr(txn, "checkout", None), "url", None)
        session_id = getattr(txn, "id", None) or ctx.idempotency_key

        if not checkout_url:
            raise ProviderError(
                "Paddle transaction missing checkout URL", status_code=502
            )

        raw_session: Dict[str, Any] = {
            "provider": self.name,
            "paddle_customer_id": customer_id,
            "paddle_address_id": address_id,
            "paddle_transaction_id": getattr(txn, "id", None),
            "billing_address_id": (ctx.metadata or {}).get("billing_address_id"),
            "tax_breakdown": ctx.tax_details,
            "tax_line_item_applied": tax_line_attempted,
        }

        return CheckoutResult(
            session_id=session_id,
            checkout_url=checkout_url,
            raw_session=raw_session,
        )

    def _resolve_price_id(self, ctx: CheckoutContext) -> Optional[str]:
        return (
            ctx.plan.paddle_price_id_monthly
            if ctx.interval == "monthly"
            else ctx.plan.paddle_price_id_yearly
        )

    def _build_transaction_operation(
        self,
        ctx: CheckoutContext,
        price_id: str,
        customer_id: str,
        address_id: str,
        include_tax_line: bool,
    ) -> CreateTransaction:
        # CreateTransaction schema depends on SDK version.
        # This is the canonical intent: items + customer + address + redirect URLs + custom_data.
        items = [{"price_id": price_id, "quantity": 1}]
        tax_cents = int((ctx.tax_details or {}).get("tax_cents") or 0)
        if include_tax_line and tax_cents > 0 and ctx.plan.paddle_product_id:
            # Add a tax line item so total charged includes tax.
            items.append(
                {
                    "price": {
                        "product_id": ctx.plan.paddle_product_id,
                        "name": f"{ctx.plan.name} Tax",
                        "description": "Tax for subscription",
                        "unit_price": {
                            "amount": f"{tax_cents / 100:.2f}",
                            "currency_code": ctx.currency.upper(),
                        },
                    },
                    "quantity": 1,
                }
            )
        return CreateTransaction(
            items=items,
            customer_id=customer_id,
            address_id=address_id,
            currency_code=ctx.currency.upper(),
            custom_data={
                "actor_id": ctx.actor_id,
                "org_id": (ctx.metadata or {}).get("org_id"),
                "plan_id": ctx.plan.id,
                "interval": ctx.interval,
                "billing_address_id": (ctx.metadata or {}).get("billing_address_id"),
                "subtotal_cents": ctx.subtotal_cents,
                "tax_cents": int((ctx.tax_details or {}).get("tax_cents") or 0),
                "tax_rate_percent": (ctx.tax_details or {}).get("tax_rate_percent"),
                "total_cents": ctx.total_cents,
            },
            # redirect_urls={
            #     "success": settings.PADDLE_CHECKOUT_SUCCESS_URL,
            #     "cancel": settings.PADDLE_CHECKOUT_CANCEL_URL,
            # },
        )
