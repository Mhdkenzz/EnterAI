"""Retention and deletion-workflow tables.

DeletionRequest.organization_id/target_id are deliberately plain strings with no
ForeignKey, matching AuditEvent's own rule (see models.py): a completed
organization deletion removes the Organization row itself, and the request that
led to it must still read back afterward as a historical record, not break or
cascade-delete with its target.
"""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import uid, now


class RetentionPolicy(Base):
    """One row per organization. Each *_retention_days is nullable -- null means
    "keep forever" for that category, since not every org wants every category
    purged automatically."""
    __tablename__ = "retention_policies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True, index=True)
    chat_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audit_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class DeletionRequest(Base):
    __tablename__ = "deletion_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String, index=True)
    requested_by: Mapped[str] = mapped_column(String)
    target_type: Mapped[str] = mapped_column(String(20))  # "user" | "organization"
    target_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|canceled|completed
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
