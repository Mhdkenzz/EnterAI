"""Phase 14.2 — real OIDC login, end to end against a genuine local IdP.

No mocking of app.sso_oidc internals: this spins up an actual HTTP server on
127.0.0.1 that speaks real OIDC (discovery document, authorization redirect,
PKCE-checked code exchange, RS256-signed ID tokens served from a real JWKS
endpoint), and drives the real production code path -- build_authorization_url,
a real httpx GET to /authorize, a real httpx POST to /token, real
signature/issuer/audience/nonce verification via PyJWKClient against the
fake IdP's own published key. This is the most honest proof available
without a live Okta/Azure AD/Google account, which this environment has no
credentials for -- see sso_oidc.py's module docstring.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select
from uuid import uuid4

from app import secrets_store, sso_oidc
from app.database import SessionLocal
from app.main import app as backend_app
from app.models import Organization, User
from app.sso_models import IdentityProvider, SCIMUserMapping

KID = "fake-idp-key-1"


class FakeIdP:
    """A real, minimal OIDC provider: an authorization endpoint that
    auto-approves (no login UI to drive), a PKCE-checked token endpoint,
    and a JWKS endpoint serving the actual public key its tokens are signed
    with."""

    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.sessions: dict[str, dict] = {}  # code -> {nonce, code_challenge, redirect_uri, client_id}
        self.client_secret = "fake-idp-client-secret-" + secrets.token_hex(8)
        # This app enforces a real, deliberate global-uniqueness constraint on
        # users.email (login resolves by email alone, with no org
        # disambiguation) -- every test here shares one real database with
        # every other test in the suite, so a fixed literal email would
        # collide across test functions exactly like it would in production
        # if two people really did share an address.
        self.default_email = f"employee-{secrets.token_hex(6)}@fakeidp.example"
        self.next_claims_override: dict | None = None
        # /jwks always serves self.private_key's public component; a test that
        # wants to simulate a forged/wrong-key-signed token sets this instead
        # of self.private_key, so JWKS stays correct while signing does not --
        # otherwise "swap the key" would swap what JWKS publishes too, and the
        # forged token would verify against its own (also-swapped) JWKS entry.
        self.sign_with_key_override = None
        self.app = self._build_app()
        self.base_url = ""  # set once the server picks a port

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        def discovery():
            return {
                "issuer": self.base_url,
                "authorization_endpoint": f"{self.base_url}/authorize",
                "token_endpoint": f"{self.base_url}/token",
                "jwks_uri": f"{self.base_url}/jwks",
            }

        @app.get("/jwks")
        def jwks():
            jwk = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
            jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
            return {"keys": [jwk]}

        @app.get("/authorize")
        def authorize(request: Request):
            q = request.query_params
            code = secrets.token_urlsafe(16)
            self.sessions[code] = {
                "nonce": q.get("nonce"), "code_challenge": q.get("code_challenge"),
                "redirect_uri": q.get("redirect_uri"), "client_id": q.get("client_id"),
            }
            return RedirectResponse(f"{q.get('redirect_uri')}?{urlencode({'code': code, 'state': q.get('state')})}")

        @app.post("/token")
        def token(grant_type: str = Form(...), code: str = Form(...), redirect_uri: str = Form(...),
                  client_id: str = Form(...), client_secret: str = Form(...), code_verifier: str = Form(...)):
            session = self.sessions.get(code)
            if not session:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if client_secret != self.client_secret:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            if session["redirect_uri"] != redirect_uri or session["client_id"] != client_id:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            expected_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            if expected_challenge != session["code_challenge"]:
                return JSONResponse({"error": "invalid_grant", "detail": "PKCE verification failed"}, status_code=400)
            now = datetime.now(timezone.utc)
            claims = {
                "iss": self.base_url, "aud": client_id, "sub": "fake-idp-subject-42",
                "email": self.default_email, "name": "Fake IdP Employee",
                "nonce": session["nonce"], "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
            }
            if self.next_claims_override:
                claims.update(self.next_claims_override)
            signing_key = self.sign_with_key_override or self.private_key
            id_token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": KID})
            del self.sessions[code]  # a code is single-use
            return {"id_token": id_token, "access_token": "unused-in-this-flow", "token_type": "Bearer"}

        return app


class _ServerThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_idp():
    idp_impl = FakeIdP()
    port = _free_port()
    idp_impl.base_url = f"http://127.0.0.1:{port}"
    thread = _ServerThread(idp_impl.app, port)
    thread.start()
    for _ in range(100):
        if thread.server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("fake IdP server did not start in time")
    try:
        yield idp_impl
    finally:
        thread.stop()
        thread.join(timeout=5)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _org_and_idp(db_session, fake_idp, *, domain=None, role_mapping=None):
    key = uuid4().hex[:12]
    domain = domain or f"{key}.example"
    org = Organization(name=f"OIDC Org {key}", slug=f"oidc-org-{key}")
    db_session.add(org)
    db_session.flush()
    idp = IdentityProvider(
        organization_id=org.id, name="Fake IdP", protocol="oidc",
        client_id="test-client-id", client_secret_encrypted=secrets_store.encrypt(fake_idp.client_secret),
        oidc_issuer=fake_idp.base_url, domain_binding=domain, active=True,
        role_mapping_json=role_mapping or {},
    )
    db_session.add(idp)
    db_session.commit()
    return org, idp


def _drive_full_login(fake_idp, redirect_uri: str, authorization_url: str) -> tuple[str | None, str | None]:
    """Stand-in for the browser: GET the authorization_url (the fake IdP
    auto-approves and 302s straight back), extract code+state from the
    Location header without following the redirect (redirect_uri is our own
    backend, which this fixture is not running here)."""
    with httpx.Client() as client:
        response = client.get(authorization_url, follow_redirects=False)
    assert response.status_code in (302, 307), response.text
    parsed = urlparse(response.headers["location"])
    qs = parse_qs(parsed.query)
    return qs.get("code", [None])[0], qs.get("state", [None])[0]


def test_full_oidc_login_round_trip_against_a_real_local_idp(db_session, fake_idp):
    org, idp = _org_and_idp(db_session, fake_idp, role_mapping={"engineering": "manager"})
    fake_idp.next_claims_override = {"roles": ["engineering"]}
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"  # never actually dialed; IdP redirects here, we intercept

    authorization_url = sso_oidc.build_authorization_url(db_session, idp, redirect_uri)
    db_session.commit()
    assert authorization_url.startswith(f"{fake_idp.base_url}/authorize?")
    assert "code_challenge=" in authorization_url and "nonce=" in authorization_url

    code, state = _drive_full_login(fake_idp, redirect_uri, authorization_url)
    assert code and state

    user, token = sso_oidc.handle_callback(db_session, code=code, state=state, idp_error=None)
    assert user.organization_id == org.id
    assert user.email == fake_idp.default_email
    assert user.role == "manager"  # resolved from the role_mapping_json via the roles claim
    assert user.password_hash == ""  # SSO-only identity, never a local password

    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["sub"] == user.id

    mapping = db_session.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == org.id, SCIMUserMapping.scim_external_id == "oidc:fake-idp-subject-42"))
    assert mapping is not None and mapping.user_id == user.id


def test_second_login_reuses_the_same_user_and_updates_role(db_session, fake_idp):
    org, idp = _org_and_idp(db_session, fake_idp, role_mapping={"eng": "manager", "admins": "admin"})
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"

    fake_idp.next_claims_override = {"roles": ["eng"]}
    url1 = sso_oidc.build_authorization_url(db_session, idp, redirect_uri)
    db_session.commit()
    code1, state1 = _drive_full_login(fake_idp, redirect_uri, url1)
    user1, _ = sso_oidc.handle_callback(db_session, code=code1, state=state1, idp_error=None)
    assert user1.role == "manager"

    fake_idp.next_claims_override = {"roles": ["admins"]}
    url2 = sso_oidc.build_authorization_url(db_session, idp, redirect_uri)
    db_session.commit()
    code2, state2 = _drive_full_login(fake_idp, redirect_uri, url2)
    user2, _ = sso_oidc.handle_callback(db_session, code=code2, state=state2, idp_error=None)

    assert user2.id == user1.id  # same subject, same local user -- not a duplicate
    assert user2.role == "admin"  # role claim change picked up on re-login


def test_tampered_id_token_signature_is_rejected(db_session, fake_idp):
    """The core security property: an id_token signed with a key other than
    the one the IdP's own JWKS publishes must be rejected, not silently
    trusted -- proves verify_id_token checks the signature for real rather
    than just parsing the token's claims."""
    org, idp = _org_and_idp(db_session, fake_idp)
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"
    url = sso_oidc.build_authorization_url(db_session, idp, redirect_uri)
    db_session.commit()

    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake_idp.sign_with_key_override = forged_key  # JWKS still publishes the real key
    code, state = _drive_full_login(fake_idp, redirect_uri, url)

    with pytest.raises(sso_oidc.OIDCError):
        sso_oidc.handle_callback(db_session, code=code, state=state, idp_error=None)


