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
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)
    stripe_price_id_monthly = Column(String(255), nullable=False)
    stripe_price_id_yearly = Column(String(255), nullable=False)
    stripe_product_id = Column(String(255))
    sbom_limit = Column(Integer, default=10)
    user_limit = Column(Integer, default=5)
    monthly_price_cents = Column(Integer, nullable=False)
    annual_price_cents = Column(Integer, nullable=False)
    currency = Column(String(10), default="usd")
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    subscriptions = relationship("Subscription", back_populates="plan")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"))
    stripe_customer_id = Column(String(255))
    stripe_subscription_id = Column(String(255), unique=True)
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
    stripe_invoice_id = Column(String(255), unique=True, nullable=False)
    subscription_id = Column(
        Integer, ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    amount_due_cents = Column(Integer, nullable=False)
    amount_paid_cents = Column(Integer, default=0)
    currency = Column(String(10), default="usd")
    invoice_pdf_url = Column(Text)
    status = Column(String(50), default="draft")
    hosted_invoice_url = Column(Text)
    period_start = Column(TIMESTAMP)
    period_end = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    subscription = relationship("Subscription", back_populates="invoices")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    stripe_payment_method_id = Column(String(255), unique=True, nullable=False)
    brand = Column(String(50))
    last4 = Column(String(4))
    exp_month = Column(Integer)
    exp_year = Column(Integer)
    is_default = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
