"""Phase 13 route-level tests: auth, re-auth gating, admin gating, and the
end-to-end HTTP flow for export / delete-account / retention policy."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import create_token, hash_password
from app.database import SessionLocal
from app.main import app
from app.models import User

from test_observability import workspace


def _member(org_id: str, password: str = "member-password"):
    with SessionLocal() as db:
        user = User(organization_id=org_id, name="Member", email=f"{uuid4().hex}@example.com",
                    password_hash=hash_password(password), role="member")
        db.add(user); db.commit()
        db.refresh(user)
        return user, {"Authorization": "Bearer " + create_token(user)}


def test_export_requires_the_correct_password():
    with TestClient(app) as c:
        headers, account = workspace(c)
        r = c.post("/api/privacy/export", json={"password": "wrong"}, headers=headers)
        assert r.status_code == 401


def test_export_with_correct_password_returns_a_scoped_download():
    with TestClient(app) as c:
        headers, account = workspace(c)
        r = c.post("/api/privacy/export", json={"password": "test-password"}, headers=headers)
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        body = r.json()
        assert body["profile"]["id"] == account["user"]["id"]


def test_delete_account_requires_reauth_then_creates_a_cancelable_pending_request():
    with TestClient(app) as c:
        headers, account = workspace(c)
        denied = c.post("/api/privacy/delete-account", json={"password": "wrong"}, headers=headers)
        assert denied.status_code == 401

        created = c.post("/api/privacy/delete-account", json={"password": "test-password"}, headers=headers)
        assert created.status_code == 201
        request_id = created.json()["id"]
        assert created.json()["status"] == "pending"

        status = c.get("/api/privacy/deletion-status", headers=headers)
        assert status.json()["id"] == request_id

        canceled = c.post(f"/api/privacy/deletion-requests/{request_id}/cancel", headers=headers)
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"

        # the account is still usable -- a canceled deletion must not anonymize
        me = c.post("/api/privacy/export", json={"password": "test-password"}, headers=headers)
        assert me.status_code == 200


def test_a_member_cannot_reach_admin_privacy_endpoints():
    with TestClient(app) as c:
        headers, account = workspace(c)
        member, member_headers = _member(account["organization"]["id"])

        for method, path in [("get", "/api/admin/privacy/retention"),
                              ("get", "/api/admin/privacy/deletion-requests")]:
            r = getattr(c, method)(path, headers=member_headers)
            assert r.status_code == 403, f"{method} {path} should be admin-only"


def test_admin_can_export_and_request_organization_deletion():
    with TestClient(app) as c:
        headers, account = workspace(c)
        org_id = account["organization"]["id"]

        export = c.post("/api/admin/privacy/export", json={"password": "test-password"}, headers=headers)
        assert export.status_code == 200
        assert export.json()["organization"]["id"] == org_id

        created = c.post("/api/admin/privacy/delete-organization", json={"password": "test-password"}, headers=headers)
        assert created.status_code == 201
        listed = c.get("/api/admin/privacy/deletion-requests", headers=headers)
        assert any(r["id"] == created.json()["id"] for r in listed.json())


def test_retention_policy_get_defaults_then_patch_round_trips():
    with TestClient(app) as c:
        headers, account = workspace(c)

        initial = c.get("/api/admin/privacy/retention", headers=headers)
        assert initial.status_code == 200
        assert initial.json()["chat_retention_days"] is None

        patched = c.patch("/api/admin/privacy/retention", json={"audit_retention_days": 90}, headers=headers)
        assert patched.status_code == 200
        assert patched.json()["audit_retention_days"] == 90
        assert patched.json()["chat_retention_days"] is None  # omitted fields stay unset, not carried over

        again = c.get("/api/admin/privacy/retention", headers=headers)
        assert again.json()["audit_retention_days"] == 90


def test_accept_tos_records_version_and_timestamp():
    with TestClient(app) as c:
        headers, account = workspace(c)
        r = c.post("/api/privacy/accept-tos", json={"version": "2026-06-01"}, headers=headers)
        assert r.status_code == 200
        assert r.json()["tos_version"] == "2026-06-01"
        assert r.json()["tos_accepted_at"] is not None


def test_deletion_request_cannot_cross_organizations():
    """A deletion request id from org A must be invisible (and unreachable) to a
    member of org B, even though it is a valid row that exists in the database."""
    with TestClient(app) as c:
        headers_a, account_a = workspace(c)
        headers_b, account_b = workspace(c)
        created = c.post("/api/admin/privacy/delete-organization", json={"password": "test-password"}, headers=headers_a)
        request_id = created.json()["id"]

        cross_org_cancel = c.post(f"/api/admin/privacy/deletion-requests/{request_id}/cancel", headers=headers_b)
        assert cross_org_cancel.status_code == 404

        cross_org_list = c.get("/api/admin/privacy/deletion-requests", headers=headers_b)
        assert all(r["id"] != request_id for r in cross_org_list.json())
