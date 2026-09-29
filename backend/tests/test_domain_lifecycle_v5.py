"""Phase 1 v5 -- verified domain ownership and the provider lifecycle.

Defects pinned shut (v5 brief items 2, 3, and the ownership half of 4):

* **The verification race.** /domain-verification re-read the provider, checked
  the binding still matched, resolved DNS, then wrote `domain_verified_at`.
  Between that read and that write another request could PATCH the binding to a
  different domain -- so a TXT record published for domain A ended up stamping
  a proof onto domain B, which nobody ever proved. The proof must be bound
  atomically to the domain that was actually verified.

* **Unverified claims squatted domains.** The exclusivity index is a plain
  UNIQUE on domain_binding, so merely TYPING a domain reserved it globally: any
  administrator could lock `gmail.com` for every tenant on the instance,
  verified or not, and could never release it. Exclusivity must attach to an
  ACTIVE, VERIFIED provider only.

* **There was no deactivate path.** `active` existed and was honoured at
  login/provisioning time, but no route ever set it, so the documented
  "deactivation takes effect immediately" claim in domain_verification.py was
  unreachable through the API.

Each of these is proven non-vacuous: the race test interleaves a real binding
change between DNS resolution and the write.
"""
from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc
from sqlalchemy import select as sa_select
from sqlalchemy import text

