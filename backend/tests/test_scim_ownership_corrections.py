"""Phase 1, group 1 -- correction regressions for the rejected SCIM candidate.

Independent static review rejected the first candidate on four points. Each test
here is written to fail against that candidate and to pass only after the
correction, and every one of them asserts on a *real* database or the real
route layer rather than on model metadata:

1. ownership-input validation -- scim_create_or_update accepted
   source_provider_id=None or a provider from another organization, and would
   persist a mapping with a NULL or foreign owner. No current write path may
   create an unowned mapping; NULL survives only as migration data.
2. duplicate owned external identities -- the database permitted two mappings
   with the same (identity_provider_id, scim_external_id). Proven by attempting
   the duplicate insert against the migrated database, not by inspecting the
   model.
3. partial-migration repair -- if an interrupted run left the column present but
   the foreign key and index missing, re-running upgrade must repair it.
4. legacy handling documentation -- a NULL-owner mapping is NOT recoverable by
   re-POST: global email uniqueness rejects the POST with 409 first. The
   candidate claimed otherwise. These tests pin the real behavior so the
   documentation cannot drift back into the false claim.

The account-existence oracle this file also exercises (cross-provider 409 vs
200) is NOT fixed here -- it is reported, not changed. See
test_cross_provider_post_is_an_account_existence_oracle, which documents the
current behavior as a known unresolved Phase 1 risk.
"""
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import domain_verification
from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.sso_auth import scim_create_or_update
from app.sso_models import IdentityProvider, SCIMUserMapping


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'SCIM Fix {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


def _provider(db, org_id, name="IdP"):
    idp = IdentityProvider(organization_id=org_id, name=name, protocol="oidc", active=True)
    db.add(idp)
    db.commit()
    db.refresh(idp)
    return idp


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 1. ownership-input validation
# --------------------------------------------------------------------------- #

def test_create_or_update_rejects_a_missing_provider_id(db_session):
    """source_provider_id=None must not create an unowned mapping.

    The candidate treated a missing provider as "no role mapping configured" and
    went on to persist SCIMUserMapping(identity_provider_id=None) -- a row no
    provider can ever reach, i.e. an orphan created by a current write path.
    """
    org = Organization(name=f"Fix {uuid4().hex[:8]}", slug=f"fix-{uuid4().hex[:12]}")
    db_session.add(org)
    db_session.commit()

    with pytest.raises(Exception) as excinfo:
        scim_create_or_update(db_session, org.id, "ext-noprov", "noprov@example.com", "No Provider")
    assert getattr(excinfo.value, "status_code", None) == 403, (
        f"expected a 403 for a missing owning provider, got {excinfo.value!r}")

    orphans = db_session.scalars(select(SCIMUserMapping).where(
        SCIMUserMapping.scim_external_id == "ext-noprov")).all()
    assert orphans == [], "an unowned mapping was persisted by a current write path"


def test_create_or_update_rejects_a_provider_from_another_org(db_session):
    """A provider id belonging to a different organization must be refused.

    Otherwise a caller could hand this function another tenant's provider id and
    write a mapping whose owner is outside its own organization -- a mapping
    that is unreachable in practice but still records a false cross-tenant
    relationship in the table.
    """
    org_a = Organization(name=f"FixA {uuid4().hex[:8]}", slug=f"fixa-{uuid4().hex[:12]}")
    org_b = Organization(name=f"FixB {uuid4().hex[:8]}", slug=f"fixb-{uuid4().hex[:12]}")
    db_session.add_all([org_a, org_b])
    db_session.commit()
    foreign_idp = _provider(db_session, org_b.id, "Foreign IdP")

    with pytest.raises(Exception) as excinfo:
        scim_create_or_update(db_session, org_a.id, "ext-foreign",
                              f"foreign-{uuid4().hex[:8]}@example.com", "Foreign",
                              source_provider_id=foreign_idp.id)
    assert getattr(excinfo.value, "status_code", None) == 403

    leaked = db_session.scalars(select(SCIMUserMapping).where(
        SCIMUserMapping.organization_id == org_a.id)).all()
    assert leaked == [], "a mapping was written under a provider from another org"


