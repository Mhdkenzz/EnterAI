"""Phase 14 — SSO / SCIM / Enterprise Identity regression and isolation tests.

Runs against the suite's real, externally-migrated database (the same
`SessionLocal`/`TestClient(app)` convention every other route-level test file in
this suite uses, e.g. test_observability.py, test_invites.py) rather than an
isolated `Base.metadata.create_all()` in-memory schema. That isolated schema
previously hid the fact that `alembic upgrade head` never actually created these
tables on any real database -- every test here passed while every real SSO/SCIM
endpoint would have 500'd. See alembic/versions/0012_sso_enterprise.py.
"""
from datetime import datetime, timezone
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


def _verified_idp(db, org_id, *, domain=None, role_mapping=None):
    """An IdP whose domain the org has proven it owns (SCIM only provisions inside
    a verified domain -- see test_sso_domain_verification.py for the proof flow)."""
    idp = IdentityProvider(
        organization_id=org_id, name="Verified IdP", protocol="oidc",
        domain_binding=domain or f"{uuid4().hex[:12]}.example", domain_verification_token="t",
        domain_verified_at=datetime.now(timezone.utc).replace(tzinfo=None),
        role_mapping_json=role_mapping or {},
    )
    db.add(idp)
    db.commit()
    return idp


def _verified_provider(c, headers, *, role_mapping=None, name='Okta'):
    """Same, through the real admin route; returns (provider_id, domain)."""
    domain = f'{uuid4().hex}.example'
    created = c.post('/api/admin/sso/providers', headers=headers,
                     json={'name': name, 'protocol': 'oidc', 'domain_binding': domain,
                           'role_mapping_json': role_mapping or {}})
    assert created.status_code == 200, created.text
    provider_id = created.json()['id']
    with SessionLocal() as db:
        db.get(IdentityProvider, provider_id).domain_verified_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    return provider_id, domain


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
    idp = _verified_idp(db_session, test_org, role_mapping={"admin_group": "admin"})
    user = scim_create_or_update(db_session, test_org, f"ext-{uuid4().hex[:8]}",
                                 f"ext2-{uuid4().hex[:8]}@{idp.domain_binding}", "Name Two", idp=idp)
    db_session.commit()
    assert user.role == "member"  # no role claim: never escalates beyond member
    user2 = scim_create_or_update(db_session, test_org, f"ext-{uuid4().hex[:8]}",
                                  f"ext3-{uuid4().hex[:8]}@{idp.domain_binding}", "Name Three",
                                  role_claim="admin_group", actor_id=None, idp=idp)
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
        provider_id, domain = _verified_provider(c, headers, name='Azure AD')
        rotated = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers)
        assert rotated.status_code == 200
        scim_token = rotated.json()['scim_token']
        scim_headers = {'Authorization': f'Bearer {scim_token}'}

        created = c.post('/scim/v2/Users', headers=scim_headers,
                         json={'userName': f'{uuid4().hex}@{domain}', 'displayName': 'New Hire'})
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
        provider_id, domain = _verified_provider(c, headers)
        scim_token = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers).json()['scim_token']
        scim_headers = {'Authorization': f'Bearer {scim_token}'}
        scim_id = f'ext-{uuid4().hex[:8]}'
        with SessionLocal() as db:
            from app.sso_auth import scim_create_or_update
            user = scim_create_or_update(db, data['organization']['id'], scim_id,
                                         f'{uuid4().hex}@{domain}', 'To Deactivate',
                                         idp=db.get(IdentityProvider, provider_id))
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


def _provisioned_scim_user(c, headers, org_id):
    provider_id, domain = _verified_provider(c, headers)
    scim_token = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers).json()['scim_token']
    scim_headers = {'Authorization': f'Bearer {scim_token}'}
    created = c.post('/scim/v2/Users', headers=scim_headers,
                     json={'userName': f'{uuid4().hex}@{domain}', 'displayName': 'Original Name'})
    assert created.status_code == 200, created.text
    # scim_create_user (POST) uses userName as the SCIM external id -- the
    # response's "id" field is the real external id to address this user by
    # in subsequent GET/PUT/PATCH calls, not a value the caller invents.
    return scim_headers, created.json()['id'], domain


def test_scim_patch_path_based_single_attribute_replace():
    """Okta/Azure AD commonly send `{"op": "replace", "path": "active", "value": false}`
    (path-based) rather than `{"op": "replace", "value": {"active": false}}`
    (value-object-based) -- both must work."""
    with TestClient(app) as c:
        headers, data = _workspace(c)
        scim_headers, scim_id, _domain = _provisioned_scim_user(c, headers, data['organization']['id'])

        patched = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers,
                          json={'Operations': [{'op': 'replace', 'path': 'displayName', 'value': 'Renamed'}]})
        assert patched.status_code == 200, patched.text
        assert patched.json()['displayName'] == 'Renamed'


