"""Organization invitations.

The interesting property is what an invite token is *not* able to do: it cannot be
redeemed into a different workspace, cannot be redeemed at a role the inviting
admin did not grant, cannot be used twice, and cannot be created at all by someone
who is not a human administrator of the organization it names.
"""
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Invite, User
from app.tokens import hash_token

from conftest import link_token
from test_observability import identity, workspace


def address(prefix: str) -> str:
    """Unique per run: these tests share one database with every other suite, and a
    fixed address would collide with the account a previous run already created."""
    return f"{prefix}-{uuid4().hex[:10]}@example.com"


def invite(client, headers, outbox, email, role="member"):
    outbox.clear()
    response = client.post("/api/auth/invites", headers=headers, json={"email": email, "role": role})
    assert response.status_code == 201, response.text
    return link_token(outbox[-1].body, "invite"), response.json()


def test_an_invited_teammate_joins_the_inviting_organization_at_the_granted_role(outbox):
    with TestClient(app) as c:
        headers, account = workspace(c)
        email = address("newcomer")
        token, created = invite(c, headers, outbox, email, "manager")
        assert created["role"] == "manager"

        preview = c.get(f"/api/auth/invites/{token}/preview")
        assert preview.status_code == 200
        assert preview.json()["organization"] == account["organization"]["name"]
        assert preview.json()["email"] == email

        accepted = c.post("/api/auth/accept-invite", json={
            "token": token, "name": "New Comer", "password": "a-fresh-password-1"})
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["organization"]["id"] == account["organization"]["id"]
        assert body["user"]["role"] == "manager"
        # Arriving through the emailed invite proves the address.
        assert body["user"]["email_verified"] is True

        # The returned session works, and sees the inviting organization's data.
        me = c.get("/api/me", headers={"Authorization": "Bearer " + body["token"]})
        assert me.status_code == 200 and me.json()["organization"]["id"] == account["organization"]["id"]


def test_an_invite_cannot_be_redeemed_twice(outbox):
    with TestClient(app) as c:
        headers, _account = workspace(c)
        email = address("once")
        token, _ = invite(c, headers, outbox, email)
        assert c.post("/api/auth/accept-invite", json={
            "token": token, "name": "First", "password": "first-password-11"}).status_code == 200
        replay = c.post("/api/auth/accept-invite", json={
            "token": token, "name": "Second", "password": "second-password-2"})
        assert replay.status_code in (400, 409)
        with SessionLocal() as db:
            assert len(db.scalars(select(User).where(User.email == email)).all()) == 1


def test_an_invite_for_one_organization_cannot_join_another(outbox):
    """The organization is read from the stored invite, never from the request, so
    holding a token for workspace A can never produce a member of workspace B."""
    with TestClient(app) as c:
        headers_a, account_a = workspace(c)
        _headers_b, account_b = workspace(c)
        token, _ = invite(c, headers_a, outbox, address("crossing"))

        accepted = c.post("/api/auth/accept-invite", json={
            "token": token, "name": "Crossing", "password": "crossing-password",
            "organization_id": account_b["organization"]["id"], "role": "admin"})
        assert accepted.status_code == 200
        assert accepted.json()["organization"]["id"] == account_a["organization"]["id"]
        assert accepted.json()["user"]["role"] == "member"


def test_an_expired_invite_is_refused(outbox):
    with TestClient(app) as c:
        headers, _account = workspace(c)
        token, created = invite(c, headers, outbox, address("stale"))
        with SessionLocal() as db:
            row = db.scalar(select(Invite).where(Invite.id == created["id"]))
            row.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
        assert c.get(f"/api/auth/invites/{token}/preview").status_code == 404
        assert c.post("/api/auth/accept-invite", json={
            "token": token, "name": "Stale", "password": "stale-password-1"}).status_code == 400


def test_a_revoked_invite_stops_working(outbox):
    with TestClient(app) as c:
        headers, _account = workspace(c)
        token, created = invite(c, headers, outbox, address("revoked"))
        assert c.delete(f"/api/auth/invites/{created['id']}", headers=headers).status_code == 204
        assert c.post("/api/auth/accept-invite", json={
            "token": token, "name": "Revoked", "password": "revoked-password"}).status_code == 400


def test_only_a_human_admin_of_that_organization_can_invite_or_revoke(outbox):
    with TestClient(app) as c:
        headers_a, account_a = workspace(c)
        headers_b, _account_b = workspace(c)
        _member_id, member_headers = identity(account_a["organization"]["id"], "member")
        _agent_id, agent_headers = identity(account_a["organization"]["id"], "admin", "agent")

        assert c.post("/api/auth/invites", headers=member_headers,
                      json={"email": address("nope")}).status_code == 403
        assert c.post("/api/auth/invites", headers=agent_headers,
                      json={"email": address("nope")}).status_code == 403
        assert c.post("/api/auth/invites", json={"email": address("nope")}).status_code in (401, 403)

        _token, created = invite(c, headers_a, outbox, address("target"))
        # An admin of a different workspace cannot see or revoke it.
        assert c.delete(f"/api/auth/invites/{created['id']}", headers=headers_b).status_code == 404
        assert all(row["id"] != created["id"] for row in c.get("/api/auth/invites", headers=headers_b).json())
        assert any(row["id"] == created["id"] for row in c.get("/api/auth/invites", headers=headers_a).json())


def test_reinviting_replaces_the_outstanding_link(outbox):
    with TestClient(app) as c:
        headers, _account = workspace(c)
        email = address("again")
        first, _ = invite(c, headers, outbox, email)
        second, _ = invite(c, headers, outbox, email)
        assert c.post("/api/auth/accept-invite", json={
            "token": first, "name": "Old", "password": "old-link-password"}).status_code == 400
        assert c.post("/api/auth/accept-invite", json={
            "token": second, "name": "New", "password": "new-link-password"}).status_code == 200


def test_inviting_an_existing_account_is_refused(outbox):
    with TestClient(app) as c:
        headers, account = workspace(c)
        response = c.post("/api/auth/invites", headers=headers,
                          json={"email": account["user"]["email"]})
        assert response.status_code == 409


def test_only_the_hash_of_an_invite_token_is_stored(outbox):
    with TestClient(app) as c:
        headers, _account = workspace(c)
        token, created = invite(c, headers, outbox, address("hashed"))
        with SessionLocal() as db:
            row = db.scalar(select(Invite).where(Invite.id == created["id"]))
            assert row.token_hash == hash_token(token)
            assert token not in row.token_hash


def test_an_invite_cannot_grant_a_role_outside_the_allowed_set(outbox):
    with TestClient(app) as c:
        headers, _account = workspace(c)
        assert c.post("/api/auth/invites", headers=headers,
                      json={"email": address("root"), "role": "superuser"}).status_code == 422