def test_wrong_nonce_is_rejected(db_session, fake_idp):
    org, idp = _org_and_idp(db_session, fake_idp)
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"
    url = sso_oidc.build_authorization_url(db_session, idp, redirect_uri)
    db_session.commit()
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    real_nonce = qs["nonce"][0]

    # Drive the login, but corrupt the session's remembered nonce at the fake
    # IdP so the id_token it issues carries a *different* nonce than the one
    # our state token expects back -- the replay defense this is testing.
    code, state = _drive_full_login(fake_idp, redirect_uri, url)
    for session in fake_idp.sessions.values():
        session["nonce"] = "a-different-nonce-entirely"

    with pytest.raises(sso_oidc.OIDCError, match="nonce"):
        sso_oidc.handle_callback(db_session, code=code, state=state, idp_error=None)


def test_expired_state_token_is_rejected(db_session, fake_idp, monkeypatch):
    org, idp = _org_and_idp(db_session, fake_idp)
    monkeypatch.setattr(sso_oidc, "STATE_TTL_SECONDS", -1)  # already expired the instant it's minted
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"
    state = sso_oidc.mint_state(idp.id, nonce="n", code_verifier="v", redirect_uri=redirect_uri)
    with pytest.raises(sso_oidc.OIDCError):
        sso_oidc.read_state(state)


