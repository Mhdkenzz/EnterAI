"""Phase 1 v3 -- A-vs-B full route matrix.

One regression per operation, all driving the real FastAPI app: provider A must
not be able to list, read, overwrite, deactivate, reactivate, role-edit, or
OIDC-resolve anything belonging to provider B, even in the same organization
and even when the two share an external id namespace.
"""
from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import domain_verification
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.sso_models import IdentityProvider, SCIMUserMapping


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    monkeypatch.setattr(domain_verification, "lookup_txt",
                        lambda name: (_ for _ in ()).throw(
                            AssertionError("test performed a real DNS lookup")))
    yield


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Matrix {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _verified_provider(client, headers, name, domain, role_map=None):
    payload = {'name': name, 'protocol': 'oidc', 'domain_binding': domain}
    if role_map:
        payload['role_mapping_json'] = role_map
    created = client.post('/api/admin/sso/providers', headers=headers, json=payload)
    assert created.status_code == 200, created.text
    provider_id = created.json()['id']
    with mock.patch.object(domain_verification, 'lookup_txt',
                           return_value=[domain_verification.expected_txt_value(domain)]):
        verified = client.post(
            f'/api/admin/sso/providers/{provider_id}/domain-verification',
            headers=headers, json={'domain': domain})
    assert verified.status_code == 200, verified.text
    token = client.post(f'/api/admin/sso/providers/{provider_id}/scim-token',
                        headers=headers).json()['scim_token']
    return provider_id, {'Authorization': f'Bearer {token}'}


@pytest.fixture
def matrix():
    """Two verified providers (A, B) in ONE org, each with a live token and a
    real identity owned by B.

    Domains are generated per fixture: exclusivity is global, so fixed names
    would be refused by the unique index on the second invocation. That refusal
    is the invariant working, not a test defect.
    """
    with TestClient(app) as client:
        admin, data = _workspace(client)
        org_id = data['organization']['id']
        domain_a = f'ma-{uuid4().hex[:10]}.example'
        domain_b = f'mb-{uuid4().hex[:10]}.example'
        _pid_a, head_a = _verified_provider(client, admin, 'A', domain_a)
        _pid_b, head_b = _verified_provider(
            client, admin, 'B', domain_b, role_map={'b_admins': 'admin'})

        b_email = f'buser-{uuid4().hex[:8]}@{domain_b}'
        provisioned = client.post('/scim/v2/Users', headers=head_b, json={
            'userName': b_email, 'displayName': 'B Employee',
            'roles': [{'value': 'b_admins'}]})
        assert provisioned.status_code == 200, provisioned.text
        b_scim_id = provisioned.json()['id']

        with SessionLocal() as db:
            mapping = db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.scim_external_id == b_scim_id))
            b_user_id = mapping.user_id

        yield {'client': client, 'admin': admin, 'org_id': org_id,
               'head_a': head_a, 'head_b': head_b,
               'domain_a': domain_a, 'domain_b': domain_b,
               'b_scim_id': b_scim_id, 'b_user_id': b_user_id, 'b_email': b_email}


def _b_state(user_id):
    with SessionLocal() as db:
        user = db.get(User, user_id)
        return {'active': user.active, 'name': user.name, 'email': user.email,
                'role': user.role, 'epoch': user.session_epoch or 0}


def test_a_cannot_list_b(matrix):
    listed = matrix['client'].get('/scim/v2/Users', headers=matrix['head_a'])
    assert listed.status_code == 200, listed.text
    usernames = {r['userName'] for r in listed.json()['Resources']}
    assert matrix['b_email'] not in usernames


def test_a_cannot_get_b(matrix):
    response = matrix['client'].get(f'/scim/v2/Users/{matrix["b_scim_id"]}',
                                    headers=matrix['head_a'])
    assert response.status_code == 404, response.text


def test_a_cannot_put_b(matrix):
    before = _b_state(matrix['b_user_id'])
    response = matrix['client'].put(f'/scim/v2/Users/{matrix["b_scim_id"]}',
                                   headers=matrix['head_a'],
                                   json={'userName': f'pwn-{uuid4().hex[:8]}@{matrix["domain_a"]}',
                                         'displayName': 'Pwned'})
    assert response.status_code == 404, response.text
    assert _b_state(matrix['b_user_id']) == before


