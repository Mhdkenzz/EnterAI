"""Phase 1, group 1 — SCIM mappings are owned by an identity provider, not just
an organization.

Before this group, every SCIM read and every mutation looked a mapping up by
`organization_id` + `scim_external_id` alone (sso_routes.py's list/GET/PUT/PATCH,
sso_auth.scim_create_or_update, sso_oidc.provision_or_update_user) and
`SCIMUserMapping` had no provider column to scope by. Two providers in the same
organization therefore shared one namespace: a bearer token minted for provider
A could read, overwrite, deactivate, reactivate, and enumerate identities that
belonged to provider B, including B's admins.

Every test here is adversarial and route-level: it drives the real FastAPI app
against the real Alembic-migrated database (the same convention as
test_sso_phase14.py, which documents why an isolated `Base.metadata.create_all()`
schema previously hid missing migrations), and it asserts the *denial*, not just
the success path. Organization isolation, the global email-collision rule, and
SCIM token authentication are exercised alongside the new boundary so a
regression in one cannot be traded for another.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import domain_verification
from app.database import SessionLocal
from app.main import app
from app.sso_models import IdentityProvider, SCIMUserMapping


def _workspace(client):
    """Register a fresh org and return (admin headers, registration payload)."""
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'SCIM Iso {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _provider_with_token(client, admin_headers, name):
    """Create a VERIFIED provider in the caller's org and mint its SCIM token.

    Returns (provider_id, scim_headers, domain). Verification is required: a
    provider that has not proven its domain may not provision anything, so an
    unverified fixture would fail at the production gate rather than at the
    isolation boundary this file is testing.
    """
    from conftest import make_verified_provider
    return make_verified_provider(client, admin_headers, name)


@pytest.fixture
def two_providers():
    """Two SCIM-authenticated providers (A = attacker, B = victim) in ONE org,
    each with a live bearer token and a real mapping owned by B."""
    with TestClient(app) as client:
        admin_headers, data = _workspace(client)
        org_id = data['organization']['id']
        provider_a, headers_a, _domain_a = _provider_with_token(
            client, admin_headers, 'IdP A')
        provider_b, headers_b, domain_b = _provider_with_token(
            client, admin_headers, 'IdP B')

        # Provider B provisions its own user. This mapping belongs to B alone.
        victim_email = f'victim-{uuid4().hex[:8]}@{domain_b}'
        provisioned = client.post('/scim/v2/Users', headers=headers_b, json={
            'userName': victim_email, 'displayName': 'B Employee'})
        assert provisioned.status_code == 200, provisioned.text
        victim_scim_id = provisioned.json()['id']

        with SessionLocal() as db:
            victim_mapping = db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.organization_id == org_id,
                SCIMUserMapping.scim_external_id == victim_scim_id))
            victim_user_id = victim_mapping.user_id

        yield {
            'client': client,
            'admin_headers': admin_headers,
            'org_id': org_id,
            'provider_a': provider_a,
            'headers_a': headers_a,
            'provider_b': provider_b,
            'headers_b': headers_b,
            'domain_a': _domain_a,
            'domain_b': domain_b,
            'victim_scim_id': victim_scim_id,
            'victim_user_id': victim_user_id,
            'victim_email': victim_email,
        }


def _victim_state(user_id):
    with SessionLocal() as db:
        from app.models import User
        user = db.get(User, user_id)
        return {'active': user.active, 'name': user.name, 'email': user.email, 'role': user.role}


# --- list -------------------------------------------------------------------

def test_provider_a_cannot_enumerate_provider_b_users(two_providers):
    """GET /scim/v2/Users as provider A must not disclose B's identities.

    Same organization, so organization isolation is satisfied and does nothing
    here; the list was scoped only by org.
    """
    listed = two_providers['client'].get('/scim/v2/Users', headers=two_providers['headers_a'])
    assert listed.status_code == 200, listed.text
    body = listed.json()
    usernames = {r['userName'] for r in body['Resources']}
    ids = {r['id'] for r in body['Resources']}
    assert two_providers['victim_email'] not in usernames
    assert two_providers['victim_scim_id'] not in ids
    assert body['totalResults'] == 0


def test_provider_a_still_sees_its_own_users(two_providers):
    """The boundary is provider ownership, not 'SCIM is broken' -- A's own
    provisioning round-trip must still work, or the fix is simply denial."""
    client = two_providers['client']
    own_email = f'own-{uuid4().hex[:8]}@{two_providers["domain_a"]}'
    created = client.post('/scim/v2/Users', headers=two_providers['headers_a'],
                          json={'userName': own_email, 'displayName': 'A Employee'})
    assert created.status_code == 200, created.text
    own_scim_id = created.json()['id']

    listed = client.get('/scim/v2/Users', headers=two_providers['headers_a'])
    assert listed.status_code == 200, listed.text
    usernames = {r['userName'] for r in listed.json()['Resources']}
    assert own_email in usernames
    assert two_providers['victim_email'] not in usernames

    fetched = client.get(f'/scim/v2/Users/{own_scim_id}', headers=two_providers['headers_a'])
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()['userName'] == own_email


# --- read -------------------------------------------------------------------

def test_provider_a_cannot_get_provider_b_user(two_providers):
    response = two_providers['client'].get(
        f'/scim/v2/Users/{two_providers["victim_scim_id"]}',
        headers=two_providers['headers_a'])
    assert response.status_code == 404, response.text
    assert two_providers['victim_email'] not in response.text


# --- write ------------------------------------------------------------------

def test_provider_a_cannot_put_update_provider_b_user(two_providers):
    """PUT previously re-resolved the mapping by org+external id and wrote
    straight through scim_create_or_update, letting A rewrite B's user."""
    before = _victim_state(two_providers['victim_user_id'])
    response = two_providers['client'].put(
        f'/scim/v2/Users/{two_providers["victim_scim_id"]}',
        headers=two_providers['headers_a'],
        json={'userName': f'attacker-{uuid4().hex[:8]}@evil.example.com',
              'displayName': 'Pwned By A'})
    assert response.status_code == 404, response.text
    assert _victim_state(two_providers['victim_user_id']) == before


