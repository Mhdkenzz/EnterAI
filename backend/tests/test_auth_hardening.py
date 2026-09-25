"""Account enumeration, brute-force limits, and proxy-header handling.

An attacker with a list of email addresses must not be able to learn which of them
are customers -- not from a status code, not from a message, and not from how long
a request takes.
"""
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import client_key, enforce
from app.database import SessionLocal
from app.main import app
from app.models import Organization
from app.ratelimit import SlidingWindowRateLimiter

from test_observability import workspace


def request_with(host="1.2.3.4", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})


def test_login_answers_identically_for_unknown_and_wrong_password():
    with TestClient(app) as c:
        _headers, account = workspace(c)
        known = c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "definitely-wrong"})
        unknown = c.post("/api/auth/login", json={"email": "nobody-at-all@example.com", "password": "definitely-wrong"})
        assert known.status_code == unknown.status_code == 401
        assert known.json() == unknown.json()


def test_login_spends_the_same_work_on_an_unknown_address():
    """Returning early for an unknown address makes the login form a timing oracle,
    however identical the response body is."""
    with TestClient(app) as c:
        _headers, account = workspace(c)

        def timed(email):
            samples = []
            for _ in range(5):
                start = time.perf_counter()
                c.post("/api/auth/login", json={"email": email, "password": "definitely-wrong"})
                samples.append(time.perf_counter() - start)
            return sorted(samples)[len(samples) // 2]

        known, unknown = timed(account["user"]["email"]), timed("nobody-at-all@example.com")
        slower, faster = max(known, unknown), min(known, unknown)
        assert slower < faster * 3, f"known={known:.4f}s unknown={unknown:.4f}s differ enough to enumerate"


def test_forgot_password_replies_the_same_for_a_real_and_a_fake_address(outbox):
    with TestClient(app) as c:
        _headers, account = workspace(c)
        real = c.post("/api/auth/forgot-password", json={"email": account["user"]["email"]})
        fake = c.post("/api/auth/forgot-password", json={"email": "no-such-person@example.com"})
        assert real.status_code == fake.status_code == 202
        assert real.json() == fake.json()
        # Both addresses get mail, so "did an email arrive" is not a signal either.
        assert {message.to for message in outbox} >= {account["user"]["email"], "no-such-person@example.com"}


def test_resend_verification_replies_the_same_for_a_fake_address():
    with TestClient(app) as c:
        _headers, account = workspace(c)
        real = c.post("/api/auth/resend-verification", json={"email": account["user"]["email"]})
        fake = c.post("/api/auth/resend-verification", json={"email": "no-such-person@example.com"})
        assert real.status_code == fake.status_code == 202
        assert real.json() == fake.json()


def test_signup_with_a_taken_address_does_not_confirm_it_and_warns_the_owner(outbox):
    from uuid import uuid4
    with TestClient(app) as c:
        _headers, account = workspace(c)
        outbox.clear()
        response = c.post("/api/auth/register", json={
            "organization_name": f"Other Org {uuid4().hex[:8]}", "name": "Impostor",
            "email": account["user"]["email"], "password": "impostor-password"})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "already" not in detail.lower() and "exists" not in detail.lower()
        # The owner of the address is told, rather than the person at the form.
        notice = next(m for m in outbox if m.to == account["user"]["email"])
        assert "signup" in notice.subject.lower() or "sign up" in notice.subject.lower()


def test_email_addresses_are_matched_case_insensitively(outbox):
    with TestClient(app) as c:
        _headers, account = workspace(c)
        email = account["user"]["email"]
        assert c.post("/api/auth/login", json={"email": email.upper(), "password": "test-password"}).status_code == 200
        outbox.clear()
        c.post("/api/auth/forgot-password", json={"email": email.upper()})
        assert any(m.to == email for m in outbox)


def test_the_limiter_rejects_a_caller_over_budget():
    limiter = SlidingWindowRateLimiter(max_calls=2, window_seconds=60)
    request = request_with()
    enforce(limiter, request)
    enforce(limiter, request)
    with pytest.raises(HTTPException) as error:
        enforce(limiter, request)
    assert error.value.status_code == 429


def test_callers_are_bucketed_by_address_not_lumped_together():
    limiter = SlidingWindowRateLimiter(max_calls=1, window_seconds=60)
    enforce(limiter, request_with("10.0.0.1"))
    enforce(limiter, request_with("10.0.0.2"))  # a different caller has its own budget
    with pytest.raises(HTTPException):
        enforce(limiter, request_with("10.0.0.1"))


def test_a_forwarded_header_cannot_choose_its_own_bucket_by_default(monkeypatch):
    """Trusting X-Forwarded-For unconditionally lets a caller send a new value per
    request, which is an unlimited rate limit."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    spoofed = request_with("10.0.0.9", {"x-forwarded-for": "203.0.113.7"})
    assert client_key(spoofed) == "10.0.0.9"


def test_the_proxy_header_is_used_only_when_the_deployment_opts_in(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    # Only the last hop -- the entry our own load balancer appended -- is taken;
    # everything to its left was supplied by the caller.
    forwarded = request_with("10.0.0.9", {"x-forwarded-for": "203.0.113.7, 198.51.100.4"})
    assert client_key(forwarded) == "198.51.100.4"
    assert client_key(request_with("10.0.0.9", {})) == "10.0.0.9"


def test_login_sets_an_httponly_session_cookie_the_browser_can_use():
    with TestClient(app) as c:
        _headers, account = workspace(c)
        login = c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "test-password"})
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"]
        assert set_cookie.startswith("enterai_session=")
        assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie and "Path=/api" in set_cookie
        assert c.get("/api/me").json()["user"]["email"] == account["user"]["email"]


def test_cookie_session_writes_need_the_client_header():
    """A cross-site form post carries the cookie but cannot add a custom header."""
    with TestClient(app) as c:
        _headers, account = workspace(c)
        c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "test-password"})
        forged = c.post("/api/teams", json={"name": "Forged"})
        assert forged.status_code == 403
        allowed = c.post("/api/teams", json={"name": "Real"}, headers={"X-EnterAI-Client": "1"})
        assert allowed.status_code in (200, 201)


def test_bearer_clients_do_not_need_the_client_header():
    with TestClient(app) as c:
        headers, _account = workspace(c)
        assert c.post("/api/teams", json={"name": "CLI"}, headers=headers).status_code in (200, 201)


def test_logout_clears_the_session_cookie():
    with TestClient(app) as c:
        _headers, account = workspace(c)
        c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "test-password"})
        assert c.get("/api/me").status_code == 200
        out = c.post("/api/auth/logout")
        assert out.status_code == 204
        assert "enterai_session=" in out.headers["set-cookie"] and "Max-Age=0" in out.headers["set-cookie"]
        assert c.get("/api/me").status_code == 401


def test_session_cookie_is_secure_in_deployment_environments(monkeypatch):
    from starlette.responses import Response
    from app.auth import set_session_cookie
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    response = Response()
    set_session_cookie(response, "t")
    assert "Secure" in response.headers["set-cookie"]
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    response = Response()
    set_session_cookie(response, "t")
    assert "Secure" not in response.headers["set-cookie"]


def test_sign_out_revokes_the_token_itself_not_just_the_cookie():
    """Clearing a cookie leaves any copied token valid for days; sign-out must end it server-side."""
    with TestClient(app) as c:
        _headers, account = workspace(c)
        first = c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "test-password"}).json()["token"]
        c.cookies.clear()
        second = c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "test-password"}).json()["token"]
        c.cookies.clear()
        stolen = {"Authorization": f"Bearer {first}"}
        assert c.get("/api/me", headers=stolen).status_code == 200
        assert c.post("/api/auth/logout", headers=stolen).status_code == 204
        assert c.get("/api/me", headers=stolen).status_code == 401
        # Other sessions of the same user keep working.
        assert c.get("/api/me", headers={"Authorization": f"Bearer {second}"}).status_code == 200


def test_sign_out_with_a_bad_token_still_succeeds():
    with TestClient(app) as c:
        assert c.post("/api/auth/logout", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 204


def test_two_organizations_may_share_a_name_without_revealing_the_first():
    """A 'name taken' error would tell anyone which companies are already customers."""
    with TestClient(app) as c:
        name = f"Acme {uuid4().hex[:6]}"
        slugs = set()
        for _ in range(2):
            key = uuid4().hex
            response = c.post("/api/auth/register", json={"organization_name": name, "name": "Owner",
                                                          "email": f"{key}@example.com", "password": "test-password"})
            assert response.status_code == 200
            assert response.json()["organization"]["name"] == name
        with SessionLocal() as db:
            slugs = {o.slug for o in db.scalars(select(Organization).where(Organization.name == name))}
        assert len(slugs) == 2


def test_signup_records_which_terms_version_was_accepted():
    from app.models import User
    from app.privacy import CURRENT_TOS_VERSION
    with TestClient(app) as c:
        key = uuid4().hex
        c.post("/api/auth/register", json={"organization_name": f"Terms {key}", "name": "Owner", "email": f"{key}@example.com",
                                           "password": "test-password", "accept_terms": True})
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == f"{key}@example.com"))
            assert user.tos_accepted_at is not None and user.tos_version == CURRENT_TOS_VERSION


@pytest.mark.parametrize("password", ["password", "12345678", "aaaaaaaa", "Qwerty123"])
def test_signup_refuses_the_most_common_passwords(password):
    with TestClient(app) as c:
        key = uuid4().hex
        response = c.post("/api/auth/register", json={"organization_name": f"Weak {key}", "name": "Owner",
                                                      "email": f"{key}@example.com", "password": password})
        assert response.status_code == 422 and "too common" in response.json()["detail"]


def test_a_password_containing_the_email_name_is_refused():
    with TestClient(app) as c:
        response = c.post("/api/auth/register", json={"organization_name": f"Weak {uuid4().hex}", "name": "Owner",
                                                      "email": "grace.hopper@example.com", "password": "grace.hopper!"})
        assert response.status_code == 422


def test_change_password_needs_the_current_one_and_ends_other_sessions():
    with TestClient(app) as c:
        headers, account = workspace(c)
        other = c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "test-password"}).json()["token"]
        c.cookies.clear()
        wrong = c.post("/api/me/password", headers=headers, json={"current_password": "nope-nope", "new_password": "a-new-passphrase"})
        assert wrong.status_code == 400
        ok = c.post("/api/me/password", headers=headers, json={"current_password": "test-password", "new_password": "a-new-passphrase"})
        assert ok.status_code == 200
        assert c.get("/api/me", headers={"Authorization": f"Bearer {other}"}).status_code == 401
        assert c.get("/api/me", headers={"Authorization": f"Bearer {ok.json()['token']}"}).status_code == 200
