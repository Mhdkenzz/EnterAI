"""SSO / SCIM / Enterprise Identity routes (Phase 14)."""
import os
from datetime import datetime, timezone
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import secrets_store, sso_oidc
from .admin import human_admin
from .auth import set_session_cookie
from .database import get_db
from .models import User, AuditEvent
from . import domain_verification
from .services import log
from .sso_models import IdentityProvider, SCIMUserMapping
from .sso_auth import (
    deactivate_scim_user, issue_scim_token, scim_create_or_update, commit_scim,
    resolve_role_mapping, resolve_scim_identity_provider, scim_mapping_for_provider,
    scim_mapping_for_scim,
    validate_org_isolation,
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


def _sso_failure_redirect() -> RedirectResponse:
    """Return a browser-safe failure handoff without exposing OIDC internals.

    The login endpoints are navigated to directly by a browser, not fetched by
    application code.  A fragment is never sent in the next HTTP request, so it
    keeps this generic UI signal out of frontend/proxy access logs as well.
    """
    return RedirectResponse(f"{_app_base_url()}/sso/callback#sso_error=1", status_code=302)

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


class DomainVerificationRequest(BaseModel):
    domain: str


def _normalize_binding(domain: str | None) -> str | None:
    """Canonical spelling for a domain binding, or None when none was given.

    A domain that was actually supplied but cannot be parsed is a 422, not a
    silent None. This used to swallow the ValueError and store NULL, which
    turned an operator typo into "this provider has no domain" -- a state with
    no error message anywhere and no way to tell it apart from never having
    set one. The v5 brief requires the rejection.

    A genuinely absent binding (None, or an empty string meaning "clear it")
    still stores NULL: that is a real, meaningful request.
    """
    if domain is None:
        return None
    if not domain.strip():
        return None
    try:
        return domain_verification.normalize_domain(domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"domain_binding is not a valid domain: {exc}",
        ) from exc


def _translate_domain_conflict(db, domain=None):
    """Commit, translating a lost exclusivity race into a controlled 409.

    Exists so the verify route and this test exercise the SAME handler, rather
    than the test asserting against a shape the route happens to implement. The
    unique index is the authority on exclusivity -- a preflight SELECT loses to
    a concurrent claim -- so the IntegrityError has to be caught at the commit
    that fires it, and it must not surface as a 500.
    """
    try:
        db.commit()
    except sa.exc.IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This email domain is already claimed by another identity provider",
        ) from exc


@router_admin.post('/providers/{provider_id}/domain-verification/challenge')
def domain_verification_challenge(
        provider_id: str, user: User = Depends(human_admin),
        db: Session = Depends(get_db)):
    """Return the TXT value the administrator must publish in DNS.

    Verification is a two-step flow on purpose: the challenge is derived from
    the domain (app/domain_verification.expected_txt_value) so it needs no
    stored state, and nothing becomes usable until /domain-verification
    confirms the record is live.
    """
    validate_org_isolation(db, user, user.organization_id)
    idp = db.scalar(select(IdentityProvider).where(
        IdentityProvider.id == provider_id,
        IdentityProvider.organization_id == user.organization_id))
    if idp is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if not idp.domain_binding:
        raise HTTPException(
            status_code=400, detail="This provider has no domain binding to verify")
    domain = domain_verification.normalize_domain(idp.domain_binding)
    return {
        'domain': domain,
        'txt_record_name': domain_verification.txt_record_name(domain),
        'txt_value': domain_verification.expected_txt_value(domain),
        'expires_in_days': domain_verification.VERIFICATION_TTL.days,
    }