def test_provider_a_cannot_patch_deactivate_provider_b_user(two_providers):
    """The highest-impact variant: deactivation retires the victim's session
    (session_epoch bump), so A could lock every B-provisioned human out of the
    workspace while B's IdP still believed them active."""
    before = _victim_state(two_providers['victim_user_id'])
    assert before['active'] is True

    response = two_providers['client'].patch(
        f'/scim/v2/Users/{two_providers["victim_scim_id"]}',
        headers=two_providers['headers_a'],
        json={'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
    assert response.status_code == 404, response.text
    assert _victim_state(two_providers['victim_user_id']) == before


def test_provider_a_cannot_patch_reactivate_provider_b_user(two_providers):
    """Reactivation is the mirror image: B deprovisions its own user, then A
    PATCHes active=true to reinstate an account its own IdP removed."""
    client = two_providers['client']
    with SessionLocal() as db:
        from app.models import User
        victim = db.get(User, two_providers['victim_user_id'])
        victim.active = False
        db.commit()

    response = client.patch(
        f'/scim/v2/Users/{two_providers["victim_scim_id"]}',
        headers=two_providers['headers_a'],
        json={'Operations': [{'op': 'replace', 'path': 'active', 'value': True}]})
    assert response.status_code == 404, response.text
    assert _victim_state(two_providers['victim_user_id'])['active'] is False


def test_provider_a_cannot_escalate_role_on_provider_b_user(two_providers):
    """Role mapping is resolved against the *authenticated* provider's config;
    cross-provider PATCH would have let A grant itself admin on B's user."""
    response = two_providers['client'].patch(
        f'/scim/v2/Users/{two_providers["victim_scim_id"]}',
        headers=two_providers['headers_a'],
        json={'Operations': [{'op': 'replace', 'path': 'roles',
                              'value': [{'value': 'admin_group'}]}]})
    assert response.status_code == 404, response.text
    assert _victim_state(two_providers['victim_user_id'])['role'] != 'admin'


# --- provider B keeps working ------------------------------------------------

def test_provider_b_retains_full_lifecycle_on_its_own_user(two_providers):
    """The fix must not break the legitimate flow: B still GETs, PATCHes
    (deactivate + reactivate), and PUTs the identity it owns."""
    client = two_providers['client']
    scim_id = two_providers['victim_scim_id']
    headers_b = two_providers['headers_b']

    fetched = client.get(f'/scim/v2/Users/{scim_id}', headers=headers_b)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()['userName'] == two_providers['victim_email']

    deactivated = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers_b,
                               json={'Operations': [{'op': 'replace', 'path': 'active',
                                                     'value': False}]})
    assert deactivated.status_code == 200, deactivated.text
    assert _victim_state(two_providers['victim_user_id'])['active'] is False

    reactivated = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers_b,
                               json={'Operations': [{'op': 'replace', 'path': 'active',
                                                     'value': True}]})
    assert reactivated.status_code == 200, reactivated.text
    assert _victim_state(two_providers['victim_user_id'])['active'] is True

    renamed = client.put(f'/scim/v2/Users/{scim_id}', headers=headers_b,
                         json={'userName': two_providers['victim_email'],
                               'displayName': 'Renamed By B'})
    assert renamed.status_code == 200, renamed.text
    assert _victim_state(two_providers['victim_user_id'])['name'] == 'Renamed By B'


