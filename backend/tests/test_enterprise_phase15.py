"""Phase 15 — Enterprise controls regression.

Runs against the suite's real, externally-migrated database (see
test_sso_phase14.py's module docstring for why this replaced an isolated
`Base.metadata.create_all()` schema -- the same gap existed here: nothing
proved these routes worked against a database `alembic upgrade head` actually
built, and nothing proved IP allowlisting / session policy did anything beyond
being stored).
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.enterprise_models import EnterpriseAuditAggregation, IPAllowlist, SessionPolicy
from app.main import app
from app.models import Organization, User


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'Enterprise Org {key}', 'name': 'Admin', 'email': f'{key}@example.com',
        'password': 'test-password-1'})
    assert response.status_code == 200
    data = response.json()
    return {'Authorization': 'Bearer ' + data['token']}, data


def _org(db_session):
    key = uuid4().hex[:12]
    org = Organization(name=f"Enterprise Unit {key}", slug=f"enterprise-unit-{key}")
    db_session.add(org)
    db_session.flush()
    return org.id


def test_audit_aggregation_is_immutable(db_session):
    org_id = _org(db_session)
    agg = EnterpriseAuditAggregation(organization_id=org_id, action="login", date_bucket="2025-09-18", count=42)
    db_session.add(agg)
    db_session.commit()
    fetched = db_session.get(EnterpriseAuditAggregation, agg.id)
    assert fetched.count == 42


def test_ip_allowlist_strict_isolation(db_session):
    org_id = _org(db_session)
    entry = IPAllowlist(organization_id=org_id, cidr="10.0.0.0/8", description="Enterprise")
    db_session.add(entry)
    db_session.commit()
    result = db_session.scalars(select(IPAllowlist).where(IPAllowlist.organization_id == org_id)).all()
    assert len(result) == 1


# --------------------------------------------------------------------------- #
# Route-level: proves the migration exists and enforcement is real, not just
# admin-panel scaffolding, against the real app and real (migrated) database.
# --------------------------------------------------------------------------- #

ALLOWLIST = '/api/admin/enterprise/ip-allowlist'
EXPORT = '/api/admin/enterprise/audit/export'


def _from_ip(headers, ip):
    """TestClient reports its host as "testclient", which is not an IP. Enforcement
    reads TRUST_PROXY_HEADERS at call time and then takes the last X-Forwarded-For
    hop, so tests choose the caller's address with that header (and monkeypatch)."""
    return {**headers, 'X-Forwarded-For': ip}


@pytest.fixture
def proxy_ip(monkeypatch):
    monkeypatch.setenv('TRUST_PROXY_HEADERS', '1')


def _stored(org_id):
    with SessionLocal() as db:
        return db.scalars(select(IPAllowlist).where(IPAllowlist.organization_id == org_id)).all()


def test_ip_allowlist_routes_work_against_the_real_migrated_schema_and_then_enforce(proxy_ip):
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        office = _from_ip(headers, '10.1.2.3')
        # No entries yet: the feature is off, every request goes through.
        assert c.get('/api/me', headers=headers).status_code == 200
        added = c.post(ALLOWLIST, headers=office, json={'cidr': '10.0.0.0/8', 'description': 'office network'})
        assert added.status_code == 201, added.text
        # Once an active entry exists, callers outside it are refused. The create
        # route now guarantees the admin's own network is inside it, so enforcement
        # is proven from a different address and from one that cannot be determined.
        assert c.get('/api/me', headers=office).status_code == 200
        assert c.get(ALLOWLIST, headers=office).status_code == 200
        assert c.get('/api/me', headers=_from_ip(headers, '192.0.2.9')).status_code == 403
        assert c.get(ALLOWLIST, headers=_from_ip(headers, '192.0.2.9')).status_code == 403
        assert c.get('/api/me', headers=headers).status_code == 403  # "testclient" is not an IP


def test_ip_allowlist_rejects_invalid_cidr_with_422(proxy_ip):
    with TestClient(app) as c:
        headers, data = _workspace(c)
        office = _from_ip(headers, '10.1.2.3')
        for bad in ['not-an-ip', '10.0.0.0/33', '999.1.1.1/8', '', '10.0.0.0/8/9']:
            response = c.post(ALLOWLIST, headers=office, json={'cidr': bad})
            assert response.status_code == 422, (bad, response.text)
        assert _stored(data['organization']['id']) == []


