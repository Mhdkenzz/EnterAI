"""Phase 1, group 1 -- correction pass 2, migration-level regressions.

2. Legacy quarantine must retire live sessions. 0021 made a NULL-owner mapping
   unreachable by every provider but left the referenced User rows exactly as
   they were. A user that the system can no longer manage kept the session
   cookies it already had, so "quarantined" did not mean "revoked". This proves
   the migration retires those sessions.

4. Partial-migration repair validated by property, not by name. The previous
   test asserted on index/constraint *names*, so a database carrying a
   differently-named FK on the right column would have been reported as broken
   and a same-named constraint on the wrong column as healthy. These assert on
   constrained columns, index columns, and uniqueness -- what the schema
   actually guarantees.
"""
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import app.models  # noqa: F401  -- registers the shared Base metadata
from app import database as app_database


def _alembic_config(url):
    import pathlib
    cfg = Config()
    backend_root = pathlib.Path(__file__).parent.parent
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _use_engine(monkeypatch, url):
    """Point the migration chain at `url`, and at the engine other suite
    modules captured at import time (see test_scim_provider_migration.py)."""
    import sys
    engine = sa.create_engine(url, future=True)
    monkeypatch.setattr(app_database, "engine", engine)
    for name in ("tests.conftest", "conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
    return engine


def _seed_legacy_row(conn, suffix, org="org-legacy", user="user-legacy"):
    """Insert the pre-0021 shape: a mapping row that has no owner yet.

    At revision 0020 the identity_provider_id column does not exist at all, so
    this deliberately omits it -- that is the genuine legacy state the
    migration has to quarantine.
    """
    conn.execute(sa.text(
        "INSERT INTO organizations (id, name, slug, created_at) VALUES "
        f"('{org}-{suffix}', 'Legacy Org', 'legacy-{suffix}', CURRENT_TIMESTAMP)"))
    conn.execute(sa.text(
        "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
        "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
        "session_epoch, created_at) VALUES "
        f"('{user}-{suffix}', '{org}-{suffix}', 'Legacy User', 'legacy-{suffix}@example.com', "
        f"'', 'member', 1, 'human', 0, 1, 0, 0, CURRENT_TIMESTAMP)"))
    conn.execute(sa.text(
        "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
        "scim_external_id, scim_active, created_at) VALUES "
        f"('map-{suffix}', '{org}-{suffix}', '{user}-{suffix}', 'ext-{suffix}', "
        "1, CURRENT_TIMESTAMP)"))


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database at revision 0020 holding a real legacy (unowned) mapping
    whose User is active with a live session epoch."""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = _use_engine(monkeypatch, url)
    _upgrade(url, "0020_trial_end_dates")
    with engine.begin() as conn:
        _seed_legacy_row(conn, "a")
    yield url, engine
    engine.dispose()


# --------------------------------------------------------------------------- #
# 2. quarantine must retire live sessions
# --------------------------------------------------------------------------- #

def test_migration_retires_sessions_of_quarantined_identities(legacy_db):
    """After 0021, a user behind a NULL-owner mapping must not keep its
    sessions.

    The mapping is unreachable by every provider from this point on, so nothing
    in the system can ever deactivate, re-enable, or rotate that account
    again. Leaving the user active with an unchanged session_epoch means an
    attacker holding a cookie issued before the migration keeps a working
    session indefinitely -- the migration would have hardened the SCIM surface
    while leaving the actual access wide open.
    """
    url, engine = legacy_db
    with engine.connect() as conn:
        # At 0020 the owner column does not exist yet, so the pre-migration
        # baseline is simply "the legacy user is active with epoch 0".
        before = conn.execute(sa.text(
            "SELECT u.active, u.session_epoch FROM users u "
            "JOIN scim_user_mappings m ON m.user_id = u.id")).fetchall()
    assert before, "fixture is wrong: no legacy mapping/user pair to check"
    assert before[0][0] == 1 and before[0][1] == 0, \
        "fixture is wrong: the legacy user should start active with epoch 0"

    _upgrade(url, "head")

    with engine.connect() as conn:
        after = conn.execute(sa.text(
            "SELECT u.active, u.session_epoch, m.identity_provider_id FROM users u "
            "JOIN scim_user_mappings m ON m.user_id = u.id "
            "WHERE m.identity_provider_id IS NULL")).fetchall()

    assert after, "the legacy mapping disappeared"
    active, epoch, owner = after[0]
    assert owner is None, "the legacy mapping was attributed to a provider"
    assert active == 0, (
        "the user behind a quarantined mapping is still active; the migration "
        "must deactivate it because no provider can ever manage it again")
    assert epoch >= 1, (
        "session_epoch was not bumped, so sessions issued before the migration "
        "remain valid for an account nothing can now revoke")


def test_migration_leaves_owned_identities_alone(tmp_path, monkeypatch):
    """Only the quarantined rows are touched -- an owned mapping's user must
    keep its active state and its epoch."""
    url = f"sqlite:///{tmp_path / 'owned.db'}"
    engine = _use_engine(monkeypatch, url)
    _upgrade(url, "head")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES "
            "('org-owned', 'Owned Org', 'owned-org', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "session_epoch, created_at) VALUES "
            "('user-owned', 'org-owned', 'Owned', 'owned@example.com', '', 'member', "
            "1, 'human', 0, 1, 0, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('idp-owned', 'org-owned', 'Owned IdP', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('map-owned', 'org-owned', 'user-owned', 'ext-owned', 'idp-owned', "
            "1, CURRENT_TIMESTAMP)"))

    # Re-run the chain to exercise the reconciliation step on a fully-owned
    # database as well.
    _upgrade(url, "head")

    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT u.active, u.session_epoch FROM users u "
            "JOIN scim_user_mappings m ON m.user_id = u.id "
            "WHERE m.identity_provider_id = 'idp-owned'")).fetchone()
    engine.dispose()
    assert row is not None
    assert row[0] == 1 and row[1] == 0, \
        "the migration deactivated or re-issued a healthy owned identity"


# --------------------------------------------------------------------------- #
# 4. property-based schema validation
# --------------------------------------------------------------------------- #

def _upgrade(url, target="head"):
    """Run the chain with SQLite's batch-recreate SAWarning suppressed.

    The warning ("SQL-parsed foreign key constraint ... could not be located in
    PRAGMA foreign_keys") is emitted by SQLAlchemy while reflecting a table
    that Alembic recreated copy-and-rename to add the foreign key -- which is
    how 0021 adds it on SQLite. It is a reflection artifact of that mechanism,
    not a schema defect, and it is not introduced by this pass: the same
    artifact appears for the pre-existing migrations that use batch mode. It
    is scoped to the migration run so no new warning is added to the suite.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*could not be located in PRAGMA foreign_keys.*",
            category=sa.exc.SAWarning,
        )
        command.upgrade(_alembic_config(url), target)


def _ownership_properties(conn):
    """What the schema guarantees, independent of any object names.

    Judged by constrained columns, index columns, uniqueness and the partial
    predicate -- never by a preferred object name -- so a database carrying an
    equivalent-but-differently-named FK is recognised as correct, and one
    carrying a same-named constraint on the wrong column is recognised as
    broken.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*could not be located in PRAGMA foreign_keys.*",
            category=sa.exc.SAWarning,
        )
        inspector = sa.inspect(conn)
        fk_columns = {
            tuple(fk.get("constrained_columns") or [])
            for fk in inspector.get_foreign_keys("scim_user_mappings")
            if fk.get("referred_table") == "identity_providers"
        }
        indexes = {}
        for index in inspector.get_indexes("scim_user_mappings"):
            indexes[tuple(index.get("column_names") or [])] = index
    return fk_columns, indexes


def test_upgraded_schema_has_owner_fk_and_ownership_indexes(tmp_path, monkeypatch):
    """A full upgrade must produce the owner FK, an owner lookup index, and a
    unique non-NULL (provider, external id) invariant -- checked by columns,
    not names."""
    url = f"sqlite:///{tmp_path / 'props.db'}"
    engine = _use_engine(monkeypatch, url)
    _upgrade(url, "head")

    with engine.connect() as conn:
        fk_columns, indexes = _ownership_properties(conn)

    assert ("identity_provider_id",) in fk_columns, \
        f"no FK from scim_user_mappings.identity_provider_id to identity_providers: {fk_columns}"

    assert ("identity_provider_id",) in indexes, \
        f"no lookup index on identity_provider_id: {list(indexes)}"

    unique = indexes.get(("identity_provider_id", "scim_external_id"))
    assert unique is not None, \
        f"no (provider, external id) index at all: {list(indexes)}"
    # SQLAlchemy reports SQLite's unique flag as int 1, not bool True, so this
    # must be a truthiness check -- `is True` would fail against a correct
    # schema, which is exactly the false negative the name-based test caused.
    assert bool(unique.get("unique")), \
        "the (provider, external id) index is not UNIQUE"
    dialect_options = unique.get("dialect_options") or {}
    assert dialect_options, (
        "the unique index has no IS NOT NULL predicate, so it would also "
        "constrain the legacy NULL-owner rows it is meant to exempt")
    predicate = str(dialect_options.get("sqlite_where", ""))
    assert "IS NOT NULL" in predicate, \
        f"unexpected uniqueness predicate: {predicate!r}"
    engine.dispose()


def test_unique_invariant_rejects_duplicates_but_allows_legacy_nulls(tmp_path, monkeypatch):
    """The property itself, exercised against a real migrated database."""
    url = f"sqlite:///{tmp_path / 'uniq.db'}"
    engine = _use_engine(monkeypatch, url)
    _upgrade(url, "head")

    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES "
            "('o1', 'O1', 'o1', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "created_at) VALUES ('u1', 'o1', 'U1', 'u1@example.com', '', 'member', "
            "1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('i1', 'o1', 'I1', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        # Two legacy NULL-owner rows sharing an external id: allowed.
        for i in ('x', 'y'):
            conn.execute(sa.text(
                "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
                "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
                f"('legacy-{i}', 'o1', 'u1', 'shared-ext', NULL, 1, CURRENT_TIMESTAMP)"))
        # One owned row, then a duplicate of it: rejected.
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('owned-1', 'o1', 'u1', 'dup-ext', 'i1', 1, CURRENT_TIMESTAMP)"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
                "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
                "('owned-2', 'o1', 'u1', 'dup-ext', 'i1', 1, CURRENT_TIMESTAMP)"))
        # The same external id under a *different* provider is fine.
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('i2', 'o1', 'I2', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('owned-3', 'o1', 'u1', 'dup-ext', 'i2', 1, CURRENT_TIMESTAMP)"))
        legacy_count = conn.execute(sa.text(
            "SELECT count(*) FROM scim_user_mappings WHERE identity_provider_id IS NULL "
            "AND scim_external_id = 'shared-ext'")).scalar()
    engine.dispose()
    assert legacy_count == 2, "legacy NULL-owner rows must remain able to coexist"


def test_repair_reconciles_a_column_only_schema(tmp_path, monkeypatch):
    """A database carrying ONLY the column -- no FK, no indexes -- must be
    reconciled on re-run.

    This is the interrupted-0021 state. The assertion is by property, so it
    does not matter what the missing objects were named: whatever the chain
    produced before it stopped, the upgraded database must end up with the
    owner FK, the owner index, and the unique invariant.
    """
    url = f"sqlite:///{tmp_path / 'colonly.db'}"
    engine = _use_engine(monkeypatch, url)
    _upgrade(url, "0020_trial_end_dates")

    # Construct the interrupted state: identity_provider_id present, no FK to
    # identity_providers, no ownership indexes, revision stamped as if 0021 had
    # completed. That is what a crash between add_column and create_foreign_key
    # leaves behind, and it is what the repair has to reconcile.
    #
    # The column is added only if it is genuinely absent, and the assertion
    # below pins that. Whether it is present at revision 0020 is not
    # deterministic: 0001_initial calls Base.metadata.create_all(), which
    # builds the schema from the *current* models, so whether the owner column
    # is already in the table at 0020 depends on which modules pytest has
    # imported by then. Asserting on the resulting shape rather than assuming
    # makes this test correct under both, instead of passing alone and failing
    # in the full suite.
    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("scim_user_mappings")}
        if "identity_provider_id" not in existing:
            conn.execute(sa.text(
                "ALTER TABLE scim_user_mappings ADD COLUMN identity_provider_id VARCHAR"))
        # The owner FK must also be REMOVED, and its presence is not
        # deterministic either: 0001_initial calls Base.metadata.create_all(),
        # which builds the table from the *current* models, so whether
        # identity_providers is already wired up at 0020 depends on whether
        # sso_models has been imported by the time the chain ran. Recreating the
        # table by hand is the only reliable way to land in the exact
        # column-without-FK state an interrupted 0021 leaves behind.
        conn.execute(sa.text(
            "CREATE TABLE scim_user_mappings_colonly AS SELECT id, organization_id, "
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
            "  PRIMARY KEY (id)\n"
            ")"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings SELECT * FROM scim_user_mappings_colonly"))
        conn.execute(sa.text("DROP TABLE scim_user_mappings_colonly"))
        conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0021_scim_provider_ownership'"))

    # Seed a legacy row in that partial state so the repair is exercised against
    # real data rather than an empty table.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES "
            "('o-col', 'O Col', 'o-col', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "created_at) VALUES ('u-col', 'o-col', 'U Col', 'u-col@example.com', '', "
            "'member', 1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, identity_provider_id, scim_active, created_at) VALUES "
            "('m-col', 'o-col', 'u-col', 'ext-col', NULL, 1, CURRENT_TIMESTAMP)"))

    with engine.connect() as conn:
        fk_columns, indexes = _ownership_properties(conn)
    assert ("identity_provider_id",) not in fk_columns, \
        "fixture is wrong: the owner FK already exists, so this proves nothing"
    assert ("identity_provider_id",) not in indexes, \
        "fixture is wrong: the owner index already exists, so this proves nothing"

    # Re-run from 0020, which is what an operator retry or a chain re-run does.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0020_trial_end_dates'"))
    _upgrade(url, "head")

    with engine.connect() as conn:
        fk_columns, indexes = _ownership_properties(conn)
        rows_preserved = conn.execute(sa.text(
            "SELECT count(*) FROM scim_user_mappings")).scalar()
        legacy_still_null = conn.execute(sa.text(
            "SELECT identity_provider_id FROM scim_user_mappings "
            "WHERE id = 'm-col'")).scalar()
    engine.dispose()

    assert ("identity_provider_id",) in fk_columns, \
        f"repair did not add the owner FK: {fk_columns}"
    assert ("identity_provider_id",) in indexes, \
        f"repair did not add the owner lookup index: {list(indexes)}"
    unique = indexes.get(("identity_provider_id", "scim_external_id"))
    assert unique is not None and bool(unique.get("unique")), \
        f"repair did not add the unique (provider, external id) invariant: {list(indexes)}"
    assert rows_preserved >= 1, "the repair lost the mapping rows"
    assert legacy_still_null is None, "the repair attributed a legacy mapping to a provider"
