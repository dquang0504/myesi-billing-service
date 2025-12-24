from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any

from app.core.config import settings


def get_tax_rate(jurisdiction: Optional[str] = None) -> float:
    """
    Resolve the tax rate for a given jurisdiction. Falls back to default rate.
    """
    if jurisdiction:
        overridden = settings.TAX_RATE_MAP.get(jurisdiction)
        if overridden is not None:
            return overridden
    return settings.TAX_DEFAULT_RATE


def calculate_tax(
    base_amount_cents: int,
    jurisdiction: Optional[str] = None,
    tax_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute tax for the given base amount (already in cents).
    Returns a dict with subtotal/tax/total and metadata for persistence.
    """
    rate = get_tax_rate(jurisdiction)
    decimal_base = Decimal(base_amount_cents)
    tax_decimal = (decimal_base * Decimal(str(rate))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    tax_cents = int(tax_decimal)
    total_cents = base_amount_cents + tax_cents

    return {
        "subtotal_cents": base_amount_cents,
        "tax_cents": tax_cents,
        "total_cents": total_cents,
        "tax_rate_percent": float(rate * 100),
        "tax_code": tax_code or settings.TAX_DEFAULT_CODE,
        "tax_jurisdiction": jurisdiction or settings.TAX_DEFAULT_JURISDICTION,
    }
