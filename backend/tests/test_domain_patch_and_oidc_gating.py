"""Phase 1 -- patch_provider domain handling and OIDC fail-closed.

Two production corrections covered here, both easy to regress silently because
nothing else exercises them:

  * `patch_provider` must normalize the domain, drop the verification when the
    domain changes, and refuse a domain another provider already holds. Without
    the invalidation, swapping domain_binding would carry the OLD domain's
    proof of control over to a domain nobody ever verified -- a provider that
    earned provisioning rights for one domain would silently keep them for
    another.

  * OIDC routing and OIDC provisioning must both fail closed for unverified or
    mismatched domains, before any User.email lookup. Provisioning is a
    provisioning path: it creates and links users, so an ungated OIDC would do
    exactly what the SCIM domain gate now prevents.

DNS is mocked; no test performs a real public DNS lookup.
"""
from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import domain_verification
from app import sso_oidc
from app.database import SessionLocal
from app.main import app
from app.sso_models import IdentityProvider


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    monkeypatch.setattr(domain_verification, "lookup_txt",
                        lambda name: (_ for _ in ()).throw(
                            AssertionError("test performed a real DNS lookup")))
    yield


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Patch {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _verified_provider(client, headers, name, domain=None):
    """Create a provider with a genuinely verified domain -> (id, domain)."""
    if domain is None:
        domain = f'{name.lower()}-{uuid4().hex[:10]}.example'
    created = client.post('/api/admin/sso/providers', headers=headers,
                          json={'name': name, 'protocol': 'oidc',
                                'domain_binding': domain})
    assert created.status_code == 200, created.text
    provider_id = created.json()['id']
    with mock.patch.object(domain_verification, 'lookup_txt',
                           return_value=[domain_verification.expected_txt_value(domain)]):
        verified = client.post(
            f'/api/admin/sso/providers/{provider_id}/domain-verification',
            headers=headers, json={'domain': domain})
    assert verified.status_code == 200, verified.text
    return provider_id, domain


def _patch_body(**overrides):
    """A complete IdPConfig payload, as this PATCH endpoint requires."""
    body = {
        'name': 'Renamed', 'protocol': 'oidc', 'domain_binding': None,
        'sso_only': False, 'role_mapping_json': {},
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# patch_provider: normalization, invalidation, exclusivity
# --------------------------------------------------------------------------- #

def test_patch_normalizes_the_domain():
    """A domain arriving in a different case or with a trailing dot must be
    stored canonically, or the uniqueness index cannot see that two providers
    bound the same domain."""
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        provider_id, _domain = _verified_provider(client, headers, 'Acme')
        updated = client.patch(f'/api/admin/sso/providers/{provider_id}',
                               headers=headers,
                               json=_patch_body(domain_binding='  ACME.Example.  '))
        assert updated.status_code == 200, updated.text
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.domain_binding == 'acme.example'


def test_patch_to_a_different_domain_drops_the_verification():
    """The core correction. domain_verified_at proves control of ONE domain;
    carrying it across a domain change would hand provisioning rights to a
    domain nobody verified."""
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        provider_id, original = _verified_provider(client, headers, 'Acme')
        new_domain = f'moved-{uuid4().hex[:10]}.example'

        updated = client.patch(f'/api/admin/sso/providers/{provider_id}',
                               headers=headers,
                               json=_patch_body(domain_binding=new_domain))
        assert updated.status_code == 200, updated.text
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.domain_binding == new_domain
            assert idp.domain_verified_at is None, (
                "verification survived a domain change: the new domain is "
                "being treated as proven without any DNS check")
            # And it is genuinely unusable, not merely flagged.
            assert domain_verification.is_domain_verified(idp) is False


def test_patch_repeated_with_the_same_domain_keeps_verification():
    """Only a CHANGE invalidates. A PATCH that resends the same domain (the
    endpoint is a full replace, so a client routinely does) must not silently
    strip a valid proof and lock the provider out."""
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        provider_id, domain = _verified_provider(client, headers, 'Acme')
        with SessionLocal() as db:
            before = db.get(IdentityProvider, provider_id).domain_verified_at

        updated = client.patch(f'/api/admin/sso/providers/{provider_id}',
                               headers=headers,
                               json=_patch_body(domain_binding=domain))
        assert updated.status_code == 200, updated.text
        with SessionLocal() as db:
            after = db.get(IdentityProvider, provider_id).domain_verified_at
            assert after is not None, "resending the same domain dropped the proof"
            assert after == before


def test_patch_can_retype_a_domain_another_provider_holds_but_loses_verification():
    """Retyping a binding is allowed -- it reserves nothing on its own. What
    must not happen is the second provider keeping the AUTHORITY that came from
    its own DNS proof while now pointing at the first provider's domain.

    So: the PATCH succeeds, and the old verified domain is surrendered with it.
    An earlier version of this test demanded a 409 here, which was only correct
    while a typed binding reserved a domain globally.
    """
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        first_id, first_domain = _verified_provider(client, headers, 'First')
        second_id, second_domain = _verified_provider(client, headers, 'Second')

        moved = client.patch(f'/api/admin/sso/providers/{second_id}',
                             headers=headers,
                             json=_patch_body(domain_binding=first_domain))
        assert moved.status_code == 200, moved.text

        with SessionLocal() as db:
            # The live claim holder is untouched.
            assert db.get(IdentityProvider, first_id).domain_binding == first_domain
            assert db.get(IdentityProvider, first_id).domain_verified_at is not None
            # The mover lost its own proof, so it holds no authority at all now.
            second = db.get(IdentityProvider, second_id)
            assert second.domain_binding == first_domain
            assert second.domain_verified_at is None
            for candidate in (second_domain, first_domain):
                assert domain_verification.provider_may_use_domain(
                    second, f'x@{candidate}') is False

        # And the stale proof is gone, not silently retained.
        with SessionLocal() as db:
            assert domain_verification.is_domain_verified(
                db.get(IdentityProvider, second_id)) is False


def test_patch_can_move_to_an_unclaimed_domain():
    """The legitimate move: no conflict, and the domain starts unverified."""
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        provider_id, _domain = _verified_provider(client, headers, 'Acme')
        fresh = f'fresh-{uuid4().hex[:10]}.example'
        moved = client.patch(f'/api/admin/sso/providers/{provider_id}',
                             headers=headers,
                             json=_patch_body(domain_binding=fresh))
        assert moved.status_code == 200, moved.text
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.domain_binding == fresh
            assert idp.domain_verified_at is None


# --------------------------------------------------------------------------- #
# OIDC provisioning and routing fail closed
# --------------------------------------------------------------------------- #

def test_oidc_provisioning_refuses_an_unverified_provider():
    """OIDC creates and links users, so it must be gated exactly like SCIM."""
    from app.sso_oidc import OIDCError, provision_or_update_user
    with TestClient(app) as client:
        headers, data = _workspace(client)
        org_id = data['organization']['id']
        created = client.post('/api/admin/sso/providers', headers=headers, json={
            'name': 'Unverified', 'protocol': 'oidc',
            'domain_binding': f'unver-{uuid4().hex[:10]}.example'})
        provider_id = created.json()['id']

        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.domain_verified_at is None
            with pytest.raises(OIDCError) as excinfo:
                provision_or_update_user(db, idp, {
                    'sub': 'sub-1', 'email': 'someone@' + idp.domain_binding})
            assert 'domain' in str(excinfo.value).lower()


def test_oidc_provisioning_refuses_an_email_outside_the_verified_domain():
    """A verified provider may still only provision inside its own domain."""
    from app.sso_oidc import OIDCError, provision_or_update_user
    with TestClient(app) as client:
        headers, data = _workspace(client)
        org_id = data['organization']['id']
        provider_id, domain = _verified_provider(client, headers, 'Acme')
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            with pytest.raises(OIDCError):
                provision_or_update_user(db, idp, {
                    'sub': 'sub-1', 'email': f'user@foreign-{uuid4().hex[:8]}.example'})
            # The in-domain case still provisions, so the gate is a boundary
            # and not a blanket denial.
            user = provision_or_update_user(db, idp, {
                'sub': 'sub-2', 'email': f'user@{domain}'})
            db.commit()
            assert user.email == f'user@{domain}'


def test_oidc_provisioning_does_not_probe_for_account_existence():
    """The refusal must happen before any User.email lookup, so a mismatched
    domain reveals nothing about whether the address exists."""
    from sqlalchemy.orm import Session

    from app.models import User
    from app.sso_oidc import OIDCError, provision_or_update_user

    with TestClient(app) as client:
        headers, data = _workspace(client)
        org_id = data['organization']['id']
        provider_id, _domain = _verified_provider(client, headers, 'Acme')

        foreign = f'foreign-{uuid4().hex[:8]}.example'
        with SessionLocal() as db:
            db.add(User(organization_id=org_id, name='Existing',
                        email=f'taken@{foreign}', password_hash='',
                        role='member', active=True, kind='human'))
            db.commit()

        probes = []
        original = Session.scalar

        def counting_scalar(self, *args, **kwargs):
            text = str(args[0]) if args else ''
            if 'users.email' in text.lower():
                probes.append(text)
            return original(self, *args, **kwargs)

        with mock.patch.object(Session, 'scalar', counting_scalar):
            with SessionLocal() as db:
                idp = db.get(IdentityProvider, provider_id)
                for address in (f'taken@{foreign}', f'free@{foreign}'):
                    with pytest.raises(OIDCError):
                        provision_or_update_user(db, idp, {
                            'sub': f'sub-{uuid4().hex[:6]}', 'email': address})
        assert probes == [], (
            f"an email-existence query ran before the domain refusal: {probes}")


def test_oidc_routing_refuses_an_unverified_provider():
    """Routing must fail closed: an unverified domain_binding is only a
    typed-in assertion and must not send a user into an OIDC flow.

    The assertion is that build_authorization_url is never CALLED. Asserting on
    the redirect target instead is vacuous here: with no real IdP behind the
    provider the flow fails at discovery either way, so a gated and an ungated
    route produce the same sso_error redirect and the test cannot tell them
    apart. It passed while the gate was deliberately removed.
    """
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        domain = f'route-{uuid4().hex[:10]}.example'
        created = client.post('/api/admin/sso/providers', headers=headers, json={
            'name': 'Unverified', 'protocol': 'oidc', 'domain_binding': domain})
        assert created.status_code == 200, created.text
        provider_id = created.json()['id']
        with SessionLocal() as db:
            assert db.get(IdentityProvider, provider_id).domain_verified_at is None

        with mock.patch.object(sso_oidc, 'build_authorization_url',
                               return_value='https://idp.invalid/authorize') as build:
            response = client.get('/api/auth/sso/start',
                                  params={'email': f'someone@{domain}'},
                                  follow_redirects=False)
        assert build.call_count == 0, (
            "an unverified domain_binding still started an OIDC flow: "
            f"{build.call_args_list}")
        assert response.status_code in (302, 307)


def test_oidc_routing_allows_a_verified_provider():
    """The other half, and what makes the test above non-vacuous: a verified
    provider MUST reach the OIDC flow. If the gate blocked everything, the
    refusal test would still pass.
    """
    with TestClient(app) as client:
        headers, _ = _workspace(client)
        _provider_id, domain = _verified_provider(client, headers, 'Acme')
        with mock.patch.object(sso_oidc, 'build_authorization_url',
                               return_value='https://idp.invalid/authorize') as build:
            response = client.get('/api/auth/sso/start',
                                  params={'email': f'someone@{domain}'},
                                  follow_redirects=False)
        assert build.call_count == 1, (
            "a verified domain was refused at the routing gate, so the "
            "refusal test proves nothing")
        assert response.status_code == 302
        assert response.headers.get('location') == 'https://idp.invalid/authorize'
