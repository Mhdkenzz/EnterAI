"""Phase 15 — Complete enterprise controls routes (admin, SIEM, audit export, policies)."""
import hashlib
import ipaddress
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session
from .admin import human_admin as admin, time_window
from .auth import _client_ip
from .database import get_db
from .models import User, Organization, AuditEvent
from .enterprise_models import IPAllowlist, SessionPolicy, EnterpriseAuditAggregation
from .services import log

router = APIRouter(prefix='/api/admin/enterprise', tags=['enterprise'])

# --- Audit / SIEM ---

@router.get('/audit/export')
def audit_export(user: User = Depends(admin), db: Session = Depends(get_db),
                 start: datetime | None = None, end: datetime | None = None,
                 limit: int = Query(1000, ge=1, le=10000)):
    """SIEM export. start/end are an inclusive created_at window (same semantics as
    admin.py's audit_search, via time_window); they used to be accepted and silently
    ignored, so a "last 24h" pull returned the newest rows regardless. limit is
    bounded so one request cannot materialize an org's entire ledger in memory."""
    # Raw-SQL audit aggregation by date/action for SIEM export (read-only, no mutation allowed)
    stmt = select(AuditEvent).where(AuditEvent.organization_id == user.organization_id)
    stmt = time_window(stmt, AuditEvent.created_at, start, end)
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

@router.post('/audit/refresh-aggregation')
def refresh_audit_aggregation_now(user: User = Depends(admin), db: Session = Depends(get_db)):
    """On-demand version of the periodic aggregation refresh (see
    observability.py's module-level comment for why this recomputes from
    audit_events rather than updating incrementally on every event). Scoped
    to this org: refreshing here can never touch another tenant's counts."""
    from .observability import refresh_audit_aggregations
    touched = refresh_audit_aggregations(db, organization_id=user.organization_id)
    return {'buckets_refreshed': touched}

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

def _normalize_cidr(raw: str) -> str:
    """Enforcement (auth._enforce_ip_allowlist) silently skips malformed CIDRs, so a
    typo'd entry would be stored, look active, and protect nothing. Reject it up
    front and store the canonical form ("10.0.0.5" -> "10.0.0.5/32", host bits
    cleared), which is also what enforcement parses with strict=False."""
    try:
        return str(ipaddress.ip_network(raw.strip(), strict=False))
    except ValueError:
        raise HTTPException(422, f'Invalid CIDR {raw!r}; expected e.g. 203.0.113.0/24 or 2001:db8::/32')

def _caller_address(request: Request):
    """The caller's IP exactly as enforcement derives it (auth._client_ip), or None
    when it is missing/unparseable -- enforcement 403s that case too."""
    raw_ip = _client_ip(request)
    if not raw_ip:
        return None
    try:
        return ipaddress.ip_address(raw_ip)
    except ValueError:
        return None

def ensure_caller_not_locked_out(request: Request, active_cidrs: list[str]) -> None:
    """Self-lockout guard for any allowlist change. Pass the cidrs that would be
    ACTIVE after the change. Once an org has one active entry, enforcement refuses
    every request (admins included) whose IP matches none of them, and the only way
    to fix the list is another request from an allowed IP -- so a change that
    excludes the caller's own IP would be unrecoverable from the API. An empty
    list means the feature is off, which is always safe. Matching mirrors
    enforcement: strict=False, malformed entries never match."""
    if not active_cidrs:
        return
    address = _caller_address(request)
    if address is None:
        raise HTTPException(409, 'Refusing to change the allowlist: your request IP could not be determined, '
                                 'and enforcement would block every request, including yours.')
    for cidr in active_cidrs:
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return
        except ValueError:
            continue
    raise HTTPException(409, f'Refusing to change the allowlist: your current IP {address} would not match any '
                             'active entry, which would lock you (and every other admin) out of this organization. '
                             f'Include {address} in the allowlist first.')

# Any future update/delete/deactivate route for entries must compute the post-change
# active set and call ensure_caller_not_locked_out before committing.

@router.post('/ip-allowlist', status_code=201)
def create_ip(data: IPIn, request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    cidr = _normalize_cidr(data.cidr)
    active_cidrs = list(db.scalars(select(IPAllowlist.cidr).where(
        IPAllowlist.organization_id == user.organization_id, IPAllowlist.active == True)))
    ensure_caller_not_locked_out(request, active_cidrs + [cidr])
    entry = IPAllowlist(organization_id=user.organization_id, cidr=cidr, description=data.description)
    db.add(entry)
    db.flush()  # entry.id is default-generated on flush, not at construction
    log(db, user.organization_id, user.id, 'ip_allowlist', entry.id, 'created', cidr=entry.cidr)
    db.commit()
    return {'id': entry.id, 'cidr': entry.cidr}
