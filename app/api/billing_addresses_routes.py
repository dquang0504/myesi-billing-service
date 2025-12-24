import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.billing_routes import _resolve_org_id
from app.db.models import PaymentAudit
from app.db.session import get_db
from app.services.billing_address_service import (
    create_address,
    list_active_addresses,
    set_default_address,
    soft_delete_address,
    update_address,
)
from app.utils.extract_client_info import extract_client_info


router = APIRouter(prefix="/api/billing", tags=["Billing Addresses"])
logger = logging.getLogger("billing_addresses")


@router.get("/billing-addresses")
async def get_billing_addresses(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user context")
    org_id = await _resolve_org_id(db, int(user_id))
    return await list_active_addresses(db, org_id)


@router.post("/billing-addresses")
async def create_billing_address(
    payload: dict, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user context")
    actor_id = int(user_id)
    org_id = await _resolve_org_id(db, actor_id)
    client_ip, user_agent = extract_client_info(request)

    address = await create_address(
        db,
        org_id,
        label=payload.get("label"),
        country_code=payload.get("country_code"),
        postal_code=payload.get("postal_code"),
        make_default=bool(payload.get("make_default", False)),
        created_by=actor_id,
    )
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="billing_address_created",
            details={"address_id": address["id"], "organization_id": org_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()
    return address


@router.put("/billing-addresses/{address_id}")
async def update_billing_address(
    address_id: int, payload: dict, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user context")
    actor_id = int(user_id)
    org_id = await _resolve_org_id(db, actor_id)
    client_ip, user_agent = extract_client_info(request)

    address = await update_address(
        db,
        org_id,
        address_id,
        label=payload.get("label"),
        country_code=payload.get("country_code"),
        postal_code=payload.get("postal_code"),
        make_default=bool(payload.get("make_default", False)),
    )
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="billing_address_updated",
            details={"address_id": address["id"], "organization_id": org_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()
    return address


@router.post("/billing-addresses/{address_id}/default")
async def set_billing_address_default(
    address_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user context")
    actor_id = int(user_id)
    org_id = await _resolve_org_id(db, actor_id)
    client_ip, user_agent = extract_client_info(request)

    address = await set_default_address(db, org_id, address_id)
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="billing_address_set_default",
            details={"address_id": address["id"], "organization_id": org_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()
    return address


@router.delete("/billing-addresses/{address_id}")
async def delete_billing_address(
    address_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user context")
    actor_id = int(user_id)
    org_id = await _resolve_org_id(db, actor_id)
    client_ip, user_agent = extract_client_info(request)

    await soft_delete_address(db, org_id, address_id)
    db.add(
        PaymentAudit(
            actor_id=actor_id,
            action="billing_address_deleted",
            details={"address_id": address_id, "organization_id": org_id},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()
    return {"success": True}
