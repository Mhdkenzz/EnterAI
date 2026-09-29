"""Phase 1 -- domain ownership: verified, globally exclusive email domains.

The account-existence oracle (SCIM POST answering 200 vs 409 for an arbitrary
email) cannot be closed by error-message wording: the endpoint is synchronous,
so a caller must be able to tell "created" from "already exists". The fix has to
remove the *question* rather than obscure the *answer* -- only a provider that
has verified ownership of an email domain may provision an identity in that
domain, so a SCIM caller probing `victim@othercorp.com` is refused before any
`User.email` lookup happens, identically whether or not that account exists.

Verification is DNS TXT based. `domain_binding` was previously a free-text
string an administrator could simply type, which meant it was an assertion
rather than a proof; an unverified binding must not route OIDC login or admit
provisioning.

DNS is mocked in tests. No test performs a real public DNS lookup.

Not implemented, and named here because it is the residual boundary: two
providers inside ONE organization. Global exclusivity forbids that, so within a
single organization a domain can only ever belong to one provider -- the
multi-provider-per-domain case is refused rather than weakened.
"""
import os
from unittest import mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import domain_verification
from app.database import SessionLocal
from app.main import app
from app.models import Organization
from app.sso_models import IdentityProvider, SCIMUserMapping
from app.sso_auth import resolve_scim_identity_provider


