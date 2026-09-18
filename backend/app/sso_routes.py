"""SSO / SCIM / Enterprise Identity routes (Phase 14)."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .admin import human_admin
from .database import get_db
from .models import User, AuditEvent
from .services import log
from .sso_models import IdentityProvider, SCIMUserMapping
from .sso_auth import (
    deactivate_scim_user, issue_scim_token, scim_create_or_update,
    resolve_role_mapping, resolve_scim_identity_provider, validate_org_isolation,
)
from .tokens import hash_token

router_admin = APIRouter(prefix='/api/admin/sso', tags=['admin-sso'])
router_scim = APIRouter(prefix='/scim/v2', tags=['scim'])

# SCIM calls come from the IdP's provisioning engine, not a browser -- they carry
# their own dedicated bearer token (see sso_auth.issue_scim_token), never a normal
# user session JWT. A separate HTTPBearer instance (vs. auth.bearer) keeps that
# distinction visible in the OpenAPI schema.
_scim_bearer = HTTPBearer(auto_error=False)


def scim_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_scim_bearer),
    db: Session = Depends(get_db),
) -> IdentityProvider:
    """The only entry point into /scim/v2/*. A member's (or admin's) ordinary
    session token never satisfies this -- it is signed with a different key and
    was never hashed into any identity_providers.scim_token_hash row -- so it
    falls straight through to the same 403 an unrecognized token gets."""
    token = credentials.credentials if credentials else None
    idp = resolve_scim_identity_provider(db, token) if token else None
    if not idp:
        raise HTTPException(status_code=403, detail="Valid SCIM service token required")
    return idp


class IdPConfig(BaseModel):
    name: str
    protocol: Literal["oidc", "saml"] = "oidc"
    sso_url: str | None = None
    acs_url: str | None = None
    entity_id: str | None = None
    certificate_pem: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # only at creation/update; stored as hash
    domain_binding: str | None = None
    sso_only: bool = False
    role_mapping_json: dict = {}


class RoleMapping(BaseModel):
    role_mapping_json: dict


# --- Admin SSO settings ---

@router_admin.get('/providers')
def list_providers(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    validate_org_isolation(db, user, user.organization_id)
    rows = db.scalars(select(IdentityProvider).where(IdentityProvider.organization_id == user.organization_id)).all()
    return [{'id': r.id, 'name': r.name, 'protocol': r.protocol, 'domain_binding': r.domain_binding,
             'sso_only': r.sso_only, 'active': r.active, 'created_at': r.created_at.isoformat() if r.created_at else None} for r in rows]


@router_admin.post('/providers')
def create_provider(data: IdPConfig, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    validate_org_isolation(db, user, user.organization_id)
    idp = IdentityProvider(
        organization_id=user.organization_id,
        name=data.name,
        protocol=data.protocol,
        sso_url=data.sso_url,
        acs_url=data.acs_url,
        entity_id=data.entity_id,
        certificate_pem=data.certificate_pem,
        client_id=data.client_id,
        client_secret_hash=hash_token(data.client_secret) if data.client_secret else None,
        domain_binding=data.domain_binding,
        sso_only=data.sso_only,
        role_mapping_json=data.role_mapping_json,
        active=True,
    )
    db.add(idp)
    db.flush()  # idp.id is server/default-generated on flush, not at construction
    log(db, user.organization_id, user.id, 'identity_provider', idp.id, 'created',
        protocol=idp.protocol, domain_binding=idp.domain_binding)
    db.commit()
    return {'id': idp.id, 'name': idp.name, 'protocol': idp.protocol}


@router_admin.patch('/providers/{provider_id}')
def patch_provider(provider_id: str, data: IdPConfig, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    validate_org_isolation(db, user, user.organization_id)
    idp = db.get(IdentityProvider, provider_id)
    if not idp or idp.organization_id != user.organization_id:
        raise HTTPException(404, 'Provider not found')
    idp.name = data.name
    idp.sso_url = data.sso_url
    idp.acs_url = data.acs_url
    idp.entity_id = data.entity_id
    idp.certificate_pem = data.certificate_pem
    idp.client_id = data.client_id
    if data.client_secret:
        idp.client_secret_hash = hash_token(data.client_secret) if data.client_secret else None
    idp.domain_binding = data.domain_binding
    idp.sso_only = data.sso_only
    idp.role_mapping_json = data.role_mapping_json
    log(db, user.organization_id, user.id, 'identity_provider', idp.id, 'updated',
        domain_binding=idp.domain_binding, sso_only=idp.sso_only)
    db.commit()
    return {'id': idp.id, 'name': idp.name, 'protocol': idp.protocol, 'sso_only': idp.sso_only}


@router_admin.post('/providers/{provider_id}/scim-token')
def rotate_scim_token(provider_id: str, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    """Mints a new SCIM bearer token for this provider and invalidates the previous
    one. The raw token is returned exactly once -- paste it into the IdP's SCIM
    configuration now; it cannot be retrieved again, only rotated."""
    validate_org_isolation(db, user, user.organization_id)
    idp = db.get(IdentityProvider, provider_id)
    if not idp or idp.organization_id != user.organization_id:
        raise HTTPException(404, 'Provider not found')
    raw_token = issue_scim_token(db, idp)
    log(db, user.organization_id, user.id, 'identity_provider', idp.id, 'scim_token_rotated')
    db.commit()
    return {'id': idp.id, 'scim_token': raw_token, 'scim_base_url': '/scim/v2'}


@router_admin.get('/audit')
def sso_audit(q: str | None = None, action: str | None = None,
              entity_type: str | None = None, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    stmt = select(AuditEvent).where(AuditEvent.organization_id == user.organization_id)
    # Filter to identity/admin actions
    allowed = {"scim_provisioned", "scim_deprovisioned", "scim_role_updated", "created", "updated", "scim_user"}
    # No filter restriction here — include all for audit; the key is strict org isolation.
    stmt = stmt.order_by(AuditEvent.created_at.desc())
    rows = db.scalars(stmt.limit(100)).all()
    keys = ['id', 'actor_id', 'initiator_id', 'source', 'action', 'entity_type', 'entity_id', 'detail', 'created_at']
    return {'items': [{key: getattr(row, key) for key in keys} for row in rows], 'total': len(rows)}


# --- SCIM endpoints ---

class SCIMUserPayload(BaseModel):
    userName: str  # email
    displayName: str | None = None
    active: bool = True
    roles: list[dict] | None = None


@router_scim.get('/Users')
def scim_list_users(idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    # SCIM user list returns only users mapped to SCIM in this IdP's org.
    mappings = db.scalars(select(SCIMUserMapping).where(SCIMUserMapping.organization_id == idp.organization_id)).all()
    results = []
    for m in mappings:
        u = db.get(User, m.user_id)
        if u:
            results.append({
                "id": m.scim_external_id,
                "externalId": m.scim_external_id,
                "userName": u.email,
                "displayName": u.name,
                "active": u.active,
                "meta": {"resourceType": "User"},
            })
    return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "totalResults": len(results), "Resources": results}


@router_scim.post('/Users')
def scim_create_user(data: SCIMUserPayload, idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    role_claim = None
    if data.roles:
        role_claim = data.roles[0].get("value") if isinstance(data.roles, list) and data.roles else None
    # Create/update via the safe handler
    created = scim_create_or_update(
        db, idp.organization_id, data.userName, data.userName, data.displayName,
        role_claim=role_claim, actor_id=None, source_provider_id=idp.id
    )
    mapping = db.scalar(select(SCIMUserMapping).where(SCIMUserMapping.user_id == created.id))
    db.commit()
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": mapping.scim_external_id if mapping else created.email,
        "userName": created.email,
        "displayName": created.name,
        "active": created.active,
        "meta": {"resourceType": "User"},
    }


@router_scim.get('/Users/{scim_id}')
def scim_get_user(scim_id: str, idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    mapping = db.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == idp.organization_id,
        SCIMUserMapping.scim_external_id == scim_id))
    if not mapping:
        raise HTTPException(404, "SCIM user not found")
    u = db.get(User, mapping.user_id)
    if not u:
        raise HTTPException(404, "SCIM user not found")
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": scim_id,
        "userName": u.email,
        "displayName": u.name,
        "active": u.active,
        "meta": {"resourceType": "User"},
    }


@router_scim.put('/Users/{scim_id}')
def scim_update_user(scim_id: str, data: SCIMUserPayload, idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    mapping = db.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == idp.organization_id,
        SCIMUserMapping.scim_external_id == scim_id))
    if not mapping:
        raise HTTPException(404, "SCIM user not found")
    role_claim = None
    if data.roles:
        role_claim = data.roles[0].get("value") if isinstance(data.roles, list) and data.roles else None
    created = scim_create_or_update(
        db, idp.organization_id, scim_id, data.userName, data.displayName,
        role_claim=role_claim, actor_id=None, source_provider_id=idp.id
    )
    db.commit()
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": scim_id,
        "userName": created.email,
        "displayName": created.name,
        "active": created.active,
        "meta": {"resourceType": "User"},
    }


@router_scim.patch('/Users/{scim_id}')
def scim_patch_user(scim_id: str, payload: dict, idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    # SCIM deactivation: if Operations set active=False, deactivate immediately.
    operations = payload.get("Operations", [{}])
    for op in operations:
        if op.get("op", "").lower() == "replace" and "active" in (op.get("value") or {}):
            active_val = op.get("value", {}).get("active", True)
            mapping = db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.organization_id == idp.organization_id,
                SCIMUserMapping.scim_external_id == scim_id))
            if mapping:
                user_target = db.get(User, mapping.user_id) if mapping else None
                if not active_val:
                    deactivate_scim_user(db, mapping, actor_id=None, source_provider_id=idp.id)
                else:
                    mapping.scim_active = True
                    user_target = db.get(User, mapping.user_id)
                    if user_target and user_target.organization_id == idp.organization_id:
                        user_target.active = True
                        user_target.session_epoch = (user_target.session_epoch or 0) + 1
                    mapping.last_synced_at = datetime.now(timezone.utc)
                db.commit()
                return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "id": scim_id, "active": user_target.active if user_target else False}
    raise HTTPException(400, "Unsupported SCIM patch operation")