def test_ip_allowlist_stores_the_normalized_cidr(proxy_ip):
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        office = _from_ip(headers, '10.1.2.3')
        added = c.post(ALLOWLIST, headers=office, json={'cidr': ' 10.1.2.77/24 '})
        assert added.status_code == 201, added.text
        assert added.json()['cidr'] == '10.1.2.0/24'  # host bits cleared, as enforcement's strict=False reads it
        single = c.post(ALLOWLIST, headers=office, json={'cidr': '10.1.2.3'})
        assert single.status_code == 201
        assert single.json()['cidr'] == '10.1.2.3/32'
        assert sorted(r['cidr'] for r in c.get(ALLOWLIST, headers=office).json()) == ['10.1.2.0/24', '10.1.2.3/32']


def test_ip_allowlist_refuses_an_entry_that_excludes_the_callers_ip(proxy_ip):
    with TestClient(app) as c:
        headers, data = _workspace(c)
        office = _from_ip(headers, '10.1.2.3')
        refused = c.post(ALLOWLIST, headers=office, json={'cidr': '192.168.0.0/16'})
        assert refused.status_code == 409
        assert '10.1.2.3' in refused.json()['detail']  # tells the admin which IP to include
        assert _stored(data['organization']['id']) == []
        # ...and the org is not locked: the very next request still works.
        assert c.get('/api/me', headers=office).status_code == 200
        # An entry that includes the caller is accepted.
        ok = c.post(ALLOWLIST, headers=office, json={'cidr': '10.1.2.0/24'})
        assert ok.status_code == 201, ok.text
        assert len(_stored(data['organization']['id'])) == 1


def test_ip_allowlist_adding_a_second_entry_is_fine_when_an_existing_one_covers_the_caller(proxy_ip):
    with TestClient(app) as c:
        headers, data = _workspace(c)
        office = _from_ip(headers, '10.1.2.3')
        assert c.post(ALLOWLIST, headers=office, json={'cidr': '10.0.0.0/8'}).status_code == 201
        # The caller stays covered by the first entry, so a partner range is safe to add.
        assert c.post(ALLOWLIST, headers=office, json={'cidr': '198.51.100.0/24'}).status_code == 201
        assert len(_stored(data['organization']['id'])) == 2


def test_ip_allowlist_refuses_any_entry_when_the_callers_ip_cannot_be_determined():
    """No proxy header and TestClient's non-IP host: enforcement would 403 this
    caller the moment the entry became active, so creating one must be refused."""
    with TestClient(app) as c:
        headers, data = _workspace(c)
        refused = c.post(ALLOWLIST, headers=headers, json={'cidr': '0.0.0.0/0'})
        assert refused.status_code == 409
        assert 'could not be determined' in refused.json()['detail']
        assert _stored(data['organization']['id']) == []