@pytest.fixture
def db_session():
    """A real database session against the test database.

    Local rather than borrowed: this repo's conftest exposes no db_session
    fixture. The exclusivity assertions below need real commit/rollback
    behaviour because the unique index is what enforces the invariant -- an
    in-memory fake would not exercise it at all.
    """
    from app import database
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Hard guarantee that no test can reach public DNS.

    domain_verification.lookup_txt delegates to an injectable resolver; this
    replaces the default one so an un-mocked call fails loudly rather than
    performing a real lookup.
    """
    monkeypatch.setattr(domain_verification, "lookup_txt",
                        lambda name: (_ for _ in ()).throw(
                            AssertionError("test performed a real DNS lookup")))
    yield


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Domain {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _create_provider(client, headers, name, domain=None):
    """Create a provider. A duplicate domain is refused with 409 at creation
    time by the unique index -- the caller asserts on that rather than
    treating it as an unexpected failure."""
    payload = {'name': name, 'protocol': 'oidc'}
    if domain:
        payload['domain_binding'] = domain
    return client.post('/api/admin/sso/providers', headers=headers, json=payload)


_ADMIN_HEADERS = {}


def admin_headers_of(client):
    return _ADMIN_HEADERS['value']


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

def test_domain_normalization_is_deterministic():
    """Case, whitespace and a trailing dot must all collapse to one form, or
    two providers could claim the same domain by spelling it differently."""
    assert domain_verification.normalize_domain("Example.COM") == "example.com"
    assert domain_verification.normalize_domain("  example.com  ") == "example.com"
    assert domain_verification.normalize_domain("example.com.") == "example.com"
    assert domain_verification.normalize_domain("EXAMPLE.com") == "example.com"


def test_invalid_domains_are_rejected():
    for bad in ("", "   ", "no-at-sign", "a@b", "exa mple.com", "@example.com"):
        with pytest.raises(ValueError):
            domain_verification.normalize_domain(bad)


# --------------------------------------------------------------------------- #
# claim / exclusivity
# --------------------------------------------------------------------------- #

def test_claim_requires_verification_before_use(db_session):
    """A binding that has not been verified must not become usable."""
    org = Organization(name=f'D {uuid4().hex[:8]}', slug=f'd-{uuid4().hex[:12]}')
    db_session.add(org)
    db_session.commit()
    idp = IdentityProvider(organization_id=org.id, name='P', protocol='oidc',
                           active=True,
                           domain_binding=f'unverified-{uuid4().hex[:10]}.example',
                           domain_verified_at=None)
    db_session.add(idp)
    db_session.commit()
    assert domain_verification.is_domain_verified(idp) is False
    assert domain_verification.provider_may_use_domain(idp, idp.domain_binding) is False


def test_verified_domain_becomes_usable(db_session):
    org = Organization(name=f'D {uuid4().hex[:8]}', slug=f'd-{uuid4().hex[:12]}')
    db_session.add(org)
    db_session.commit()
    domain = f'usable-{uuid4().hex[:10]}.example'
    idp = IdentityProvider(organization_id=org.id, name='P', protocol='oidc',
                           active=True, domain_binding=domain,
                           domain_verified_at=None)
    db_session.add(idp)
    db_session.commit()
    now = domain_verification.utcnow()
    idp.domain_verified_at = now
    db_session.commit()
    assert domain_verification.is_domain_verified(idp) is True
    assert domain_verification.provider_may_use_domain(
        idp, domain.upper()) is True
    assert domain_verification.provider_may_use_domain(idp, 'other.com') is False


def test_domain_cannot_be_claimed_by_two_providers_same_org(db_session):
    """The exclusivity invariant, at the model layer, within one org.

    The domain is per-test because exclusivity is global and the index is real:
    a fixed name would collide with whatever an earlier test left behind, which
    is the invariant working rather than a test bug.
    """
    org = Organization(name=f'D {uuid4().hex[:8]}', slug=f'd-{uuid4().hex[:12]}')
    other = Organization(name=f'D {uuid4().hex[:8]}', slug=f'e-{uuid4().hex[:12]}')
    db_session.add_all([org, other])
    db_session.commit()
    domain = f'excl-{uuid4().hex[:10]}.example'
    first = IdentityProvider(organization_id=org.id, name='A', protocol='oidc',
                             active=True, domain_binding=domain,
                             domain_verified_at=domain_verification.utcnow())
    db_session.add(first)
    db_session.commit()

    # A second provider anywhere -- same org or a different one -- cannot take
    # the same domain. This is global, not per-organization.
    with pytest.raises(sa.exc.IntegrityError):
        db_session.add(IdentityProvider(
            organization_id=org.id, name='B', protocol='oidc', active=True,
            domain_binding=domain,
            domain_verified_at=domain_verification.utcnow()))
        db_session.flush()
    db_session.rollback()

    # The same domain spelled differently must still collide -- that is what
    # normalizing on the way in buys, and why the index is trustworthy.
    other_provider = IdentityProvider(
        organization_id=other.id, name='C', protocol='oidc', active=True,
        domain_binding=domain_verification.normalize_domain(domain.upper()),
        domain_verified_at=domain_verification.utcnow())
    db_session.add(other_provider)
    with pytest.raises(sa.exc.IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_stale_verification_stops_being_usable(db_session):
    """Verification must expire; a domain whose TXT record was removed must
    stop routing login rather than working forever."""
    org = Organization(name=f'D {uuid4().hex[:8]}', slug=f'd-{uuid4().hex[:12]}')
    db_session.add(org)
    db_session.commit()
    domain = f'stale-{uuid4().hex[:10]}.example'
    idp = IdentityProvider(organization_id=org.id, name='P', protocol='oidc',
                           active=True, domain_binding=domain,
                           domain_verified_at=domain_verification.utcnow())
    db_session.add(idp)
    db_session.commit()
    assert domain_verification.provider_may_use_domain(idp, domain) is True
    # A verification from well in the past is treated as stale.
    idp.domain_verified_at = domain_verification.ago(days=400)
    db_session.commit()
    assert domain_verification.is_domain_verified(idp) is False
    assert domain_verification.provider_may_use_domain(idp, 'example.com') is False


# --------------------------------------------------------------------------- #
# DNS TXT verification mechanism
# --------------------------------------------------------------------------- #

def test_txt_verification_succeeds_on_matching_record(monkeypatch):
    token = domain_verification.expected_txt_value('example.com')
    def fake_lookup(name):
        return [token]
    monkeypatch.setattr(domain_verification, "lookup_txt", fake_lookup)
    assert domain_verification.verify_domain('example.com') is True


def test_txt_verification_fails_on_mismatch_or_missing(monkeypatch):
    monkeypatch.setattr(domain_verification, "lookup_txt", lambda name: ["some-other-value"])
    assert domain_verification.verify_domain('example.com') is False
    monkeypatch.setattr(domain_verification, "lookup_txt", lambda name: [])
    assert domain_verification.verify_domain('example.com') is False


def test_txt_verification_treats_resolver_error_as_failure(monkeypatch):
    """A resolver timeout must not be read as success, and must not crash the
    caller -- fail closed."""
    def boom(name):
        raise domain_verification.DNSResolutionError("timeout")
    monkeypatch.setattr(domain_verification, "lookup_txt", boom)
    assert domain_verification.verify_domain('example.com') is False


# --------------------------------------------------------------------------- #
# admin route surface
# --------------------------------------------------------------------------- #

def test_admin_can_request_and_complete_verification():
    """The full claim flow: create a provider with a domain, receive a
    challenge, publish it in DNS, then verify."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        _ADMIN_HEADERS['value'] = admin
        domain = f'acme-{uuid4().hex[:10]}.example'
        created = _create_provider(client, admin, 'Acme', domain)
        assert created.status_code == 200, created.text
        provider_id = created.json()['id']

        # Unverified: the binding exists but is not usable.
        listed = client.get('/api/admin/sso/providers', headers=admin).json()
        entry = next(p for p in listed if p['id'] == provider_id)
        assert entry['domain_binding'] == domain
        assert entry['domain_verified'] is False

        challenge = client.post(
            f'/api/admin/sso/providers/{provider_id}/domain-verification/challenge',
            headers=admin)
        assert challenge.status_code == 200, challenge.text
        txt_value = challenge.json()['txt_value']
        assert txt_value == domain_verification.expected_txt_value(domain)

        # Wrong TXT -> refused.
        with mock.patch.object(domain_verification, 'lookup_txt',
                               return_value=['nope']):
            bad = client.post(
                f'/api/admin/sso/providers/{provider_id}/domain-verification',
                headers=admin, json={'domain': domain})
        assert bad.status_code == 400, bad.text

        # Correct TXT -> verified.
        with mock.patch.object(domain_verification, 'lookup_txt',
                               return_value=[txt_value]):
            good = client.post(
                f'/api/admin/sso/providers/{provider_id}/domain-verification',
                headers=admin, json={'domain': domain})
        assert good.status_code == 200, good.text
        assert good.json()['verified'] is True

        listed = client.get('/api/admin/sso/providers', headers=admin).json()
        entry = next(p for p in listed if p['id'] == provider_id)
        assert entry['domain_verified'] is True


