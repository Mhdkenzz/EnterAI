"""SCIM / IdP-sync hardening: provisioning stays inside the verified domain, the
last admin survives IdP changes, each provider uses its own role mapping, and the
SSO audit log filters work.

Route-level against the real migrated database, like test_sso_phase14.py.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, User
from app.sso_auth import deactivate_scim_user, scim_create_or_update
from app.sso_models import IdentityProvider, SCIMUserMapping


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Scim Org {key}', 'name': 'Admin', 'email': f'{key}@example.com',
        'password': 'test-password-1'})
    assert response.status_code == 200
    client.cookies.clear()
    data = response.json()
    return {'Authorization': 'Bearer ' + data['token']}, data


def _provider(c, headers, *, verified=True, domain=True, role_mapping=None, name='Okta'):
    """Create a provider through the admin route and mint its SCIM token.
    Returns (provider_id, domain, scim_headers)."""
    body = {'name': name, 'protocol': 'oidc', 'role_mapping_json': role_mapping or {}}
    domain_name = f'{uuid4().hex}.example' if domain else None
    if domain_name:
        body['domain_binding'] = domain_name
    created = c.post('/api/admin/sso/providers', headers=headers, json=body)
    assert created.status_code == 200, created.text
    provider_id = created.json()['id']
    if verified and domain_name:
        with SessionLocal() as db:
            db.get(IdentityProvider, provider_id).domain_verified_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
    token = c.post(f'/api/admin/sso/providers/{provider_id}/scim-token', headers=headers).json()['scim_token']
    return provider_id, domain_name, {'Authorization': f'Bearer {token}'}


def _scim_user(c, scim_headers, domain, *, roles=None, name='Person'):
    payload = {'userName': f'{uuid4().hex}@{domain}', 'displayName': name}
    if roles:
        payload['roles'] = [{'value': r} for r in roles]
    response = c.post('/scim/v2/Users', headers=scim_headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()['id']


def _db_user(scim_id):
    with SessionLocal() as db:
        mapping = db.scalar(select(SCIMUserMapping).where(SCIMUserMapping.scim_external_id == scim_id))
        user = db.get(User, mapping.user_id)
        return user.id, user.role, user.active


def _actions(org_id):
    with SessionLocal() as db:
        return [row.action for row in db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_id))]


# --------------------------------------------------------------------------- #
# Email domain rules
# --------------------------------------------------------------------------- #

def test_scim_create_is_confined_to_the_providers_verified_domain():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        _pid, domain, scim = _provider(c, headers)

        for outside in ('ceo@gmail.com', f'ceo@evil{domain}', f'ceo@{domain}.evil.example', 'no-at-sign'):
            rejected = c.post('/scim/v2/Users', headers=scim, json={'userName': outside})
            assert rejected.status_code == 400, (outside, rejected.text)
            assert 'verified domain' in rejected.json()['detail']
        assert c.get('/scim/v2/Users', headers=scim).json()['totalResults'] == 0

        assert c.post('/scim/v2/Users', headers=scim, json={'userName': f'a@{domain}'}).status_code == 200
        assert c.post('/scim/v2/Users', headers=scim, json={'userName': f'B@Emea.{domain.upper()}'}).status_code == 200
        assert c.get('/scim/v2/Users', headers=scim).json()['totalResults'] == 2


def test_scim_create_is_refused_while_the_providers_domain_is_unverified_or_absent():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        for kwargs in ({'verified': False}, {'domain': False}):
            _pid, domain, scim = _provider(c, headers, **kwargs)
            response = c.post('/scim/v2/Users', headers=scim, json={'userName': f'a@{domain or "corp.example"}'})
            assert response.status_code == 400, response.text
            assert 'verified' in response.json()['detail']
            assert c.get('/scim/v2/Users', headers=scim).json()['totalResults'] == 0


def test_scim_create_with_an_email_that_already_has_an_account_is_a_clean_409():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        _pid, domain, scim = _provider(c, headers)
        taken = f'taken-{uuid4().hex[:8]}@{domain}'
        with SessionLocal() as db:
            db.add(User(organization_id=data['organization']['id'], name='Existing', email=taken,
                        password_hash='x', role='member', kind='human'))
            db.commit()
        for spelling in (taken, taken.upper()):
            response = c.post('/scim/v2/Users', headers=scim, json={'userName': spelling})
            assert response.status_code == 409, response.text
        assert c.get('/scim/v2/Users', headers=scim).json()['totalResults'] == 0


def test_scim_patch_username_must_stay_inside_the_verified_domain():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        _pid, domain, scim = _provider(c, headers)
        scim_id = _scim_user(c, scim, domain)
        before = c.get(f'/scim/v2/Users/{scim_id}', headers=scim).json()['userName']

        rejected = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={'Operations': [
            {'op': 'replace', 'path': 'userName', 'value': 'someone@gmail.com'}]})
        assert rejected.status_code == 400
        assert c.get(f'/scim/v2/Users/{scim_id}', headers=scim).json()['userName'] == before

        allowed = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={'Operations': [
            {'op': 'replace', 'path': 'userName', 'value': f'renamed@{domain}'}]})
        assert allowed.status_code == 200 and allowed.json()['userName'] == f'renamed@{domain}'


# --------------------------------------------------------------------------- #
# Last-admin protection
# --------------------------------------------------------------------------- #

def _sole_scim_admin(c):
    """An org whose only active human admin is a SCIM-provisioned user: the
    registered admin is deactivated directly in the database. Returns
    (org_id, registered_admin_id, scim_headers, scim_id)."""
    headers, data = _workspace(c)
    org_id = data['organization']['id']
    _pid, domain, scim = _provider(c, headers, role_mapping={'it-admins': 'admin'})
    scim_id = _scim_user(c, scim, domain, roles=['it-admins'])
    _uid, role, _active = _db_user(scim_id)
    assert role == 'admin'
    registered_admin = data['user']['id']
    with SessionLocal() as db:
        db.get(User, registered_admin).active = False
        db.commit()
    return org_id, registered_admin, scim, scim_id


def _reinstate(user_id):
    with SessionLocal() as db:
        db.get(User, user_id).active = True
        db.commit()


def test_scim_patch_role_cannot_demote_the_last_active_admin():
    with TestClient(app) as c:
        org_id, registered_admin, scim, scim_id = _sole_scim_admin(c)
        demote = {'Operations': [{'op': 'replace', 'path': 'roles', 'value': [{'value': 'unmapped-group'}]}]}

        assert c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json=demote).status_code == 200
        assert _db_user(scim_id)[1:] == ('admin', True)
        assert 'scim_last_admin_protected' in _actions(org_id)

        _reinstate(registered_admin)  # a second active admin exists: the same change is allowed
        assert c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json=demote).status_code == 200
        assert _db_user(scim_id)[1] == 'member'


def test_scim_patch_active_false_cannot_deactivate_the_last_active_admin():
    with TestClient(app) as c:
        org_id, registered_admin, scim, scim_id = _sole_scim_admin(c)
        off = {'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]}

        protected = c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json=off)
        assert protected.status_code == 200  # the sync does not fail...
        assert protected.json()['active'] is True  # ...it just does not remove the last admin
        assert _db_user(scim_id)[2] is True
        assert 'scim_last_admin_protected' in _actions(org_id)

        _reinstate(registered_admin)
        assert c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json=off).json()['active'] is False
        assert _db_user(scim_id)[2] is False


def test_scim_put_and_deactivate_helper_also_honour_the_last_admin_rule():
    with TestClient(app) as c:
        org_id, registered_admin, scim, scim_id = _sole_scim_admin(c)
        with SessionLocal() as db:
            mapping = db.scalar(select(SCIMUserMapping).where(SCIMUserMapping.scim_external_id == scim_id))
            idp = db.scalar(select(IdentityProvider).where(IdentityProvider.organization_id == org_id))
            user = db.get(User, mapping.user_id)

            # PUT-style update that maps the user to a non-admin role.
            scim_create_or_update(db, org_id, scim_id, user.email, user.name, role_claim='unmapped', idp=idp)
            db.commit()
            assert db.get(User, user.id).role == 'admin'

            assert deactivate_scim_user(db, mapping, actor_id=None, source_provider_id=idp.id) is False
            db.commit()
            db.refresh(user)
            db.refresh(mapping)
            assert user.active is True and mapping.scim_active is True

        _reinstate(registered_admin)
        with SessionLocal() as db:
            mapping = db.scalar(select(SCIMUserMapping).where(SCIMUserMapping.scim_external_id == scim_id))
            assert deactivate_scim_user(db, mapping, actor_id=None) is True
            db.commit()
            assert db.get(User, mapping.user_id).active is False


def test_deactivating_a_non_admin_is_never_blocked_by_the_last_admin_rule():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        org_id = data['organization']['id']
        _pid, domain, scim = _provider(c, headers)
        scim_id = _scim_user(c, scim, domain)  # a plain member
        # The registered admin is the only admin; deactivating the *member* is fine.
        assert c.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
            'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]}).json()['active'] is False
        assert 'scim_last_admin_protected' not in _actions(org_id)


# --------------------------------------------------------------------------- #
# Role mapping comes from the provider that made the call
# --------------------------------------------------------------------------- #

def test_scim_uses_the_calling_providers_role_mapping_not_the_orgs_first_provider():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        # Created in this order, "the first active IdP in the org" is the admin-mapping one.
        _a, domain_a, scim_a = _provider(c, headers, role_mapping={'staff': 'admin'}, name='Provider A')
        _b, domain_b, scim_b = _provider(c, headers, role_mapping={'staff': 'manager'}, name='Provider B')

        via_b = _scim_user(c, scim_b, domain_b, roles=['staff'])
        via_a = _scim_user(c, scim_a, domain_a, roles=['staff'])
        assert _db_user(via_b)[1] == 'manager'
        assert _db_user(via_a)[1] == 'admin'

        # PATCH roles and PUT follow the same rule.
        assert c.patch(f'/scim/v2/Users/{via_a}', headers=scim_b, json={'Operations': [
            {'op': 'replace', 'path': 'roles', 'value': [{'value': 'staff'}]}]}).status_code == 200
        assert _db_user(via_a)[1] == 'manager'


# --------------------------------------------------------------------------- #
# GET /api/admin/sso/audit filters
# --------------------------------------------------------------------------- #

def test_sso_audit_applies_action_entity_type_and_q_filters():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        provider_id, _domain, _scim = _provider(c, headers)  # 'created' + 'scim_token_rotated'
        c.patch(f'/api/admin/sso/providers/{provider_id}', headers=headers, json={'name': 'Renamed', 'protocol': 'oidc'})

        def fetch(**params):
            response = c.get('/api/admin/sso/audit', headers=headers, params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['total'] == len(body['items'])
            return body['items']

        everything = fetch()
        assert {'created', 'updated', 'scim_token_rotated', 'registered'} <= {i['action'] for i in everything}

        created = fetch(action='created')
        assert created and {i['action'] for i in created} == {'created'}
        assert fetch(action='scim_token') == []  # exact match, not a prefix

        providers = fetch(entity_type='identity_provider')
        assert providers and {i['entity_type'] for i in providers} == {'identity_provider'}
        assert 'registered' not in {i['action'] for i in providers}
        assert {i['action'] for i in fetch(entity_type='user')} >= {'registered'}

        both = fetch(action='updated', entity_type='identity_provider')
        assert both and {(i['action'], i['entity_type']) for i in both} == {('updated', 'identity_provider')}
        assert fetch(action='created', entity_type='user') == []

        # q: case-insensitive substring over action and entity_type.
        assert {'scim_token_rotated'} <= {i['action'] for i in fetch(q='SCIM_TOKEN')}
        identity = fetch(q='IDENTITY')
        assert identity and all('identity' in (i['entity_type'] + i['action']).lower() for i in identity)
        assert fetch(q='no-such-event-xyz') == []
        # LIKE wildcards in q are literals, not patterns.
        assert fetch(q='%') == []
        assert fetch(q='scim%rotated') == []
        assert fetch(q='scim_token_rotat') != []
        assert fetch(q='scim-token') == []

        # Still strictly this organization's events.
        assert {i['entity_id'] for i in fetch(action='registered')} == {data['user']['id']}
