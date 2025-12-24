from datetime import datetime
import uuid
from pydantic import BaseModel, EmailStr
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
    Numeric,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship


# --- Request Schema ---
class CheckoutRequest(BaseModel):
    actor_id: int
    email: EmailStr
    amount: int


Base = declarative_base()


class CheckoutRecord(Base):
    __tablename__ = "checkout_records"

    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    session_id = Column(String(255), unique=True, nullable=False)
    customer_email = Column(String(255), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    currency = Column(String(10), default="usd")
    status = Column(String(50), default="created", index=True)
    idempotency_key = Column(PGUUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    raw_session = Column(JSON)
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))
    updated_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(255), unique=True, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(100), default="developer", nullable=False)
    status = Column(String(50), default="active", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True), nullable=True)


class PaymentAudit(Base):
    __tablename__ = "payment_audit"

    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    action = Column(String(255), nullable=False)
    session_id = Column(String(255))
    details = Column(JSON)
    ip_address = Column(INET)
    user_agent = Column(String)
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)

    # Plan identity
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)

    # Stripe mapping
    stripe_price_id_monthly = Column(String(255), nullable=False)
    stripe_price_id_yearly = Column(String(255), nullable=False)
    stripe_product_id = Column(String(255))
    paddle_price_id_monthly = Column(String(255))
    paddle_price_id_yearly = Column(String(255))
    paddle_product_id = Column(String(255))

    # Limits (these are used by /plans, /subscription, /usage)
    sbom_limit = Column(Integer, nullable=False, default=10)
    project_scan_limit = Column(Integer, nullable=False, default=10)
    scan_rate_limit = Column(Integer, nullable=False, default=60)
    user_limit = Column(Integer, nullable=False, default=5)

    # Pricing
    monthly_price_cents = Column(Integer, nullable=False)
    annual_price_cents = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default="usd")

    # State + timestamps
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    subscriptions = relationship("Subscription", back_populates="plan")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    last_updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    billing_contact_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"))
    stripe_customer_id = Column(String(255))
    stripe_subscription_id = Column(String(255), unique=True)
    paddle_customer_id = Column(String(255))
    paddle_subscription_id = Column(String(255), unique=True)
    provider = Column(String(50), default="stripe")
    status = Column(String(50), default="active")
    current_period_start = Column(TIMESTAMP)
    current_period_end = Column(TIMESTAMP)
    cancel_at_period_end = Column(Boolean, default=False)
    trial_end = Column(TIMESTAMP)
    quantity = Column(Integer, default=1)
    interval = Column(String(10), default="monthly")
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow)

    plan = relationship("SubscriptionPlan", back_populates="subscriptions")
    invoices = relationship("Invoice", back_populates="subscription")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    stripe_invoice_id = Column(String(255), unique=True)
    paddle_transaction_id = Column(String(255))
    paddle_invoice_id = Column(String(255))

    subscription_id = Column(
        Integer, ForeignKey("subscriptions.id", ondelete="CASCADE")
    )

    amount_due_cents = Column(Integer, nullable=False)
    amount_paid_cents = Column(Integer, default=0)
    currency = Column(String(10), default="usd")

    invoice_pdf_url = Column(Text)
    hosted_invoice_url = Column(Text)
    status = Column(String(50), default="draft")

    period_start = Column(TIMESTAMP(timezone=True))
    period_end = Column(TIMESTAMP(timezone=True))

    subtotal_cents = Column(Integer)
    tax_cents = Column(Integer)
    total_cents = Column(Integer)
    tax_rate_percent = Column(Numeric(6, 3))

    tax_code = Column(String(50))
    tax_jurisdiction = Column(String(100))

    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))

    subscription = relationship("Subscription", back_populates="invoices")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id = Column(Integer, primary_key=True)

    # Stripe
    stripe_customer_id = Column(String(255))
    stripe_payment_method_id = Column(String(255), unique=True)

    # Paddle
    paddle_customer_id = Column(String(255))
    paddle_transaction_id = Column(String(255))

    provider = Column(String(50), nullable=False)  # 'stripe' | 'paddle'

    brand = Column(String(50))
    last4 = Column(String(4))
    exp_month = Column(Integer)
    exp_year = Column(Integer)

    is_default = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))


class BillingAddress(Base):
    __tablename__ = "billing_addresses"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, index=True, nullable=False)
    label = Column(String(255))
    country_code = Column(String(2), nullable=False)
    postal_code = Column(String(32), nullable=False)
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CancellationRequest(Base):
    __tablename__ = "cancellation_requests"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(
        Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    stripe_subscription_id = Column(String(255), nullable=False)
    mode = Column(String(50), nullable=False)
    refund_mode = Column(String(50), default="none")
    refund_amount_cents = Column(Integer, default=0)
    refund_currency = Column(String(10), default="usd")
    stripe_refund_id = Column(String(255))
    stripe_invoice_id = Column(String(255))
    payment_intent_id = Column(String(255))
    created_at = Column(TIMESTAMP(timezone=True), server_default=text("NOW()"))
