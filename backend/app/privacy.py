"""Self-service and admin privacy endpoints: export, deletion requests, consent,
and retention policy (Phase 13). See privacy_service.py for the underlying logic.

Every export and every deletion request re-checks the caller's password even
though they are already signed in -- a stolen bearer token should not be enough
to walk off with a full data export or start an account deletion clock.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user, verify_password
from .admin import human_admin
from .database import get_db
from .models import Organization, User
from .privacy_models import DeletionRequest, RetentionPolicy
from .services import log
from . import privacy_service

router = APIRouter(prefix="/api/privacy", tags=["privacy"])
admin_router = APIRouter(prefix="/api/admin/privacy", tags=["admin", "privacy"])

CURRENT_TOS_VERSION = "2026-01-01"


class Reauth(BaseModel):
    password: str
    reason: str | None = Field(default=None, max_length=500)


def _reauth(user: User, data: Reauth) -> None:
    if not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Password confirmation required")


def _json_download(payload: dict, filename: str) -> Response:
    import json
    return Response(content=json.dumps(payload, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------- #
# Self-service
# --------------------------------------------------------------------------- #

@router.post("/export")
def export_my_data(data: Reauth, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _reauth(user, data)
    payload = privacy_service.export_user_data(db, user)
    log(db, user.organization_id, user.id, "user", user.id, "data_exported")
    db.commit()
    return _json_download(payload, f"enterai-export-{user.id}.json")


@router.post("/delete-account", status_code=201)
def delete_my_account(data: Reauth, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _reauth(user, data)
    request = privacy_service.request_deletion(
        db, organization_id=user.organization_id, requested_by=user.id,
        target_type="user", target_id=user.id, reason=data.reason)
    db.commit()
    return _deletion_request_out(request)


@router.get("/deletion-status")
def my_deletion_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    request = db.scalars(select(DeletionRequest).where(
        DeletionRequest.target_type == "user", DeletionRequest.target_id == user.id,
        DeletionRequest.status == "pending")).first()
    return _deletion_request_out(request) if request else None


@router.post("/deletion-requests/{request_id}/cancel")
def cancel_my_deletion(request_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    request = db.get(DeletionRequest, request_id)
    if not request or request.target_type != "user" or request.target_id != user.id:
        raise HTTPException(404, "Deletion request not found")
    try:
        privacy_service.cancel_deletion(db, request, actor_id=user.id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    db.commit()
    return _deletion_request_out(request)


class TosAcceptance(BaseModel):
    version: str = Field(default=CURRENT_TOS_VERSION, max_length=20)


@router.post("/accept-tos")
def accept_tos(data: TosAcceptance, user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.tos_accepted_at = datetime.now(timezone.utc)
    user.tos_version = data.version
    db.commit()
    return {"tos_accepted_at": user.tos_accepted_at, "tos_version": user.tos_version}


# --------------------------------------------------------------------------- #
# Admin (organization-scoped)
# --------------------------------------------------------------------------- #

def _deletion_request_out(r: DeletionRequest | None):
    if r is None:
        return None
    return {"id": r.id, "target_type": r.target_type, "target_id": r.target_id, "status": r.status,
            "reason": r.reason, "requested_at": r.requested_at, "scheduled_for": r.scheduled_for,
            "completed_at": r.completed_at, "canceled_at": r.canceled_at}


@admin_router.post("/export")
def export_organization(data: Reauth, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    _reauth(user, data)
    payload = privacy_service.export_organization_data(db, user.organization_id)
    log(db, user.organization_id, user.id, "organization", user.organization_id, "data_exported")
    db.commit()
    return _json_download(payload, f"enterai-export-{user.organization_id}.json")


@admin_router.post("/delete-organization", status_code=201)
def delete_organization(data: Reauth, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    _reauth(user, data)
    request = privacy_service.request_deletion(
        db, organization_id=user.organization_id, requested_by=user.id,
        target_type="organization", target_id=user.organization_id, reason=data.reason)
    db.commit()
    return _deletion_request_out(request)


@admin_router.get("/deletion-requests")
def list_deletion_requests(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(DeletionRequest).where(DeletionRequest.organization_id == user.organization_id)
                      .order_by(DeletionRequest.requested_at.desc())).all()
    return [_deletion_request_out(r) for r in rows]


@admin_router.post("/deletion-requests/{request_id}/cancel")
def cancel_deletion_request(request_id: str, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    request = db.get(DeletionRequest, request_id)
    if not request or request.organization_id != user.organization_id:
        raise HTTPException(404, "Deletion request not found")
    try:
        privacy_service.cancel_deletion(db, request, actor_id=user.id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    db.commit()
    return _deletion_request_out(request)


class RetentionPolicyIn(BaseModel):
    chat_retention_days: int | None = Field(default=None, ge=1)
    audit_retention_days: int | None = Field(default=None, ge=1)
    activity_retention_days: int | None = Field(default=None, ge=1)
    document_retention_days: int | None = Field(default=None, ge=1)


def _retention_out(p: RetentionPolicy):
    return {"chat_retention_days": p.chat_retention_days, "audit_retention_days": p.audit_retention_days,
            "activity_retention_days": p.activity_retention_days, "document_retention_days": p.document_retention_days,
            "updated_at": p.updated_at}


def _get_or_create_policy(db: Session, organization_id: str) -> RetentionPolicy:
    policy = db.scalars(select(RetentionPolicy).where(RetentionPolicy.organization_id == organization_id)).first()
    if policy is None:
        policy = RetentionPolicy(organization_id=organization_id)
        db.add(policy)
        db.flush()
    return policy


@admin_router.get("/retention")
def get_retention_policy(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    return _retention_out(_get_or_create_policy(db, user.organization_id))


@admin_router.patch("/retention")
def update_retention_policy(data: RetentionPolicyIn, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    policy = _get_or_create_policy(db, user.organization_id)
    for field, value in data.model_dump().items():
        setattr(policy, field, value)
    log(db, user.organization_id, user.id, "organization", user.organization_id, "retention_policy_updated")
    db.commit()
    return _retention_out(policy)


@admin_router.post("/run-retention-cleanup")
def run_retention_cleanup_now(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    totals = privacy_service.run_retention_cleanup(db, organization_id=user.organization_id)
    log(db, user.organization_id, user.id, "organization", user.organization_id, "retention_cleanup_run")
    db.commit()
    return totals
