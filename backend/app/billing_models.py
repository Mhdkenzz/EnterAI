from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Numeric, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import uid, now

class BillingPlan(Base):
    __tablename__ = "billing_plans"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    stripe_plan_id: Mapped[str] = mapped_column(String(255), unique=True)
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    seat_limit: Mapped[int] = mapped_column(Integer, nullable=True)
    ai_usage_limit: Mapped[int] = mapped_column(Integer, nullable=True)  # monthly AI calls
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class OrganizationSubscription(Base):
    __tablename__ = "organization_subscriptions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String, ForeignKey("organizations.id"), index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(255), nullable=True)
    plan_id: Mapped[str] = mapped_column(String, ForeignKey("billing_plans.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="trial")  # trial, active, past_due, canceled, incomplete
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String, ForeignKey("organizations.id"), index=True)
    metric: Mapped[str] = mapped_column(String(50))  # ai_calls, seats
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    period_start: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class BillingWebHookEvent(Base):
    __tablename__ = "billing_webhook_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    stripe_event_id: Mapped[str] = mapped_column(String(255), unique=True)
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
