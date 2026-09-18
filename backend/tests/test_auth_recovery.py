"""Password reset and email verification.

Both ride on the same single-use token table, so the properties that matter are
the same: the link works once, stops working when it expires, and a database
containing the tokens cannot be turned back into a working link.
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuthToken, User
from app.tokens import PASSWORD_RESET, hash_token

from conftest import link_token
from test_observability import workspace


def reset_token_for(client, outbox, email):
    outbox.clear()
    assert client.post("/api/auth/forgot-password", json={"email": email}).status_code == 202
    return link_token(outbox[-1].body, "reset")


def test_a_reset_link_changes_the_password_exactly_once(outbox):
    with TestClient(app) as c:
        _headers, account = workspace(c)
        email = account["user"]["email"]
        token = reset_token_for(c, outbox, email)

        changed = c.post("/api/auth/reset-password", json={"token": token, "password": "a-brand-new-password"})
        assert changed.status_code == 200

        signed_in = c.post("/api/auth/login", json={"email": email, "password": "a-brand-new-password"})
        assert signed_in.status_code == 200
        assert c.post("/api/auth/login", json={"email": email, "password": "test-password"}).status_code == 401

        # The same link a second time is refused, and the password stays put.
        replayed = c.post("/api/auth/reset-password", json={"token": token, "password": "attacker-chosen-password"})
        assert replayed.status_code == 400
        assert c.post("/api/auth/login", json={"email": email, "password": "attacker-chosen-password"}).status_code == 401
        assert c.post("/api/auth/login", json={"email": email, "password": "a-brand-new-password"}).status_code == 200


def test_resetting_a_password_signs_existing_sessions_out(outbox):
    """Whoever caused the reset may be holding a live session; it must not survive."""
    with TestClient(app) as c:
        headers, account = workspace(c)
        assert c.get("/api/me", headers=headers).status_code == 200

        token = reset_token_for(c, outbox, account["user"]["email"])
        assert c.post("/api/auth/reset-password", json={"token": token, "password": "rotated-password-99"}).status_code == 200

        assert c.get("/api/me", headers=headers).status_code == 401

        fresh = c.post("/api/auth/login", json={"email": account["user"]["email"], "password": "rotated-password-99"})
        assert c.get("/api/me", headers={"Authorization": "Bearer " + fresh.json()["token"]}).status_code == 200


def test_an_expired_reset_link_is_refused(outbox):
    with TestClient(app) as c:
        _headers, account = workspace(c)
        token = reset_token_for(c, outbox, account["user"]["email"])
        with SessionLocal() as db:
            row = db.scalar(select(AuthToken).where(AuthToken.token_hash == hash_token(token)))
            row.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
        assert c.post("/api/auth/reset-password", json={"token": token, "password": "too-late-password"}).status_code == 400


def test_requesting_a_second_link_retires_the_first(outbox):
    with TestClient(app) as c:
        _headers, account = workspace(c)
        email = account["user"]["email"]
        first = reset_token_for(c, outbox, email)
        second = reset_token_for(c, outbox, email)
        assert first != second
        assert c.post("/api/auth/reset-password", json={"token": first, "password": "from-old-link-pw"}).status_code == 400
        assert c.post("/api/auth/reset-password", json={"token": second, "password": "from-new-link-pw"}).status_code == 200


def test_only_the_hash_of_a_token_is_stored(outbox):
    """A database disclosure must not be replayable into an account takeover."""
    with TestClient(app) as c:
        _headers, account = workspace(c)
        token = reset_token_for(c, outbox, account["user"]["email"])
        with SessionLocal() as db:
            rows = db.scalars(select(AuthToken).where(AuthToken.purpose == PASSWORD_RESET)).all()
        stored = {row.token_hash for row in rows}
        assert token not in stored
        assert hash_token(token) in stored


def test_a_forged_or_unknown_token_is_refused():
    with TestClient(app) as c:
        assert c.post("/api/auth/reset-password", json={"token": "n" * 43, "password": "whatever-password"}).status_code == 400
        assert c.post("/api/auth/verify-email", json={"token": "n" * 43}).status_code == 400


def test_registering_sends_a_verification_link_that_confirms_the_address(outbox):
    with TestClient(app) as c:
        outbox.clear()
        _headers, account = workspace(c)
        assert account["user"]["email_verified"] is False
        verification = next(m for m in outbox if "Confirm" in m.subject)
        assert verification.to == account["user"]["email"]

        token = link_token(verification.body, "verify")
        confirmed = c.post("/api/auth/verify-email", json={"token": token})
        assert confirmed.status_code == 200

        with SessionLocal() as db:
            assert db.get(User, account["user"]["id"]).email_verified_at is not None
        # Spent, like every other emailed token.
        assert c.post("/api/auth/verify-email", json={"token": token}).status_code == 400


def test_verification_can_be_resent_only_while_it_is_still_needed(outbox):
    with TestClient(app) as c:
        _headers, account = workspace(c)
        email = account["user"]["email"]
        outbox.clear()
        assert c.post("/api/auth/resend-verification", json={"email": email}).status_code == 202
        token = link_token(outbox[-1].body, "verify")
        assert c.post("/api/auth/verify-email", json={"token": token}).status_code == 200

        # Already verified: same generic reply, but nothing more is sent.
        outbox.clear()
        assert c.post("/api/auth/resend-verification", json={"email": email}).status_code == 202
        assert outbox == []


def test_a_reset_link_also_settles_verification(outbox):
    """Reaching the inbox is the same proof the verification email asks for."""
    with TestClient(app) as c:
        _headers, account = workspace(c)
        token = reset_token_for(c, outbox, account["user"]["email"])
        c.post("/api/auth/reset-password", json={"token": token, "password": "proved-by-inbox-1"})
        with SessionLocal() as db:
            assert db.get(User, account["user"]["id"]).email_verified_at is not None
