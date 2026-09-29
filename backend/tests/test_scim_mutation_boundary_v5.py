"""Phase 1 v5 -- the SCIM provisioning/mutation boundary.

Defects this file pins shut (v5 brief items 1 and 6):

* SCIM PATCH /Users/{id} accepted a `userName` replacement in ANY domain. It ran
  a global `users.email` uniqueness query and answered 409 on a hit, so a
  provider holding its own token could both re-point an identity at a foreign
  domain AND use the endpoint as an account-existence oracle.
* SCIM POST accepted an empty or malformed `userName`, which skipped the domain
  gate entirely (the gate is `if email and ...`) and produced a synthetic
  `@enterai.local` address.
* SCIM PUT silently ignored a submitted `userName` change, so an IdP that
  renamed a person could never correct the address.

The invariant every one of these tests asserts: a provisioning/mutation
attempt with a malformed or foreign address is refused with ONE stable 4xx,
before any User or mapping lookup, and the response is byte-identical whether
or not the target address is already taken. Identity is never reachable for the
answer.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import User
from app.sso_models import SCIMUserMapping


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Boundary {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _verified_provider(client, admin_headers, name, role_map=None):
    from conftest import make_verified_provider
    return make_verified_provider(client, admin_headers, name, role_map=role_map)


def _provision(client, scim_headers, email, external_id=None):
    return client.post('/scim/v2/Users', headers=scim_headers, json={
        'userName': email,
        'displayName': 'Person',
        **({'externalId': external_id} if external_id else {})})


@pytest.fixture
def env():
    """A workspace, one domain-verified SCIM provider, and its domain."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        pid, scim, domain = _verified_provider(client, admin, 'Acme')
        yield {'client': client, 'admin': admin, 'org': data['organization']['id'],
               'provider': pid, 'scim': scim, 'domain': domain}


# --------------------------------------------------------------------------- #
# the validator itself
# --------------------------------------------------------------------------- #

MALFORMED_USER_NAMES = [
    '',                                  # empty
    '   ',                               # whitespace only
    'notanemail',                        # bare local part, no domain
    env_example := 'example.com',        # a bare DOMAIN, not an address
    'a@b@example.com',                   # two '@'
    '@example.com',                      # no local part
    'user@',                             # no domain
    'user@localhost',                    # no dot in the domain
    'user name@example.com',             # space in the local part
    'user@exam ple.com',                 # space in the domain
    'user@-example.com',                 # label starts with a dash
    'user@example-.com',                 # label ends with a dash
    'user@.example.com',                 # empty leading label
    'user@example..com',                 # empty inner label
    'user@example.com.',                 # trailing dot: a domain, not an address
]


@pytest.mark.parametrize('bad', MALFORMED_USER_NAMES)
def test_strict_validator_rejects_malformed(bad):
    """The single validator used by every provisioning/mutation path."""
    from app.domain_verification import normalize_provisioning_email
    with pytest.raises(ValueError):
        normalize_provisioning_email(bad)


def test_strict_validator_normalizes_a_real_address():
    from app.domain_verification import normalize_provisioning_email
    assert normalize_provisioning_email('  User.Name@EXAMPLE.com  ') == \
        'User.Name@example.com'


def test_routing_parser_is_not_reused_for_full_addresses():
    """The brief's explicit requirement. email_domain() accepts a bare domain
    (correct for login routing); provisioning must not inherit that leniency, or
    'example.com' would pass as an address."""
    from app.domain_verification import email_domain, normalize_provisioning_email
    assert email_domain('example.com') == 'example.com'      # routing: a domain is fine
    with pytest.raises(ValueError):
        normalize_provisioning_email('example.com')          # provisioning: it is not


# --------------------------------------------------------------------------- #
# POST: malformed and foreign addresses, refused before any lookup
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('bad', ['', 'notanemail', 'example.com', 'a@b@example.com',
                                 'user@', '@example.com', 'user@example.com.'])
