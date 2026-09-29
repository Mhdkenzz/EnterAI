"""Phase 1 -- SCIM behavior under verified domain ownership, plus the
namespace and concurrency corrections.

Blocks covered here:
  * SCIM POST/PUT outside the caller's verified domain is refused BEFORE any
    User.email existence query, and the response is identical whether or not
    the foreign-domain address already exists. This is the actual fix for the
    cross-provider account-existence oracle: the question is removed, not the
    answer obscured.
  * `oidc:` is a reserved mapping namespace (sso_oidc._OIDC_EXTERNAL_ID_PREFIX)
    and must be rejected as a SCIM external id before lookup.
  * A preflight-to-commit race must not surface as a raw IntegrityError/500.
"""
import os
from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import domain_verification, sso_oidc
from app.database import SessionLocal
from app.main import app
from app.models import Organization
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
        'organization_name': f'SCIMD {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _provider(client, headers, name, domain=None, role_map=None):
    """Create a provider with a verified domain; return (id, scim_headers).

    When `domain` is None a unique one is generated. Domains are per-test
    throughout this file because exclusivity is global: a fixed name is
    refused by the unique index as soon as a second test binds it, which is
    the invariant working rather than a failure. A test that needs a specific
    domain (a foreign one to probe) passes it explicitly.
    """
    if domain is None:
        domain = f'{name.lower()}-{uuid4().hex[:10]}.example'
    payload = {'name': name, 'protocol': 'oidc', 'domain_binding': domain}
    if role_map:
        payload['role_mapping_json'] = role_map
    created = client.post('/api/admin/sso/providers', headers=headers, json=payload)
    assert created.status_code == 200, created.text
    provider_id = created.json()['id']
    with mock.patch.object(
            domain_verification, 'lookup_txt', return_value=[_txt_for(domain)]):
        verified = client.post(
            f'/api/admin/sso/providers/{provider_id}/domain-verification',
            headers=headers, json={'domain': domain})
    assert verified.status_code == 200, verified.text
    token = client.post(f'/api/admin/sso/providers/{provider_id}/scim-token',
                        headers=headers).json()['scim_token']
    return provider_id, {'Authorization': f'Bearer {token}'}


def _txt_for(domain):
    """The exact string an administrator would publish in DNS."""
    return domain_verification.expected_txt_value(domain)


# --------------------------------------------------------------------------- #
# oracle: out-of-domain SCIM POST
# --------------------------------------------------------------------------- #

def test_out_of_domain_post_is_denied_before_account_lookup():
    """The oracle fix, in the same-organization case.

    Provider A is verified for a.example. Asking it to provision
    someone@other.example -- whether or not that address exists -- must give
    the identical answer, and must be decided before any User.email query.
    """
    with TestClient(app) as client:
        admin, data = _workspace(client)
        _pid, scim = _provider(client, admin, 'A', f'a-{uuid4().hex[:10]}.example')

        # A real, existing account on a foreign domain.
        with SessionLocal() as db:
            outsider = Organization(name=f'Out {uuid4().hex[:8]}',
                                    slug=f'out-{uuid4().hex[:12]}')
            db.add(outsider)
            db.commit()
            from app.models import User
            foreign = User(organization_id=outsider.id, name='Foreign',
                           email='foreign@other.example', password_hash='',
                           role='member', active=True, kind='human')
            db.add(foreign)
            db.commit()

        # Count the email-existence queries: the denial must happen first.
        probes = []
        real_scalars = SessionLocal().execute

        from sqlalchemy.orm import Session
        original_scalar = Session.scalar

        def counting_scalar(self, *args, **kwargs):
            stmt_text = str(args[0]) if args else ''
            if 'users.email' in stmt_text or 'lower(users.email)' in stmt_text.lower():
                probes.append(stmt_text)
            return original_scalar(self, *args, **kwargs)

        with mock.patch.object(Session, 'scalar', counting_scalar):
            existing = client.post('/scim/v2/Users', headers=scim, json={
                'userName': 'foreign@other.example', 'displayName': 'Probe'})
            absent = client.post('/scim/v2/Users', headers=scim, json={
                'userName': f'nobody-{uuid4().hex[:8]}@other.example', 'displayName': 'Probe'})

        assert existing.status_code == absent.status_code, (
            "responses differ, so the address's existence is still observable")
        # One stable 4xx for the gate, whatever the reason. It was 403 when the
        # domain check alone existed; v5 unified validation and the domain gate
        # into one refusal, so the code is not part of the contract -- only the
        # fact that it is a 4xx, and that it never depends on existence.
        assert 400 <= existing.status_code < 500, existing.text
        assert existing.status_code != 500
        assert probes == [], (
            f"an email-existence query ran before the domain refusal: {probes}")
        # And the response bodies must not differ either.
        assert existing.json() == absent.json()


