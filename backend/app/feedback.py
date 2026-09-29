"""Organization-scoped demo feedback and support triage.

Only a small, explicit diagnostics allow-list is stored. Never add request
headers, cookies, authorization values, environment dumps, prompts, uploads, or
exception text to a report: a feedback form is user input, not a debugging
backchannel for secrets.
"""
from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .admin import human_admin
from .auth import current_user
from .database import get_db
from .models import FeedbackReport, User
from .ratelimit import build_rate_limiter
from .services import log

Category = Literal["bug", "usability", "performance", "accessibility", "security", "other"]
Impact = Literal["blocking", "high", "medium", "low"]
FeedbackStatus = Literal["new", "acknowledged", "investigating", "resolved", "closed"]

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
admin_router = APIRouter(prefix="/api/admin/feedback", tags=["admin", "feedback"])
_REPORT_LIMITER = build_rate_limiter(
    int(os.getenv("FEEDBACK_RATE_LIMIT_PER_HOUR", "10")), 3600, "feedback-report"
)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|api[_ -]?key|token|authorization|bearer|cookie)"
    r"\s*[:=]\s*[^\s,;]+"
)
_TOKEN_SHAPES = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,})\b")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class FeedbackCreate(BaseModel):
    category: Category
    impact: Impact
    description: str = Field(min_length=10, max_length=5000)
    page: str | None = Field(default=None, max_length=500)
    client_version: str | None = Field(default=None, max_length=100)


class FeedbackUpdate(BaseModel):
    status: FeedbackStatus | None = None
    assignee_id: str | None = None
    admin_note: str | None = Field(default=None, max_length=4000)


def _redact(value: str) -> str:
    value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _TOKEN_SHAPES.sub("[REDACTED]", value)


def _safe_page(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return path[:160]


def _safe_identifier(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if value and _SAFE_IDENTIFIER.fullmatch(value) else None


def _diagnostics(data: FeedbackCreate, request: Request) -> tuple[str | None, dict]:
    release = _safe_identifier(os.getenv("RELEASE_SHA") or os.getenv("APP_VERSION"))
    page = _safe_page(data.page)
    client_version = _safe_identifier(data.client_version)
    # Deliberately exclude all headers. The API release and normalized page are
    # enough to group demo failures without persisting cookies or credentials.
    return release, {
        "environment": os.getenv("ENVIRONMENT", "development").strip().lower()[:20],
        "release": release or "unknown",
        "page": page,
        "client_version": client_version,
        "api_path": request.url.path,
    }


def _report_out(report: FeedbackReport, reporter: User | None = None, assignee: User | None = None) -> dict:
    return {
        "id": report.id,
        "category": report.category,
        "impact": report.impact,
        "description": report.description,
        "page": report.page,
        "diagnostics": report.diagnostics or {},
        "release_identifier": report.release_identifier,
        "status": report.status,
        "admin_note": report.admin_note,
        "reporter": ({"id": reporter.id, "name": reporter.name, "email": reporter.email}
                     if reporter else None),
        "assignee": ({"id": assignee.id, "name": assignee.name, "email": assignee.email}
                     if assignee else None),
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def submit_feedback(data: FeedbackCreate, request: Request,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not _REPORT_LIMITER.allow(user.id):
        raise HTTPException(429, "Feedback submission limit reached. Try again later.")
    release, diagnostics = _diagnostics(data, request)
    report = FeedbackReport(
        organization_id=user.organization_id,
        reporter_id=user.id,
        category=data.category,
        impact=data.impact,
        description=_redact(data.description.strip()),
        page=_safe_page(data.page),
        diagnostics=diagnostics,
        release_identifier=release,
        status="new",
    )
    db.add(report)
    db.flush()
    log(db, user.organization_id, user.id, "feedback_report", report.id, "submitted",
        category=report.category, impact=report.impact, release=release or "unknown")
    db.commit()
    return _report_out(report)


@router.get("")
def my_feedback(user: User = Depends(current_user), db: Session = Depends(get_db)):
    reports = db.scalars(select(FeedbackReport).where(
        FeedbackReport.organization_id == user.organization_id,
        FeedbackReport.reporter_id == user.id,
    ).order_by(FeedbackReport.created_at.desc(), FeedbackReport.id.desc()).limit(50)).all()
    return [_report_out(report, reporter=user) for report in reports]


@admin_router.get("")
def list_feedback(status_filter: FeedbackStatus | None = Query(default=None, alias="status"),
                  impact: Impact | None = None,
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                  user: User = Depends(human_admin), db: Session = Depends(get_db)):
    stmt = select(FeedbackReport).where(FeedbackReport.organization_id == user.organization_id)
    if status_filter:
        stmt = stmt.where(FeedbackReport.status == status_filter)
    if impact:
        stmt = stmt.where(FeedbackReport.impact == impact)
    reports = db.scalars(stmt.order_by(FeedbackReport.created_at.desc(), FeedbackReport.id.desc())
                         .offset(offset).limit(limit)).all()
    user_ids = {report.reporter_id for report in reports} | {report.assignee_id for report in reports if report.assignee_id}
    people = {person.id: person for person in db.scalars(select(User).where(
        User.organization_id == user.organization_id, User.id.in_(user_ids))).all()} if user_ids else {}
    return {
        "items": [_report_out(report, people.get(report.reporter_id), people.get(report.assignee_id))
                  for report in reports],
        "offset": offset,
        "limit": limit,
    }


@admin_router.patch("/{report_id}")
def update_feedback(report_id: str, data: FeedbackUpdate,
                    user: User = Depends(human_admin), db: Session = Depends(get_db)):
    report = db.scalar(select(FeedbackReport).where(
        FeedbackReport.id == report_id, FeedbackReport.organization_id == user.organization_id))
    if not report:
        raise HTTPException(404, "Feedback report not found")
    if "assignee_id" in data.model_fields_set:
        if data.assignee_id is None:
            report.assignee_id = None
        else:
            assignee = db.scalar(select(User).where(
                User.id == data.assignee_id, User.organization_id == user.organization_id,
                User.kind == "human", User.active.is_(True)))
            if not assignee:
                raise HTTPException(422, "Assignee must be an active human in this organization")
            report.assignee_id = assignee.id
    if data.status is not None:
        report.status = data.status
    if "admin_note" in data.model_fields_set:
        report.admin_note = _redact(data.admin_note.strip()) if data.admin_note else None
    log(db, user.organization_id, user.id, "feedback_report", report.id, "triaged",
        status=report.status, assignee_id=report.assignee_id)
    db.commit()
    reporter = db.scalar(select(User).where(
        User.id == report.reporter_id, User.organization_id == user.organization_id))
    assignee = db.scalar(select(User).where(
        User.id == report.assignee_id, User.organization_id == user.organization_id)) if report.assignee_id else None
    return _report_out(report, reporter, assignee)