def test_admin_cannot_claim_a_domain_another_provider_holds():
    """Binding a domain another ACTIVE, VERIFIED provider holds is refused.

    The check moved from "is the string present anywhere" to "is a live claim
    held elsewhere", because v5 stopped a merely-typed binding from reserving a
    domain globally. So the refusal is asserted at the point where the domain is
    actually contested -- verification -- not at creation.
    """
    with TestClient(app) as client:
        admin, data = _workspace(client)
        _ADMIN_HEADERS['value'] = admin
        shared = f'shared-{uuid4().hex[:10]}.example'
        first = _create_provider(client, admin, 'First', shared).json()['id']
        shared_txt = domain_verification.expected_txt_value(shared)
        with mock.patch.object(domain_verification, 'lookup_txt',
                               return_value=[shared_txt]):
            verified = client.post(
                f'/api/admin/sso/providers/{first}/domain-verification',
                headers=admin, json={'domain': shared})
        assert verified.status_code == 200, verified.text

        # A second provider may TYPE the same domain -- that reserves nothing --
        # but it cannot verify it while the first holds a live claim.
        second = _create_provider(client, admin, 'Second', shared).json()['id']
        with mock.patch.object(domain_verification, 'lookup_txt',
                               return_value=[shared_txt]):
            clash = client.post(
                f'/api/admin/sso/providers/{second}/domain-verification',
                headers=admin, json={'domain': shared})
        assert clash.status_code == 409, clash.text

        # Cross-organization: a different tenant's admin cannot take it either.
        admin_b, data_b = _workspace(client)
        _ADMIN_HEADERS['value'] = admin_b
        third = _create_provider(client, admin_b, 'Third', shared).json()['id']
        with mock.patch.object(domain_verification, 'lookup_txt',
                               return_value=[shared_txt]):
            cross = client.post(
                f'/api/admin/sso/providers/{third}/domain-verification',
                headers=admin_b, json={'domain': shared})
        assert cross.status_code == 409, cross.text


def test_revoking_a_provider_stops_its_domain_being_usable():
    """Deactivating the provider must stop the domain from admitting anything."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        _ADMIN_HEADERS['value'] = admin
        # A per-test domain: exclusivity is global, so a fixed name would
        # collide with the provider an earlier test left behind.
        domain = f'revoke-{uuid4().hex[:10]}.example'
        created = _create_provider(client, admin, 'Acme', domain)
        assert created.status_code == 200, created.text
        provider_id = created.json()['id']
        txt = domain_verification.expected_txt_value(domain)
        with mock.patch.object(domain_verification, 'lookup_txt',
                               return_value=[txt]):
            verified = client.post(
                f'/api/admin/sso/providers/{provider_id}/domain-verification',
                headers=admin, json={'domain': domain})
        assert verified.status_code == 200, verified.text
        client.post(f'/api/admin/sso/providers/{provider_id}/scim-token',
                    headers=admin)
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert domain_verification.provider_may_use_domain(idp, f'x@{domain}') is True
            idp.active = False
            db.commit()
            assert domain_verification.provider_may_use_domain(idp, f'x@{domain}') is False