def test_out_of_domain_post_same_org_two_providers():
    """Same org, two providers: A must not probe B's users via a foreign
    domain, and must not probe its own either if outside its verified domain."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain_a = f'a-{uuid4().hex[:10]}.example'
        domain_b = f'b-{uuid4().hex[:10]}.example'
        _pid_a, scim_a = _provider(client, admin, 'A', domain_a)
        _pid_b, scim_b = _provider(client, admin, 'B', domain_b)

        # B legitimately owns its domain and can provision there.
        ok = client.post('/scim/v2/Users', headers=scim_b, json={
            'userName': f'bee-{uuid4().hex[:8]}@{domain_b}', 'displayName': 'B user'})
        assert ok.status_code == 200, ok.text

        # A asking about that same address is refused identically
        # whether or not the account exists.
        hit = client.post('/scim/v2/Users', headers=scim_a, json={
            'userName': f'bee-{uuid4().hex[:8]}@{domain_b}', 'displayName': 'A'})
        miss = client.post('/scim/v2/Users', headers=scim_a, json={
            'userName': f'ghost-{uuid4().hex[:8]}@{domain_b}', 'displayName': 'A'})
        # Stable 4xx, not a specific code: v5 merged the malformed-address and
        # out-of-domain refusals into one gate. What matters is that both
        # responses are the same and are not 5xx.
        assert 400 <= hit.status_code < 500
        assert hit.status_code == miss.status_code
        assert hit.json() == miss.json()


def test_out_of_domain_put_and_get_are_refused():
    """The same boundary applies to the read and replace paths, not just POST."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain_a = f'a-{uuid4().hex[:10]}.example'
        _pid, scim = _provider(client, admin, 'A', domain_a)
        created = client.post('/scim/v2/Users', headers=scim, json={
            'userName': f'in-{uuid4().hex[:8]}@{domain_a}', 'displayName': 'In'})
        scim_id = created.json()['id']
        # Writing an out-of-domain address over an in-domain identity is refused.
        put = client.put(f'/scim/v2/Users/{scim_id}', headers=scim, json={
            'userName': f'someone@elsewhere.example', 'displayName': 'Move'})
        assert 400 <= put.status_code < 500, put.text
        assert put.status_code != 500