from app import domain_verification
from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent
from app.sso_models import IdentityProvider


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Domain {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _create(client, admin, name, domain):
    created = client.post('/api/admin/sso/providers', headers=admin, json={
        'name': name, 'protocol': 'oidc', 'domain_binding': domain})
    assert created.status_code == 200, created.text
    return created.json()['id']


def _verify(client, admin, provider_id, domain):
    with mock.patch.object(domain_verification, 'lookup_txt',
                           return_value=[domain_verification.expected_txt_value(domain)]):
        return client.post(f'/api/admin/sso/providers/{provider_id}/domain-verification',
                           headers=admin, json={'domain': domain})


def _domain(tag):
    return f'{tag}-{uuid4().hex[:10]}.example'


# --------------------------------------------------------------------------- #
# verification is bound to the domain that was actually verified
# --------------------------------------------------------------------------- #

def test_verification_race_cannot_stamp_a_replacement_binding():
    """The interleaving that must fail.

    Sequence: verify is called for domain A -> DNS resolves -> the provider's
    binding is changed to B by a concurrent PATCH -> the verify write lands.
    The result must be a 409 and domain B must remain UNVERIFIED. Anything else
    means a proof for A was laundered onto B.
    """
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain_a, domain_b = _domain('race-a'), _domain('race-b')
        provider_id = _create(client, admin, 'Racer', domain_a)

        real_verify = domain_verification.verify_domain

        def verify_then_swap(candidate):
            """Resolve DNS, and while we are still inside the verify call,
            change the binding out from under it."""
            assert real_verify(candidate, resolver=lambda name: [
                domain_verification.expected_txt_value(candidate)]), 'setup'
            moved = client.patch(f'/api/admin/sso/providers/{provider_id}', headers=admin,
                                 json=_patch_body(domain_binding=domain_b))
            assert moved.status_code == 200, moved.text
            return True

        with mock.patch.object(domain_verification, 'verify_domain',
                               side_effect=verify_then_swap):
            response = client.post(
                f'/api/admin/sso/providers/{provider_id}/domain-verification',
                headers=admin, json={'domain': domain_a})

        assert response.status_code == 409, (
            f"expected the stale verification to be refused, got {response.status_code}: "
            f"{response.text}")
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.domain_binding == domain_b
            assert idp.domain_verified_at is None, (
                "a proof obtained for domain A was stamped onto domain B")


def test_verification_succeeds_when_nothing_moved():
    """The other half, so the race test is not passing because everything 409s."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('quiet')
        provider_id = _create(client, admin, 'Quiet', domain)
        response = _verify(client, admin, provider_id, domain)
        assert response.status_code == 200, response.text
        with SessionLocal() as db:
            assert domain_verification.is_domain_verified(
                db.get(IdentityProvider, provider_id)) is True


def test_verification_is_not_audited_with_the_txt_value():
    """The brief forbids storing the TXT value unnecessarily. It is a
    challenge/response pair: publishing it is not a secret, but logging it
    would still make the audit trail a working forgery kit for anyone who can
    write to the zone... which they already can. Assert we keep it out."""
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain = _domain('audit')
        provider_id = _create(client, admin, 'Audited', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200

        with SessionLocal() as db:
            rows = db.query(AuditEvent).filter(
                AuditEvent.organization_id == data['organization']['id']).all()
        assert rows, "domain verification wrote no audit event at all"
        blob = ' '.join(str(r.detail or '') + str(r.action or '') for r in rows)
        assert 'enterai-domain-verification=' not in blob, (
            "the TXT value was written into the audit trail")
        # A failed verification is audited too.
        with SessionLocal() as db:
            before = db.query(AuditEvent).count()
        failed_domain = _domain('audit-fail')
        pid2 = _create(client, admin, 'AuditedFail', failed_domain)
        with mock.patch.object(domain_verification, 'lookup_txt', return_value=[]):
            out = client.post(f'/api/admin/sso/providers/{pid2}/domain-verification',
                              headers=admin, json={'domain': failed_domain})
        assert out.status_code == 400
        with SessionLocal() as db:
            assert db.query(AuditEvent).count() > before, (
                "a failed domain verification left no audit trace")


# --------------------------------------------------------------------------- #
# an unverified claim must not squat a domain globally
# --------------------------------------------------------------------------- #

def test_an_unverified_typing_does_not_reserve_a_domain():
    """A plain UNIQUE index on domain_binding let any admin reserve
    `gmail.com` for the whole instance by typing it, forever, verified or not.
    Exclusivity must attach to an active+verified provider only."""
    with TestClient(app) as client:
        admin_a, _ = _workspace(client)
        admin_b, _ = _workspace(client)
        contested = _domain('contested')

        first = _create(client, admin_a, 'Squatter', contested)
        # The first org merely TYPED the domain: not verified.
        second = client.post('/api/admin/sso/providers', headers=admin_b, json={
            'name': 'Real', 'protocol': 'oidc', 'domain_binding': contested})
        assert second.status_code == 200, (
            f"an unverified, never-used claim blocked another tenant: {second.text}")
        second_id = second.json()['id']

        # And the legitimate claimant can actually verify it and provision.
        assert _verify(client, admin_b, second_id, contested).status_code == 200
        with SessionLocal() as db:
            assert domain_verification.is_domain_verified(
                db.get(IdentityProvider, second_id)) is True
            # The squatter's row is not usable.
            assert domain_verification.is_domain_verified(
                db.get(IdentityProvider, first)) is False


def test_an_inactive_verified_provider_does_not_hold_its_domain():
    """Deactivation must release the claim, so the domain can be verified
    elsewhere rather than being stranded."""
    with TestClient(app) as client:
        admin_a, _ = _workspace(client)
        admin_b, _ = _workspace(client)
        domain = _domain('release')
        first = _create(client, admin_a, 'Holder', domain)
        assert _verify(client, admin_a, first, domain).status_code == 200

        assert client.post(f'/api/admin/sso/providers/{first}/deactivate',
                           headers=admin_a).status_code == 200

        second = client.post('/api/admin/sso/providers', headers=admin_b, json={
            'name': 'Taker', 'protocol': 'oidc', 'domain_binding': domain})
        assert second.status_code == 200, second.text
        assert _verify(client, admin_b, second.json()['id'], domain).status_code == 200


def test_a_verified_and_active_domain_is_still_exclusive():
    """The invariant must survive the loosening above."""
    with TestClient(app) as client:
        admin_a, _ = _workspace(client)
        admin_b, _ = _workspace(client)
        domain = _domain('exclusive')
        first = _create(client, admin_a, 'First', domain)
        assert _verify(client, admin_a, first, domain).status_code == 200

        second = client.post('/api/admin/sso/providers', headers=admin_b, json={
            'name': 'Second', 'protocol': 'oidc', 'domain_binding': domain})
        assert second.status_code == 200
        # Typed, but cannot be verified: an active+verified provider holds it.
        assert _verify(client, admin_b, second.json()['id'], domain).status_code == 409


# --------------------------------------------------------------------------- #
# the deactivate/activate lifecycle route
# --------------------------------------------------------------------------- #

def test_deactivate_blocks_scim_and_oidc_immediately():
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain = _domain('lifecycle')
        provider_id = _create(client, admin, 'Lifecycle', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        token = client.post(f'/api/admin/sso/providers/{provider_id}/scim-token',
                            headers=admin).json()['scim_token']
        scim = {'Authorization': f'Bearer {token}'}
        email = f'life-{uuid4().hex[:8]}@{domain}'
        assert client.post('/scim/v2/Users', headers=scim, json={
            'userName': email, 'displayName': 'P'}).status_code == 200

        assert client.post(f'/api/admin/sso/providers/{provider_id}/deactivate',
                           headers=admin).status_code == 200

        # SCIM token is refused straight away.
        denied = client.post('/scim/v2/Users', headers=scim, json={
            'userName': f'after-{uuid4().hex[:8]}@{domain}', 'displayName': 'P'})
        assert denied.status_code in (401, 403), denied.text

        # OIDC routing is refused straight away.
        route = client.get('/api/auth/sso/start', params={'email': f'x@{domain}'},
                           follow_redirects=False)
        assert route.status_code in (302, 307)
        assert 'sso_error' in route.headers.get('location', ''), (
            "an inactive provider still routed an OIDC login")


def test_deactivation_is_audited():
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain = _domain('audited-lifecycle')
        provider_id = _create(client, admin, 'AuditedLifecycle', domain)
        with SessionLocal() as db:
            before = db.query(AuditEvent).filter(
                AuditEvent.organization_id == data['organization']['id']).count()
        assert client.post(f'/api/admin/sso/providers/{provider_id}/deactivate',
                           headers=admin).status_code == 200
        with SessionLocal() as db:
            after = db.query(AuditEvent).filter(
                AuditEvent.organization_id == data['organization']['id']).count()
        assert after > before, "deactivation wrote no audit event"


def test_reactivation_requires_reverification():
    """Documented release/re-verify behaviour: reactivating restores the
    provider but NOT its proof, because DNS may no longer point at us."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('reverify')
        provider_id = _create(client, admin, 'Reverify', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200

        assert client.post(f'/api/admin/sso/providers/{provider_id}/deactivate',
                           headers=admin).status_code == 200
        assert client.post(f'/api/admin/sso/providers/{provider_id}/activate',
                           headers=admin).status_code == 200

        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            assert idp.active is True
            assert idp.domain_verified_at is None, (
                "reactivation silently restored a DNS proof that may no longer hold")
            assert domain_verification.is_domain_verified(idp) is False
        # Re-verifying makes it usable again.
        assert _verify(client, admin, provider_id, domain).status_code == 200


def test_config_patch_is_not_a_silent_activation_toggle():
    """The brief forbids broadening ordinary PATCH into an activation toggle."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('no-toggle')
        provider_id = _create(client, admin, 'NoToggle', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        assert client.post(f'/api/admin/sso/providers/{provider_id}/deactivate',
                           headers=admin).status_code == 200

        # An ordinary config PATCH must not bring it back.
        response = client.patch(f'/api/admin/sso/providers/{provider_id}', headers=admin,
                                json=_patch_body(name='Renamed', domain_binding=domain))
        assert response.status_code == 200, response.text
        with SessionLocal() as db:
            assert db.get(IdentityProvider, provider_id).active is False, (
                "a config PATCH silently reactivated a deactivated provider")


def test_invalid_domain_binding_is_rejected_not_silently_none():
    """'reject invalid input with 422 rather than silently converting it to
    None' -- a mangled binding must not quietly become 'no domain'."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        created = client.post('/api/admin/sso/providers', headers=admin, json={
            'name': 'Bad', 'protocol': 'oidc', 'domain_binding': 'not a domain'})
        assert created.status_code == 422, (
            f"a malformed domain_binding was accepted or blanked: {created.text}")


