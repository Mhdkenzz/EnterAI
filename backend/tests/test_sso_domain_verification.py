"""SSO domain ownership: a domain_binding means nothing until DNS proves it.

Before this, any organization admin could bind an identity provider to any domain
(another customer's, or gmail.com) and, because login picked the first provider
bound to the domain, capture that company's sign-ins. These tests drive the real
routes against the real migrated schema; the only fake is the DNS lookup, which is
a single function (sso_auth.lookup_txt_records) precisely so it can be replaced.
"""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import sso_auth
from app.auth import create_token
from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Organization, User
from app.sso_models import IdentityProvider


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Domain Org {key}', 'name': 'Admin', 'email': f'{key}@example.com',
        'password': 'test-password-1'})
    assert response.status_code == 200
    client.cookies.clear()  # authenticate with explicit bearer headers only
    data = response.json()
    return {'Authorization': 'Bearer ' + data['token']}, data


def _create(client, headers, domain, **extra):
    response = client.post('/api/admin/sso/providers', headers=headers,
                           json={'name': 'Okta', 'protocol': 'oidc', 'domain_binding': domain, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _dns(records_by_name):
    """A stand-in for sso_auth.lookup_txt_records that also records what was asked."""
    asked = []

    def lookup(name):
        asked.append(name)
        return list(records_by_name.get(name, []))

    lookup.asked = asked
    return lookup


def test_create_returns_the_token_and_the_exact_txt_record_and_normalizes_the_domain():
    key = uuid4().hex
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        created = _create(c, headers, f'  Corp-{key}.Example.  ')
        domain = f'corp-{key}.example'
        assert created['domain_binding'] == domain
        assert created['domain_verified'] is False
        token = created['domain_verification_token']
        assert len(token) >= 20
        assert created['domain_verification_record'] == {
            'type': 'TXT', 'name': f'_enterai-verification.{domain}',
            'value': f'enterai-domain-verification={token}'}

        listed = next(p for p in c.get('/api/admin/sso/providers', headers=headers).json() if p['id'] == created['id'])
        assert listed['domain_binding'] == domain
        assert listed['domain_verified'] is False
        assert listed['domain_verification_token'] == token  # still pending, so the admin can re-read it


@pytest.mark.parametrize('bad', ['not a domain', 'someone@gmail.com', 'com', 'https://corp.example', 'corp.example/path', '-x.example'])
def test_malformed_domains_are_rejected(bad):
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        response = c.post('/api/admin/sso/providers', headers=headers,
                          json={'name': 'Okta', 'protocol': 'oidc', 'domain_binding': bad})
        assert response.status_code == 422


def test_a_provider_without_a_domain_has_no_token_and_a_blank_domain_means_none():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        created = c.post('/api/admin/sso/providers', headers=headers,
                         json={'name': 'Okta', 'protocol': 'oidc', 'domain_binding': '  '}).json()
        assert created['domain_binding'] is None
        assert created['domain_verification_token'] is None and created['domain_verification_record'] is None


def test_verify_domain_succeeds_when_the_txt_record_is_published(monkeypatch):
    domain = f'verify-{uuid4().hex}.example'
    with TestClient(app) as c:
        headers, data = _workspace(c)
        created = _create(c, headers, domain)
        lookup = _dns({f'_enterai-verification.{domain}': [
            'v=spf1 -all', created['domain_verification_record']['value']]})
        monkeypatch.setattr(sso_auth, 'lookup_txt_records', lookup)

        verified = c.post(f"/api/admin/sso/providers/{created['id']}/verify-domain", headers=headers)
        assert verified.status_code == 200, verified.text
        body = verified.json()
        assert body['domain_verified'] is True and body['domain_verified_at']
        assert body['domain_verification_token'] is None  # nothing left to publish
        assert lookup.asked == [f'_enterai-verification.{domain}']

        listed = next(p for p in c.get('/api/admin/sso/providers', headers=headers).json() if p['id'] == created['id'])
        assert listed['domain_verified'] is True
        with SessionLocal() as db:
            assert db.scalar(select(AuditEvent.id).where(
                AuditEvent.organization_id == data['organization']['id'], AuditEvent.action == 'domain_verified'))


@pytest.mark.parametrize('published', [
    [],
    ['enterai-domain-verification=not-the-token'],
    ['v=spf1 include:_spf.example -all', 'google-site-verification=abc'],
])
def test_verify_domain_rejects_a_missing_or_wrong_record_with_400(monkeypatch, published):
    domain = f'wrong-{uuid4().hex}.example'
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        created = _create(c, headers, domain)
        monkeypatch.setattr(sso_auth, 'lookup_txt_records', _dns({f'_enterai-verification.{domain}': published}))
        response = c.post(f"/api/admin/sso/providers/{created['id']}/verify-domain", headers=headers)
        assert response.status_code == 400
        assert '_enterai-verification.' + domain in response.json()['detail']
        listed = next(p for p in c.get('/api/admin/sso/providers', headers=headers).json() if p['id'] == created['id'])
        assert listed['domain_verified'] is False


def test_verify_domain_reports_a_dns_outage_distinctly_from_a_missing_record(monkeypatch):
    def broken(name):
        raise sso_auth.DomainLookupError('timeout')

    monkeypatch.setattr(sso_auth, 'lookup_txt_records', broken)
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        created = _create(c, headers, f'outage-{uuid4().hex}.example')
        outage = c.post(f"/api/admin/sso/providers/{created['id']}/verify-domain", headers=headers)
        assert outage.status_code == 400 and 'Could not query DNS' in outage.json()['detail']


def test_verify_domain_without_a_domain_is_a_400():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        provider_id = c.post('/api/admin/sso/providers', headers=headers,
                             json={'name': 'Okta', 'protocol': 'oidc'}).json()['id']
        assert c.post(f'/api/admin/sso/providers/{provider_id}/verify-domain', headers=headers).status_code == 400


def test_a_second_org_cannot_verify_a_domain_that_is_already_verified(monkeypatch):
    domain = f'taken-{uuid4().hex}.example'
    name = f'_enterai-verification.{domain}'
    with TestClient(app) as c:
        headers_a, _ = _workspace(c)
        headers_b, _ = _workspace(c)
        a = _create(c, headers_a, domain)
        b = _create(c, headers_b, domain)  # claiming an unverified domain is allowed -- it just does nothing
        assert a['domain_verification_token'] != b['domain_verification_token']

        # Even a DNS zone that (somehow) carries both tokens only ever yields one verified owner.
        monkeypatch.setattr(sso_auth, 'lookup_txt_records', _dns({name: [
            a['domain_verification_record']['value'], b['domain_verification_record']['value']]}))
        assert c.post(f"/api/admin/sso/providers/{a['id']}/verify-domain", headers=headers_a).status_code == 200
        conflict = c.post(f"/api/admin/sso/providers/{b['id']}/verify-domain", headers=headers_b)
        assert conflict.status_code == 409

        by_id = {p['id']: p for p in c.get('/api/admin/sso/providers', headers=headers_b).json()}
        assert by_id[b['id']]['domain_verified'] is False
        by_id = {p['id']: p for p in c.get('/api/admin/sso/providers', headers=headers_a).json()}
        assert by_id[a['id']]['domain_verified'] is True


def test_an_org_cannot_verify_or_see_another_orgs_provider(monkeypatch):
    domain = f'iso-{uuid4().hex}.example'
    monkeypatch.setattr(sso_auth, 'lookup_txt_records', _dns({}))
    with TestClient(app) as c:
        headers_a, _ = _workspace(c)
        headers_b, _ = _workspace(c)
        a = _create(c, headers_a, domain)
        assert c.post(f"/api/admin/sso/providers/{a['id']}/verify-domain", headers=headers_b).status_code == 404


def test_verify_domain_requires_a_human_admin():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        provider = _create(c, headers, f'rbac-{uuid4().hex}.example')
        with SessionLocal() as db:
            member = User(organization_id=data['organization']['id'], name='Member',
                          email=f'{uuid4().hex}@example.com', password_hash='x', role='member', kind='human')
            db.add(member)
            db.commit()
            member_headers = {'Authorization': 'Bearer ' + create_token(member)}
        assert c.post(f"/api/admin/sso/providers/{provider['id']}/verify-domain", headers=member_headers).status_code == 403


def test_changing_the_domain_resets_verification_and_rotates_the_token(monkeypatch):
    domain = f'before-{uuid4().hex}.example'
    after = f'after-{uuid4().hex}.example'
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        created = _create(c, headers, domain)
        monkeypatch.setattr(sso_auth, 'lookup_txt_records', _dns({
            f'_enterai-verification.{domain}': [created['domain_verification_record']['value']]}))
        assert c.post(f"/api/admin/sso/providers/{created['id']}/verify-domain", headers=headers).status_code == 200

        # PATCH with the same domain keeps the proof (and does not mint a new token).
        same = c.patch(f"/api/admin/sso/providers/{created['id']}", headers=headers,
                       json={'name': 'Okta renamed', 'protocol': 'oidc', 'domain_binding': domain.upper()})
        assert same.status_code == 200
        assert same.json()['domain_verified'] is True
        with SessionLocal() as db:
            assert db.get(IdentityProvider, created['id']).domain_verification_token == created['domain_verification_token']

        moved = c.patch(f"/api/admin/sso/providers/{created['id']}", headers=headers,
                        json={'name': 'Okta', 'protocol': 'oidc', 'domain_binding': after})
        assert moved.status_code == 200, moved.text
        body = moved.json()
        assert body['domain_binding'] == after
        assert body['domain_verified'] is False and body['domain_verified_at'] is None
        assert body['domain_verification_token'] not in (None, created['domain_verification_token'])
        assert body['domain_verification_record']['name'] == f'_enterai-verification.{after}'
        with SessionLocal() as db:
            row = db.get(IdentityProvider, created['id'])
            assert row.domain_verified_at is None

        # The old record proves nothing about the new domain.
        assert c.post(f"/api/admin/sso/providers/{created['id']}/verify-domain", headers=headers).status_code == 400

        # Clearing the domain drops the token too.
        cleared = c.patch(f"/api/admin/sso/providers/{created['id']}", headers=headers,
                          json={'name': 'Okta', 'protocol': 'oidc'})
        assert cleared.json()['domain_binding'] is None and cleared.json()['domain_verification_token'] is None


def test_verification_that_is_skipped_for_local_development_never_applies_in_deployments(monkeypatch):
    def must_not_be_called(name):
        raise AssertionError('DNS must not be consulted when verification is skipped')

    with SessionLocal() as db:
        key = uuid4().hex[:12]
        org = Organization(name=f'Skip Org {key}', slug=f'skip-org-{key}')
        db.add(org)
        db.flush()
        idp = IdentityProvider(organization_id=org.id, name='Dev IdP', protocol='oidc',
                               domain_binding=f'skip-{key}.example', domain_verification_token='tok')
        db.add(idp)
        db.commit()

        # Development: opt-in only.
        monkeypatch.setenv('ENVIRONMENT', 'development')
        monkeypatch.delenv('SSO_SKIP_DOMAIN_VERIFICATION', raising=False)
        assert sso_auth.domain_verification_skipped() is False
        monkeypatch.setenv('SSO_SKIP_DOMAIN_VERIFICATION', '1')
        assert sso_auth.domain_verification_skipped() is True
        monkeypatch.setattr(sso_auth, 'lookup_txt_records', must_not_be_called)
        assert sso_auth.verify_provider_domain(db, idp) == 'skipped'
        assert idp.domain_verified_at is not None
        db.rollback()

        # Staging and production ignore the variable entirely and require the record.
        monkeypatch.setattr(sso_auth, 'lookup_txt_records', _dns({}))
        for environment in ('staging', 'production'):
            monkeypatch.setenv('ENVIRONMENT', environment)
            assert sso_auth.domain_verification_skipped() is False
            with pytest.raises(HTTPException) as refused:
                sso_auth.verify_provider_domain(db, idp)
            assert refused.value.status_code == 400
            assert idp.domain_verified_at is None


def test_the_database_allows_only_one_verified_provider_per_domain():
    """Backs the 409 for the race the route-level check cannot see: two orgs proving
    the same domain at once. Also proves migration 0021 created the partial index."""
    domain = f'unique-{uuid4().hex}.example'
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        orgs = []
        for n in range(3):
            key = uuid4().hex[:12]
            org = Organization(name=f'Unique Org {key}', slug=f'unique-org-{key}')
            db.add(org)
            orgs.append(org)
        db.flush()
        # Any number of unverified claims coexist...
        db.add_all([IdentityProvider(organization_id=o.id, name='claim', protocol='oidc', domain_binding=domain)
                    for o in orgs[:2]])
        db.commit()
        # ...one verified owner is fine...
        db.add(IdentityProvider(organization_id=orgs[0].id, name='owner', protocol='oidc',
                                domain_binding=domain, domain_verified_at=now))
        db.commit()
        # ...a second is not.
        db.add(IdentityProvider(organization_id=orgs[2].id, name='squatter', protocol='oidc',
                                domain_binding=domain, domain_verified_at=now))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