def test_scim_patch_applies_every_operation_in_one_request_not_just_the_first():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        scim_headers, scim_id, domain = _provisioned_scim_user(c, headers, data['organization']['id'])

        patched = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers, json={'Operations': [
            {'op': 'replace', 'path': 'displayName', 'value': 'Multi Op'},
            {'op': 'replace', 'path': 'userName', 'value': f'{uuid4().hex}@renamed.{domain}'},
        ]})
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body['displayName'] == 'Multi Op'
        assert body['userName'].endswith(f'@renamed.{domain}')  # a subdomain of the verified domain


def test_scim_patch_updates_role_from_role_mapping():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        provider_id, domain = _verified_provider(c, headers, role_mapping={'eng-admins': 'admin'})
        scim_token = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers).json()['scim_token']
        scim_headers = {'Authorization': f'Bearer {scim_token}'}
        created = c.post('/scim/v2/Users', headers=scim_headers,
                         json={'userName': f'{uuid4().hex}@{domain}', 'displayName': 'Role Test'})
        assert created.status_code == 200, created.text
        scim_id = created.json()['id']

        patched = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers, json={'Operations': [
            {'op': 'replace', 'path': 'roles', 'value': [{'value': 'eng-admins'}]},
        ]})
        assert patched.status_code == 200, patched.text

        with SessionLocal() as db:
            mapping = db.scalar(select(SCIMUserMapping).where(SCIMUserMapping.scim_external_id == scim_id))
            user = db.get(User, mapping.user_id)
            assert user.role == 'admin'


def test_scim_patch_reactivation_via_path_based_active_true():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        scim_headers, scim_id, _domain = _provisioned_scim_user(c, headers, data['organization']['id'])
        deactivated = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers,
                              json={'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
        assert deactivated.json()['active'] is False

        reactivated = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers,
                              json={'Operations': [{'op': 'replace', 'path': 'active', 'value': True}]})
        assert reactivated.status_code == 200
        assert reactivated.json()['active'] is True


def test_scim_patch_rejects_completely_unrecognized_operations():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        scim_headers, scim_id, _domain = _provisioned_scim_user(c, headers, data['organization']['id'])
        response = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim_headers,
                           json={'Operations': [{'op': 'remove', 'path': 'phoneNumbers'}]})
        assert response.status_code == 400


def test_scim_patch_404s_for_an_unknown_scim_id():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        provider_id = c.post('/api/admin/sso/providers', headers=headers,
                             json={'name': 'Okta', 'protocol': 'oidc'}).json()['id']
        scim_token = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers).json()['scim_token']
        response = c.patch('/scim/v2/Users/does-not-exist', headers={'Authorization': f'Bearer {scim_token}'},
                           json={'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
        assert response.status_code == 404


def test_scim_patch_username_collision_returns_409_not_a_raw_500():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        scim_headers, scim_id_a, _domain_a = _provisioned_scim_user(c, headers, data['organization']['id'])
        _, scim_id_b, _domain_b = _provisioned_scim_user(c, headers, data['organization']['id'])

        # Look up user A's real email so we can try to PATCH user B onto it.
        listed = c.get('/scim/v2/Users', headers=scim_headers).json()['Resources']
        email_a = next(r['userName'] for r in listed if r['id'] == scim_id_a)

        collided = c.patch(f'/scim/v2/Users/{scim_id_b}', headers=scim_headers,
                           json={'Operations': [{'op': 'replace', 'path': 'userName', 'value': email_a}]})
        assert collided.status_code == 409


def test_patch_provider_omitting_oidc_issuer_does_not_wipe_it():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        created = c.post('/api/admin/sso/providers', headers=headers,
                         json={'name': 'Okta', 'protocol': 'oidc', 'oidc_issuer': 'https://real-idp.example'})
        provider_id = created.json()['id']

        # A caller that PATCHes some other field without resending oidc_issuer
        # (easy to do -- it was added to this endpoint after sso_only/domain_binding).
        patched = c.patch(f'/api/admin/sso/providers/{provider_id}', headers=headers,
                          json={'name': 'Okta', 'protocol': 'oidc', 'sso_only': True})
        assert patched.status_code == 200

        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.oidc_issuer == 'https://real-idp.example'
            assert idp.sso_only is True