# --- cross-org isolation and email collision must survive -------------------

def test_cross_org_provider_cannot_reach_other_org_mapping():
    """Organization isolation is unchanged and must stay intact: a different
    org's provider gets nothing, and the victim org's own provider is
    unaffected."""
    with TestClient(app) as client:
        admin_a, data_a = _workspace(client)
        _pid_a, headers_a, domain_a = _provider_with_token(client, admin_a, 'Org A IdP')
        provisioned = client.post('/scim/v2/Users', headers=headers_a, json={
            'userName': f'org-a-{uuid4().hex[:8]}@{domain_a}', 'displayName': 'A User'})
        assert provisioned.status_code == 200, provisioned.text

        admin_b, _ = _workspace(client)
        _pid_b, headers_b, _domain_b = _provider_with_token(client, admin_b, 'Org B IdP')
        listed_b = client.get('/scim/v2/Users', headers=headers_b)
        assert listed_b.status_code == 200, listed_b.text
        assert listed_b.json()['totalResults'] == 0
        got_b = client.get(f'/scim/v2/Users/{provisioned.json()["id"]}', headers=headers_b)
        assert got_b.status_code == 404


def test_email_collision_between_providers_still_returns_409():
    """users.email is globally unique, so a genuine collision must stay a clean
    4xx, never a raw 500.

    Reaches the collision by a route the domain gate does not intercept: the
    address already exists globally and belongs to NO provider, so the first
    POST is a legitimate create and a second POST for it collides. The 409 must
    come from the email constraint itself.

    Note a same-provider re-POST is NOT a collision: the external id resolves to
    the existing mapping, so SCIM treats it as an idempotent update and returns
    200. A true cross-provider collision cannot be constructed through the
    route, because a provider cannot provision outside its own verified domain
    and two providers cannot share a domain -- which is precisely the oracle
    fix. The pre-existing-account case is therefore exercised directly, against
    the real global unique constraint, rather than through a route that is now
    closed by design.
    """
    with TestClient(app) as client:
        admin_headers, data = _workspace(client)
        org_id = data['organization']['id']
        _pid_a, headers_a, domain_a = _provider_with_token(client, admin_headers, 'IdP A')
        _pid_b, headers_b, _domain_b = _provider_with_token(client, admin_headers, 'IdP B')

        # Inside A's verified domain: a legitimate create.
        shared_email = f'shared-{uuid4().hex[:8]}@{domain_a}'
        first = client.post('/scim/v2/Users', headers=headers_a,
                            json={'userName': shared_email, 'displayName': 'A claims it'})
        assert first.status_code == 200, first.text

        # A re-POST for the same external id is an update, not a conflict.
        again = client.post('/scim/v2/Users', headers=headers_a,
                            json={'userName': shared_email, 'displayName': 'A again'})
        assert again.status_code == 200, again.text
        assert again.json()['id'] == first.json()['id']

        # A pre-existing account that A's provider does not own. This is the
        # genuine 409 path: the address exists, is not linked to this provider,
        # and the preflight refuses it rather than hijacking the user.
        from app.sso_auth import scim_create_or_update
        from fastapi import HTTPException
        from app.models import User

        foreign_email = f'foreign-{uuid4().hex[:8]}@{domain_a}'
        with SessionLocal() as db:
            db.add(User(organization_id=org_id, name='Pre-existing',
                        email=foreign_email, password_hash='', role='member',
                        active=True, kind='human'))
            db.commit()

        with SessionLocal() as db:
            with pytest.raises(HTTPException) as excinfo:
                scim_create_or_update(db, org_id, 'ext-foreign', foreign_email,
                                      'Claimed', source_provider_id=_pid_a)
            assert excinfo.value.status_code == 409

        # And B probing A's domain is refused outright -- the oracle is closed,
        # and identically so whether or not the address exists.
        probe_hit = client.post('/scim/v2/Users', headers=headers_b,
                                json={'userName': shared_email, 'displayName': 'B probes'})
        probe_miss = client.post('/scim/v2/Users', headers=headers_b,
                                 json={'userName': f'ghost-{uuid4().hex[:8]}@{domain_a}',
                                       'displayName': 'B probes'})
        # A stable 4xx is the contract; the exact code is not (v5 unified the
        # domain gate and address validation into one refusal).
        assert 400 <= probe_hit.status_code < 500
        assert probe_hit.status_code == probe_miss.status_code
        assert probe_hit.json() == probe_miss.json()


