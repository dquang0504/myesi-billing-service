from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ProviderError(Exception):
    """Standardized provider error that carries an HTTP status code."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PlanSnapshot:
    id: int
    name: str
    currency: str
    stripe_price_id_monthly: Optional[str] = None
    stripe_price_id_yearly: Optional[str] = None
    paddle_price_id_monthly: Optional[str] = None
    paddle_price_id_yearly: Optional[str] = None
    paddle_product_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def snapshot_plan(plan: Any) -> PlanSnapshot:
    """Safely capture plan information for downstream provider logic."""
    return PlanSnapshot(
        id=getattr(plan, "id"),
        name=getattr(plan, "name"),
        currency=getattr(plan, "currency", "usd"),
        stripe_price_id_monthly=getattr(plan, "stripe_price_id_monthly", None),
        stripe_price_id_yearly=getattr(plan, "stripe_price_id_yearly", None),
        paddle_price_id_monthly=getattr(plan, "paddle_price_id_monthly", None),
        paddle_price_id_yearly=getattr(plan, "paddle_price_id_yearly", None),
        paddle_product_id=getattr(plan, "paddle_product_id", None),
        metadata={},
    )


@dataclass
class CheckoutContext:
    plan: PlanSnapshot
    interval: str
    actor_id: Optional[int]
    actor_email: Optional[str]
    subtotal_cents: int
    total_cents: int
    currency: str
    tax_details: Dict[str, Any]
    idempotency_key: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutResult:
    session_id: str
    checkout_url: str
    raw_session: Dict[str, Any]


class PaymentProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def create_checkout(self, ctx: CheckoutContext) -> CheckoutResult:
        raise NotImplementedError

    async def cancel_subscription(self, *args, **kwargs):
        raise NotImplementedError

    async def update_subscription(self, *args, **kwargs):
        raise NotImplementedError

    async def verify_webhook(self, *args, **kwargs):
        raise NotImplementedError

    async def normalize_event(self, *args, **kwargs):
        raise NotImplementedError
