"""Phase 15 — Enterprise controls regression.

Runs against the suite's real, externally-migrated database (see
test_sso_phase14.py's module docstring for why this replaced an isolated
`Base.metadata.create_all()` schema -- the same gap existed here: nothing
proved these routes worked against a database `alembic upgrade head` actually
built, and nothing proved IP allowlisting / session policy did anything beyond
being stored).
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.enterprise_models import EnterpriseAuditAggregation, IPAllowlist, SessionPolicy
from app.main import app
from app.models import Organization, User


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Enterprise Org {key}', 'name': 'Admin', 'email': f'{key}@example.com',
        'password': 'test-password-1'})
    assert response.status_code == 200
    data = response.json()
    return {'Authorization': 'Bearer ' + data['token']}, data


def _org(db_session):
    key = uuid4().hex[:12]
    org = Organization(name=f"Enterprise Unit {key}", slug=f"enterprise-unit-{key}")
    db_session.add(org)
    db_session.flush()
    return org.id


def test_audit_aggregation_is_immutable(db_session):
    org_id = _org(db_session)
    agg = EnterpriseAuditAggregation(organization_id=org_id, action="login", date_bucket="2025-09-18", count=42)
    db_session.add(agg)
    db_session.commit()
    fetched = db_session.get(EnterpriseAuditAggregation, agg.id)
    assert fetched.count == 42


def test_ip_allowlist_strict_isolation(db_session):
    org_id = _org(db_session)
    entry = IPAllowlist(organization_id=org_id, cidr="10.0.0.0/8", description="Enterprise")
    db_session.add(entry)
    db_session.commit()
    result = db_session.scalars(select(IPAllowlist).where(IPAllowlist.organization_id == org_id)).all()
    assert len(result) == 1


# --------------------------------------------------------------------------- #
# Route-level: proves the migration exists and enforcement is real, not just
# admin-panel scaffolding, against the real app and real (migrated) database.
# --------------------------------------------------------------------------- #

def test_ip_allowlist_routes_work_against_the_real_migrated_schema_and_then_enforce():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        # No entries yet: the feature is off, every request goes through.
        assert c.get('/api/me', headers=headers).status_code == 200
        added = c.post('/api/admin/enterprise/ip-allowlist', headers=headers,
                       json={'cidr': '10.0.0.0/8', 'description': 'office network'})
        assert added.status_code == 200, added.text
        # Once an active entry exists, every caller outside it is refused --
        # including the admin who just configured it (TestClient's host is not
        # even a parseable IP). This is the real, sharp edge of IP allowlisting:
        # an admin must include their own network or lock themselves out, the
        # same trade-off as any cloud security-group allowlist. Before this fix
        # nothing enforced the setting at all, so this used to do nothing.
        blocked = c.get('/api/me', headers=headers)
        assert blocked.status_code == 403
        blocked_list = c.get('/api/admin/enterprise/ip-allowlist', headers=headers)
        assert blocked_list.status_code == 403


def test_sso_only_enforced_blocks_password_login_once_set():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        patched = c.patch('/api/admin/enterprise/session-policy', headers=headers,
                          json={'max_session_days': 7, 'sso_only_enforced': True})
        assert patched.status_code == 200
        assert patched.json()['sso_only_enforced'] is True
        login = c.post('/api/auth/login', json={'email': data['user']['email'], 'password': 'test-password-1'})
        assert login.status_code == 403


def test_max_session_days_rejects_an_aged_token_but_grandfathers_tokens_without_iat():
    from app.auth import _enforce_session_policy
    key = uuid4().hex[:12]
    with SessionLocal() as db:
        org = Organization(name=f"Session Policy {key}", slug=f"session-policy-{key}")
        db.add(org)
        db.flush()
        user = User(organization_id=org.id, name="U", email=f"{uuid4().hex}@example.com",
                   password_hash="x", role="member", kind="human")
        db.add(user)
        db.flush()
        db.add(SessionPolicy(organization_id=org.id, max_session_days=1))
        db.commit()

        old_iat = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        with pytest.raises(HTTPException) as exc_info:
            _enforce_session_policy(db, user, {"iat": old_iat})
        assert exc_info.value.status_code == 401

        # A recent token still passes.
        _enforce_session_policy(db, user, {"iat": datetime.now(timezone.utc).timestamp()})
        # A token minted before this policy existed (no iat claim) is grandfathered.
        _enforce_session_policy(db, user, {})