def test_post_refuses_malformed_username(env, bad):
    response = _provision(env['client'], env['scim'], bad)
    assert 400 <= response.status_code < 500, response.text
    assert response.status_code in (400, 422), response.text


def test_post_refuses_foreign_domain(env):
    response = _provision(env['client'], env['scim'],
                          f'person@foreign-{uuid4().hex[:8]}.example')
    assert 400 <= response.status_code < 500
    assert response.status_code != 500


def test_post_refusal_is_identical_for_taken_and_absent_foreign_addresses(env):
    """The oracle. A taken foreign address and an absent one must be
    indistinguishable, because no email lookup may run at all."""
    client, scim = env['client'], env['scim']
    with SessionLocal() as db:
        db.add(User(organization_id=env['org'], name='Victim',
                    email=f'taken@foreign-{uuid4().hex[:8]}.example',
                    password_hash='', role='member', active=True, kind='human'))
        db.commit()
        taken = db.scalar(db.query(User).order_by(User.created_at.desc()).limit(1)).email
    absent = f'absent-{uuid4().hex[:8]}@foreign-{uuid4().hex[:8]}.example'

    first = _provision(client, scim, taken)
    second = _provision(client, scim, absent)
    assert first.status_code == second.status_code
    assert first.json() == second.json(), (
        "the two refusals differ, so the endpoint still distinguishes an "
        f"existing account: {first.json()} vs {second.json()}")


def test_post_in_domain_address_still_works(env):
    """Not a blanket denial."""
    email = f'ok-{uuid4().hex[:8]}@{env["domain"]}'
    response = _provision(env['client'], env['scim'], email)
    assert response.status_code == 200, response.text
    assert response.json()['userName'] == email


# --------------------------------------------------------------------------- #
# PATCH: the confirmed defect
# --------------------------------------------------------------------------- #

def test_patch_cannot_move_an_identity_to_a_foreign_domain(env):
    client, scim = env['client'], env['scim']
    email = f'mover-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, email).status_code == 200
    scim_id = _mapping_id(email, env['org'])

    response = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'userName',
                        'value': f'x@foreign-{uuid4().hex[:8]}.example'}]})
    assert 400 <= response.status_code < 500, response.text
    # And the address really did not move.
    with SessionLocal() as db:
        assert db.scalar(db.query(User).filter(User.email == email)) is not None


def test_patch_foreign_refusal_does_not_reveal_whether_the_address_is_taken(env):
    client, scim = env['client'], env['scim']
    email = f'probe-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, email).status_code == 200
    scim_id = _mapping_id(email, env['org'])

    taken = f'taken@foreign-{uuid4().hex[:8]}.example'
    with SessionLocal() as db:
        db.add(User(organization_id=env['org'], name='Victim', email=taken,
                    password_hash='', role='member', active=True, kind='human'))
        db.commit()

    a = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'userName', 'value': taken}]})
    b = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'userName',
                        'value': f'free@foreign-{uuid4().hex[:8]}.example'}]})
    assert a.status_code == b.status_code
    assert a.json() == b.json(), (
        f"PATCH still answers the existence question: {a.json()} vs {b.json()}")


@pytest.mark.parametrize('bad', ['', 'notanemail', 'example.com', 'user@localhost'])
def test_patch_refuses_malformed_username(env, bad):
    client, scim = env['client'], env['scim']
    email = f'mal-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, email).status_code == 200
    scim_id = _mapping_id(email, env['org'])
    response = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'userName', 'value': bad}]})
    assert response.status_code in (400, 422), response.text


def test_patch_valid_in_domain_rename_succeeds(env):
    client, scim = env['client'], env['scim']
    old = f'old-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, old).status_code == 200
    scim_id = _mapping_id(old, env['org'])
    new = f'new-{uuid4().hex[:8]}@{env["domain"]}'

    response = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'userName', 'value': new}]})
    assert response.status_code == 200, response.text
    assert response.json()['userName'] == new
    with SessionLocal() as db:
        assert db.scalar(db.query(User).filter(User.email == new)) is not None
        assert db.scalar(db.query(User).filter(User.email == old)) is None


