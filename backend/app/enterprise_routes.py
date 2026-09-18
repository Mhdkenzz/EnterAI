"""Phase 15 — Complete enterprise controls routes (admin, SIEM, audit export, policies)."""
import hashlib
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session
from .admin import human_admin as admin
from .database import get_db
from .models import User, Organization, AuditEvent
from .enterprise_models import IPAllowlist, SessionPolicy, EnterpriseAuditAggregation
from .services import log

router = APIRouter(prefix='/api/admin/enterprise', tags=['enterprise'])

# --- Audit / SIEM ---

@router.get('/audit/export')
def audit_export(user: User = Depends(admin), db: Session = Depends(get_db),
                 start: str | None = None, end: str | None = None, limit: int = 1000):
    # Raw-SQL audit aggregation by date/action for SIEM export (read-only, no mutation allowed)
    stmt = select(AuditEvent).where(AuditEvent.organization_id == user.organization_id)
    # Audit immutability: this endpoint is read-only; the underlying AuditEvent table has no mutation routes.
    rows = db.scalars(stmt.order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    return [{'id': r.id, 'action': r.action, 'entity_type': r.entity_type,
             'actor_id': r.actor_id, 'entity_id': r.entity_id, 'detail': r.detail,
             'created_at': r.created_at.isoformat() if r.created_at else None} for r in rows]

@router.get('/audit/aggregation')
def audit_aggregation(user: User = Depends(admin), db: Session = Depends(get_db)):
    agg_rows = db.scalars(select(EnterpriseAuditAggregation)
                          .where(EnterpriseAuditAggregation.organization_id == user.organization_id)
                          .order_by(EnterpriseAuditAggregation.last_updated.desc()).limit(500)).all()
    return [{'id': r.id, 'action': r.action, 'date_bucket': r.date_bucket,
             'count': r.count, 'last_updated': r.last_updated.isoformat() if r.last_updated else None} for r in agg_rows]

# --- Session / Org Policies ---

class PolicyIn(BaseModel):
    max_session_days: int = 7
    sso_only_enforced: bool = False

@router.get('/session-policy')
def get_session_policy(user: User = Depends(admin), db: Session = Depends(get_db)):
    # A GET must not have side effects: persisting a default row here would
    # silently turn on max_session_days=7 enforcement for the whole org the
    # first time anyone merely loads the settings page, before any admin
    # actually opted into it. Report the same defaults without writing them;
    # only PATCH (an explicit save) persists a row.
    policy = db.scalar(select(SessionPolicy).where(SessionPolicy.organization_id == user.organization_id))
    if not policy:
        return {'id': None, 'max_session_days': 7, 'sso_only_enforced': False}
    return {'id': policy.id, 'max_session_days': policy.max_session_days, 'sso_only_enforced': policy.sso_only_enforced}

@router.patch('/session-policy')
def patch_session_policy(data: PolicyIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    policy = db.scalar(select(SessionPolicy).where(SessionPolicy.organization_id == user.organization_id))
    if not policy:
        policy = SessionPolicy(organization_id=user.organization_id)
        db.add(policy)
    policy.max_session_days = data.max_session_days
    policy.sso_only_enforced = data.sso_only_enforced
    policy.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {'max_session_days': policy.max_session_days, 'sso_only_enforced': policy.sso_only_enforced}

# --- IP Allowlisting ---

class IPIn(BaseModel):
    cidr: str
    description: str | None = None

@router.get('/ip-allowlist')
def list_ip(user: User = Depends(admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(IPAllowlist).where(IPAllowlist.organization_id == user.organization_id)).all()
    return [{'id': r.id, 'cidr': r.cidr, 'description': r.description, 'active': r.active} for r in rows]

@router.post('/ip-allowlist')
def create_ip(data: IPIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    entry = IPAllowlist(organization_id=user.organization_id, cidr=data.cidr, description=data.description)
    db.add(entry)
    db.flush()  # entry.id is default-generated on flush, not at construction
    log(db, user.organization_id, user.id, 'ip_allowlist', entry.id, 'created', cidr=entry.cidr)
    db.commit()
    return {'id': entry.id, 'cidr': entry.cidr}
