from typing import Dict

from app.utils.payment_provider.base import (
    CheckoutContext,
    CheckoutResult,
    PaymentProvider,
    PlanSnapshot,
    ProviderError,
    snapshot_plan,
)
from app.utils.payment_provider.stripe_provider import StripePaymentProvider
from app.utils.payment_provider.paddle_provider import PaddlePaymentProvider

_REGISTRY: Dict[str, PaymentProvider] = {}


def register_provider(provider: PaymentProvider) -> None:
    _REGISTRY[provider.name] = provider


def get_payment_provider(name: str = "stripe") -> PaymentProvider:
    provider = _REGISTRY.get(name)
    if not provider:
        raise ProviderError(f"Unknown payment provider '{name}'", status_code=400)
    return provider


register_provider(StripePaymentProvider())
register_provider(PaddlePaymentProvider())

__all__ = [
    "CheckoutContext",
    "CheckoutResult",
    "PaymentProvider",
    "PlanSnapshot",
    "ProviderError",
    "get_payment_provider",
    "register_provider",
    "snapshot_plan",
]