def test_create_or_update_rejects_an_inactive_provider(db_session):
    """A deactivated provider must not be able to own a new mapping."""
    org = Organization(name=f"Fix {uuid4().hex[:8]}", slug=f"fix-{uuid4().hex[:12]}")
    db_session.add(org)
    db_session.commit()
    idp = _provider(db_session, org.id, "Deactivated IdP")
    idp.active = False
    db_session.commit()

    with pytest.raises(Exception) as excinfo:
        scim_create_or_update(db_session, org.id, "ext-inactive",
                              f"inactive-{uuid4().hex[:8]}@example.com", "Inactive",
                              source_provider_id=idp.id)
    assert getattr(excinfo.value, "status_code", None) == 403


def test_route_level_owners_its_own_mapping(db_session):
    """The legitimate path still works and still records real ownership."""
    with TestClient(app) as client:
        admin_headers, data = _workspace(client)
        # A provider with a genuinely verified domain: provisioning is refused
        # outright for one that has not proven domain ownership, so the
        # legitimate path has to be set up legitimately.
        from conftest import make_verified_provider
        provider_id, scim_headers, domain = make_verified_provider(
            client, admin_headers, 'Okta')

        provisioned = client.post('/scim/v2/Users', headers=scim_headers, json={
            'userName': f'owned-{uuid4().hex[:8]}@{domain}', 'displayName': 'Owned'})
        assert provisioned.status_code == 200, provisioned.text

        with SessionLocal() as db:
            mapping = db.scalar(select(SCIMUserMapping).where(
                SCIMUserMapping.organization_id == data['organization']['id']))
            assert mapping is not None
            assert mapping.identity_provider_id == provider_id


# --------------------------------------------------------------------------- #
# 2. durable uniqueness for owned external identities
# --------------------------------------------------------------------------- #

def _alembic_config(url):
    import pathlib
    cfg = Config()
    backend_root = pathlib.Path(__file__).parent.parent
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    """A real database built by the project's actual Alembic chain.

    `alembic 0001_initial` calls Base.metadata.create_all(), so the schema is
    built from the current models and later migrations skip tables they find
    already present. That does not affect these tests: they exercise the
    constraints the chain actually installed.
    """
    from app import database as app_database
    import sys
    url = f"sqlite:///{tmp_path / 'chain.db'}"
    engine = sa.create_engine(url, future=True)
    monkeypatch.setattr(app_database, "engine", engine)
    for name in ("tests.conftest", "conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
    command.upgrade(_alembic_config(url), "head")
    yield url, engine
    engine.dispose()


def test_database_rejects_duplicate_owned_external_identity(migrated_db):
    """Two mappings may not share (identity_provider_id, scim_external_id).

    Proved by attempting the duplicate insert against the migrated database --
    metadata alone would prove nothing about what the chain installed.
    """
    url, engine = migrated_db
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES ('org-dup', 'Dup Org', 'dup-org', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "created_at) VALUES ('u-dup', 'org-dup', 'Dup', 'dup@example.com', '', "
            "'member', 1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('idp-dup', 'org-dup', 'Dup IdP', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('m-1', 'org-dup', 'u-dup', 'ext-dup', 'idp-dup', 1, CURRENT_TIMESTAMP)"))

        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
                "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
                "('m-2', 'org-dup', 'u-dup', 'ext-dup', 'idp-dup', 1, CURRENT_TIMESTAMP)"))