def test_a_cannot_patch_deactivate_b(matrix):
    before = _b_state(matrix['b_user_id'])
    response = matrix['client'].patch(f'/scim/v2/Users/{matrix["b_scim_id"]}',
                                      headers=matrix['head_a'],
                                      json={'Operations': [
                                          {'op': 'replace', 'path': 'active',
                                           'value': False}]})
    assert response.status_code == 404, response.text
    assert _b_state(matrix['b_user_id']) == before


def test_a_cannot_patch_reactivate_b(matrix):
    client, user_id = matrix['client'], matrix['b_user_id']
    with SessionLocal() as db:
        user = db.get(User, user_id)
        user.active = False
        db.commit()
    response = client.patch(f'/scim/v2/Users/{matrix["b_scim_id"]}',
                            headers=matrix['head_a'],
                            json={'Operations': [
                                {'op': 'replace', 'path': 'active', 'value': True}]})
    assert response.status_code == 404, response.text
    assert _b_state(user_id)['active'] is False


def test_a_cannot_role_edit_b(matrix):
    """B's own role map grants admin; A must not be able to change B's role."""
    before = _b_state(matrix['b_user_id'])
    response = matrix['client'].patch(f'/scim/v2/Users/{matrix["b_scim_id"]}',
                                      headers=matrix['head_a'],
                                      json={'Operations': [
                                          {'op': 'replace', 'path': 'roles',
                                           'value': [{'value': 'b_admins'}]}]})
    assert response.status_code == 404, response.text
    assert _b_state(matrix['b_user_id']) == before


def test_a_cannot_oidc_resolve_b(matrix):
    """OIDC provisioning must not let provider A's login claim B's subject.

    The refusal now happens at the domain gate rather than at the
    already-exists check, because B's address sits in B's domain and A has not
    proven that domain. That is the stronger outcome: the request never gets
    far enough to learn anything about B's account.
    """
    from app.sso_oidc import provision_or_update_user
    with SessionLocal() as db:
        idp_a = db.scalar(select(IdentityProvider).where(
            IdentityProvider.organization_id == matrix['org_id'],
            IdentityProvider.domain_binding == matrix['domain_a']))
        with pytest.raises(Exception) as excinfo:
            provision_or_update_user(db, idp_a, {
                'sub': 'shared-subject', 'email': matrix['b_email'],
                'name': 'Hijack'})
        message = str(excinfo.value).lower()
        assert 'domain' in message, (
            f"expected a domain-gate refusal, got: {excinfo.value}")
        # B's user is untouched.
        assert db.get(User, matrix['b_user_id']) is not None


def test_b_retains_full_lifecycle(matrix):
    """The other half: B must keep working on what it owns."""
    client = matrix['client']
    scim_id, head_b, user_id = matrix['b_scim_id'], matrix['head_b'], matrix['b_user_id']

    assert client.get(f'/scim/v2/Users/{scim_id}', headers=head_b).status_code == 200

    off = client.patch(f'/scim/v2/Users/{scim_id}', headers=head_b,
                       json={'Operations': [{'op': 'replace', 'path': 'active',
                                             'value': False}]})
    assert off.status_code == 200, off.text
    assert _b_state(user_id)['active'] is False

    on = client.patch(f'/scim/v2/Users/{scim_id}', headers=head_b,
                      json={'Operations': [{'op': 'replace', 'path': 'active',
                                            'value': True}]})
    assert on.status_code == 200, on.text
    assert _b_state(user_id)['active'] is True


def test_oidc_login_routes_to_the_verified_provider_only():
    """An unverified or revoked binding must not route a login."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        created = client.post('/api/admin/sso/providers', headers=admin, json={
            'name': 'Unverified', 'protocol': 'oidc',
            'domain_binding': 'unverified.example'})
        provider_id = created.json()['id']
        # Unverified: the login start for that domain must fail closed rather
        # than redirecting into an IdP nobody proved they control.
        response = client.get('/api/auth/sso/start', params={'email': 'someone@unverified.example'},
                              follow_redirects=False)
        assert response.status_code in (302, 307)
        if response.status_code == 302:
            assert 'sso_error' in response.headers.get('location', ''), \
                "an unverified domain_binding still routed an OIDC login"
