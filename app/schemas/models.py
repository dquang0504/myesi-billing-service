from pydantic import BaseModel, EmailStr
from typing import Literal, Optional


class UpdateSubRequest(BaseModel):
    action: str  # "upgrade" | "downgrade" | "cycle"
    planId: int
    interval: str | None = None
    stripeSubscriptionId: str
    customerEmail: EmailStr
    customerId: int
    targetPlanId: int


class CancelSubscriptionRequest(BaseModel):
    mode: Literal["cycle_end", "immediate"] = "cycle_end"
    refund: Optional[Literal["full", "prorated", "none"]] = "none"
