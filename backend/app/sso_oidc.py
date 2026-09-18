"""Real OIDC login (Phase 14.2): authorization-code flow with PKCE, ID token
signature/issuer/audience/nonce verification via the IdP's own JWKS, and
just-in-time user provisioning mapped into existing org/RBAC.

Everything server-side (state, nonce, PKCE verifier) is carried in a single
signed, short-lived JWT passed as the OAuth `state` parameter -- the same
stateless-token shape as auth.py's confirmation tokens -- so no server-side
session store is needed and it works unmodified across replicas.

What this deliberately does NOT do: SAML. python3-saml (the only maintained
SAML SP library for Python) requires the system xmlsec1/libxml2 development
headers, which this environment cannot install (no package-manager access),
and hand-rolling XML-DSig verification is a well-known way to introduce a
signature-wrapping or XXE vulnerability. `IdentityProvider.protocol == "saml"`
remains configurable for the admin UI/API shape, but there is no SAML ACS
endpoint -- only OIDC actually logs anyone in. See docs/PHASE_17_LAUNCH_READINESS.md.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import secrets_store
from .auth import SECRET, create_token
from .models import User
from .services import log
from .sso_auth import resolve_role_mapping
from .sso_models import IdentityProvider, SCIMUserMapping

# Derived from the same JWT_SECRET as everything else in auth.py/copilot.py, so
# it rotates with it, but distinct so an OIDC state token can never be replayed
# as a session token or vice versa (same reasoning as CONFIRMATION_SECRET).
STATE_SECRET = hashlib.sha256(f"enterai:sso-oidc-state:v1:{SECRET}".encode()).hexdigest()
STATE_KIND = "sso_oidc_state"
STATE_TTL_SECONDS = int(os.getenv("SSO_STATE_TTL_SECONDS", "600"))
DISCOVERY_TTL_SECONDS = int(os.getenv("SSO_DISCOVERY_TTL_SECONDS", str(24 * 3600)))
HTTP_TIMEOUT_SECONDS = float(os.getenv("SSO_HTTP_TIMEOUT_SECONDS", "10"))

# The SCIM external-id namespace is reused for OIDC subjects (see module
# docstring in sso_auth.py) -- both are just "this org's external identity
# provider says this local user is that external id", regardless of protocol.
# Prefixed so a SCIM external id and an OIDC subject can never collide.
_OIDC_EXTERNAL_ID_PREFIX = "oidc:"


class OIDCError(Exception):
    """Raised for any step of the flow that fails -- caught at the route layer
    and turned into a redirect with a generic error, never a stack trace or an
    IdP-supplied string reflected back to the browser."""


def _http_client() -> httpx.Client:
    """A single seam tests override (see conftest_oidc fixtures / test_sso_oidc.py)
    to point at a local fake IdP instead of the real network. Production always
    gets a plain client with a short timeout -- discovery/token calls block a
    request thread and must never hang indefinitely."""
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)


def mint_state(idp_id: str, *, nonce: str, code_verifier: str, redirect_uri: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "kind": STATE_KIND, "idp_id": idp_id, "nonce": nonce,
        "code_verifier": code_verifier, "redirect_uri": redirect_uri,
        "iat": int(now.timestamp()), "exp": now.timestamp() + STATE_TTL_SECONDS,
    }, STATE_SECRET, algorithm="HS256")


def read_state(token: str) -> dict:
    try:
        payload = jwt.decode(token, STATE_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as error:
        raise OIDCError("Invalid or expired SSO request") from error
    if payload.get("kind") != STATE_KIND:
        raise OIDCError("Invalid SSO state")
    return payload


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = hashlib.sha256(verifier.encode()).digest()
    import base64
    challenge_b64 = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    return verifier, challenge_b64


def discover(db: Session, idp: IdentityProvider, *, force: bool = False) -> dict:
    """Resolve authorization/token endpoints and the JWKS URI from the issuer,
    caching the document on the row (shared across replicas, survives a
    restart) rather than in process memory. A transient fetch failure falls
    back to a still-cached copy rather than breaking login outright."""
    if not idp.oidc_issuer:
        raise OIDCError("This identity provider has no OIDC issuer configured")
    stale = (
        not idp.oidc_discovery_json
        or not idp.oidc_discovery_fetched_at
        or (datetime.now(timezone.utc) - idp.oidc_discovery_fetched_at.replace(tzinfo=timezone.utc)).total_seconds() > DISCOVERY_TTL_SECONDS
    )
    if not force and not stale:
        return idp.oidc_discovery_json
    issuer = idp.oidc_issuer.rstrip("/")
    try:
        with _http_client() as client:
            response = client.get(f"{issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, ValueError) as error:
        if idp.oidc_discovery_json:
            return idp.oidc_discovery_json  # serve stale rather than fail a login over a transient fetch
        raise OIDCError("Could not reach this identity provider's discovery endpoint") from error
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if required not in document:
            raise OIDCError(f"Identity provider discovery document is missing {required!r}")
    if document["issuer"].rstrip("/") != issuer:
        # RFC 8414/OIDC Discovery 1.0 sec 4.3: the returned `issuer` MUST match
        # the one requested, or a compromised/misdirected discovery endpoint
        # could redirect token verification at a different authority entirely.
        raise OIDCError("Identity provider discovery document issuer mismatch")
    idp.oidc_discovery_json = document
    idp.oidc_discovery_fetched_at = datetime.now(timezone.utc)
    db.flush()
    return document


def build_authorization_url(db: Session, idp: IdentityProvider, redirect_uri: str) -> str:
    if not idp.client_id:
        raise OIDCError("This identity provider has no client_id configured")
    document = discover(db, idp)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    state = mint_state(idp.id, nonce=nonce, code_verifier=verifier, redirect_uri=redirect_uri)
    params = {
        "response_type": "code", "client_id": idp.client_id, "redirect_uri": redirect_uri,
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{document['authorization_endpoint']}?{urlencode(params)}"


class TokenResponseError(OIDCError):
    pass


def _client_secret(idp: IdentityProvider) -> str:
    if not idp.client_secret_encrypted:
        raise OIDCError("This identity provider has no client secret configured for token exchange")
    try:
        return secrets_store.decrypt(idp.client_secret_encrypted)
    except secrets_store.SecretDecryptionError as error:
        raise OIDCError("This identity provider's stored client secret could not be decrypted") from error


def exchange_code(document: dict, idp: IdentityProvider, code: str,
                  redirect_uri: str, code_verifier: str) -> dict:
    client_secret = _client_secret(idp)
    try:
        with _http_client() as client:
            response = client.post(document["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": idp.client_id, "client_secret": client_secret, "code_verifier": code_verifier,
            }, headers={"Accept": "application/json"})
    except httpx.HTTPError as error:
        raise TokenResponseError("Could not reach this identity provider's token endpoint") from error
    if response.status_code != 200:
        raise TokenResponseError("This identity provider rejected the login")
    try:
        body = response.json()
    except ValueError as error:
        raise TokenResponseError("This identity provider returned an invalid token response") from error
    if "id_token" not in body:
        raise TokenResponseError("This identity provider's token response had no id_token")
    return body


_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _jwks_client(jwks_uri: str) -> jwt.PyJWKClient:
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri, timeout=HTTP_TIMEOUT_SECONDS)
        _jwks_clients[jwks_uri] = client
    return client


def verify_id_token(document: dict, id_token: str, *, client_id: str, nonce: str) -> dict:
    """Real cryptographic verification against the IdP's own published keys --
    signature, issuer, audience and expiry via PyJWT, nonce by hand (PyJWT has
    no opinion on it; it is this flow's replay defense, not a JWT-standard
    claim)."""
    try:
        signing_key = _jwks_client(document["jwks_uri"]).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=["RS256", "ES256"],
            audience=client_id, issuer=document["issuer"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as error:
        raise OIDCError("This identity provider's login token failed verification") from error
    if not claims.get("nonce") or claims["nonce"] != nonce:
        raise OIDCError("This identity provider's login token failed nonce verification")
    return claims


def _role_claim(claims: dict) -> str | None:
    roles = claims.get("roles")
    if isinstance(roles, list) and roles:
        return str(roles[0])
    role = claims.get("role")
    return str(role) if role else None


def provision_or_update_user(db: Session, idp: IdentityProvider, claims: dict) -> User:
    """Just-in-time provisioning, org-scoped exactly like SCIM: the subject
    claim is namespaced and looked up only within this IdP's organization, so
    a subject id can never resolve into a different org's user."""
    subject = claims.get("sub")
    if not subject:
        raise OIDCError("This identity provider's login token had no subject")
    external_id = _OIDC_EXTERNAL_ID_PREFIX + str(subject)
    email = claims.get("email") or f"{external_id}@{(idp.domain_binding or 'sso.enterai.local')}"
    name = claims.get("name") or claims.get("preferred_username") or email
    role_claim = _role_claim(claims)

    mapping = db.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == idp.organization_id,
        SCIMUserMapping.scim_external_id == external_id))
    if mapping:
        user = db.get(User, mapping.user_id)
        if not user or user.organization_id != idp.organization_id:
            raise OIDCError("This login could not be matched to a workspace account")
        if not user.active:
            raise OIDCError("This account has been deactivated")
        if role_claim is not None:
            new_role = resolve_role_mapping(idp, role_claim)
            if user.role != new_role:
                before = user.role
                user.role = new_role
                log(db, idp.organization_id, user.id, "user", user.id, "sso_role_updated",
                    scim_identity_provider_id=idp.id, before=before, after=new_role)
        mapping.last_synced_at = datetime.now(timezone.utc)
        return user

    # users.email is a real, deliberate global-uniqueness constraint (login
    # resolves by email alone, with no org disambiguation) -- a claimed email
    # already in use by a different local account (created manually before
    # SSO was configured, or by a different IdP subject entirely) must fail
    # with an actionable message, not a raw IntegrityError/500.
    if db.scalar(select(User).where(func.lower(User.email) == email.lower())):
        raise OIDCError(
            "An account with this email already exists and is not linked to this identity provider"
        )
    user = User(
        organization_id=idp.organization_id, name=name, email=email,
        role=resolve_role_mapping(idp, role_claim), active=True, kind="human",
    )
    user.password_hash = ""  # this identity only ever authenticates via SSO
    db.add(user)
    db.flush()
    db.add(SCIMUserMapping(
        organization_id=idp.organization_id, user_id=user.id,
        scim_external_id=external_id, scim_active=True,
        last_synced_at=datetime.now(timezone.utc),
    ))
    log(db, idp.organization_id, user.id, "user", user.id, "sso_provisioned",
        scim_identity_provider_id=idp.id, role=user.role)
    return user