# --------------------------------------------------------------------------- #
# OIDC selection must reject a bad provider before the email query
# --------------------------------------------------------------------------- #

def test_oidc_provisioning_refuses_an_inactive_provider():
    from app.sso_oidc import OIDCError, provision_or_update_user
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain = _domain('oidc-inactive')
        provider_id = _create(client, admin, 'OidcInactive', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        assert client.post(f'/api/admin/sso/providers/{provider_id}/deactivate',
                           headers=admin).status_code == 200
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            with pytest.raises(OIDCError):
                provision_or_update_user(db, idp, {
                    'sub': 's1', 'email': f'a@{domain}'})


def test_oidc_provisioning_refuses_a_malformed_claim_email():
    from app.sso_oidc import OIDCError, provision_or_update_user
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('oidc-malformed')
        provider_id = _create(client, admin, 'OidcMalformed', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            for bad in ['', 'notanemail', 'example.com', 'a@b@example.com']:
                with pytest.raises(OIDCError):
                    provision_or_update_user(db, idp, {'sub': 's1', 'email': bad})


def test_oidc_provisioning_proceeds_for_a_fully_configured_provider():
    """The brief's explicit requirement: prove a CORRECTLY configured provider
    proceeds. Without this, a gate that refuses everything passes every refusal
    test above."""
    from app.sso_oidc import provision_or_update_user
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain = _domain('oidc-good')
        provider_id = _create(client, admin, 'OidcGood', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            user = provision_or_update_user(
                db, idp, {'sub': 's1', 'email': f'good@{domain}', 'name': 'Good'})
            db.commit()
            assert user.email == f'good@{domain}'
            assert user.organization_id == data['organization']['id']


def test_oidc_routing_proceeds_for_a_fully_configured_provider():
    """Same for the routing half: a verified, configured provider must reach the
    flow, so the refusal tests are not passing by blocking everything."""
    from app import sso_oidc
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('route-good')
        provider_id = _create(client, admin, 'RouteGood', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        with mock.patch.object(sso_oidc, 'build_authorization_url',
                               return_value='https://idp.invalid/authorize') as build:
            response = client.get('/api/auth/sso/start', params={'email': f'a@{domain}'},
                                  follow_redirects=False)
        assert build.call_count == 1, (
            "a correctly configured provider did not reach the OIDC flow")


def test_index_predicate_limits_exclusivity_to_active_verified_rows():
    """The declared index predicate must require active AND verified.

    A behavioural check alone is not enough: the suite shares one database file
    created once at import, so editing the model's predicate does not change the
    already-created index and a behaviour-only test would stay green through a
    regression. The predicate is therefore asserted on the INDEX OBJECT (which
    is what 0022 repairs and what both dialects use) as well as exercised.

    A weakened predicate -- a plain unique index on domain_binding, or one
    missing either term -- fails the assertion below.
    """
    index = next(i for i in IdentityProvider.__table__.indexes
                 if i.name == 'uq_identity_providers_domain_binding')
    assert index.unique, "the domain index is not unique"
    # Both dialect predicates are checked: SQLite is what the suite runs, and
    # PostgreSQL is what actually runs in production.
    for dialect in ('sqlite', 'postgresql'):
        # dialect_options[dialect] is dict-like, not attribute-accessible, and
        # the value is a TextClause -- which raises on truth-testing, so this
        # must be an explicit None check rather than `or ''`.
        clause = index.dialect_options[dialect].get('where')
        assert clause is not None, f"{dialect} declares no WHERE predicate"
        compact = ' '.join(str(clause).lower().split())
        assert 'domain_binding is not null' in compact, dialect
        assert 'active' in compact, f"{dialect} predicate ignores active: {compact}"
        assert 'domain_verified_at is not null' in compact, (
            f"{dialect} predicate ignores verification: {compact}")

    # And the behaviour: an unverified row may sit on a live claim's domain.
    from app.database import SessionLocal
    with TestClient(app) as client:
        admin, data = _workspace(client)
        org = data['organization']['id']
        domain = _domain('predicate')
        live_id = _create(client, admin, 'Live', domain)
        assert _verify(client, admin, live_id, domain).status_code == 200

        with SessionLocal() as db:
            db.add(IdentityProvider(organization_id=org, name='Unverified',
                                    protocol='oidc', active=True,
                                    domain_binding=domain))
            db.commit()

        try:
            with SessionLocal() as db:
                # If exclusivity were not limited to active verified rows, the
                # unverified row above would have collided on commit.
                rows = db.scalars(sa_select(IdentityProvider).where(
                    IdentityProvider.domain_binding == domain)).all()
                assert len(rows) == 2, (
                    f"expected the live claim plus the unverified row, "
                    f"got {len(rows)}")
                assert db.get(IdentityProvider, live_id).domain_verified_at is not None
        except sa_exc.IntegrityError as exc:  # pragma: no cover - failure path
            pytest.fail(
                f"an unverified binding reserved the domain, so exclusivity is "
                f"not limited to active verified rows: {exc}")

        # A second active+verified claim is still refused: that is the case the
        # index exists to stop.
        with TestClient(app) as c2:
            admin2, data2 = _workspace(c2)
            rival = _create(c2, admin2, 'Rival', domain)
            with mock.patch.object(
                    domain_verification, 'lookup_txt',
                    return_value=[domain_verification.expected_txt_value(domain)]):
                clash = c2.post(
                    f'/api/admin/sso/providers/{rival}/domain-verification',
                    headers=admin2, json={'domain': domain})
            assert clash.status_code == 409, clash.text

            # Releasing the live holder frees the domain for the rival.
            with SessionLocal() as db:
                db.get(IdentityProvider, live_id).active = False
                db.commit()
            with mock.patch.object(
                    domain_verification, 'lookup_txt',
                    return_value=[domain_verification.expected_txt_value(domain)]):
                ok = c2.post(f'/api/admin/sso/providers/{rival}/domain-verification',
                             headers=admin2, json={'domain': domain})
            assert ok.status_code == 200, ok.text


def _patch_body(**overrides):
    body = {'name': 'Renamed', 'protocol': 'oidc', 'domain_binding': None,
            'sso_only': False, 'role_mapping_json': {}}
    body.update(overrides)
    return body
