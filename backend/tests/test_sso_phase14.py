"""Phase 14 — SSO / SCIM / Enterprise Identity regression and isolation tests.

Runs against the suite's real, externally-migrated database (the same
`SessionLocal`/`TestClient(app)` convention every other route-level test file in
this suite uses, e.g. test_observability.py, test_invites.py) rather than an
isolated `Base.metadata.create_all()` in-memory schema. That isolated schema
previously hid the fact that `alembic upgrade head` never actually created these
tables on any real database -- every test here passed while every real SSO/SCIM
endpoint would have 500'd. See alembic/versions/0012_sso_enterprise.py.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import create_token
from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.sso_auth import deactivate_scim_user, issue_scim_token, resolve_role_mapping, scim_create_or_update
from app.sso_models import IdentityProvider, SCIMUserMapping


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_org(db_session):
    key = uuid4().hex[:12]
    org = Organization(name=f"SSO Test {key}", slug=f"sso-test-{key}")
    db_session.add(org)
    db_session.commit()
    return org.id


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'SSO Org {key}', 'name': 'Admin', 'email': f'{key}@example.com',
        'password': 'test-password-1'})
    assert response.status_code == 200
    data = response.json()
    return {'Authorization': 'Bearer ' + data['token']}, data


def _member_headers(db_session, org_id):
    user = User(organization_id=org_id, name='Member', email=f'{uuid4().hex}@example.com',
                password_hash='unused', role='member', kind='human')
    db_session.add(user)
    db_session.commit()
    return {'Authorization': 'Bearer ' + create_token(user)}


def test_identity_provider_strict_org_isolation(db_session, test_org):
    idp = IdentityProvider(
        organization_id=test_org, name="Test IdP", protocol="oidc",
        domain_binding="test.local", sso_only=True
    )
    db_session.add(idp)
    db_session.commit()
    rows = db_session.scalars(select(IdentityProvider).where(IdentityProvider.organization_id == test_org)).all()
    assert len(rows) == 1
    assert rows[0].domain_binding == "test.local"


def test_scim_deactivate_removes_user_access(db_session, test_org):
    user = User(organization_id=test_org, name="SCIM User", email=f"scim-{uuid4().hex[:8]}@test.local",
                role="member", active=True, kind="human", password_hash="")
    db_session.add(user)
    db_session.commit()
    mapping = SCIMUserMapping(organization_id=test_org, user_id=user.id,
                               scim_external_id=f"ext-{uuid4().hex[:8]}", scim_active=True)
    db_session.add(mapping)
    db_session.commit()
    deactivate_scim_user(db_session, mapping, actor_id=None)
    db_session.commit()
    refreshed = db_session.get(User, user.id)
    assert refreshed.active is False
    refreshed_map = db_session.scalar(select(SCIMUserMapping).where(SCIMUserMapping.id == mapping.id))
    assert refreshed_map.scim_active is False


def test_scim_create_updates_role_mapping(db_session, test_org):
    user = scim_create_or_update(db_session, test_org, f"ext-{uuid4().hex[:8]}",
                                 f"ext2-{uuid4().hex[:8]}@test.local", "Name Two")
    db_session.commit()
    assert user.role == "member"
    idp = IdentityProvider(organization_id=test_org, name="Map IdP", protocol="oidc",
                           role_mapping_json={"admin_group": "admin"})
    db_session.add(idp)
    db_session.commit()
    user2 = scim_create_or_update(db_session, test_org, f"ext-{uuid4().hex[:8]}",
                                  f"ext3-{uuid4().hex[:8]}@test.local", "Name Three",
                                   role_claim="admin_group", actor_id=None)
    db_session.commit()
    assert user2.role == "admin"


def test_cross_org_isolation_prevented(db_session, test_org):
    user = User(organization_id=test_org, name="Cross", email=f"cross-{uuid4().hex[:8]}@test.local",
                role="member", active=True, kind="human", password_hash="")
    db_session.add(user)
    db_session.commit()
    other_org = "other-org-" + uuid4().hex[:8]
    result = db_session.scalars(select(User).where(User.organization_id == other_org, User.id == user.id)).all()
    assert len(result) == 0


# --------------------------------------------------------------------------- #
# Route-level: proves the migration exists and SCIM auth is actually enforced,
# against the real app and the real (migrated) database -- not a mock.
# --------------------------------------------------------------------------- #

def test_admin_sso_provider_routes_work_against_the_real_migrated_schema():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        created = c.post('/api/admin/sso/providers', headers=headers, json={'name': 'Okta', 'protocol': 'saml'})
        assert created.status_code == 200, created.text
        listed = c.get('/api/admin/sso/providers', headers=headers)
        assert listed.status_code == 200
        assert any(p['name'] == 'Okta' for p in listed.json())


def test_scim_rejects_a_normal_member_session_token_with_403():
    """The bug this closes: /scim/v2/* used to accept any signed-in member's
    session token, so a member could provision or deactivate any user --
    including admins -- through SCIM. A member's session token must not work
    here at all."""
    with TestClient(app) as c:
        admin_headers, data = _workspace(c)
        with SessionLocal() as db:
            member_headers = _member_headers(db, data['organization']['id'])
        response = c.get('/scim/v2/Users', headers=member_headers)
        assert response.status_code == 403
        response = c.patch('/scim/v2/Users/anything', headers=member_headers,
                           json={'Operations': [{'op': 'replace', 'value': {'active': False}}]})
        assert response.status_code == 403


def test_scim_rejects_requests_with_no_token_and_with_a_garbage_token():
    # No Authorization header at all: 401 (unauthenticated). A header present
    # but not a live SCIM token: 403 (authenticated as the wrong principal) --
    # the same distinction test_scim_rejects_a_normal_member_session_token_with_403
    # exercises with a real, otherwise-valid session token.
    with TestClient(app) as c:
        assert c.get('/scim/v2/Users').status_code == 401
        assert c.get('/scim/v2/Users', headers={'Authorization': 'Bearer not-a-real-token'}).status_code == 403


def test_scim_accepts_a_real_issued_service_token_and_stays_org_scoped():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        provider_id = c.post('/api/admin/sso/providers', headers=headers,
                             json={'name': 'Azure AD', 'protocol': 'oidc'}).json()['id']
        rotated = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers)
        assert rotated.status_code == 200
        scim_token = rotated.json()['scim_token']
        scim_headers = {'Authorization': f'Bearer {scim_token}'}

        created = c.post('/scim/v2/Users', headers=scim_headers,
                         json={'userName': f'{uuid4().hex}@example.com', 'displayName': 'New Hire'})
        assert created.status_code == 200, created.text

        listed = c.get('/scim/v2/Users', headers=scim_headers)
        assert listed.status_code == 200
        assert listed.json()['totalResults'] == 1

        # A second org's SCIM token must never see the first org's users.
        other_headers, _ = _workspace(c)
        other_provider_id = c.post('/api/admin/sso/providers', headers=other_headers,
                                   json={'name': 'Other IdP', 'protocol': 'oidc'}).json()['id']
        other_token = c.post(f'/api/admin/sso/providers/{other_provider_id}/scim-token',
                             headers=other_headers).json()['scim_token']
        other_scim_headers = {'Authorization': f'Bearer {other_token}'}
        other_listed = c.get('/scim/v2/Users', headers=other_scim_headers)
        assert other_listed.json()['totalResults'] == 0


def test_scim_patch_deactivation_through_the_real_route_kills_the_users_session():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        provider_id = c.post('/api/admin/sso/providers', headers=headers,
                             json={'name': 'Okta', 'protocol': 'saml'}).json()['id']
        scim_token = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers).json()['scim_token']
        scim_headers = {'Authorization': f'Bearer {scim_token}'}
        scim_id = f'ext-{uuid4().hex[:8]}'
        with SessionLocal() as db:
            from app.sso_auth import scim_create_or_update
            user = scim_create_or_update(db, data['organization']['id'], scim_id,
                                         f'{uuid4().hex}@example.com', 'To Deactivate')
            db.commit()
            user_token = create_token(user)

        me = c.get('/api/me', headers={'Authorization': f'Bearer {user_token}'})
        assert me.status_code == 200

        patched = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers,
                          json={'Operations': [{'op': 'replace', 'value': {'active': False}}]})
        assert patched.status_code == 200
        assert patched.json()['active'] is False

        me_after = c.get('/api/me', headers={'Authorization': f'Bearer {user_token}'})
        assert me_after.status_code == 401