@router_admin.post('/providers/{provider_id}/domain-verification')
def domain_verification_verify(
        provider_id: str, data: DomainVerificationRequest,
        user: User = Depends(human_admin), db: Session = Depends(get_db)):
    """Verify the DNS TXT record and, on success, mark the domain verified.

    Exclusivity is checked BEFORE the DNS lookup so a clash is refused without
    a network round trip, and again implicitly by the unique index when the
    verified timestamp is written -- that is what makes the guarantee hold when
    two admins race for the same domain.
    """
    validate_org_isolation(db, user, user.organization_id)
    idp = db.scalar(select(IdentityProvider).where(
        IdentityProvider.id == provider_id,
        IdentityProvider.organization_id == user.organization_id))
    if idp is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    try:
        domain = domain_verification.normalize_domain(data.domain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bound = (domain_verification.normalize_domain(idp.domain_binding)
             if idp.domain_binding else None)
    if bound is None:
        raise HTTPException(
            status_code=400, detail="This provider has no domain binding to verify")
    if domain != bound:
        # The proof must be for the bound domain, not an arbitrary one the
        # caller happens to control.
        raise HTTPException(
            status_code=400, detail="Domain does not match this provider's binding")

    # Release any EXPIRED claim on this domain BEFORE contending for it.
    #
    # The exclusivity index is partial on `domain_verified_at IS NOT NULL`, and
    # a partial index cannot call a non-immutable function -- so the index
    # cannot tell a fresh claim from a 91-day-old one. An expired row therefore
    # occupies the exclusive slot forever and no legitimate re-claim can ever
    # win it, with no route to release it. Clearing expired proofs here is the
    # sound fix: it only ever REMOVES a claim, never grants one, and it is
    # scoped to this one canonical domain so no unrelated live claim is
    # disturbed.
    released = domain_verification.revoke_expired_claims(db, domain)
    if released:
        log(db, idp.organization_id, user.id, "identity_provider", provider_id,
            "domain_claim_expired", domain=domain, released=released)

    # Another ACTIVE, VERIFIED provider (anywhere on the instance) already
    # holding this domain.
    #
    # The active/verified restriction is not a nicety: the exclusivity index is
    # partial on exactly those conditions, so a merely-typed binding reserves
    # nothing and must not be treated as a holder here either. Otherwise the
    # preflight would refuse a legitimate claim while the index would happily
    # admit it, and the two would disagree. Freshness is re-checked because the
    # preflight is advisory; the index is the authority.
    clash = db.scalar(select(IdentityProvider).where(
        IdentityProvider.domain_binding == domain,
        IdentityProvider.id != provider_id,
        domain_verification.live_verified_domain_clause()))
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail="This email domain is already claimed by another identity provider")

    # DNS resolution is an external operation and can take seconds. Release
    # the request's SQLite/PostgreSQL transaction before waiting so a concurrent
    # provider PATCH is able to move the binding; the conditional UPDATE below
    # then turns that interleaving into the intended 409 instead of a database
    # lock timeout. The only writes performed during preflight are the explicit
    # expiry cleanup and its audit event, so committing here cannot publish an
    # unverified claim.
    db.commit()

    if not domain_verification.verify_domain(domain):
        log(db, idp.organization_id, user.id, "identity_provider", provider_id,
            "domain_verification_failed", domain=domain)
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=("No matching DNS TXT record found. Publish the challenge value "
                    f"at {domain_verification.txt_record_name(domain)} and retry."))

    # Bind the proof to the domain that was actually verified, atomically.
    #
    # The check above ("domain == bound") happened BEFORE the DNS lookup, which
    # can take seconds. A concurrent PATCH can swap domain_binding in that
    # window, and writing `idp.domain_verified_at` on the stale in-memory object
    # would stamp a proof obtained for domain A onto domain B -- handing
    # provisioning rights for a domain nobody ever proved. So the write is a
    # conditional UPDATE that matches the binding we just verified, and reports
    # how many rows it actually changed. Zero means the binding moved under us.
    #
    # `domain` is deliberately not in the audit payload: the TXT value is
    # derived from the domain, so logging the domain is enough to reconstruct
    # it, and nothing here needs the response.
    stamped = db.execute(
        sa.update(IdentityProvider)
        .where(IdentityProvider.id == provider_id,
               IdentityProvider.organization_id == user.organization_id,
               IdentityProvider.domain_binding == domain)
        .values(domain_verified_at=domain_verification.utcnow())
    ).rowcount
    if not stamped:
        db.rollback()
        log(db, user.organization_id, user.id, "identity_provider", provider_id,
            "domain_verification_stale", domain=domain)
        db.commit()
        raise HTTPException(
            status_code=409,
            detail="The domain binding changed while this verification was in "
                   "flight; nothing was verified. Retry against the current binding.")
    log(db, idp.organization_id, user.id, "identity_provider", provider_id,
        "domain_verified", domain=domain)
    _translate_domain_conflict(db, domain)
    return {'verified': True, 'domain': domain,
            'domain_verified_at': domain_verification.utcnow().isoformat()}


