import pytest
from starlette.requests import Request

from app.db.models import SubscriptionPlan
from app.api import billing_routes
from app.utils.payment_provider.base import CheckoutResult


def make_payload(plan_id=1, interval="monthly", email="dev@example.com"):
    return {
        "planId": plan_id,
        "interval": interval,
        "user": {"id": 11, "email": email},
    }


def enqueue_plan(fake_db, plan: SubscriptionPlan | None):
    row = None
    if plan:
        row = type(
            "Row",
            (),
            {
                "id": plan.id,
                "name": plan.name,
                "description": plan.description,
                "sbom_limit": plan.sbom_limit,
                "user_limit": plan.user_limit,
                "project_scan_limit": plan.project_scan_limit,
                "currency": plan.currency,
                "stripe_price_id_monthly": plan.stripe_price_id_monthly,
                "stripe_price_id_yearly": plan.stripe_price_id_yearly,
                "is_active": plan.is_active,
                "monthly_price_cents": plan.monthly_price_cents,
                "annual_price_cents": plan.annual_price_cents,
            },
        )()
    fake_db.execute_results.append(
        type("R", (), {"scalar_one_or_none": lambda self: row})()
    )


@pytest.mark.asyncio
async def test_create_checkout_missing_plan_id(async_client):
    resp = await async_client.post("/api/billing/create-checkout-session", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing planId"


@pytest.mark.asyncio
async def test_create_checkout_invalid_interval(async_client):
    resp = await async_client.post(
        "/api/billing/create-checkout-session",
        json=make_payload(interval="weekly"),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid interval value"


@pytest.mark.asyncio
async def test_create_checkout_plan_not_found(async_client, fake_db):
    enqueue_plan(fake_db, None)
    resp = await async_client.post(
        "/api/billing/create-checkout-session",
        json=make_payload(),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Plan not found"


@pytest.mark.asyncio
async def test_create_checkout_plan_inactive(async_client, fake_db):
    plan = SubscriptionPlan(
        id=1,
        name="Basic",
        description="desc",
        stripe_price_id_monthly="price_month",
        stripe_price_id_yearly="price_year",
        sbom_limit=10,
        user_limit=5,
        monthly_price_cents=1000,
        annual_price_cents=10000,
        currency="usd",
        is_active=False,
    )
    enqueue_plan(fake_db, plan)
    resp = await async_client.post(
        "/api/billing/create-checkout-session",
        json=make_payload(),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Plan is not active"


@pytest.mark.asyncio
async def test_create_checkout_success(async_client, fake_db, monkeypatch):
    plan = SubscriptionPlan(
        id=1,
        name="Pro",
        description="",
        stripe_price_id_monthly="price_month",
        stripe_price_id_yearly="price_year",
        sbom_limit=10,
        user_limit=5,
        monthly_price_cents=1000,
        annual_price_cents=10000,
        currency="usd",
        is_active=True,
    )
    enqueue_plan(fake_db, plan)

    class FakeProvider:
        async def create_checkout(self, ctx):
            return CheckoutResult(
                session_id="sess_123",
                checkout_url="https://checkout",
                raw_session={
                    "id": "sess_123",
                    "url": "https://checkout",
                    "mode": "subscription",
                    "currency": "usd",
                    "tax_breakdown": ctx.tax_details,
                },
            )

    monkeypatch.setattr(
        billing_routes, "get_payment_provider", lambda name="stripe": FakeProvider()
    )

    resp = await async_client.post(
        "/api/billing/create-checkout-session",
        json=make_payload(),
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["session_id"] == "sess_123"
    assert fake_db.commits >= 1
    assert len(fake_db.added) >= 2
