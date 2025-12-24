# app/utils/paddle_client.py
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from typing import Any, Dict, Optional

import httpx

from paddle_billing import Client as PaddleSDKClient, Environment, Options
from paddle_billing.Exceptions.ApiError import ApiError
from paddle_billing.Resources.Customers.Operations import CreateCustomer
from paddle_billing.Resources.Addresses.Operations import CreateAddress
from paddle_billing.Resources.Transactions.Operations import CreateTransaction

from app.core.config import settings


class PaddleAPIError(RuntimeError):
    pass


def _paddle_base_url() -> str:
    env = (settings.PADDLE_ENV or "production").lower()
    return (
        "https://sandbox-api.paddle.com"
        if env == "sandbox"
        else "https://api.paddle.com"
    )


def _paddle_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.PADDLE_API_KEY}",
        "Accept": "application/json",
        "Paddle-Version": "1",
    }


async def fetch_paddle_customer_payment_methods(
    customer_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Calls Paddle API:
      GET /customers/{customer_id}/payment-methods

    Returns the raw JSON body.
    """
    if not settings.PADDLE_API_KEY:
        raise PaddleAPIError("Missing PADDLE_API_KEY")

    url = f"{settings.PADDLE_API_BASE}/customers/{customer_id}/payment-methods"
    headers = {
        "Authorization": f"Bearer {settings.PADDLE_API_KEY}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code >= 400:
        raise PaddleAPIError(f"Paddle API error {resp.status_code}: {resp.text}")

    return resp.json()


async def fetch_paddle_invoice_pdf_url(transaction_id: str) -> Optional[str]:
    """
    GET /transactions/{id}/invoice
    Returns {"data": {"url": "https://...pdf?..."}}
    """
    if not settings.PADDLE_API_KEY:
        return None

    url = f"{_paddle_base_url()}/transactions/{transaction_id}/invoice"
    params = {"disposition": "inline"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_paddle_headers(), params=params)
            if r.status_code >= 400:
                return None
            body = r.json() or {}
            return (body.get("data") or {}).get("url")
    except Exception:
        return None


async def fetch_paddle_transaction_details(
    transaction_id: str,
) -> Optional[Dict[str, Any]]:
    """
    GET /transactions/{id}
    Shape varies by Paddle version; returns raw `data`.
    """
    if not settings.PADDLE_API_KEY:
        return None

    url = f"{_paddle_base_url()}/transactions/{transaction_id}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_paddle_headers())
            if r.status_code >= 400:
                return None
            body = r.json() or {}
            return body.get("data") or None
    except Exception:
        return None


def extract_default_payment_method_summary(
    paddle_list_resp: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Expected Paddle format thường là:
      { "data": [ ...payment_method_objects... ], "meta": {...} }
    Mỗi payment method object thường có type/card fields.
    """
    methods = paddle_list_resp.get("data") or []
    if not methods:
        return None

    # ưu tiên default nếu payload có field default/primary
    default = None
    for m in methods:
        if m.get("default") is True or m.get("is_default") is True:
            default = m
            break

    chosen = default or methods[0]

    # Card example: chosen["card"]["last4"], chosen["card"]["expiry_month"], chosen["card"]["expiry_year"], chosen["card"]["brand"]
    card = chosen.get("card") or {}

    return {
        "payment_method_id": chosen.get("id"),  # paymtd_...
        "type": chosen.get("type"),
        "brand": card.get("brand"),
        "last4": card.get("last4"),
        "exp_month": card.get("expiry_month"),
        "exp_year": card.get("expiry_year"),
    }


class PaddleClient:
    """
    Thin wrapper around the official Paddle Billing Python SDK (paddle-python-sdk)
    plus some helpers.
    """

    def __init__(self) -> None:
        if not settings.PADDLE_API_KEY:
            raise RuntimeError("PADDLE_API_KEY is not configured")

        env = (settings.PADDLE_ENV or "production").lower()
        options = (
            Options(Environment.SANDBOX)
            if env == "sandbox"
            else Options(Environment.PRODUCTION)
        )
        self._client = PaddleSDKClient(settings.PADDLE_API_KEY, options=options)

    async def get_customer_by_email(self, email: str):
        def _run():
            try:
                it = self._client.customers.list(email=email)
                for c in it:
                    if getattr(c, "email", "").lower() == email.lower():
                        return c
                return None
            except TypeError:
                it = self._client.customers.list()
                for c in it:
                    if getattr(c, "email", "").lower() == email.lower():
                        return c
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)

    async def create_or_get_customer(
        self, email: str, custom_data: Optional[Dict[str, Any]] = None
    ):
        def _create():
            op = CreateCustomer(email=email, custom_data=custom_data or {})
            return self._client.customers.create(op)

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, _create)
        except ApiError as e:
            if getattr(e, "error_code", None) == "customer_already_exists":
                existing = await self.get_customer_by_email(email)
                if existing:
                    return existing
            detail = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
            errors = getattr(e, "errors", None)
            raise RuntimeError(
                f"Paddle API error (create_customer): {e.error_code} {detail} {errors}"
            ) from e

    async def create_address(
        self, customer_id: str, country_code: str, postal_code: str
    ):
        def _run():
            op = CreateAddress(country_code=country_code, postal_code=postal_code)
            return self._client.addresses.create(customer_id, op)

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, _run)
        except ApiError as e:
            detail = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
            errors = getattr(e, "errors", None)
            raise RuntimeError(
                f"Paddle API error (create_address): {e.error_code} {detail} {errors}"
            ) from e

    async def create_transaction(self, operation: CreateTransaction):
        def _run():
            return self._client.transactions.create(operation)

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, _run)
        except ApiError as e:
            detail = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
            errors = getattr(e, "errors", None)
            raise RuntimeError(
                f"Paddle API error (create_transaction): {e.error_code} {detail} {errors}"
            ) from e

    @staticmethod
    def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
        """
        Hookdeck HMAC verification helper (not Paddle-Signature).
        """
        if not payload or not signature or not secret:
            return False
        digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        expected_hex = digest.hex()
        expected_b64 = base64.b64encode(digest).decode("utf-8")
        provided = signature.strip()
        return hmac.compare_digest(provided, expected_hex) or hmac.compare_digest(
            provided, expected_b64
        )
