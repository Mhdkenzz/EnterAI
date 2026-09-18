"""Phase 15 — Enterprise Controls & Compliance Hardening models."""
from datetime import datetime, timezone
from .database import Base
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

def now():
    return datetime.now(timezone.utc)

class IPAllowlist(Base):
    __tablename__ = "ip_allowlists"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: __import__('uuid').uuid4().hex)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    cidr: Mapped[str] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class SessionPolicy(Base):
    __tablename__ = "session_policies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: __import__('uuid').uuid4().hex)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True, index=True)
    max_session_days: Mapped[int] = mapped_column(Integer, default=7)
    sso_only_enforced: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class EnterpriseAuditAggregation(Base):
    """Materialized aggregation for large-volume audit queries (SIEM/export)."""
    __tablename__ = "enterprise_audit_aggregations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: __import__('uuid').uuid4().hex)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    date_bucket: Mapped[str] = mapped_column(String(20), index=True)  # YYYY-MM-DD
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=now)