class RoleMapping(BaseModel):
    role_mapping_json: dict


# --- Provider lifecycle ---
#
# Deliberately its own audited route, not a field on the config PATCH. Two
# reasons, both of which are correctness rather than taste:
#
#  * `active` gates provisioning AND holds the global domain claim (the
#    exclusivity index is partial on active + verified). A field buried in a
#    full-replace config PATCH would make "I renamed the provider" silently
#    deprovision every identity it owns.
#  * Deactivation is a security event. It needs its own audit record and its
#    own request, not to ride along with a name change.
#
# Reactivation deliberately does NOT restore domain_verified_at. DNS may have
# moved while the provider was off; the proof is re-earned by calling
# /domain-verification, which is the same thing an operator must do on a fresh
# install. is_domain_verified() checks `active`, so a deactivated provider's
# claim stops being live the instant this commits.
#
# Deactivation, by contrast, retires every local session behind the provider's
# own identities. Deactivation IS the compromise response: it is what an
# operator reaches for when a token or an IdP account is suspected. If it only
# flipped a provisioning flag, every session already minted from that provider
# would stay valid until it expired, and the one control meant to stop a
# compromised IdP would do nothing to a compromise already in progress. Going
# through the provider's mappings is what makes the blast radius the provider's
# own identities and nothing else.

def _set_provider_active(db, user, provider_id, active: bool, action: str):
    validate_org_isolation(db, user, user.organization_id)
    idp = db.get(IdentityProvider, provider_id)
    if not idp or idp.organization_id != user.organization_id:
        raise HTTPException(404, 'Provider not found')
    if bool(idp.active) == active:
        # Idempotent, but still audited: a repeated deactivate is exactly what an
        # operator does while troubleshooting, and the trail should show it.
        log(db, user.organization_id, user.id, 'identity_provider', idp.id,
            f'provider_{action}_noop', active=active)
        db.commit()
        return {'id': idp.id, 'active': active, 'users_revoked': 0}
    idp.active = active
    if active and idp.domain_verified_at is not None:
        # Reactivation does NOT restore the proof. domain_verified_at says DNS
        # pointed here when it was checked; while the provider was off, that
        # could have changed hands, and the uniqueness index does not protect
        # this -- a reactivated row re-enters the partial index and could
        # collide with a domain verified elsewhere in the meantime. The operator
        # re-runs /domain-verification, exactly as on a fresh install.
        idp.domain_verified_at = None

    revoked = 0
    if not active:
        # Retire the sessions of every identity this provider provisioned.
        # Scoped through the provider's OWN mappings, so a user's access granted
        # by some other IdP is not collateral damage.
        mappings = db.scalars(select(SCIMUserMapping).where(
            SCIMUserMapping.organization_id == idp.organization_id,
            SCIMUserMapping.identity_provider_id == idp.id)).all()
        seen = set()
        for mapping in mappings:
            mapping.scim_active = False
            target = db.get(User, mapping.user_id)
            if (target is None or target.id in seen
                    or target.organization_id != idp.organization_id):
                continue
            seen.add(target.id)
            if target.active:
                target.active = False
                # Bumping the epoch invalidates every JWT already issued to
                # this user; without it "deactivate" would leave live sessions.
                target.session_epoch = (target.session_epoch or 0) + 1
                revoked += 1
        log(db, idp.organization_id, user.id, 'scim_user', idp.id,
            'provider_identities_retired', provider_id=idp.id,
            users_revoked=revoked, mappings=len(mappings))

    log(db, user.organization_id, user.id, 'identity_provider', idp.id,
        f'provider_{action}', active=active,
        domain_binding=idp.domain_binding)
    db.commit()
    return {'id': idp.id, 'active': active, 'users_revoked': revoked}