def test_patch_rename_onto_a_taken_address_is_a_clean_4xx(env):
    """In-domain, but the address belongs to someone else."""
    client, scim = env['client'], env['scim']
    a = f'a-{uuid4().hex[:8]}@{env["domain"]}'
    b = f'b-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, a).status_code == 200
    assert _provision(client, scim, b).status_code == 200
    scim_id = _mapping_id(a, env['org'])
    response = client.patch(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'Operations': [{'op': 'replace', 'path': 'userName', 'value': b}]})
    assert response.status_code == 409, response.text


def test_patch_cannot_address_another_providers_identity(env):
    """Cross-provider paths stay inaccessible even through PATCH."""
    client, admin = env['client'], env['admin']
    other_pid, other_scim, other_domain = _verified_provider(client, admin, 'Other')
    other_email = f'other-{uuid4().hex[:8]}@{other_domain}'
    assert _provision(client, other_scim, other_email).status_code == 200
    other_id = _mapping_id(other_email, env['org'])

    response = client.patch(f'/scim/v2/Users/{other_id}', headers=env['scim'], json={
        'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]})
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# PUT: must honour a submitted userName change (v5 item 6)
# --------------------------------------------------------------------------- #

def test_put_honours_a_valid_in_domain_username_change(env):
    client, scim = env['client'], env['scim']
    old = f'put-old-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, old).status_code == 200
    scim_id = _mapping_id(old, env['org'])
    new = f'put-new-{uuid4().hex[:8]}@{env["domain"]}'

    response = client.put(f'/scim/v2/Users/{scim_id}', headers=scim,
                          json={'userName': new, 'displayName': 'Person'})
    assert response.status_code == 200, response.text
    assert response.json()['userName'] == new
    with SessionLocal() as db:
        assert db.scalar(db.query(User).filter(User.email == new)) is not None


def test_put_refuses_a_foreign_domain_username(env):
    client, scim = env['client'], env['scim']
    old = f'putf-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, old).status_code == 200
    scim_id = _mapping_id(old, env['org'])
    response = client.put(f'/scim/v2/Users/{scim_id}', headers=scim, json={
        'userName': f'x@foreign-{uuid4().hex[:8]}.example', 'displayName': 'Person'})
    assert 400 <= response.status_code < 500
    assert response.status_code != 500


def test_put_refuses_malformed_username(env):
    client, scim = env['client'], env['scim']
    old = f'putm-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, old).status_code == 200
    scim_id = _mapping_id(old, env['org'])
    response = client.put(f'/scim/v2/Users/{scim_id}', headers=scim,
                          json={'userName': 'not-an-email', 'displayName': 'Person'})
    assert response.status_code in (400, 422), response.text


def test_put_keeps_the_address_when_userName_is_unchanged(env):
    """PUT is a full replace, but an unchanged userName must not churn."""
    client, scim = env['client'], env['scim']
    email = f'stable-{uuid4().hex[:8]}@{env["domain"]}'
    assert _provision(client, scim, email).status_code == 200
    scim_id = _mapping_id(email, env['org'])
    response = client.put(f'/scim/v2/Users/{scim_id}', headers=scim,
                          json={'userName': email, 'displayName': 'Renamed'})
    assert response.status_code == 200, response.text
    assert response.json()['userName'] == email
    assert response.json()['displayName'] == 'Renamed'


def _mapping_id(email, org_id):
    with SessionLocal() as db:
        row = db.query(SCIMUserMapping).filter(
            SCIMUserMapping.organization_id == org_id).join(
            User, User.id == SCIMUserMapping.user_id).filter(
            User.email == email).one()
        return row.scim_external_id
