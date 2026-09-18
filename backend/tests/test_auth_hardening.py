"""Account enumeration, brute-force limits, and proxy-header handling.

An attacker with a list of email addresses must not be able to learn which of them
are customers -- not from a status code, not from a message, and not from how long
a request takes.
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.accounts import client_key, enforce
from app.main import app
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