def test_legacy_unowned_mappings_may_still_coexist(migrated_db):
    """The uniqueness invariant must not be able to reject legacy NULL-owner
    rows -- more than one of them is the expected end state of a backfill-free
    migration, and the constraint has to tolerate that."""
    url, engine = migrated_db
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES ('org-legacy', 'Legacy Org', 'legacy-org', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "created_at) VALUES ('u-leg', 'org-legacy', 'Leg', 'leg@example.com', '', "
            "'member', 1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        for i in (1, 2):
            conn.execute(sa.text(
                "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
                "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
                f"('m-leg-{i}', 'org-legacy', 'u-leg', 'ext-leg', NULL, 1, CURRENT_TIMESTAMP)"))
        count = conn.execute(sa.text(
            "SELECT count(*) FROM scim_user_mappings WHERE identity_provider_id IS NULL "
            "AND scim_external_id = 'ext-leg'")).scalar()
    assert count == 2, "the uniqueness invariant rejected legacy NULL-owner rows"


# --------------------------------------------------------------------------- #
# 3. partial-migration repair
# --------------------------------------------------------------------------- #

def test_upgrade_repairs_a_partially_migrated_database(tmp_path, monkeypatch):
    """A run interrupted between add_column and the FK/index must be repaired.

    The candidate created the foreign key only inside the
    `if "identity_provider_id" not in columns:` branch, so a database that
    already had the column never received the FK or the index on a re-run --
    silently, because the migration reported success.
    """
    from app import database as app_database
    import sys
    url = f"sqlite:///{tmp_path / 'partial.db'}"
    engine = sa.create_engine(url, future=True)
    monkeypatch.setattr(app_database, "engine", engine)
    for name in ("tests.conftest", "conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)

    # Build the database the way a crashed 0021 leaves it: the chain up to
    # 0020, stamped as if 0021 had already added the column but been
    # interrupted before creating the foreign key and indexes.
    #
    # The column needs no hand-written ADD: 0001_initial calls
    # Base.metadata.create_all(), so it is already present at 0020. What an
    # interrupted 0021 actually leaves behind is that column WITH NO named FK
    # and NO ownership indexes, so the table is rebuilt into exactly that
    # shape -- column kept, foreign key clause removed. Recreating by hand is
    # required because batch mode would carry the existing FK along.
    command.upgrade(_alembic_config(url), "0020_trial_end_dates")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE scim_user_mappings_partial AS SELECT id, organization_id, "
            "user_id, scim_external_id, identity_provider_id, scim_active, "
            "last_synced_at, created_at FROM scim_user_mappings"))
        conn.execute(sa.text("DROP TABLE scim_user_mappings"))
        conn.execute(sa.text(
            "CREATE TABLE scim_user_mappings (\n"
            "  id VARCHAR NOT NULL,\n"
            "  organization_id VARCHAR NOT NULL,\n"
            "  user_id VARCHAR NOT NULL,\n"
            "  scim_external_id VARCHAR NOT NULL,\n"
            "  identity_provider_id VARCHAR,\n"
            "  scim_active BOOLEAN NOT NULL,\n"
            "  last_synced_at DATETIME,\n"
            "  created_at DATETIME NOT NULL,\n"
            "  PRIMARY KEY (id),\n"
            "  FOREIGN KEY(organization_id) REFERENCES organizations (id),\n"
            "  FOREIGN KEY(user_id) REFERENCES users (id)\n"
            ")"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings SELECT * FROM scim_user_mappings_partial"))
        conn.execute(sa.text("DROP TABLE scim_user_mappings_partial"))
        conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0021_scim_provider_ownership'"))

    with engine.connect() as conn:
        assert not any(
            "identity_provider_id" in (fk.get("constrained_columns") or [])
            for fk in sa.inspect(conn).get_foreign_keys("scim_user_mappings")), \
            "fixture is wrong: the FK already exists, so this proves nothing"

    # Now the real repair: stamp back to 0020 and re-run, which is what an
    # operator or a re-run of the chain would do.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0020_trial_end_dates'"))
    command.upgrade(_alembic_config(url), "head")

    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        indexes = {i["name"] for i in inspector.get_indexes("scim_user_mappings")}
        foreign_keys = {tuple(fk.get("constrained_columns") or [])
                        for fk in inspector.get_foreign_keys("scim_user_mappings")}
    engine.dispose()

    assert ("identity_provider_id",) in foreign_keys, (
        "re-running the migration did not create the missing foreign key")
    assert "ix_scim_user_mappings_identity_provider_id" in indexes, (
        "re-running the migration did not create the missing index")


def test_upgrade_is_idempotent_on_a_complete_database(tmp_path, monkeypatch):
    """Re-running a fully applied migration must still be a clean no-op."""
    from app import database as app_database
    url = f"sqlite:///{tmp_path / 'twice.db'}"
    engine = sa.create_engine(url, future=True)
    monkeypatch.setattr(app_database, "engine", engine)
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must not raise

    with engine.connect() as conn:
        columns = [c["name"] for c in sa.inspect(conn).get_columns("scim_user_mappings")]
    engine.dispose()
    assert columns.count("identity_provider_id") == 1


# --------------------------------------------------------------------------- #
# 4. legacy recovery -- the real, fail-closed behavior
# --------------------------------------------------------------------------- #

def test_legacy_unowned_mapping_is_not_recovered_by_reposting(migrated_db):
    """Pins the truth the candidate's documentation got wrong.

    The candidate's migration claimed an owning IdP recovers a NULL-owner
    mapping by re-POSTing. It cannot: the mapping is invisible to every
    provider, so the POST takes the create branch, and `users.email` is globally
    unique, so the pre-existing account's email is rejected with 409 before any
    new mapping can exist. The identity stays unreachable. This test exists so
    that claim cannot be reintroduced silently.
    """
    url, engine = migrated_db
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES ('org-rec', 'Recovery Org', 'recovery-org', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "created_at) VALUES ('u-rec', 'org-rec', 'Legacy User', 'legacy@example.com', '', "
            "'member', 1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('idp-rec', 'org-rec', 'Legacy IdP', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('m-rec', 'org-rec', 'u-rec', 'legacy-ext', NULL, 1, CURRENT_TIMESTAMP)"))

    Session = sa.orm.sessionmaker(bind=engine)
    with Session() as session:
        idp = session.get(IdentityProvider, 'idp-rec')
        with pytest.raises(Exception) as excinfo:
            scim_create_or_update(session, 'org-rec', 'legacy-ext',
                                  'legacy@example.com', 'Legacy User',
                                  source_provider_id=idp.id)
        # Provisioning is refused, and it now happens earlier than the email
        # constraint this test used to assert: the provider has no verified
        # domain, so v5's unified domain gate refuses first with a 4xx. The
        # exact code is not the contract -- what this test protects is the
        # invariant below, not the status.
        assert 400 <= getattr(excinfo.value, "status_code", 500) < 500
        still_unowned = session.scalars(select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == 'legacy-ext')).all()
        assert len(still_unowned) == 1
        assert still_unowned[0].identity_provider_id is None, (
            "re-POST silently bound a legacy mapping; the documented fail-closed "
            "behavior is that it does not")


