import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.billing_routes import _resolve_org_id
from app.services.payment_method_service import (
    get_cached_default_paddle_payment_method,
    get_paddle_customer_id_for_org,
    fetch_and_cache_paddle_default_payment_method,
)

router = APIRouter(prefix="/api/billing", tags=["Payment Methods"])
logger = logging.getLogger("payment_methods")


@router.get("/payment-method")
async def get_payment_method(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user context")

    org_id = await _resolve_org_id(db, int(user_id))

    cached = await get_cached_default_paddle_payment_method(db, org_id)
    if cached and cached.get("brand") and cached.get("last4"):
        return {"provider": "paddle", "payment_method": cached, "source": "cache"}

    paddle_customer_id = await get_paddle_customer_id_for_org(db, org_id)
    if not paddle_customer_id:
        return {
            "provider": "paddle",
            "payment_method": None,
            "source": "missing_customer_id",
        }

    pm = await fetch_and_cache_paddle_default_payment_method(
        db, org_id, paddle_customer_id
    )
    return {
        "provider": "paddle",
        "payment_method": pm,
        "source": "paddle_api" if pm else "unavailable",
    }