def complete_login(db: Session, idp: IdentityProvider, claims: dict) -> tuple[User, str]:
    """Provision/find the user, issue a real session token, and audit the
    login itself (not just provisioning) -- a security review of this
    org's sign-in history needs to see every SSO login, not only the first
    one that created the account."""
    user = provision_or_update_user(db, idp, claims)
    token = create_token(user)
    log(db, idp.organization_id, user.id, "user", user.id, "sso_login",
        scim_identity_provider_id=idp.id)
    db.commit()
    return user, token


def handle_callback(db: Session, *, code: str | None, state: str,
                    idp_error: str | None) -> tuple[User, str]:
    """The whole callback in one place, so the route handler stays a thin
    HTTP/redirect shim: decode state -> load the provider it names -> IdP
    error passthrough -> discovery -> code exchange -> ID token verification
    -> provisioning -> session issuance -> audit."""
    payload = read_state(state)
    idp = db.get(IdentityProvider, payload["idp_id"])
    if not idp or not idp.active:
        raise OIDCError("This identity provider is no longer available")
    if idp_error:
        # The IdP itself refused/canceled the login (access_denied, etc). Audit
        # against the right org even though there is no user yet.
        log(db, idp.organization_id, None, "identity_provider", idp.id, "sso_login_failed",
            reason="idp_error")
        db.commit()
        raise OIDCError("Login was not completed at the identity provider")
    if not code:
        raise OIDCError("This identity provider did not return an authorization code")
    try:
        document = discover(db, idp)
        token_response = exchange_code(document, idp, code, payload["redirect_uri"], payload["code_verifier"])
        claims = verify_id_token(document, token_response["id_token"], client_id=idp.client_id, nonce=payload["nonce"])
        return complete_login(db, idp, claims)
    except OIDCError:
        log(db, idp.organization_id, None, "identity_provider", idp.id, "sso_login_failed",
            reason="verification_failed")
        db.commit()
        raise