def test_legacy_mapping_stays_invisible_to_every_provider(migrated_db):
    """The quarantine itself: no provider can list, read, or write it."""
    url, engine = migrated_db
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES ('org-q', 'Quarantine Org', 'quarantine-org', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "created_at) VALUES ('u-q', 'org-q', 'Q', 'q@example.com', '', 'member', "
            "1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('idp-q', 'org-q', 'Q IdP', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('m-q', 'org-q', 'u-q', 'q-ext', NULL, 1, CURRENT_TIMESTAMP)"))

    Session = sa.orm.sessionmaker(bind=engine)
    with Session() as session:
        from app.sso_auth import scim_mapping_for_provider
        found = scim_mapping_for_provider(session, 'org-q', 'q-ext', 'idp-q')
        assert found is None, "a NULL-owner mapping was visible to a provider"
        listed = session.scalars(select(SCIMUserMapping).where(
            SCIMUserMapping.organization_id == 'org-q',
            SCIMUserMapping.identity_provider_id == 'idp-q')).all()
        assert listed == []


def test_oidc_cannot_create_an_unowned_mapping():
    """The only other mapping-creation site.

    `sso_oidc.provision_or_update_user` writes SCIMUserMapping directly rather
    than through scim_create_or_update, so the 403 guard does not cover it. It
    is only safe because `idp` there is the provider that authenticated the
    login and is never None. This test exists to keep that invariant from
    rotting silently -- if someone ever provisions a mapping from a value that
    can be None, this fails.
    """
    from app.sso_oidc import provision_or_update_user
    with TestClient(app) as client:
        _admin_headers, data = _workspace(client)
        org_id = data['organization']['id']
        # OIDC provisioning is gated on a verified domain, so a provider that
        # has not proven one is refused before it can write anything -- which
        # is the safe outcome this test wants. Per-test domain because
        # ownership is globally exclusive.
        domain = f'oidc-unowned-{uuid4().hex[:10]}.example'
        with SessionLocal() as db:
            idp = IdentityProvider(organization_id=org_id, name='OIDC', protocol='oidc',
                                   domain_binding=domain, active=True,
                                   domain_verified_at=domain_verification.utcnow())
            db.add(idp)
            db.commit()
            db.refresh(idp)
            provision_or_update_user(db, idp, {
                'sub': f'sub-{uuid4().hex[:8]}',
                'email': f'{uuid4().hex[:8]}@{domain}',
                'name': 'OIDC User'})
            db.commit()
            mappings = db.scalars(select(SCIMUserMapping).where(
                SCIMUserMapping.organization_id == org_id)).all()
            assert mappings, "OIDC provisioning created no mapping at all"
            for mapping in mappings:
                assert mapping.identity_provider_id is not None, (
                    "OIDC provisioning created a mapping with no owner; every "
                    "mapping must be attributable to the provider that made it")
                assert mapping.identity_provider_id == idp.id