@router_admin.post('/providers/{provider_id}/deactivate')
def deactivate_provider(provider_id: str, user: User = Depends(human_admin),
                        db: Session = Depends(get_db)):
    return _set_provider_active(db, user, provider_id, False, 'deactivated')


@router_admin.post('/providers/{provider_id}/activate')
def activate_provider(provider_id: str, user: User = Depends(human_admin),
                      db: Session = Depends(get_db)):
    return _set_provider_active(db, user, provider_id, True, 'activated')


# --- Admin SSO settings ---

@router_admin.get('/providers')
def list_providers(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    validate_org_isolation(db, user, user.organization_id)
    rows = db.scalars(select(IdentityProvider).where(IdentityProvider.organization_id == user.organization_id)).all()
    return [{'id': r.id, 'name': r.name, 'protocol': r.protocol, 'domain_binding': r.domain_binding,
             'domain_verified': domain_verification.is_domain_verified(r),
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
        # Normalized on the way in: the uniqueness index is the real
        # exclusivity guarantee, and it can only fire if both spellings of one
        # domain are stored identically. An unverified binding is stored as an
        # assertion; it admits nothing until /domain-verification proves it.
        domain_binding=(_normalize_binding(data.domain_binding)
                       if data.domain_binding else None),
        sso_only=data.sso_only,
        role_mapping_json=data.role_mapping_json,
        active=True,
    )
    db.add(idp)
    try:
        db.flush()  # idp.id is server/default-generated on flush, not at construction
    except sa.exc.IntegrityError as exc:
        # Global domain exclusivity. The unique index is the authority -- a
        # pre-check in Python would lose to a concurrent claim -- so the
        # violation is translated here rather than surfacing as a 500.
        db.rollback()
        if "domain_binding" in str(getattr(exc, "orig", exc)).lower():
            raise HTTPException(
                status_code=409,
                detail="This email domain is already bound to another identity provider",
            ) from exc
        raise
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
    # Domain changes invalidate the proof. domain_verified_at records that the
    # administrator proved control of ONE domain via DNS; a PATCH that swaps
    # domain_binding to a different domain carries no such proof, so leaving the
    # old timestamp in place would let an unverified new domain keep the
    # provisioning rights the old one had earned. Same reasoning as
    # oidc_issuer below resetting its cached discovery document.
    new_binding = _normalize_binding(data.domain_binding)
    if new_binding != idp.domain_binding:
        idp.domain_verified_at = None
    idp.domain_binding = new_binding
    # The unique index is the authority on exclusivity (a Python pre-check
    # loses to a concurrent claim), so the violation is translated here rather
    # than surfacing as a 500.
    try:
        db.flush()
    except sa.exc.IntegrityError as exc:
        db.rollback()
        if "domain_binding" in str(getattr(exc, "orig", exc)).lower():
            raise HTTPException(
                status_code=409,
                detail="This email domain is already bound to another identity provider",
            ) from exc
        raise
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
def sso_login_start(email: EmailStr, db: Session = Depends(get_db)):
    """Resolve the active OIDC provider bound to this email's domain and
    redirect the browser to its authorization endpoint. A real 302, not a JSON
    body with a URL for the frontend to navigate to -- the IdP's login page
    needs to be reached by an actual browser navigation, not a fetch().

    `email: EmailStr` (not a hand-checked `str`) so a malformed address is
    rejected by FastAPI's own request validation, whose 422 response matches
    the OpenAPI schema it publishes (an array of ValidationError, not a bare
    string) -- CI's schemathesis run caught a hand-raised HTTPException(422,
    "...") here violating that schema."""
    domain = email.lower().rsplit('@', 1)[-1]
    # The liveness conditions belong in the QUERY, not in a check on the single
    # row that came back. v5 selected "an arbitrary active matching binding"
    # and only then called is_domain_verified() on it: with an unverified
    # duplicate row present, that arbitrary row was the unverified one, the
    # check failed, and the entire domain was denied login even though a valid
    # verified provider existed and could have served the request. A refusal
    # based on a row we never needed to look at is an availability bug caused by
    # a security check.
    idp = db.scalar(select(IdentityProvider).where(
        IdentityProvider.domain_binding == domain,
        IdentityProvider.protocol == 'oidc',
        domain_verification.live_verified_domain_clause()))
    if not idp:
        return _sso_failure_redirect()
    # Re-checked in Python against the fetched row. Redundant with the clause
    # above by design: the SQL predicate is evaluated with the database's clock
    # and this one with the application's, and is_domain_verified() is the
    # definition every other path uses. Disagreement between the two is a bug
    # worth catching here rather than at provisioning.
    if not domain_verification.is_domain_verified(idp):
        return _sso_failure_redirect()
    redirect_uri = f'{_api_base_url()}/api/auth/sso/callback'
    try:
        authorization_url = sso_oidc.build_authorization_url(db, idp, redirect_uri)
        db.commit()  # discover() may have cached a freshly-fetched discovery document
    except sso_oidc.OIDCError:
        return _sso_failure_redirect()
    return RedirectResponse(authorization_url, status_code=302)


@router_login.get('/callback')
def sso_login_callback(request: Request, db: Session = Depends(get_db)):
    """The IdP redirects the browser back here with `code`+`state` (success)
    or `error` (the user canceled, access was denied, etc). Never renders
    anything itself -- always redirects on to the frontend, carrying a session
    cookie on success or a generic error code on failure, so the frontend owns
    the actual sign-in UI."""
    params = request.query_params
    state = params.get('state')
    if not state:
        return _sso_failure_redirect()
    try:
        _user, token = sso_oidc.handle_callback(
            db, code=params.get('code'), state=state, idp_error=params.get('error'))
    except sso_oidc.OIDCError:
        return _sso_failure_redirect()
    # The session travels as an httpOnly cookie set on this redirect, so the
    # token never appears in a URL, a referrer, a log line, or page script.
    response = RedirectResponse(f'{_app_base_url()}/sso/callback', status_code=302)
    set_session_cookie(response, token)
    return response


# --- SCIM endpoints ---

class SCIMUserPayload(BaseModel):
    userName: str  # email
    displayName: str | None = None
    active: bool = True
    roles: list[dict] | None = None


@router_scim.get('/Users')
def scim_list_users(idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    # SCIM user list returns only users mapped through *this* IdP -- not every
    # mapping in the organization. Scoping by organization alone disclosed
    # provider B's entire directory to provider A's provisioning token.
    mappings = db.scalars(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == idp.organization_id,
        SCIMUserMapping.identity_provider_id == idp.id)).all()
    results = []
    for m in mappings:
        u = db.get(User, m.user_id)
        # Defensive tenant check. Every normal write path keeps
        # mapping.organization_id and user.organization_id aligned, so this is
        # not expected to fire -- which is exactly why it needs to be enforced
        # rather than assumed. A corrupted or badly migrated row would
        # otherwise disclose another organization's user (name, email) to this
        # provider's directory listing.
        if u and u.organization_id == idp.organization_id:
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
        role_claim=role_claim, actor_id=None, source_provider_id=idp.id,
        active=data.active,
    )
    mapping = db.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.user_id == created.id,
        SCIMUserMapping.identity_provider_id == idp.id))
    commit_scim(db)
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
    # Provider-owned lookup: `idp` is the principal that authenticated this
    # request, so a mapping belonging to a different IdP in the same org is
    # not found at all (404), rather than readable and mutable.
    mapping = scim_mapping_for_scim(
        db, idp.organization_id, scim_id, idp.id)
    if not mapping:
        raise HTTPException(404, "SCIM user not found")
    u = db.get(User, mapping.user_id)
    # Defensive tenant check, as in scim_list_users: a mapping row that is
    # owned by this provider but points at another organization's User must
    # read as "not found" rather than serving that user's details.
    if not u or u.organization_id != idp.organization_id:
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
    # Provider-owned lookup: `idp` is the principal that authenticated this
    # request, so a mapping belonging to a different IdP in the same org is
    # not found at all (404), rather than readable and mutable.
    mapping = scim_mapping_for_scim(
        db, idp.organization_id, scim_id, idp.id)
    if not mapping:
        raise HTTPException(404, "SCIM user not found")
    role_claim = None
    if data.roles:
        role_claim = data.roles[0].get("value") if isinstance(data.roles, list) and data.roles else None
    # SCIM PUT is a full resource replacement, so the handler asks
    # scim_create_or_update for replace semantics rather than the partial
    # update POST uses. Omitting an optional attribute clears it.
    created = scim_create_or_update(
        db, idp.organization_id, scim_id, data.userName, data.displayName,
        role_claim=role_claim, actor_id=None, source_provider_id=idp.id,
        active=data.active, full_replace=True,
    )
    commit_scim(db)
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


