import pytest
from fastapi import status

from app.api import billing_routes
from app.db.models import BillingEvent


@pytest.mark.asyncio
async def test_webhook_missing_sig(async_client):
    resp = await async_client.post("/api/billing/webhook", content=b"{}")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing signature header"


@pytest.mark.asyncio
async def test_webhook_invalid_signature(async_client, monkeypatch):
    class SignatureVerificationError(Exception):
        pass

    monkeypatch.setattr(
        billing_routes.stripe.Webhook,
        "construct_event",
        lambda payload, sig, secret: (_ for _ in ()).throw(SignatureVerificationError()),
    )

    resp = await async_client.post(
        "/api/billing/webhook",
        headers={"stripe-signature": "sig"},
        content=b"{}",
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid signature"


@pytest.mark.asyncio
async def test_webhook_idempotent(async_client, fake_db, monkeypatch):
    fake_db.queue_result(FakeResult(scalar=1))

    monkeypatch.setattr(
        billing_routes.stripe.Webhook,
        "construct_event",
        lambda payload, sig, secret: {"id": "evt_1", "type": "charge.succeeded"},
    )

    resp = await async_client.post(
        "/api/billing/webhook",
        headers={"stripe-signature": "sig"},
        content=b"{}",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_processed"


class FakeResult:
    def __init__(self, scalar=None, fetchone=None):
        self._scalar = scalar
        self._fetchone = fetchone

    def scalar(self):
        return self._scalar

    def fetchone(self):
        return self._fetchone


@pytest.mark.asyncio
async def test_webhook_invoice_payment_success(async_client, fake_db, monkeypatch):
    # 1) idempotency: not processed
    fake_db.queue_result(FakeResult(scalar=None))

    # 2) subscription lookup returns actor_id + sub_db_id
    fake_db.queue_result(FakeResult(fetchone=(11, 99)))

    # 3) invoice upsert execute -> no fetch needed
    fake_db.queue_result(FakeResult())

    # 4) plan name lookup
    fake_db.queue_result(FakeResult(scalar="Basic"))

    # 5) org lookup
    fake_db.queue_result(FakeResult(fetchone=(7,)))

    def fake_construct(payload, sig, secret):
        return {
            "id": "evt_invoice",
            "type": "invoice.payment_succeeded",
            "data": {"object": {
                "id": "in_1",
                "subscription": "sub_123",
                "customer": "cus_1",
                "amount_paid": 1000,
                "amount_due": 1000,
                "currency": "usd",
                "status": "paid",
                "hosted_invoice_url": "https://invoice"
            }}
        }

    monkeypatch.setattr(
        billing_routes.stripe.Webhook, "construct_event", fake_construct
    )
    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(billing_routes, "notify_payment", fake_notify)

    resp = await async_client.post(
        "/api/billing/webhook",
        headers={"stripe-signature": "sig"},
        content=b"{}",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert notify_calls  # called even if org_id None (function handles)