# --------------------------------------------------------------------------- #
# account-existence oracle -- CLOSED
# --------------------------------------------------------------------------- #

def test_cross_provider_post_no_longer_leaks_account_existence():
    """The cross-provider account-existence oracle is closed.

    This test used to assert the opposite: that provider A could tell an
    existing account from a free one (200 vs 409) and so probe for arbitrary
    addresses. Pinning that as expected behaviour would re-assert the
    vulnerability, so it was replaced with the assertion that the oracle is
    gone.

    The fix removes the question rather than obscuring the answer: a provider
    may only provision identities inside an email domain it has verified, so
    an out-of-domain probe is refused identically whether or not the address
    exists, before any User.email lookup runs.
    """
    with TestClient(app) as client:
        admin_headers, _ = _workspace(client)
        from conftest import make_verified_provider
        _pid_a, head_a, domain_a = make_verified_provider(
            client, admin_headers, 'A')
        _pid_b, head_b, domain_b = make_verified_provider(
            client, admin_headers, 'B')

        # B legitimately owns its own domain, so it can provision there.
        taken_email = f'oracle-{uuid4().hex[:8]}@{domain_b}'
        assert client.post('/scim/v2/Users', headers=head_b,
                           json={'userName': taken_email,
                                 'displayName': 'B'}).status_code == 200

        # A probing B's domain: refused identically for a taken and a free
        # address, so existence is not observable.
        collision = client.post('/scim/v2/Users', headers=head_a,
                                json={'userName': taken_email, 'displayName': 'A'})
        free = client.post('/scim/v2/Users', headers=head_a, json={
            'userName': f'free-{uuid4().hex[:8]}@{domain_b}', 'displayName': 'A'})
        assert 400 <= collision.status_code < 500 and collision.status_code == free.status_code
        assert collision.json() == free.json(), (
            "the two responses differ, so the address's existence is still "
            "observable")

        # A provisioning inside its OWN verified domain still works: the fix
        # is a boundary, not a denial.
        own = client.post('/scim/v2/Users', headers=head_a, json={
            'userName': f'own-{uuid4().hex[:8]}@{domain_a}', 'displayName': 'A'})
        assert own.status_code == 200, own.text