def _plan_patch_operations(operations: list, idp) -> list[dict]:
    """Parse and validate a PATCH body into an ordered plan, writing nothing.

    Every value is checked here, before the handler has resolved a mapping or
    touched a row. Two rules follow from that:

    * A malformed `userName` is refused as bad INPUT, with the same answer
      whether or not the target user exists. Resolving first (as the v5 code
      did) turned a 400 into a 404 for unknown ids, which is an enumeration
      oracle reachable with nothing but a provider's SCIM token.
    * `active` accepts a JSON boolean and nothing else. The v5 code compared
      with `is False` / `is True`, so `"active": "false"` -- a string, which
      several IdPs send -- matched neither branch, skipped the write, and
      returned 200. To the IdP that is indistinguishable from a successful
      deprovision, so a suspended user keeps signing in.
    """
    plan: list[dict] = []
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise HTTPException(400, f"SCIM operation {index} is not an object")
        action = (op.get("op") or "").strip().lower()
        if action not in ("replace", "add"):
            # 'remove' has no defined effect on a user resource here. Skipping
            # it is unchanged behaviour and is NOT counted as an applied
            # change, so a request made only of removals still 400s.
            continue
        attrs = _patch_attrs(op)
        for key, value in attrs.items():
            if key == "active":
                # `isinstance(True, int)` is True in Python, so the bool check
                # has to come first or 0/1 would be accepted as booleans.
                if not isinstance(value, bool):
                    raise HTTPException(
                        400, "SCIM 'active' must be a JSON boolean")
                plan.append({'kind': 'active', 'value': value})
            elif key in ("displayname", "name.formatted"):
                if value is None:
                    # An explicit clear is meaningful; a non-string is not.
                    plan.append({'kind': 'display_name', 'value': ''})
                    continue
                if not isinstance(value, str):
                    raise HTTPException(400, "SCIM 'displayName' must be a string")
                plan.append({'kind': 'display_name', 'value': value})
            elif key == "username":
                try:
                    normalized = domain_verification.assert_provisioning_address_allowed(
                        idp, value)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                plan.append({'kind': 'username', 'value': normalized})
            elif key == "roles":
                role_claim = None
                if isinstance(value, list) and value:
                    first = value[0]
                    role_claim = first.get("value") if isinstance(first, dict) \
                        else (str(first) if isinstance(first, (str, int)) else None)
                elif isinstance(value, (str, int)):
                    role_claim = str(value)
                if role_claim is not None:
                    plan.append({'kind': 'roles',
                                 'value': resolve_role_mapping(idp, role_claim)})
            # Unrecognized attributes are ignored, as before.
    return plan


