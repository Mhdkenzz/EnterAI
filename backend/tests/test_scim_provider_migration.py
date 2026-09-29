"""Phase 1, group 1 — migration behavior for 0021_scim_provider_ownership.

These run against a real SQLite database migrated through the project's actual
Alembic chain, not `Base.metadata.create_all()`, and they start from a
*populated* legacy schema: the interesting question is what happens to rows
that already exist when provider ownership arrives, not whether a fresh table
gets a new column.

The invariant under test: a legacy mapping is never silently attributed to a
provider. It must end up unowned and therefore unreachable by every provider,
which is the fail-closed outcome. A backfill to "the first active provider in
the org" would satisfy a naive "column is not null" assertion while handing a
tenant's identities to an arbitrary IdP -- precisely the disclosure this
migration exists to close.
"""
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import app.models  # noqa: F401  -- registers the shared Base metadata
from app import database as app_database


def _alembic_config(url):
    """Build a Config pointing at the project's real Alembic directory.

    `sqlalchemy.url` is set explicitly for the same reason tests/conftest.py
    sets it. backend/alembic.ini declares a PostgreSQL URL, and a bare Config()
    inherits it -- so without this the chain resolves its target database from
    the ini rather than from the engine this test installed, and the resulting
    schema is not the one being asserted against. (alembic/env.py itself takes
    the engine from app.database; the ini value is what the Config falls back
    to when resolving script state.)
    """
    import pathlib
    cfg = Config()
    backend_root = pathlib.Path(__file__).parent.parent
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _use_engine(monkeypatch, url):
    """Point the whole migration chain at `url` for the duration of a test.

    `alembic/env.py` does `from app.database import ... engine` at import time
    and executes fresh on every run, so patching `app_database.engine` is
    enough for the chain itself. It is NOT enough for the suite's other
    modules: tests/conftest.py likewise binds `engine` (and `SessionLocal` is
    bound to the original engine inside app.database), so once another test
    file has run, the suite database is the one any straggler reference still
    points at. Hence this rebinds the name in conftest too -- without it these
    tests pass alone and fail anywhere else in the suite.
    """
    import sys
    engine = sa.create_engine(url, future=True)
    monkeypatch.setattr(app_database, "engine", engine)
    for module_name in ("tests.conftest", "conftest"):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
    return engine


def _build_legacy_db(tmp_path, monkeypatch):
    """Migrate to 0020 (immediately before this group), insert a provider and a
    SCIM mapping the old way, and leave the database there."""
    db_path = tmp_path / "legacy.db"
    url = f"sqlite:///{db_path}"
    _use_engine(monkeypatch, url)
    command.upgrade(_alembic_config(url), "0020_trial_end_dates")

    engine_legacy = sa.create_engine(url)
    with engine_legacy.begin() as conn:
        # Hand-written SQL against the *0020* schema, which has no
        # identity_provider_id column. The ORM cannot be used here: it would
        # emit the current model, whose scim_user_mappings carries that column,
        # against a table that does not have it yet.
        #
        # Note on the column set: alembic 0001_initial calls
        # Base.metadata.create_all(), so the whole schema is built from the
        # *current models* and every later migration (0012 included) skips
        # creating a table it finds already present. That is why
        # identity_providers has no server defaults here and why the
        # NOT NULL model columns must all be supplied explicitly.
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES ('org-1', 'Legacy Org', 'legacy-org', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, "
            "role, active, kind, consecutive_task_failures, execution_enabled, "
            "hierarchy_level, created_at) VALUES "
            "('user-1', 'org-1', 'Legacy User', 'legacy@example.com', '', "
            "'member', 1, 'human', 0, 1, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, organization_id, name, protocol, "
            "sso_only, role_mapping_json, active, created_at) VALUES "
            "('idp-1', 'org-1', 'Legacy IdP', 'oidc', 0, '{}', 1, CURRENT_TIMESTAMP)"))
        # The legacy row, written exactly the way the pre-fix code wrote it.
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, scim_active, created_at) VALUES "
            "('map-1', 'org-1', 'user-1', 'legacy-ext-1', 1, CURRENT_TIMESTAMP)"))
    engine_legacy.dispose()
    return url, db_path


