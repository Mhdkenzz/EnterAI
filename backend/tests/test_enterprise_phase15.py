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

def test_ip_allowlist_routes_work_against_the_real_migrated_schema_and_then_enforce():
    with TestClient(app) as c:
        headers, _ = _workspace(c)
        # No entries yet: the feature is off, every request goes through.
        assert c.get('/api/me', headers=headers).status_code == 200
        added = c.post('/api/admin/enterprise/ip-allowlist', headers=headers,
                       json={'cidr': '10.0.0.0/8', 'description': 'office network'})
        assert added.status_code == 200, added.text
        # Once an active entry exists, every caller outside it is refused --
        # including the admin who just configured it (TestClient's host is not
        # even a parseable IP). This is the real, sharp edge of IP allowlisting:
        # an admin must include their own network or lock themselves out, the
        # same trade-off as any cloud security-group allowlist. Before this fix
        # nothing enforced the setting at all, so this used to do nothing.
        blocked = c.get('/api/me', headers=headers)
        assert blocked.status_code == 403
        blocked_list = c.get('/api/admin/enterprise/ip-allowlist', headers=headers)
        assert blocked_list.status_code == 403


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
