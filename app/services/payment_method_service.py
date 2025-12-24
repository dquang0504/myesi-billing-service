from __future__ import annotations

from typing import Any, Dict, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.paddle_client import (
    fetch_paddle_customer_payment_methods,
    extract_default_payment_method_summary,
)


async def get_cached_default_paddle_payment_method(
    db: AsyncSession, org_id: int
) -> Optional[Dict[str, Any]]:
    res = await db.execute(
        text(
            """
            SELECT brand, last4, exp_month, exp_year, paddle_customer_id, paddle_payment_method_id
            FROM payment_methods
            WHERE organization_id = :org_id
              AND provider = 'paddle'
              AND is_default = TRUE
            LIMIT 1
        """
        ),
        {"org_id": org_id},
    )
    row = res.mappings().first()
    return dict(row) if row else None


async def get_paddle_customer_id_for_org(
    db: AsyncSession, org_id: int
) -> Optional[str]:
    res = await db.execute(
        text(
            """
            SELECT s.paddle_customer_id
            FROM organizations o
            JOIN subscriptions s ON s.id = o.subscription_id
            WHERE o.id = :org_id
            LIMIT 1
        """
        ),
        {"org_id": org_id},
    )
    return res.scalar_one_or_none()


async def fetch_and_cache_paddle_default_payment_method(
    db: AsyncSession,
    org_id: int,
    paddle_customer_id: str,
) -> Optional[Dict[str, Any]]:
    payload = await fetch_paddle_customer_payment_methods(paddle_customer_id)
    if not payload:
        return None

    pm = extract_default_payment_method_summary(payload)
    if not pm:
        return None

    await db.execute(
        text(
            """
            INSERT INTO payment_methods (
                organization_id,
                provider,
                paddle_customer_id,
                paddle_payment_method_id,
                brand,
                last4,
                exp_month,
                exp_year,
                is_default,
                created_at
            ) VALUES (
                :org_id,
                'paddle',
                :paddle_customer_id,
                :paddle_payment_method_id,
                :brand,
                :last4,
                :exp_month,
                :exp_year,
                TRUE,
                NOW()
            )
            ON CONFLICT (provider, paddle_customer_id, paddle_payment_method_id) WHERE provider = 'paddle' DO UPDATE SET
                brand      = COALESCE(EXCLUDED.brand, payment_methods.brand),
                last4      = COALESCE(EXCLUDED.last4, payment_methods.last4),
                exp_month  = COALESCE(EXCLUDED.exp_month, payment_methods.exp_month),
                exp_year   = COALESCE(EXCLUDED.exp_year, payment_methods.exp_year),
                is_default = TRUE
        """
        ),
        {
            "org_id": org_id,
            "paddle_customer_id": paddle_customer_id,
            "paddle_payment_method_id": pm.get("payment_method_id"),
            "brand": pm.get("brand"),
            "last4": pm.get("last4"),
            "exp_month": pm.get("exp_month"),
            "exp_year": pm.get("exp_year"),
        },
    )
    await db.commit()
    return pm


async def get_latest_paddle_transaction_id_for_org(
    db: AsyncSession, org_id: int
) -> Optional[str]:
    res = await db.execute(
        text(
            """
            SELECT i.paddle_transaction_id
            FROM invoices i
            JOIN subscriptions s ON s.id = i.subscription_id
            JOIN organizations o ON o.subscription_id = s.id
            WHERE o.id = :org_id
              AND i.paddle_transaction_id IS NOT NULL
            ORDER BY i.created_at DESC
            LIMIT 1
        """
        ),
        {"org_id": org_id},
    )
    return res.scalar_one_or_none()