def test_idp_error_response_is_audited_and_not_exposed_raw(db_session, fake_idp):
    org, idp = _org_and_idp(db_session, fake_idp)
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"
    url = sso_oidc.build_authorization_url(db_session, idp, redirect_uri)
    db_session.commit()
    parsed = urlparse(url)
    state = parse_qs(parsed.query)["state"][0]

    with pytest.raises(sso_oidc.OIDCError):
        sso_oidc.handle_callback(db_session, code=None, state=state, idp_error="access_denied")

    from app.models import AuditEvent
    row = db_session.scalar(select(AuditEvent).where(
        AuditEvent.organization_id == org.id, AuditEvent.action == "sso_login_failed"
    ).order_by(AuditEvent.created_at.desc()))
    assert row is not None


def test_two_orgs_logins_stay_isolated_even_via_the_same_fake_idp(db_session, fake_idp):
    """Two different organizations' IdP rows, both real logins driven end to
    end concurrent in the same test database -- each must land in its own
    org, never cross-contaminate."""
    org_a, idp_a = _org_and_idp(db_session, fake_idp)
    org_b, idp_b = _org_and_idp(db_session, fake_idp)
    redirect_uri = "http://127.0.0.1:9/api/auth/sso/callback"

    # Two different real people at two different companies -- this app's
    # users.email is a genuine, deliberate global-uniqueness constraint (login
    # resolves by email alone), so this is not an artificial test
    # restriction, it is the realistic shape of "two orgs, same fake IdP".
    fake_idp.next_claims_override = {"email": f"person-a-{uuid4().hex[:8]}@fakeidp.example"}
    url_a = sso_oidc.build_authorization_url(db_session, idp_a, redirect_uri)
    db_session.commit()
    code_a, state_a = _drive_full_login(fake_idp, redirect_uri, url_a)
    user_a, _ = sso_oidc.handle_callback(db_session, code=code_a, state=state_a, idp_error=None)

    fake_idp.next_claims_override = {"email": f"person-b-{uuid4().hex[:8]}@fakeidp.example"}
    url_b = sso_oidc.build_authorization_url(db_session, idp_b, redirect_uri)
    db_session.commit()
    code_b, state_b = _drive_full_login(fake_idp, redirect_uri, url_b)
    user_b, _ = sso_oidc.handle_callback(db_session, code=code_b, state=state_b, idp_error=None)

    assert user_a.organization_id == org_a.id
    assert user_b.organization_id == org_b.id
    assert user_a.id != user_b.id  # same IdP subject claim value reused by the fake IdP each time, different orgs


# --------------------------------------------------------------------------- #
# Route-level: the real HTTP endpoints against the real (migrated) app DB.
# --------------------------------------------------------------------------- #

def test_sso_start_redirects_to_the_real_authorization_endpoint(fake_idp):
    with TestClient(backend_app) as c, SessionLocal() as db:
        org, idp = _org_and_idp(db, fake_idp, domain="startroute.example")
        response = c.get("/api/auth/sso/start", params={"email": "someone@startroute.example"},
                         follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"].startswith(f"{fake_idp.base_url}/authorize?")


def test_sso_start_404s_for_an_unconfigured_domain():
    with TestClient(backend_app) as c:
        response = c.get("/api/auth/sso/start", params={"email": f"nobody@{uuid4().hex}.example"})
        assert response.status_code == 404


def test_sso_callback_route_completes_a_real_login_and_redirects_with_a_token(fake_idp):
    with TestClient(backend_app) as c, SessionLocal() as db:
        org, idp = _org_and_idp(db, fake_idp, domain="callbackroute.example")
        start = c.get("/api/auth/sso/start", params={"email": f"u@callbackroute.example"}, follow_redirects=False)
        authorization_url = start.headers["location"]
        with httpx.Client() as raw:
            idp_redirect = raw.get(authorization_url, follow_redirects=False)
        parsed = urlparse(idp_redirect.headers["location"])
        callback_qs = parse_qs(parsed.query)

        callback = c.get("/api/auth/sso/callback", params={
            "code": callback_qs["code"][0], "state": callback_qs["state"][0],
        }, follow_redirects=False)
        assert callback.status_code == 302
        redirect_qs = parse_qs(urlparse(callback.headers["location"]).query)
        assert "token" in redirect_qs
        assert redirect_qs["organization_id"][0] == org.id

        me = c.get("/api/me", headers={"Authorization": f"Bearer {redirect_qs['token'][0]}"})
        assert me.status_code == 200
