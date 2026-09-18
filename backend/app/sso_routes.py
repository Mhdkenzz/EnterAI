"""SSO / SCIM / Enterprise Identity routes (Phase 14)."""
import os
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import secrets_store, sso_oidc
from .admin import human_admin
from .database import get_db
from .models import User, AuditEvent
from .services import log
from .sso_models import IdentityProvider, SCIMUserMapping
from .sso_auth import (
    deactivate_scim_user, issue_scim_token, scim_create_or_update,
    resolve_role_mapping, resolve_scim_identity_provider, validate_org_isolation,
)

router_admin = APIRouter(prefix='/api/admin/sso', tags=['admin-sso'])
router_scim = APIRouter(prefix='/scim/v2', tags=['scim'])
# Public (unauthenticated by construction -- this IS how a user gets a session
# token in the first place) browser-facing OIDC login endpoints.
router_login = APIRouter(prefix='/api/auth/sso', tags=['sso-login'])


def _api_base_url() -> str:
    # The redirect_uri registered with the IdP must be *this backend's* own
    # publicly reachable URL, not APP_BASE_URL (the frontend, used for email
    # links elsewhere) -- the token exchange that follows sends the client
    # secret and must happen server-to-server, never in the browser.
    return os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")


def _app_base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:3000").rstrip("/")

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
    falls straight through to the same 403 an unrecognized token gets.

    No Authorization header at all is 401 (unauthenticated); a header that
    doesn't resolve to a live SCIM token -- including a perfectly valid user
    session token -- is 403 (authenticated as the wrong kind of principal).
    Collapsing both to 403 is what schemathesis's negative_data_rejection
    check caught: HTTP requires 401 for "no/invalid credentials presented".
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="SCIM bearer token required")
    idp = resolve_scim_identity_provider(db, credentials.credentials)
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
    client_secret: str | None = None  # only at creation/update; stored encrypted at rest
    # OIDC only. Endpoints are resolved from this at login time via discovery
    # (sso_oidc.discover), not entered by hand -- see sso_oidc.py's docstring.
    oidc_issuer: str | None = None
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
        client_secret_encrypted=secrets_store.encrypt(data.client_secret) if data.client_secret else None,
        oidc_issuer=data.oidc_issuer,
        domain_binding=data.domain_binding,
        sso_only=data.sso_only,
        role_mapping_json=data.role_mapping_json,
        active=True,
    )
    db.add(idp)
    db.flush()  # idp.id is server/default-generated on flush, not at construction
    # Config changes are audited, including the ones that don't include the
    # secret itself -- a security review of "who could sign in as whom" needs
    # to see every provider config change, not just role-mapping edits.
    log(db, user.organization_id, user.id, 'identity_provider', idp.id, 'created',
        protocol=idp.protocol, domain_binding=idp.domain_binding, oidc_issuer=idp.oidc_issuer)
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
        idp.client_secret_encrypted = secrets_store.encrypt(data.client_secret)
    # Every other field on this PATCH is a full replace (the pre-existing
    # contract of this endpoint), but oidc_issuer is deliberately not: a
    # caller that PATCHes sso_only or domain_binding without resending
    # oidc_issuer (easy to miss on a field added after this endpoint already
    # existed) must not silently blank out a working OIDC configuration and
    # its cached discovery document. Only an explicit, non-empty new issuer
    # changes it; clearing OIDC config entirely means deactivating/deleting
    # the provider, not omitting one field.
    if data.oidc_issuer and data.oidc_issuer != idp.oidc_issuer:
        idp.oidc_issuer = data.oidc_issuer
        idp.oidc_discovery_json = None
        idp.oidc_discovery_fetched_at = None
    idp.domain_binding = data.domain_binding
    idp.sso_only = data.sso_only
    idp.role_mapping_json = data.role_mapping_json
    log(db, user.organization_id, user.id, 'identity_provider', idp.id, 'updated',
        domain_binding=idp.domain_binding, sso_only=idp.sso_only, oidc_issuer=idp.oidc_issuer)
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