@router_scim.patch('/Users/{scim_id}')
def scim_patch_user(scim_id: str, payload: dict, idp: IdentityProvider = Depends(scim_auth), db: Session = Depends(get_db)):
    """Applies every recognized operation in the request (SCIM PATCH allows
    several per call), not just the first match -- an IdP that sends
    `[{replace active=false}, {replace displayName=...}]` in one PATCH (Azure
    AD does this on some attribute-sync passes) previously had the second
    operation silently ignored.

    Order is deliberate: parse and validate the WHOLE request, then resolve the
    resource, then apply. Two defects came from the old order.

    * Validation after resolution made the response a resource oracle. A PATCH
      with a malformed `userName` against a missing id returned 404 and against
      an existing one returned 400, so a caller holding a provider token could
      enumerate which ids are real. Refusing the input first makes the two
      cases identical.
    * Applying as it walked meant a malformed operation late in the list left
      the earlier ones written and then returned 400. Whether those partial
      writes survived depended on the session's final disposition, which is not
      something a request handler gets to decide. Nothing is written until every
      operation has been accepted.
    """
    operations = payload.get("Operations") or []
    if not operations:
        raise HTTPException(400, "SCIM patch requires at least one operation")

    # ---- parse and validate, touching nothing ----
    plan = _plan_patch_operations(operations, idp)

    # ---- resolve, only now that the request itself is known good ----
    # Provider-owned lookup: `idp` is the principal that authenticated this
    # request, so a mapping belonging to a different IdP in the same org is
    # not found at all (404), rather than readable and mutable.
    mapping = scim_mapping_for_scim(
        db, idp.organization_id, scim_id, idp.id)
    if not mapping:
        raise HTTPException(404, "SCIM user not found")
    user = db.get(User, mapping.user_id)
    if not user or user.organization_id != idp.organization_id:
        raise HTTPException(404, "SCIM user not found")

    # ---- apply ----
    applied = False
    for change in plan:
        if change['kind'] == 'active':
            if change['value'] is False:
                deactivate_scim_user(db, mapping, actor_id=None,
                                     source_provider_id=idp.id)
            elif not user.active:
                user.active = True
                user.session_epoch = (user.session_epoch or 0) + 1
                mapping.scim_active = True
                mapping.last_synced_at = datetime.now(timezone.utc)
            applied = True
        elif change['kind'] == 'display_name':
            user.name = change['value']
            applied = True
        elif change['kind'] == 'username':
            new_email = change['value']
            if new_email != user.email:
                taken = db.scalar(select(User).where(
                    func.lower(User.email) == new_email.lower(), User.id != user.id))
                if taken is not None:
                    raise HTTPException(409, "Another account already uses this email")
                before = user.email
                user.email = new_email
                log(db, idp.organization_id, None, "user", user.id,
                    "scim_email_updated", scim_mapping_id=mapping.id,
                    scim_identity_provider_id=idp.id, before=before, after=new_email)
                applied = True
        elif change['kind'] == 'roles':
            new_role = change['value']
            if new_role is not None and user.role != new_role:
                before = user.role
                user.role = new_role
                log(db, idp.organization_id, None, "user", user.id,
                    "scim_role_updated", scim_mapping_id=mapping.id,
                    scim_identity_provider_id=idp.id, before=before, after=new_role)
                applied = True

    if not applied:
        raise HTTPException(400, "Unsupported SCIM patch operation")
    # Not a bare db.commit(): the email rename above can lose a uniqueness race
    # here, and an unhandled IntegrityError on this path surfaced as a 500.
    commit_scim(db)
    user = db.get(User, mapping.user_id)
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "id": scim_id,
        "userName": user.email if user else None, "displayName": user.name if user else None,
        "active": user.active if user else False,
        "meta": {"resourceType": "User"},
    }