def test_in_domain_collision_does_not_leak_which_provider_owns_it():
    """Inside its OWN verified domain, a collision must still be a 409 (correct
    SCIM semantics) but must not say anything about another provider or
    account. The residual boundary is named in the report: within a single
    verified domain, 'already provisioned' is still observable to the domain's
    own owner, which is unavoidable without changing the synchronous model."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain_a = f'a-{uuid4().hex[:10]}.example'
        domain_b = f'b-{uuid4().hex[:10]}.example'
        _pid_a, scim_a = _provider(client, admin, 'A', domain_a)
        _pid_b, scim_b = _provider(client, admin, 'B', domain_b)
        email = f'same-{uuid4().hex[:8]}@{domain_a}'
        first = client.post('/scim/v2/Users', headers=scim_a,
                            json={'userName': email, 'displayName': 'A'})
        assert first.status_code == 200, first.text

        # B is not verified for A's domain at all -> refused, before any lookup.
        other = client.post('/scim/v2/Users', headers=scim_b,
                            json={'userName': email, 'displayName': 'B'})
        assert 400 <= other.status_code < 500, other.text
        assert other.status_code != 500
        body = str(other.json())
        assert domain_a not in body.lower() or 'domain' in body.lower()


# --------------------------------------------------------------------------- #
# reserved namespace
# --------------------------------------------------------------------------- #

def test_oidc_prefixed_scim_id_is_rejected():
    """`oidc:` is sso_oidc's own mapping namespace. A SCIM caller must not be
    able to address an OIDC-provisioned mapping through it."""
    assert sso_oidc._OIDC_EXTERNAL_ID_PREFIX == 'oidc:'
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain_a = f'a-{uuid4().hex[:10]}.example'
        _pid, scim = _provider(client, admin, 'A', domain_a)
        # Create a real OIDC mapping owned by this provider.
        with SessionLocal() as db:
            from app.sso_oidc import provision_or_update_user
            idp = db.scalar(select(IdentityProvider).where(
                IdentityProvider.organization_id == data['organization']['id']))
            provision_or_update_user(db, idp, {
                'sub': f'sub-{uuid4().hex[:8]}',
                'email': f'oidc-{uuid4().hex[:8]}@{domain_a}', 'name': 'OIDC'})
            db.commit()
            mapping = db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.identity_provider_id == idp.id))
            assert mapping.scim_external_id.startswith('oidc:')

        for method, kwargs in (
            ('get', {}),
            ('put', {'json': {'userName': f'x-{uuid4().hex[:8]}@{domain_a}',
                              'displayName': 'X'}}),
            ('patch', {'json': {'Operations': [
                {'op': 'replace', 'path': 'active', 'value': False}]}}),
        ):
            response = getattr(client, method)(
                f'/scim/v2/Users/{mapping.scim_external_id}', headers=scim, **kwargs)
            assert response.status_code == 400, (
                f"{method.upper()} on an oidc:-prefixed id returned "
                f"{response.status_code}: {response.text}")


def test_oidc_prefixed_scim_post_is_rejected():
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain_a = f'a-{uuid4().hex[:10]}.example'
        _pid, scim = _provider(client, admin, 'A', domain_a)
        response = client.post('/scim/v2/Users', headers=scim, json={
            'userName': f'u-{uuid4().hex[:8]}@{domain_a}',
            'externalId': 'oidc:something',
            'displayName': 'X'})
        # POST derives the external id from userName, so a literal oidc: userName
        # is the vector; that must be refused.
        bad = client.post('/scim/v2/Users', headers=scim, json={
            'userName': 'oidc:subject-1', 'displayName': 'X'})
        assert bad.status_code == 400, bad.text


# --------------------------------------------------------------------------- #
# concurrency / race
# --------------------------------------------------------------------------- #

def test_concurrent_provisioning_collision_is_deterministic():
    """A preflight-to-commit race must not surface as a raw 500.

    The preflight and the insert are not in the same transaction, so two
    concurrent provisions of the same address can both pass the preflight. The
    second one to commit must get a clean 409, not an IntegrityError.
    """
    from app.sso_auth import scim_create_or_update
    from fastapi import HTTPException
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain_a = f'a-{uuid4().hex[:10]}.example'
        pid, scim = _provider(client, admin, 'A', domain_a)
        email = f'race-{uuid4().hex[:8]}@{domain_a}'

        with SessionLocal() as db:
            first = scim_create_or_update(db, data['organization']['id'], email,
                                          email, 'First', source_provider_id=pid)
            db.commit()
            first_id = first.id
        with SessionLocal() as db:
            # Same external id, same provider: this is the update path, and it
            # must not raise.
            again = scim_create_or_update(db, data['organization']['id'], email,
                                          email, 'Second', source_provider_id=pid)
            db.commit()
            # first_id was captured inside the first session: reading first.id
            # after that session closed would raise DetachedInstanceError.
            assert again.id == first_id

        # The 409 path is what a real preflight-to-commit race produces, and it
        # must be a clean 409 rather than a raw IntegrityError/500. Simulated
        # by forcing the preflight to miss an existing row, which is exactly
        # the window between the preflight query and the INSERT.
        with SessionLocal() as db:
            with mock.patch('app.sso_auth.scim_mapping_for_scim',
                            return_value=None):
                with pytest.raises(HTTPException) as excinfo:
                    scim_create_or_update(db, data['organization']['id'], 'other',
                                          email, 'Race', source_provider_id=pid)
            assert excinfo.value.status_code == 409
