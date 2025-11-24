from pydantic import BaseModel, EmailStr


class UpdateSubRequest(BaseModel):
    action: str  # "upgrade" | "downgrade" | "cycle"
    planId: int
    interval: str | None = None
    stripeSubscriptionId: str
    customerEmail: EmailStr
    customerId: int
    targetPlanId: int
