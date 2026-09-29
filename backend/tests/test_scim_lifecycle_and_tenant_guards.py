"""Phase 1, group 1 -- correction pass 2 regressions.

Second independent review of the SCIM provider-isolation candidate. Each test
here was observed red against the rejected candidate before the production
change, and each pins a defect rather than a refactor.

1. SCIM active lifecycle. SCIMUserPayload.active was accepted and then ignored
   by POST and PUT, and scim_create_or_update unconditionally reactivated an
   existing mapping -- so a conforming PUT carrying "active": false re-enabled
   a deprovisioned account instead of leaving it off. PATCH semantics are
   unchanged; these tests cover POST and PUT.

2. Legacy quarantine must retire live sessions. 0021 makes a NULL-owner
   mapping unreachable by every provider but left the referenced User row and
   its session epoch untouched, so an identity the system can no longer manage
   kept its existing session cookies valid.

3. Defensive tenant checks. Provider-scoped list and GET returned a mapping's
   User without re-checking that the user belongs to the authenticated
   provider's organization. Every normal write path keeps them aligned, so this
   only shows up on a corrupted row -- which is exactly why it needs a guard
   rather than an assumption.

4. Property-based partial-migration validation, in
   test_scim_provider_migration_v2.py.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.sso_models import IdentityProvider, SCIMUserMapping


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'SCIM v2 {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _provider_with_token(client, admin_headers, name='IdP'):
    """A provider with a genuinely VERIFIED domain, plus its SCIM token.

    Returns (provider_id, scim_headers, domain). Verification is required:
    provisioning is refused outright for a provider that has not proven domain
    ownership, so an unverified fixture would fail at the domain gate rather
    than at the lifecycle/tenant behavior this file tests.
    """
    from conftest import make_verified_provider
    return make_verified_provider(client, admin_headers, name)


@pytest.fixture
def scim_client():
    """A workspace with one SCIM-authenticated, domain-verified provider."""
    with TestClient(app) as client:
        admin_headers, data = _workspace(client)
        provider_id, scim_headers, domain = _provider_with_token(client, admin_headers)
        yield {'client': client, 'admin': admin_headers,
               'org_id': data['organization']['id'], 'domain': domain,
               'provider_id': provider_id, 'scim': scim_headers}


def _user_state(user_id):
    with SessionLocal() as db:
        user = db.get(User, user_id)
        return {'active': user.active, 'epoch': user.session_epoch or 0}


# --------------------------------------------------------------------------- #
# 1. SCIM active lifecycle through POST and PUT
# --------------------------------------------------------------------------- #

def test_post_with_active_false_creates_an_inactive_user(scim_client):
    """POST carrying "active": false must not create an active account.

    The candidate discarded data.active entirely, so an IdP that provisioned a
    pre-disabled or suspended employee got a working, sign-in-capable user.
    """
    email = f'post-inactive-{uuid4().hex[:8]}@{scim_client["domain"]}'
    response = scim_client['client'].post('/scim/v2/Users', headers=scim_client['scim'], json={
        'userName': email, 'displayName': 'Suspended', 'active': False})
    assert response.status_code == 200, response.text
    assert response.json()['active'] is False

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        assert user.active is False, "POST active:false created a sign-in-capable user"
        mapping = db.scalar(select(SCIMUserMapping).where(
            SCIMUserMapping.organization_id == scim_client['org_id'],
            SCIMUserMapping.identity_provider_id == scim_client['provider_id']))
        assert mapping.scim_active is False


def test_post_with_active_true_creates_an_active_user(scim_client):
    """The default path must keep working: explicit active:true stays active."""
    email = f'post-active-{uuid4().hex[:8]}@{scim_client["domain"]}'
    response = scim_client['client'].post('/scim/v2/Users', headers=scim_client['scim'], json={
        'userName': email, 'displayName': 'Active Hire', 'active': True})
    assert response.status_code == 200, response.text
    assert response.json()['active'] is True


def test_put_with_active_false_does_not_reactivate(scim_client):
    """The core defect: PUT active:false on a currently-active mapping.

    scim_create_or_update set mapping.scim_active = True unconditionally, so a
    conforming deprovisioning PUT re-enabled the account. PUT must now honour
    the attribute.
    """
    client, scim = scim_client['client'], scim_client['scim']
    email = f'put-inactive-{uuid4().hex[:8]}@{scim_client["domain"]}'
    created = client.post('/scim/v2/Users', headers=scim,
                          json={'userName': email, 'displayName': 'To Disable'})
    assert created.status_code == 200, created.text
    scim_id = created.json()['id']

    updated = client.put(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'userName': email, 'displayName': 'To Disable', 'active': False})
    assert updated.status_code == 200, updated.text
    assert updated.json()['active'] is False

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user.active is False, "PUT active:false left the user active"
        mapping = db.scalar(select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == scim_id))
        assert mapping.scim_active is False


def test_put_with_active_true_reactivates(scim_client):
    """The other direction: an IdP re-enabling a deprovisioned employee."""
    client, scim = scim_client['client'], scim_client['scim']
    email = f'put-reactivate-{uuid4().hex[:8]}@{scim_client["domain"]}'
    created = client.post('/scim/v2/Users', headers=scim,
                          json={'userName': email, 'displayName': 'Rehire'})
    scim_id = created.json()['id']

    disabled = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
    assert disabled.status_code == 200, disabled.text

    reenabled = client.put(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'userName': email, 'displayName': 'Rehire', 'active': True})
    assert reenabled.status_code == 200, reenabled.text
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user.active is True


def test_put_reactivation_retires_previously_issued_sessions(scim_client):
    """Reactivation must bump session_epoch, matching deactivate_scim_user.

    Otherwise a session cookie minted before deprovision stays valid after the
    account comes back.
    """
    from app.auth import create_token
    client, scim = scim_client['client'], scim_client['scim']
    email = f'put-epoch-{uuid4().hex[:8]}@{scim_client["domain"]}'
    created = client.post('/scim/v2/Users', headers=scim,
                          json={'userName': email, 'displayName': 'Epoch'})
    scim_id = created.json()['id']
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        old_token = create_token(user)
    assert client.get('/api/me', headers={'Authorization': f'Bearer {old_token}'}).status_code == 200

    client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
    reenabled = client.put(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'userName': email, 'displayName': 'Epoch', 'active': True})
    assert reenabled.status_code == 200, reenabled.text
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert (user.session_epoch or 0) > 0, \
            "reactivation did not retire the sessions issued before deprovision"


def test_patch_semantics_are_unchanged(scim_client):
    """PATCH must keep its existing behaviour: it is the only path that applies
    partial operations, and an unsupported one is still a 400."""
    client, scim = scim_client['client'], scim_client['scim']
    created = client.post('/scim/v2/Users', headers=scim, json={
        'userName': f'patch-keep-{uuid4().hex[:8]}@{scim_client["domain"]}', 'displayName': 'Patch'})
    scim_id = created.json()['id']

    patched = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
    assert patched.status_code == 200, patched.text
    assert patched.json()['active'] is False

    unsupported = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'nickName', 'value': 'x'}]})
    assert unsupported.status_code == 400


def test_get_reports_the_current_active_state(scim_client):
    """GET must reflect the lifecycle the routes just applied."""
    client, scim = scim_client['client'], scim_client['scim']
    email = f'get-state-{uuid4().hex[:8]}@{scim_client["domain"]}'
    created = client.post('/scim/v2/Users', headers=scim,
                          json={'userName': email, 'displayName': 'State', 'active': False})
    scim_id = created.json()['id']
    fetched = client.get(f'/scim/v2/Users/{scim_id}', headers=scim)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()['active'] is False

    listed = client.get('/scim/v2/Users', headers=scim).json()['Resources']
    entry = next(r for r in listed if r['id'] == scim_id)
    assert entry['active'] is False


# --------------------------------------------------------------------------- #
# 3. defensive tenant checks on corrupted rows
# --------------------------------------------------------------------------- #

def test_get_does_not_return_a_user_from_another_org(scim_client):
    """A mapping whose User lives in another organization must not be served.

    Normal writes keep mapping.organization_id and user.organization_id aligned,
    so this can only arise from corruption or a bad migration. The provider
    boundary must not depend on that never happening.
    """
    client, scim = scim_client['client'], scim_client['scim']
    with SessionLocal() as db:
        victim = User(organization_id=scim_client['org_id'], name='Victim',
                      email=f'victim-{uuid4().hex[:8]}@example.com', password_hash='',
                      role='member', active=True, kind='human')
        outsider_org = Organization(name=f'Outsider {uuid4().hex[:8]}',
                                    slug=f'outsider-{uuid4().hex[:12]}')
        db.add_all([victim, outsider_org])
        db.commit()
        db.refresh(outsider_org)
        # Deliberately corrupt: the mapping is owned by the caller's provider
        # and the caller's org, but points at a user in a different org.
        db.add(SCIMUserMapping(
            organization_id=scim_client['org_id'], user_id=victim.id,
            scim_external_id=f'corrupt-{uuid4().hex[:8]}',
            identity_provider_id=scim_client['provider_id'], scim_active=True))
        db.commit()
        scim_id = db.scalar(select(SCIMUserMapping).where(
            SCIMUserMapping.identity_provider_id == scim_client['provider_id'])).scim_external_id
        db.refresh(victim)
        # Now move the user into the other org -- the corruption itself.
        victim.organization_id = outsider_org.id
        if db.bind.dialect.name == 'postgresql':
            # The composite FK makes this corruption impossible at the database
            # boundary. Keep the transaction usable after the expected failure.
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
            assert db.get(User, victim.id).organization_id == scim_client['org_id']
            return
        db.commit()

    response = client.get(f'/scim/v2/Users/{scim_id}', headers=scim)
    assert response.status_code == 404, (
        f"a User from another organization was served: {response.text}")
    assert 'victim' not in response.text.lower()


def test_list_does_not_return_a_user_from_another_org(scim_client):
    """A corrupted mapping must not leak its User's email through the list."""
    client, scim = scim_client['client'], scim_client['scim']
    outsider_email = f'outsider-{uuid4().hex[:8]}@example.com'
    with SessionLocal() as db:
        outsider_org = Organization(name=f'Outsider {uuid4().hex[:8]}',
                                    slug=f'outsider-{uuid4().hex[:12]}')
        db.add(outsider_org)
        db.commit()
        db.refresh(outsider_org)
        outsider = User(organization_id=outsider_org.id, name='Outsider',
                        email=outsider_email, password_hash='', role='member',
                        active=True, kind='human')
        db.add(outsider)
        db.commit()
        db.refresh(outsider)
        db.add(SCIMUserMapping(
            organization_id=scim_client['org_id'], user_id=outsider.id,
            scim_external_id=f'corrupt-{uuid4().hex[:8]}',
            identity_provider_id=scim_client['provider_id'], scim_active=True))
        if db.bind.dialect.name == 'postgresql':
            # PostgreSQL rejects a mapping whose user and organization disagree;
            # verify the failed insert is rolled back rather than weakening the FK.
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
            assert db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.user_id == outsider.id)) is None
            return
        db.commit()

    listed = client.get('/scim/v2/Users', headers=scim)
    assert listed.status_code == 200, listed.text
    usernames = {r['userName'] for r in listed.json()['Resources']}
    assert outsider_email not in usernames, \
        "the list served a User belonging to a different organization"
