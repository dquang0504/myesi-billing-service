# app/api/paddle_webhook_routes.py
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import BillingEvent, PaymentAudit
from app.db.session import get_db
from app.utils.extract_client_info import extract_client_info
from app.api.billing_routes import _resolve_org_id, notify_payment
from app.utils.paddle_client import (
    fetch_paddle_invoice_pdf_url,
    fetch_paddle_transaction_details,
)

router = APIRouter(prefix="/api/billing/paddle", tags=["Paddle Webhook"])
logger = logging.getLogger("paddle_webhook")


# ----------------------------
# Signature verification (Paddle)
# ----------------------------


def _parse_paddle_sig(header: str) -> Tuple[str, list[str]]:
    # Format: ts=...;h1=... (sometimes multiple h1=... if rotating)
    parts = [p.strip() for p in header.split(";") if p.strip()]
    kv = dict(p.split("=", 1) for p in parts if "=" in p)
    ts = kv.get("ts") or ""
    h1s = [p.split("=", 1)[1] for p in parts if p.startswith("h1=")]
    return ts, h1s


async def _update_subscription_paddle_customer_id(
    db: AsyncSession,
    *,
    subscription_db_id: int,
    paddle_customer_id: str,
) -> None:
    if not subscription_db_id or not paddle_customer_id:
        return

    await db.execute(
        text(
            """
            UPDATE subscriptions
            SET paddle_customer_id = COALESCE(paddle_customer_id, :pcid),
                updated_at = NOW()
            WHERE id = :sid
            """
        ),
        {"pcid": paddle_customer_id, "sid": subscription_db_id},
    )
    await db.commit()


def verify_paddle_signature(
    raw: bytes, header: str, secret: str, tolerance_sec: int = 300
) -> bool:
    """
    Paddle signature: HMAC-SHA256(secret, f"{ts}:{raw_body}") => hex, compare to h1.
    Hookdeck adds latency; use tolerance (e.g. 300s).
    """
    if not raw or not header or not secret:
        return False

    ts, h1s = _parse_paddle_sig(header)
    if not ts or not h1s:
        return False

    try:
        if abs(int(time.time()) - int(ts)) > int(tolerance_sec):
            return False
    except (TypeError, ValueError):
        return False

    signed_payload = ts.encode("utf-8") + b":" + raw
    expected = hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()

    return any(hmac.compare_digest(expected, h1) for h1 in h1s)


# ----------------------------
# Payload helpers
# ----------------------------


