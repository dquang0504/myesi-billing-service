from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _normalize_country(code: str) -> str:
    code = (code or "").strip().upper()
    if len(code) != 2:
        raise HTTPException(status_code=400, detail="country_code must be 2 letters")
    return code


def _normalize_postal(postal: str) -> str:
    postal = (postal or "").strip()
    if not postal:
        raise HTTPException(status_code=400, detail="postal_code is required")
    return postal


async def list_active_addresses(db: AsyncSession, org_id: int) -> List[Dict[str, Any]]:
    rows = await db.execute(
        text(
            """
            SELECT id, label, country_code, postal_code, is_default
            FROM billing_addresses
            WHERE organization_id = :org_id AND is_active = TRUE
            ORDER BY is_default DESC, id DESC
            """
        ),
        {"org_id": org_id},
    )
    return [
        {
            "id": row.id,
            "label": row.label,
            "country_code": row.country_code,
            "postal_code": row.postal_code,
            "is_default": row.is_default,
        }
        for row in rows.fetchall()
    ]


async def get_address_by_id(
    db: AsyncSession, org_id: int, address_id: int
) -> Optional[Dict[str, Any]]:
    row = await db.execute(
        text(
            """
            SELECT id, label, country_code, postal_code, is_default
            FROM billing_addresses
            WHERE id = :id AND organization_id = :org_id AND is_active = TRUE
            """
        ),
        {"id": address_id, "org_id": org_id},
    )
    record = row.fetchone()
    if not record:
        return None
    return {
        "id": record.id,
        "label": record.label,
        "country_code": record.country_code,
        "postal_code": record.postal_code,
        "is_default": record.is_default,
    }


async def get_default_address(
    db: AsyncSession, org_id: int
) -> Optional[Dict[str, Any]]:
    row = await db.execute(
        text(
            """
            SELECT id, label, country_code, postal_code, is_default
            FROM billing_addresses
            WHERE organization_id = :org_id
              AND is_active = TRUE
              AND is_default = TRUE
            LIMIT 1
            """
        ),
        {"org_id": org_id},
    )
    record = row.fetchone()
    if not record:
        return None
    return {
        "id": record.id,
        "label": record.label,
        "country_code": record.country_code,
        "postal_code": record.postal_code,
        "is_default": record.is_default,
    }


async def create_address(
    db: AsyncSession,
    org_id: int,
    *,
    label: Optional[str],
    country_code: str,
    postal_code: str,
    make_default: bool,
    created_by: Optional[int],
) -> Dict[str, Any]:
    country_code = _normalize_country(country_code)
    postal_code = _normalize_postal(postal_code)

    if make_default:
        await db.execute(
            text(
                """
                UPDATE billing_addresses
                SET is_default = FALSE
                WHERE organization_id = :org_id
                """
            ),
            {"org_id": org_id},
        )

    row = await db.execute(
        text(
            """
            INSERT INTO billing_addresses (
                organization_id, label, country_code, postal_code,
                is_default, is_active, created_by, created_at, updated_at
            )
            VALUES (:org_id, :label, :country_code, :postal_code,
                    :is_default, TRUE, :created_by, NOW(), NOW())
            RETURNING id, label, country_code, postal_code, is_default
            """
        ),
        {
            "org_id": org_id,
            "label": label,
            "country_code": country_code,
            "postal_code": postal_code,
            "is_default": make_default,
            "created_by": created_by,
        },
    )
    created = row.fetchone()
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create billing address")
    return {
        "id": created.id,
        "label": created.label,
        "country_code": created.country_code,
        "postal_code": created.postal_code,
        "is_default": created.is_default,
    }


async def update_address(
    db: AsyncSession,
    org_id: int,
    address_id: int,
    *,
    label: Optional[str],
    country_code: str,
    postal_code: str,
    make_default: bool,
) -> Dict[str, Any]:
    country_code = _normalize_country(country_code)
    postal_code = _normalize_postal(postal_code)

    if make_default:
        await db.execute(
            text(
                """
                UPDATE billing_addresses
                SET is_default = FALSE
                WHERE organization_id = :org_id
                """
            ),
            {"org_id": org_id},
        )

    row = await db.execute(
        text(
            """
            UPDATE billing_addresses
            SET label = :label,
                country_code = :country_code,
                postal_code = :postal_code,
                is_default = :is_default,
                updated_at = NOW()
            WHERE id = :id
              AND organization_id = :org_id
              AND is_active = TRUE
            RETURNING id, label, country_code, postal_code, is_default
            """
        ),
        {
            "id": address_id,
            "org_id": org_id,
            "label": label,
            "country_code": country_code,
            "postal_code": postal_code,
            "is_default": make_default,
        },
    )
    updated = row.fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="Billing address not found")
    return {
        "id": updated.id,
        "label": updated.label,
        "country_code": updated.country_code,
        "postal_code": updated.postal_code,
        "is_default": updated.is_default,
    }


async def set_default_address(
    db: AsyncSession, org_id: int, address_id: int
) -> Dict[str, Any]:
    await db.execute(
        text(
            """
            UPDATE billing_addresses
            SET is_default = FALSE
            WHERE organization_id = :org_id
            """
        ),
        {"org_id": org_id},
    )
    row = await db.execute(
        text(
            """
            UPDATE billing_addresses
            SET is_default = TRUE, updated_at = NOW()
            WHERE id = :id
              AND organization_id = :org_id
              AND is_active = TRUE
            RETURNING id, label, country_code, postal_code, is_default
            """
        ),
        {"id": address_id, "org_id": org_id},
    )
    updated = row.fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="Billing address not found")
    return {
        "id": updated.id,
        "label": updated.label,
        "country_code": updated.country_code,
        "postal_code": updated.postal_code,
        "is_default": updated.is_default,
    }


async def soft_delete_address(db: AsyncSession, org_id: int, address_id: int) -> None:
    result = await db.execute(
        text(
            """
            UPDATE billing_addresses
            SET is_active = FALSE, is_default = FALSE, updated_at = NOW()
            WHERE id = :id
              AND organization_id = :org_id
              AND is_active = TRUE
            """
        ),
        {"id": address_id, "org_id": org_id},
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Billing address not found")