@router_admin.post('/rotate-secrets')
def rotate_provider_secrets(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    """Re-encrypts every stored OIDC client_secret for this organization under
    the current SECRETS_ENCRYPTION_KEY -- see secrets_store.py's module
    docstring for the full rotation procedure (set SECRETS_ENCRYPTION_KEY to a
    new value, keep the old one in SECRETS_ENCRYPTION_KEY_PREVIOUS, call this,
    then remove the previous key). Scoped to this org: one tenant's rotation
    can never touch another's secrets."""
    result = secrets_store.rotate_stored_secrets(
        db, IdentityProvider, 'client_secret_encrypted', organization_id=user.organization_id)
    log(db, user.organization_id, user.id, 'identity_provider', user.organization_id, 'secrets_rotated',
        count=result['rewritten'])
    db.commit()
    return result


# --- OIDC login (public: this is how a browser gets a session in the first place) ---

@router_login.get('/start')
def sso_login_start(email: str, db: Session = Depends(get_db)):
    """Resolve the active OIDC provider bound to this email's domain and
    redirect the browser to its authorization endpoint. A real 302, not a JSON
    body with a URL for the frontend to navigate to -- the IdP's login page
    needs to be reached by an actual browser navigation, not a fetch()."""
    domain = email.strip().lower().rsplit('@', 1)[-1] if '@' in email else ''
    if not domain:
        raise HTTPException(422, 'A valid email address is required')
    idp = db.scalar(select(IdentityProvider).where(
        IdentityProvider.domain_binding == domain, IdentityProvider.protocol == 'oidc',
        IdentityProvider.active == True))
    if not idp:
        raise HTTPException(404, 'No SSO identity provider is configured for this email domain')
    redirect_uri = f'{_api_base_url()}/api/auth/sso/callback'
    try:
        authorization_url = sso_oidc.build_authorization_url(db, idp, redirect_uri)
        db.commit()  # discover() may have cached a freshly-fetched discovery document
    except sso_oidc.OIDCError as error:
        raise HTTPException(503, str(error)) from error
    return RedirectResponse(authorization_url, status_code=302)


@router_login.get('/callback')
def sso_login_callback(request: Request, db: Session = Depends(get_db)):
    """The IdP redirects the browser back here with `code`+`state` (success)
    or `error` (the user canceled, access was denied, etc). Never renders
    anything itself -- always redirects on to the frontend, with a session
    token on success or a generic error code on failure, so the frontend owns
    the actual sign-in UI. The token travels as a query param the frontend
    reads once and discards from the URL, the same handoff shape as an
    emailed verification link."""
    params = request.query_params
    state = params.get('state')
    if not state:
        raise HTTPException(400, 'Missing SSO state')
    try:
        user, token = sso_oidc.handle_callback(
            db, code=params.get('code'), state=state, idp_error=params.get('error'))
    except sso_oidc.OIDCError as error:
        query = urlencode({'error': str(error)})
        return RedirectResponse(f'{_app_base_url()}/sso/callback?{query}', status_code=302)
    query = urlencode({'token': token, 'organization_id': user.organization_id})
    return RedirectResponse(f'{_app_base_url()}/sso/callback?{query}', status_code=302)


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


def _patch_attrs(op: dict) -> dict:
    """SCIM PATCH allows either `{"path": "active", "value": false}` (the
    shape Okta/Azure AD send for a single-attribute change) or
    `{"value": {"active": false, "displayName": "..."}}` (path omitted,
    multiple attributes in one operation) -- normalize both into one dict."""
    path = (op.get("path") or "").strip().lower()
    value = op.get("value")
    if path:
        return {path: value}
    if isinstance(value, dict):
        return {k.lower(): v for k, v in value.items()}
    return {}


@router_scim.patch('/Users/{scim_id}')
def scim_patch_user(scim_id: str, payload: dict, idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    """Applies every recognized operation in the request (SCIM PATCH allows
    several per call), not just the first match -- an IdP that sends
    `[{replace active=false}, {replace displayName=...}]` in one PATCH (Azure
    AD does this on some attribute-sync passes) previously had the second
    operation silently ignored."""
    mapping = db.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == idp.organization_id,
        SCIMUserMapping.scim_external_id == scim_id))
    if not mapping:
        raise HTTPException(404, "SCIM user not found")
    user = db.get(User, mapping.user_id)
    if not user or user.organization_id != idp.organization_id:
        raise HTTPException(404, "SCIM user not found")

    operations = payload.get("Operations") or []
    if not operations:
        raise HTTPException(400, "SCIM patch requires at least one operation")

    applied = False
    for op in operations:
        if (op.get("op") or "").strip().lower() not in ("replace", "add"):
            continue  # 'remove' has no defined effect on a user resource here; ignored, not fatal
        attrs = _patch_attrs(op)

        if "active" in attrs:
            applied = True
            if attrs["active"] is False:
                deactivate_scim_user(db, mapping, actor_id=None, source_provider_id=idp.id)
            elif attrs["active"] is True and not user.active:
                user.active = True
                user.session_epoch = (user.session_epoch or 0) + 1
                mapping.scim_active = True
                mapping.last_synced_at = datetime.now(timezone.utc)

        display_name = attrs.get("displayname") or attrs.get("name.formatted")
        if display_name:
            user.name = display_name
            applied = True

        if attrs.get("username") and attrs["username"] != user.email:
            new_email = attrs["username"]
            # users.email is a real, global-uniqueness constraint (see
            # sso_oidc.provision_or_update_user's identical check) -- an
            # unhandled collision here would surface as a raw 500 from the
            # later db.commit(), not a clean 4xx.
            taken = db.scalar(select(User).where(
                func.lower(User.email) == new_email.lower(), User.id != user.id))
            if taken:
                raise HTTPException(409, "Another account already uses this email")
            user.email = new_email
            applied = True

        if "roles" in attrs:
            roles_value = attrs["roles"]
            role_claim = None
            if isinstance(roles_value, list) and roles_value:
                first = roles_value[0]
                role_claim = first.get("value") if isinstance(first, dict) else str(first)
            if role_claim is not None:
                new_role = resolve_role_mapping(idp, role_claim)
                if user.role != new_role:
                    before = user.role
                    user.role = new_role
                    log(db, idp.organization_id, None, "user", user.id, "scim_role_updated",
                        scim_mapping_id=mapping.id, scim_identity_provider_id=idp.id,
                        before=before, after=new_role)
                applied = True

    if not applied:
        raise HTTPException(400, "Unsupported SCIM patch operation")
    db.commit()
    user = db.get(User, mapping.user_id)
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "id": scim_id,
        "userName": user.email if user else None, "displayName": user.name if user else None,
        "active": user.active if user else False,
        "meta": {"resourceType": "User"},
    }