# --- model / schema level ---------------------------------------------------

def test_new_mappings_record_provider_ownership(two_providers):
    """A mapping created through the SCIM route must carry the provider that
    created it, in the row itself -- route-level filtering alone would not
    make the boundary durable."""
    with SessionLocal() as db:
        victim_mapping = db.scalar(select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == two_providers['victim_scim_id']))
        assert victim_mapping.identity_provider_id == two_providers['provider_b']


def test_schema_enforces_provider_ownership_in_the_model():
    """The ownership column is part of the model (with a real foreign key), so
    a mapping cannot name a provider that does not exist.

    v5 added a second, composite FK on (identity_provider_id, organization_id)
    so the mapping's tenant cannot drift from the provider's. Select the
    single-column FK explicitly rather than unpacking whatever is there.
    """
    from app.sso_models import SCIMUserMapping as Model
    column = Model.__table__.columns['identity_provider_id']
    assert column.nullable is True  # legacy rows stay unowned; see migration
    single = [fk for fk in column.foreign_keys
              if list(fk.constraint.columns.keys()) == ['identity_provider_id']]
    assert len(single) == 1, f"expected one provider FK, found {single}"
    assert single[0].column.table.name == 'identity_providers'


def test_oidc_mapping_is_tied_to_the_oidc_provider_that_authenticated():
    """OIDC reuses SCIMUserMapping. A subject provisioned through provider A
    must be owned by A, and provider B's login must never resolve into it --
    the same namespace-collision flaw, one protocol over."""
    from app.sso_oidc import provision_or_update_user

    with TestClient(app) as client:
        admin_headers, data = _workspace(client)
        org_id = data['organization']['id']
        # Both providers need VERIFIED domains: OIDC provisioning creates and
        # links users, so it is gated on domain proof exactly like SCIM.
        # Domains are per-test because ownership is globally exclusive.
        domain_a = f'oidc-a-{uuid4().hex[:10]}.example'
        with SessionLocal() as db:
            idp_a = IdentityProvider(organization_id=org_id, name='OIDC A', protocol='oidc',
                                     domain_binding=domain_a, active=True,
                                     domain_verified_at=domain_verification.utcnow())
            db.add(idp_a)
            db.commit()
            db.refresh(idp_a)
            idp_a_id = idp_a.id

            claims = {'sub': 'shared-subject-1', 'email': f'{uuid4().hex[:8]}@{domain_a}',
                      'name': 'OIDC A User'}
            user = provision_or_update_user(db, idp_a, claims)
            db.commit()

            mapping = db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.user_id == user.id))
            assert mapping is not None
            assert mapping.identity_provider_id == idp_a_id

            # A second, unrelated provider in the same org must not resolve
            # that subject to the same account. It gets its OWN verified domain
            # and replays A's claims verbatim -- same subject, same email.
            # The domain gate refuses first, because the replayed address is in
            # A's domain and B has not proven it. That is the stronger outcome:
            # the refusal happens before B can learn anything about the
            # existing account. (The already-exists check is unreachable for a
            # replayed claim, since any address that would satisfy it is by
            # definition inside the wrong domain.)
            domain_b = f'oidc-b-{uuid4().hex[:10]}.example'
            idp_b = IdentityProvider(organization_id=org_id, name='OIDC B', protocol='oidc',
                                     domain_binding=domain_b, active=True,
                                     domain_verified_at=domain_verification.utcnow())
            db.add(idp_b)
            db.commit()
            db.refresh(idp_b)

            with pytest.raises(Exception) as excinfo:
                provision_or_update_user(db, idp_b, dict(claims))
            assert 'domain' in str(excinfo.value).lower()
            # And nothing was silently written: A's mapping still belongs to A.
            db.expire_all()
            assert db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.user_id == user.id)).identity_provider_id == idp_a_id