def _parse_event(payload: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    event_id = payload.get("event_id") or payload.get("id") or ""
    event_type = payload.get("event_type") or payload.get("type") or ""
    data = payload.get("data") or {}
    return event_id, event_type, data


def _to_cents(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value * 100))
    if isinstance(value, str):
        try:
            if "." in value:
                return int(round(float(value) * 100))
            return int(value)
        except ValueError:
            return 0
    return 0


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _extract_context_from_custom_data(
    data: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    cd = data.get("custom_data") or payload.get("custom_data") or {}
    tax_breakdown = cd.get("tax_breakdown") or {}
    return {
        "actor_id": _safe_int(cd.get("actor_id")),
        "org_id": _safe_int(cd.get("org_id")),
        "plan_id": _safe_int(cd.get("plan_id")),
        "interval": cd.get("interval") or "monthly",
        "subtotal_cents": _to_cents(cd.get("subtotal_cents")),
        "tax_cents": _to_cents(cd.get("tax_cents") or tax_breakdown.get("tax_cents")),
        "total_cents": _to_cents(cd.get("total_cents")),
        "tax_rate_percent": cd.get("tax_rate_percent")
        or tax_breakdown.get("tax_rate_percent"),
        "billing_address_id": _safe_int(cd.get("billing_address_id")),
    }


# ----------------------------
# DB lookup helpers
# ----------------------------


async def _lookup_existing_subscription_row(
    db: AsyncSession, paddle_subscription_id: str
) -> Optional[Dict[str, Any]]:
    res = await db.execute(
        text(
            """
            SELECT id, created_by, plan_id, interval, status
            FROM subscriptions
            WHERE paddle_subscription_id = :sid
            """
        ),
        {"sid": paddle_subscription_id},
    )
    row = res.mappings().first()
    return dict(row) if row else None


async def _lookup_org_id_by_subscription_db_id(
    db: AsyncSession, subscription_db_id: int
) -> Optional[int]:
    res = await db.execute(
        text("SELECT id FROM organizations WHERE subscription_id = :sid LIMIT 1"),
        {"sid": subscription_db_id},
    )
    return res.scalar_one_or_none()


async def _lookup_subscription_db_id_by_paddle_id(
    db: AsyncSession, paddle_subscription_id: str
) -> Optional[int]:
    res = await db.execute(
        text("SELECT id FROM subscriptions WHERE paddle_subscription_id = :sid"),
        {"sid": paddle_subscription_id},
    )
    return res.scalar_one_or_none()


# ----------------------------
# Core writes
# ----------------------------


async def _upsert_subscription_strong(
    db: AsyncSession,
    *,
    actor_id: Optional[int],
    plan_id: Optional[int],
    status: str,
    interval: str,
    paddle_subscription_id: str,
    current_period_start: Optional[datetime],
    current_period_end: Optional[datetime],
) -> Optional[int]:
    """
    Upsert subscription but NEVER allow downgrade from 'active' -> 'pending'.
    Also prevents overwriting good data with NULLs via COALESCE.
    """
    status = (status or "").lower()
    interval = interval or "monthly"

    await db.execute(
        text(
            """
            INSERT INTO subscriptions (
                created_by, last_updated_by, billing_contact_user_id,
                plan_id, status, interval, paddle_subscription_id,
                current_period_start, current_period_end, provider,
                created_at, updated_at
            )
            VALUES (
                :actor, :actor, :actor,
                :plan_id, :status, :interval, :psid,
                :cps, :cpe, 'paddle',
                NOW(), NOW()
            )
            ON CONFLICT (paddle_subscription_id) DO UPDATE
            SET
                status = CASE
                    WHEN subscriptions.status = 'active' AND EXCLUDED.status IN ('pending') THEN subscriptions.status
                    ELSE EXCLUDED.status
                END,
                interval = COALESCE(EXCLUDED.interval, subscriptions.interval),
                plan_id = COALESCE(EXCLUDED.plan_id, subscriptions.plan_id),
                current_period_start = COALESCE(EXCLUDED.current_period_start, subscriptions.current_period_start),
                current_period_end = COALESCE(EXCLUDED.current_period_end, subscriptions.current_period_end),
                last_updated_by = COALESCE(EXCLUDED.last_updated_by, subscriptions.last_updated_by),
                billing_contact_user_id = COALESCE(EXCLUDED.billing_contact_user_id, subscriptions.billing_contact_user_id),
                updated_at = NOW()
            """
        ),
        {
            "actor": actor_id,
            "plan_id": plan_id,
            "status": status,
            "interval": interval,
            "psid": paddle_subscription_id,
            "cps": current_period_start,
            "cpe": current_period_end,
        },
    )
    await db.commit()

    return await _lookup_subscription_db_id_by_paddle_id(db, paddle_subscription_id)


async def _ensure_subscription_exists_only(
    db: AsyncSession,
    *,
    actor_id: Optional[int],
    plan_id: Optional[int],
    interval: str,
    paddle_subscription_id: str,
) -> Optional[int]:
    """
    Create skeleton subscription row ONLY IF missing.
    Critically: DO NOTHING on conflict (no status updates => no downgrade).
    """
    interval = interval or "monthly"

    await db.execute(
        text(
            """
            INSERT INTO subscriptions (
                created_by, last_updated_by, billing_contact_user_id,
                plan_id, status, interval, paddle_subscription_id,
                current_period_start, current_period_end, provider,
                created_at, updated_at
            )
            VALUES (
                :actor, :actor, :actor,
                :plan_id, 'pending', :interval, :psid,
                NULL, NULL, 'paddle',
                NOW(), NOW()
            )
            ON CONFLICT (paddle_subscription_id) DO NOTHING
            """
        ),
        {
            "actor": actor_id,
            "plan_id": plan_id,
            "interval": interval,
            "psid": paddle_subscription_id,
        },
    )
    await db.commit()

    return await _lookup_subscription_db_id_by_paddle_id(db, paddle_subscription_id)


async def _link_org_subscription(
    db: AsyncSession, org_id: int, subscription_db_id: int
) -> None:
    await db.execute(
        text("UPDATE organizations SET subscription_id = :sid WHERE id = :oid"),
        {"sid": subscription_db_id, "oid": org_id},
    )
    await db.commit()


async def _upsert_invoice_terminal(
    db: AsyncSession,
    *,
    actor_id: Optional[int],
    subscription_db_id: Optional[int],
    paddle_transaction_id: str,
    paddle_invoice_id: Optional[str],
    status: str,
    currency: str,
    subtotal_cents: int,
    tax_cents: int,
    total_cents: int,
    tax_rate_percent: Optional[Any],
    period_start: Optional[datetime],
    period_end: Optional[datetime],
) -> None:
    """
    Create/update one invoice per paddle_transaction_id for terminal-ish states.
    NOTE: Requires a UNIQUE constraint or unique index on invoices.paddle_transaction_id.
    """
    status = (status or "").lower()
    currency = (currency or "usd").lower()

    await db.execute(
        text(
            """
            INSERT INTO invoices (
                user_id,
                stripe_invoice_id,
                paddle_transaction_id,
                paddle_invoice_id,
                subscription_id,
                amount_due_cents,
                amount_paid_cents,
                currency,
                status,
                invoice_pdf_url,
                hosted_invoice_url,
                period_start,
                period_end,
                subtotal_cents,
                tax_cents,
                total_cents,
                tax_rate_percent,
                created_at
            )
            VALUES (
                :uid,
                NULL,
                :txid,
                :pinv,
                :subid,
                :total,
                :paid,
                :cur,
                :st,
                NULL,
                NULL,
                :pstart,
                :pend,
                NULLIF(:subtotal, 0),
                NULLIF(:tax, 0),
                NULLIF(:total, 0),
                :tax_rate,
                NOW()
            )
            ON CONFLICT (paddle_transaction_id)
            WHERE paddle_transaction_id IS NOT NULL
            DO UPDATE SET
                subscription_id   = COALESCE(EXCLUDED.subscription_id, invoices.subscription_id),
                paddle_invoice_id = COALESCE(EXCLUDED.paddle_invoice_id, invoices.paddle_invoice_id),
                currency          = COALESCE(EXCLUDED.currency, invoices.currency),
                status            = EXCLUDED.status,
                amount_due_cents  = COALESCE(EXCLUDED.amount_due_cents, invoices.amount_due_cents),
                amount_paid_cents = COALESCE(EXCLUDED.amount_paid_cents, invoices.amount_paid_cents),
                period_start      = COALESCE(EXCLUDED.period_start, invoices.period_start),
                period_end        = COALESCE(EXCLUDED.period_end, invoices.period_end),
                subtotal_cents    = COALESCE(EXCLUDED.subtotal_cents, invoices.subtotal_cents),
                tax_cents         = COALESCE(EXCLUDED.tax_cents, invoices.tax_cents),
                total_cents       = COALESCE(EXCLUDED.total_cents, invoices.total_cents),
                tax_rate_percent  = COALESCE(EXCLUDED.tax_rate_percent, invoices.tax_rate_percent)
            """
        ),
        {
            "uid": actor_id,
            "subid": subscription_db_id,
            "txid": paddle_transaction_id,
            "pinv": paddle_invoice_id,
            "total": total_cents,
            "paid": total_cents if status in {"paid", "completed"} else 0,
            "cur": currency,
            "st": status,
            "subtotal": subtotal_cents,
            "tax": tax_cents,
            "tax_rate": tax_rate_percent,
            "pstart": period_start,
            "pend": period_end,
        },
    )
    await db.commit()


async def _update_invoice_urls(
    db: AsyncSession, *, txid: str, pdf_url: Optional[str]
) -> None:
    if not pdf_url:
        return
    await db.execute(
        text(
            """
            UPDATE invoices
            SET invoice_pdf_url = COALESCE(invoice_pdf_url, :pdf),
                hosted_invoice_url = COALESCE(hosted_invoice_url, :pdf)
            WHERE paddle_transaction_id = :txid
            """
        ),
        {"pdf": pdf_url, "txid": txid},
    )
    await db.commit()


# ----------------------------
# Payment method persistence (Paddle)
# ----------------------------


# ----------------------------
# Webhook handler
# ----------------------------


@router.post("/webhook")
async def paddle_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    raw = await request.body()
    sig = request.headers.get("Paddle-Signature")

    if not sig:
        raise HTTPException(status_code=400, detail="Missing Paddle-Signature header")
    if not settings.PADDLE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500, detail="Paddle webhook secret not configured"
        )

    try:
        if not verify_paddle_signature(
            raw, sig, settings.PADDLE_WEBHOOK_SECRET, tolerance_sec=300
        ):
            raise HTTPException(status_code=400, detail="Invalid Paddle signature")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Paddle webhook verification error: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Paddle signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_id, event_type, data = _parse_event(payload)
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event_id")
    if not event_type:
        raise HTTPException(status_code=400, detail="Missing event_type")

    # Idempotency guard
    db.add(BillingEvent(event_id=event_id, payload=payload))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {"status": "already_processed"}

    # Audit
    client_ip, user_agent = extract_client_info(request)
    db.add(
        PaymentAudit(
            actor_id=None,
            action="paddle_webhook_received",
            session_id=event_id,
            details={"event_type": event_type},
            ip_address=client_ip,
            user_agent=user_agent,
        )
    )
    await db.commit()

    ctx = _extract_context_from_custom_data(data, payload)
    actor_id = ctx["actor_id"]
    org_id = ctx["org_id"]
    plan_id = ctx["plan_id"]
    interval = ctx["interval"]

    if not org_id and actor_id:
        org_id = await _resolve_org_id(db, actor_id)

    # ----------------------------
    # subscription.* events (authoritative for status + billing period)
    # ----------------------------
    if event_type.startswith("subscription."):
        paddle_subscription_id = (
            data.get("id")
            or data.get("subscription_id")
            or data.get("subscription")
            or ""
        )
        if not paddle_subscription_id:
            logger.warning("subscription event missing subscription id: %s", event_id)
            return {"status": "ok", "warning": "missing_subscription_id"}

        status = (data.get("status") or event_type.split(".")[-1] or "").lower()

        cbp = (
            data.get("current_billing_period")
            or data.get("current_billing_cycle")
            or {}
        )
        cps = _parse_dt(cbp.get("starts_at"))
        cpe = _parse_dt(cbp.get("ends_at"))

        existing = await _lookup_existing_subscription_row(db, paddle_subscription_id)
        if existing:
            actor_id = actor_id or existing.get("created_by")
            plan_id = plan_id or existing.get("plan_id")
            interval = interval or existing.get("interval")

        sub_db_id = await _upsert_subscription_strong(
            db,
            actor_id=actor_id,
            plan_id=plan_id,
            status=status,
            interval=interval,
            paddle_subscription_id=paddle_subscription_id,
            current_period_start=cps,
            current_period_end=cpe,
        )

        if sub_db_id:
            if not org_id:
                org_id = await _lookup_org_id_by_subscription_db_id(db, sub_db_id)
            if org_id:
                await _link_org_subscription(db, org_id, sub_db_id)

        return {"status": "ok"}

    # ----------------------------
    # transaction.* events (authoritative for money; not for subscription status)
    # ----------------------------
    if event_type.startswith("transaction."):
        txid = data.get("id") or data.get("transaction_id") or ""
        if not txid:
            logger.warning("transaction event missing transaction id: %s", event_id)
            return {"status": "ok", "warning": "missing_transaction_id"}

        paddle_subscription_id = data.get("subscription_id") or data.get("subscription")
        status = (data.get("status") or event_type.split(".")[-1] or "").lower()

        currency = (
            data.get("currency_code")
            or data.get("currency")
            or (data.get("details", {}).get("totals", {}) or {}).get("currency_code")
            or "usd"
        )

        totals = data.get("details", {}).get("totals") or data.get("totals") or {}
        paddle_total = totals.get("total")
        paddle_subtotal = totals.get("subtotal")
        paddle_tax = totals.get("tax")

        subtotal_cents = ctx["subtotal_cents"] or _to_cents(paddle_subtotal)
        tax_cents = ctx["tax_cents"] or _to_cents(paddle_tax)
        total_cents = ctx["total_cents"] or _to_cents(paddle_total)

        # Ensure subscription exists if subscription_id is present, but DO NOT update status here.
        sub_db_id: Optional[int] = None
        if paddle_subscription_id:
            existing = await _lookup_existing_subscription_row(
                db, paddle_subscription_id
            )
            if existing:
                sub_db_id = existing["id"]
            else:
                sub_db_id = await _ensure_subscription_exists_only(
                    db,
                    actor_id=actor_id,
                    plan_id=plan_id,
                    interval=interval,
                    paddle_subscription_id=paddle_subscription_id,
                )
                if sub_db_id and org_id:
                    await _link_org_subscription(db, org_id, sub_db_id)

        # Only act on terminal-ish states
        if status in {"paid", "completed"}:
            # Prefer invoice_id from payload; but sometimes null
            paddle_invoice_id = data.get("invoice_id")
            paddle_customer_id = data.get("customer_id")

            # Save paddle_customer_id into subscription for later convenience
            if sub_db_id and paddle_customer_id:
                try:
                    await _update_subscription_paddle_customer_id(
                        db,
                        subscription_db_id=sub_db_id,
                        paddle_customer_id=paddle_customer_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to store paddle_customer_id on subscription: %s", exc
                    )

            tx_detail: Optional[Dict[str, Any]] = None
            need_tx_detail = (not paddle_invoice_id) or (not paddle_customer_id)

            if need_tx_detail:
                tx_detail = await fetch_paddle_transaction_details(txid)
                if tx_detail:
                    paddle_invoice_id = paddle_invoice_id or tx_detail.get("invoice_id")
                    paddle_customer_id = paddle_customer_id or tx_detail.get(
                        "customer_id"
                    )

            # Derive period window from tx_detail if possible; otherwise leave null
            period_start = None
            period_end = None
            if tx_detail:
                bp = tx_detail.get("billing_period") or {}
                period_start = _parse_dt(
                    bp.get("starts_at") or bp.get("start_at") or bp.get("start")
                )
                period_end = _parse_dt(
                    bp.get("ends_at") or bp.get("end_at") or bp.get("end")
                )

            await _upsert_invoice_terminal(
                db,
                actor_id=actor_id,
                subscription_db_id=sub_db_id,
                paddle_transaction_id=txid,
                paddle_invoice_id=paddle_invoice_id,
                status=status,
                currency=currency,
                subtotal_cents=subtotal_cents,
                tax_cents=tax_cents,
                total_cents=total_cents,
                tax_rate_percent=ctx["tax_rate_percent"],
                period_start=period_start,
                period_end=period_end,
            )

            pdf_url = await fetch_paddle_invoice_pdf_url(txid)
            await _update_invoice_urls(db, txid=txid, pdf_url=pdf_url)

            # Notify success
            if org_id and plan_id:
                await notify_payment(
                    org_id,
                    "success",
                    total_cents,
                    currency.lower(),
                    f"plan_{plan_id}",
                )
        return {"status": "ok"}

    logger.info("Unhandled Paddle event type: %s", event_type)
    return {"status": "ok", "unhandled": event_type}
