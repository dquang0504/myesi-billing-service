import hashlib
import hmac
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

from app.core.config import settings


class LemonSqueezyClient:
    """
    Minimal async helper for Lemon Squeezy's JSON:API.

    The billing service talks to Lemon Squeezy by creating hosted checkout
    sessions (PCI compliance) and by validating webhook payloads for
    subscription lifecycle events.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        store_id: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.LEMONSQUEEZY_API_KEY
        self.store_id = store_id or settings.LEMONSQUEEZY_STORE_ID
        self.api_base = (api_base or settings.LEMONSQUEEZY_API_BASE).rstrip("/")
        if not self.api_key:
            raise RuntimeError("LEMONSQUEEZY_API_KEY is required")
        if not self.store_id:
            raise RuntimeError("LEMONSQUEEZY_STORE_ID is required")

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Bearer {self.api_key}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Perform a JSON:API call and raise HTTPException on errors so routes
        can surface coherent responses.
        """
        url = f"{self.api_base}{path}"
        async with httpx.AsyncClient(timeout=30.0, headers=self._headers) as client:
            resp = await client.request(method, url, json=payload, params=query)

        if resp.status_code >= 400:
            detail: Any
            try:
                parsed = resp.json()
                detail = parsed.get("errors", parsed)
            except ValueError:
                detail = resp.text
            raise HTTPException(
                status_code=resp.status_code,
                detail={"message": "Lemon Squeezy request failed", "errors": detail},
            )

        return resp.json()

    async def create_checkout_session(
        self,
        variant_id: Optional[str] = None,
        *,
        customer_email: Optional[str] = None,
        custom_price_cents: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
        checkout_data: Optional[Dict[str, Any]] = None,
        test_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Create a hosted checkout that will redirect back to the dashboard.

        Returns the Lemon Squeezy payload which includes hosted checkout URL.
        """
        resolved_variant = variant_id or settings.LEMONSQUEEZY_DEFAULT_VARIANT_ID
        if not resolved_variant:
            raise HTTPException(400, "Variant ID must be supplied for checkout")

        attributes: Dict[str, Any] = {
            "checkout_data": checkout_data or {},
            "product_options": {
                "redirect_url": success_url
                or settings.LEMONSQUEEZY_CHECKOUT_SUCCESS_URL,
                "cancel_url": cancel_url or settings.LEMONSQUEEZY_CHECKOUT_CANCEL_URL,
            },
        }
        if customer_email:
            attributes["checkout_data"].setdefault("email", customer_email)
        if custom_price_cents is not None:
            attributes["custom_price"] = custom_price_cents
        if metadata:
            attributes["checkout_data"].setdefault("custom", metadata)
        if test_mode is not None:
            attributes["test_mode"] = test_mode

        payload = {
            "data": {
                "type": "checkouts",
                "attributes": attributes,
                "relationships": {
                    "store": {
                        "data": {"type": "stores", "id": str(self.store_id)},
                    },
                    "variant": {
                        "data": {"type": "variants", "id": str(resolved_variant)},
                    },
                },
            }
        }

        return await self._request("POST", "/checkouts", payload=payload)

    async def get_variant(self, variant_id: str) -> Dict[str, Any]:
        """
        Fetch a Lemon Squeezy variant so the service can introspect pricing.
        """
        if not variant_id:
            raise HTTPException(400, "variant_id is required")
        return await self._request("GET", f"/variants/{variant_id}")

    @staticmethod
    def verify_webhook_signature(body: bytes, signature: Optional[str]) -> bool:
        """
        Validate X-Signature header using the configured webhook secret.
        """
        secret = settings.LEMONSQUEEZY_WEBHOOK_SECRET
        if not secret or not signature:
            return False
        mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
        digest = mac.hexdigest()
        return hmac.compare_digest(digest, signature)


lemonsqueezy_client = LemonSqueezyClient()