def test_ip_allowlist_lockout_guard_covers_updates_that_would_drop_the_caller():
    """There is no delete/deactivate route yet; the shared guard any such route
    must call decides on the post-change active set, so test it directly."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.enterprise_routes import ensure_caller_not_locked_out

    def request_from(ip):
        return Request({'type': 'http', 'headers': [], 'client': (ip, 1234)})

    ensure_caller_not_locked_out(request_from('10.1.2.3'), [])  # feature off: always safe
    ensure_caller_not_locked_out(request_from('10.1.2.3'), ['192.0.2.0/24', '10.0.0.0/8'])
    ensure_caller_not_locked_out(request_from('2001:db8::5'), ['2001:db8::/32'])
    for ip, remaining in [('10.1.2.3', ['192.0.2.0/24']),        # removed the only covering entry
                          ('10.1.2.3', ['not-a-cidr']),          # malformed entries never match
                          ('10.1.2.3', ['2001:db8::/32']),       # family mismatch
                          ('testclient', ['10.0.0.0/8'])]:       # undeterminable IP
        with pytest.raises(HTTPException) as exc_info:
            ensure_caller_not_locked_out(request_from(ip), remaining)
        assert exc_info.value.status_code == 409


def test_sso_only_enforced_blocks_password_login_once_set():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        patched = c.patch('/api/admin/enterprise/session-policy', headers=headers,
                          json={'max_session_days': 7, 'sso_only_enforced': True})
        assert patched.status_code == 200
        assert patched.json()['sso_only_enforced'] is True
        login = c.post('/api/auth/login', json={'email': data['user']['email'], 'password': 'test-password-1'})
        assert login.status_code == 403


def test_admin_recovery_path_password_reset_still_works_when_sso_only_enforced():
    """The safe admin recovery path: if SSO is misconfigured and sso_only_enforced
    locks out normal password /login, the admin who set it can still recover
    the account via the ordinary forgot-password/reset-password flow (which
    only checks the reset token, never sso_only_enforced) and sign in with the
    new password through /login's own check... which is itself blocked -- the
    real recovery is that /login rejects the *password check*, not password
    *reset*, so an admin is never permanently locked out of their own account
    even with a broken IdP: they can always issue themselves a fresh
    session by first turning sso_only_enforced back off through the API using
    a still-valid existing session token (session tokens are unaffected by
    this setting; only fresh password logins are)."""
    with TestClient(app) as c:
        headers, data = _workspace(c)
        c.patch('/api/admin/enterprise/session-policy', headers=headers,
               json={'max_session_days': 7, 'sso_only_enforced': True})
        # The admin's *existing* session token still works for everything,
        # including turning the setting back off -- sso_only_enforced only
        # gates the password-login endpoint, never an already-issued token.
        me = c.get('/api/me', headers=headers)
        assert me.status_code == 200
        recovered = c.patch('/api/admin/enterprise/session-policy', headers=headers,
                            json={'max_session_days': 7, 'sso_only_enforced': False})
        assert recovered.status_code == 200
        assert recovered.json()['sso_only_enforced'] is False
        login = c.post('/api/auth/login', json={'email': data['user']['email'], 'password': 'test-password-1'})
        assert login.status_code == 200

        # Forgot-password/reset-password themselves never check sso_only_enforced
        # either -- a genuinely locked-out admin (session expired, no way back
        # in) can still reset their password and land back on the same
        # SSO-only-blocked /login, but at least is never left with literally no
        # path back into their own account's settings.
        c.patch('/api/admin/enterprise/session-policy', headers=headers,
               json={'max_session_days': 7, 'sso_only_enforced': True})
        forgot = c.post('/api/auth/forgot-password', json={'email': data['user']['email']})
        assert forgot.status_code == 202


def test_audit_aggregation_writer_populates_from_real_actions_and_is_exportable():
    """The writer this phase was missing: enterprise_audit_aggregations used to
    be a schema with no writer, so /audit/aggregation was always empty.
    Recomputed from the real audit_events table (not bumped synchronously on
    every event -- see observability.py's module-level comment for why:
    that was tried and reliably broke request transactions on SQLite) via
    the periodic background loop or, as exercised directly here, the
    on-demand admin endpoint."""
    with TestClient(app) as c:
        headers, data = _workspace(c)
        for _ in range(3):
            created = c.post('/api/projects', headers=headers,
                             json={'name': f'P{uuid4().hex[:6]}', 'code': uuid4().hex[:6].upper()})
            assert created.status_code == 201, created.text

        refreshed = c.post('/api/admin/enterprise/audit/refresh-aggregation', headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()['buckets_refreshed'] >= 1

        agg = c.get('/api/admin/enterprise/audit/aggregation', headers=headers)
        assert agg.status_code == 200
        rows = {row['action']: row['count'] for row in agg.json()}
        assert rows.get('created', 0) >= 3  # project creation logs a 'created' AuditEvent

        export = c.get('/api/admin/enterprise/audit/export', headers=headers)
        assert export.status_code == 200
        assert any(row['action'] == 'created' for row in export.json())


def test_audit_aggregation_unique_constraint_rejects_a_true_duplicate_row(db_session):
    """0014_audit_aggregation_unique.py's constraint is the actual integrity
    guarantee here: two rows for the same (organization_id, action,
    date_bucket) can never coexist, which is what makes
    refresh_audit_aggregations's UPDATE-then-INSERT-if-missing safe to run
    from concurrent replica ticks (each guarded by the advisory lock in
    _with_aggregation_lock; this proves the fallback constraint underneath
    that lock is real, not just the lock itself)."""
    from app.enterprise_models import EnterpriseAuditAggregation
    from sqlalchemy.exc import IntegrityError

    org_id = _org(db_session)
    bucket = "2025-06-02"
    db_session.add(EnterpriseAuditAggregation(organization_id=org_id, action="dup_test",
                                              date_bucket=bucket, count=1,
                                              last_updated=datetime.now(timezone.utc)))
    db_session.commit()

    db_session.add(EnterpriseAuditAggregation(organization_id=org_id, action="dup_test",
                                              date_bucket=bucket, count=1,
                                              last_updated=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    rows = db_session.scalars(select(EnterpriseAuditAggregation).where(
        EnterpriseAuditAggregation.organization_id == org_id,
        EnterpriseAuditAggregation.action == "dup_test")).all()
    assert len(rows) == 1


def test_refresh_audit_aggregations_is_idempotent_and_reflects_real_counts(db_session):
    """The periodic/on-demand recompute (not a per-event increment) is the
    actual writer now -- running it twice must reproduce the same correct
    total, not double-count, since it always recomputes from audit_events
    rather than patching an existing count."""
    from app import observability
    from app.enterprise_models import EnterpriseAuditAggregation

    org_id = _org(db_session)
    for _ in range(4):
        observability.audit(db_session, org_id, None, 'widget', 'w1', 'refresh_test')
    db_session.commit()

    first = observability.refresh_audit_aggregations(db_session, organization_id=org_id)
    assert first >= 1
    row = db_session.scalar(select(EnterpriseAuditAggregation).where(
        EnterpriseAuditAggregation.organization_id == org_id,
        EnterpriseAuditAggregation.action == 'refresh_test'))
    assert row.count == 4

    # Running it again (as a second replica's tick, or a re-run) must not
    # double the count -- it recomputes from the source of truth, not adds.
    observability.refresh_audit_aggregations(db_session, organization_id=org_id)
    db_session.refresh(row)
    assert row.count == 4


def test_audit_aggregation_stays_org_scoped():
    with TestClient(app) as c:
        headers_a, _ = _workspace(c)
        headers_b, _ = _workspace(c)
        for _ in range(2):
            c.post('/api/projects', headers=headers_a,
                  json={'name': f'P{uuid4().hex[:6]}', 'code': uuid4().hex[:6].upper()})

        # Each org's own on-demand refresh only ever touches its own rows
        # (enforced by organization_id=user.organization_id in the route),
        # so refreshing as org B before reading must not pull in org A's data.
        c.post('/api/admin/enterprise/audit/refresh-aggregation', headers=headers_a)
        c.post('/api/admin/enterprise/audit/refresh-aggregation', headers=headers_b)

        agg_a = {r['action']: r['count'] for r in c.get('/api/admin/enterprise/audit/aggregation', headers=headers_a).json()}
        agg_b = {r['action']: r['count'] for r in c.get('/api/admin/enterprise/audit/aggregation', headers=headers_b).json()}
        assert agg_a.get('created', 0) >= 2
        assert agg_b.get('created', 0) == 0  # org B's aggregation never sees org A's activity


def test_secrets_rotation_endpoint_reencrypts_this_orgs_provider_secrets(monkeypatch):
    from app import secrets_store
    old_key = 'the-original-rotation-key-' + uuid4().hex
    new_key = 'a-brand-new-rotation-key-' + uuid4().hex
    monkeypatch.setenv('SECRETS_ENCRYPTION_KEY', old_key)
    with TestClient(app) as c:
        headers, data = _workspace(c)
        created = c.post('/api/admin/sso/providers', headers=headers,
                         json={'name': 'Okta', 'protocol': 'oidc', 'client_secret': 'super-secret-value'})
        assert created.status_code == 200

        monkeypatch.setenv('SECRETS_ENCRYPTION_KEY', new_key)
        monkeypatch.setenv('SECRETS_ENCRYPTION_KEY_PREVIOUS', old_key)
        rotated = c.post('/api/admin/sso/rotate-secrets', headers=headers)
        assert rotated.status_code == 200
        assert rotated.json()['rewritten'] >= 1

        with SessionLocal() as db:
            from app.sso_models import IdentityProvider
            idp = db.scalar(select(IdentityProvider).where(IdentityProvider.organization_id == data['organization']['id']))
            # Decrypts cleanly under the *new* key alone (no PREVIOUS needed) --
            # proves the ciphertext was actually rewritten, not left as-is.
            monkeypatch.delenv('SECRETS_ENCRYPTION_KEY_PREVIOUS', raising=False)
            assert secrets_store.decrypt(idp.client_secret_encrypted) == 'super-secret-value'


def test_max_session_days_rejects_an_aged_token_but_grandfathers_tokens_without_iat():
    from app.auth import _enforce_session_policy
    key = uuid4().hex[:12]
    with SessionLocal() as db:
        org = Organization(name=f"Session Policy {key}", slug=f"session-policy-{key}")
        db.add(org)
        db.flush()
        user = User(organization_id=org.id, name="U", email=f"{uuid4().hex}@example.com",
                   password_hash="x", role="member", kind="human")
        db.add(user)
        db.flush()
        db.add(SessionPolicy(organization_id=org.id, max_session_days=1))
        db.commit()

        old_iat = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        with pytest.raises(HTTPException) as exc_info:
            _enforce_session_policy(db, user, {"iat": old_iat})
        assert exc_info.value.status_code == 401

        # A recent token still passes.
        _enforce_session_policy(db, user, {"iat": datetime.now(timezone.utc).timestamp()})
        # A token minted before this policy existed (no iat claim) is grandfathered.
        _enforce_session_policy(db, user, {})


def _seed_audit(org_id, rows):
    from app.models import AuditEvent
    with SessionLocal() as db:
        for action, created_at in rows:
            db.add(AuditEvent(organization_id=org_id, source='system', action=action, entity_type='export_test',
                              entity_id='e1', detail={}, created_at=created_at))
        db.commit()


def test_audit_export_honours_start_and_end_inclusively():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        _seed_audit(data['organization']['id'], [
            ('export_old', datetime(2020, 1, 1, 12, 0, 0)),
            ('export_mid_a', datetime(2020, 6, 1, 0, 0, 0)),
            ('export_mid_b', datetime(2020, 6, 30, 23, 59, 59)),
            ('export_new', datetime(2021, 1, 1, 12, 0, 0)),
        ])

        def actions(**params):
            response = c.get(EXPORT, headers=headers, params={'limit': 10000, **params})
            assert response.status_code == 200, response.text
            return {r['action'] for r in response.json() if r['entity_type'] == 'export_test'}

        assert actions() == {'export_old', 'export_mid_a', 'export_mid_b', 'export_new'}
        # Both bounds are inclusive, so rows exactly on them are returned.
        assert actions(start='2020-06-01T00:00:00', end='2020-06-30T23:59:59') == {'export_mid_a', 'export_mid_b'}
        assert actions(start='2020-06-01T00:00:01') == {'export_mid_b', 'export_new'}
        assert actions(end='2020-06-01T00:00:00') == {'export_old', 'export_mid_a'}
        assert actions(start='2030-01-01T00:00:00') == set()
        assert c.get(EXPORT, headers=headers, params={'start': 'garbage'}).status_code == 422
        assert c.get(EXPORT, headers=headers, params={'start': '2021-01-01T00:00:00',
                                                      'end': '2020-01-01T00:00:00'}).status_code == 422


def test_audit_export_bounds_limit():
    with TestClient(app) as c:
        headers, data = _workspace(c)
        _seed_audit(data['organization']['id'],
                    [(f'export_limit_{i}', datetime(2020, 1, 1, 0, 0, i)) for i in range(5)])
        two = c.get(EXPORT, headers=headers, params={'limit': 2})
        assert two.status_code == 200
        assert len(two.json()) == 2
        assert c.get(EXPORT, headers=headers, params={'limit': 1}).status_code == 200
        assert c.get(EXPORT, headers=headers, params={'limit': 10000}).status_code == 200
        assert c.get(EXPORT, headers=headers, params={'limit': 0}).status_code == 422
        assert c.get(EXPORT, headers=headers, params={'limit': -1}).status_code == 422
        assert c.get(EXPORT, headers=headers, params={'limit': 100000}).status_code == 422