def test_migration_preserves_legacy_rows_unowned(tmp_path, monkeypatch):
    """The whole point: pre-existing rows survive the migration and stay
    unowned, so no provider can claim them."""
    url, _db_path = _build_legacy_db(tmp_path, monkeypatch)

    command.upgrade(_alembic_config(url), "head")

    engine_check = sa.create_engine(url)
    with engine_check.connect() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("scim_user_mappings")}
        assert "identity_provider_id" in columns, "migration did not add the ownership column"

        rows = conn.execute(sa.text(
            "SELECT id, organization_id, user_id, scim_external_id, identity_provider_id "
            "FROM scim_user_mappings")).fetchall()

    engine_check.dispose()

    # The row itself is untouched -- no data loss, no external-id rewrite.
    assert len(rows) == 1
    row_id, org_id, user_id, external_id, owner = rows[0]
    assert (row_id, org_id, user_id, external_id) == ("map-1", "org-1", "user-1", "legacy-ext-1")

    # ...and it is explicitly NOT attributed to the org's only provider. This
    # is the fail-closed guarantee: a guess here would have handed 'legacy-ext-1'
    # to whichever IdP an admin created first.
    assert owner is None, (
        "legacy mapping was backfilled to a provider; it must stay unowned so it "
        "cannot be resolved by any provider"
    )


def test_migration_creates_ownership_index(tmp_path, monkeypatch):
    """Every lookup in the fixed code filters on identity_provider_id, so the
    index has to actually exist on a real migrated database."""
    url, _db_path = _build_legacy_db(tmp_path, monkeypatch)

    command.upgrade(_alembic_config(url), "head")

    engine_check = sa.create_engine(url)
    with engine_check.connect() as conn:
        indexes = {i["name"] for i in sa.inspect(conn).get_indexes("scim_user_mappings")}
    engine_check.dispose()

    assert "ix_scim_user_mappings_identity_provider_id" in indexes


def test_migration_is_idempotent_on_re_run(tmp_path, monkeypatch):
    """The project convention (every migration since 0008) is an idempotent
    guard, so re-running the chain must not raise."""
    url, _db_path = _build_legacy_db(tmp_path, monkeypatch)

    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must be a no-op, not a duplicate-column error

    engine_check = sa.create_engine(url)
    with engine_check.connect() as conn:
        columns = [c["name"] for c in sa.inspect(conn).get_columns("scim_user_mappings")]
    engine_check.dispose()
    assert columns.count("identity_provider_id") == 1


def test_migration_downgrade_round_trips(tmp_path, monkeypatch):
    """test_migration_chain.py requires `alembic downgrade` to work; a column
    this migration adds must come back off cleanly."""
    url, _db_path = _build_legacy_db(tmp_path, monkeypatch)

    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0020_trial_end_dates")

    engine_check = sa.create_engine(url)
    with engine_check.connect() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("scim_user_mappings")}
    engine_check.dispose()

    assert "identity_provider_id" not in columns


def test_model_metadata_declares_ownership_for_create_all_paths():
    """The model is the other half of the durable boundary: a deployment that
    builds from Base.metadata rather than the chain still gets the column and
    a real foreign key to identity_providers."""
    from app.database import Base
    from app.sso_models import SCIMUserMapping as Model
    column = Model.__table__.columns['identity_provider_id']
    assert column.nullable is True
    # v5 added a composite (identity_provider_id, organization_id) FK too; the
    # single-column provider FK is the one this test is about.
    single = [fk for fk in column.foreign_keys
              if list(fk.constraint.columns.keys()) == ['identity_provider_id']]
    assert len(single) == 1, f"expected one provider FK, found {single}"
    assert single[0].column.table.name == 'identity_providers'
    assert 'identity_providers' in Base.metadata.tables
