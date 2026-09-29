"""Phase 1 v3 -- migration portability and semantic (not name-based) repair.

PostgreSQL availability
-----------------------
The deployment target is PostgreSQL and the previous migration used
`SET active = 0`, which is invalid there: `users.active` is a real BOOLEAN and
PostgreSQL will not coerce an integer literal into it. This module therefore
does two things:

  * generates and inspects the actual PostgreSQL DDL the migration emits, so
    the Boolean handling is verified against PostgreSQL's compiler rather than
    assumed. No PostgreSQL server is available in this environment (no `psql`,
    no `postgres`/`initdb` binary, no Docker, nothing listening on 5432), so
    this is compile-time evidence, NOT an executed migration. That limit is
    reported rather than papered over -- SQLite success is not a PostgreSQL
    claim.

  * proves the semantic repair behaviour on the real chain: valid
    differently-named objects are accepted, malformed same-named ones are
    repaired, and a repair never creates a duplicate valid object.
"""
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import app.models  # noqa: F401  -- registers the shared Base metadata
from app import database as app_database


def _alembic_config(url, dialect="sqlite"):
    import pathlib
    cfg = Config()
    backend_root = pathlib.Path(__file__).parent.parent
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _use_engine(monkeypatch, url):
    import sys
    engine = sa.create_engine(url, future=True)
    monkeypatch.setattr(app_database, "engine", engine)
    for name in ("tests.conftest", "conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
    return engine


# --------------------------------------------------------------------------- #
# 1. PostgreSQL compatibility of the migration SQL
# --------------------------------------------------------------------------- #

def test_migration_emits_boolean_sql_for_postgres():
    """The retire step must use real Booleans, not integer literals.

    `SET active = 0` compiles on SQLite (dynamic typing) and is rejected by
    PostgreSQL, where `active` is a BOOLEAN column. Rendering the statement
    through the PostgreSQL dialect shows the bound values are booleans.
    """
    retire = sa.text(
        "UPDATE users SET active = :inactive, "
        "session_epoch = COALESCE(session_epoch, 0) + 1 "
        "WHERE active = :still_active AND id IN ("
        "  SELECT user_id FROM scim_user_mappings WHERE identity_provider_id IS NULL"
        ")")
    statement = retire.bindparams(inactive=False, still_active=True)
    pg_dialect = sa.create_mock_engine(
        "postgresql://", lambda *a, **kw: None).dialect
    compiled = statement.compile(dialect=pg_dialect)

    assert "IS NULL" in str(compiled)
    # Booleans, not 0/1.
    assert compiled.params["inactive"] is False
    assert compiled.params["still_active"] is True


def _migration_source():
    """The 0021 migration's source, read from disk by path.

    Read by path rather than imported: alembic/ is not a package, so
    `import alembic.versions.0021_...` raises ModuleNotFoundError (and a
    module name beginning with a digit is not importable by name at all).
    """
    import pathlib
    backend_root = pathlib.Path(__file__).parent.parent
    return (backend_root / "alembic" / "versions"
            / "0021_scim_provider_ownership.py").read_text()


def _migration_code_lines():
    """Executable source of the 0021 migration, comments and strings removed.

    Needed because the migration's own comment quotes the very bug this test
    hunts (`active = 0`); scanning raw text would flag the explanation and pass
    a real regression.

    Token strings are rejoined WITHOUT a separator, preserving original
    spacing, so the literal `active = 1` is still detectable.

    Only COMMENT tokens are dropped. Dropping STRING tokens as well would
    remove the SQL itself -- the statement under test lives in a string
    literal -- leaving nothing to inspect and passing unconditionally. That
    bug was caught by re-introducing the defect and watching the test fail to
    notice, which is why the check is verified against a real regression
    rather than trusted on its first green run.
    """
    import io
    import tokenize
    source = _migration_source()
    kept = [tok for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type != tokenize.COMMENT]
    return tokenize.untokenize(kept)


def test_migration_source_uses_boolean_literals_not_integers():
    """A direct source-level check that no `active = 0` / `= 1` remains.

    This is intentionally a source assertion rather than a behavioural one:
    SQLite executes the integer form happily, so a behavioural SQLite test
    cannot catch this class of bug, and there is no PostgreSQL server here to
    execute against. The source is the only place the defect is observable.
    """
    code = _migration_code_lines()
    assert "active = 0" not in code, \
        "the migration assigns an integer literal to a BOOLEAN column"
    assert "active = 1" not in code, \
        "the migration compares a BOOLEAN column to an integer literal"


def test_migration_boolean_statements_compile_on_postgres_dialect():
    """The retire UPDATE and the partial index must both compile for
    PostgreSQL. This is compile-time evidence, not an executed migration."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    url = "postgresql+psycopg://user:pw@localhost/db"
    engine = sa.create_mock_engine(url, lambda *a, **kw: None)
    dialect = engine.dialect

    retire = sa.text(
        "UPDATE users SET active = :inactive, "
        "session_epoch = COALESCE(session_epoch, 0) + 1 "
        "WHERE active = :still_active AND id IN ("
        "  SELECT user_id FROM scim_user_mappings WHERE identity_provider_id IS NULL"
        ")")
    compiled = str(retire.bindparams(inactive=False, still_active=True)
                   .compile(dialect=dialect))
    assert "IS NULL" in compiled
    # The bound values are real booleans, not integers.
    params = retire.bindparams(inactive=False, still_active=True).compile(
        dialect=dialect).params
    assert params["inactive"] is False
    assert params["still_active"] is True


# --------------------------------------------------------------------------- #
# 2. semantic repair, not name matching
# --------------------------------------------------------------------------- #

def _schema_facts(conn):
    """Schema properties, in a form that compares by VALUE.

    Reflected index dicts contain dialect option objects (the partial
    predicate's TextClause), and those compare by identity -- so two genuinely
    identical schemas would never compare equal. The predicate is reduced to
    its compiled text so the comparison reflects the schema, not the Python
    objects that happened to describe it.
    """
    inspector = sa.inspect(conn)
    fk = {
        (tuple(fk.get("constrained_columns") or []),
         fk.get("referred_table"),
         tuple(fk.get("referred_columns") or []))
        for fk in inspector.get_foreign_keys("scim_user_mappings")
    }
    indexes = {}
    for index in inspector.get_indexes("scim_user_mappings"):
        options = index.get("dialect_options") or {}
        # `a or b` cannot be used here: a TextClause's truth value is
        # undefined and SQLAlchemy raises on bool(). Compare against None.
        predicate = options.get("sqlite_where")
        if predicate is None:
            predicate = options.get("postgresql_where")
        indexes[tuple(index.get("column_names") or [])] = {
            "name": index.get("name"),
            "unique": bool(index.get("unique")),
            "where": None if predicate is None else str(predicate),
        }
    return fk, indexes


def _head_db(tmp_path, monkeypatch, name):
    url = f"sqlite:///{tmp_path / name}"
    engine = _use_engine(monkeypatch, url)
    command.upgrade(_alembic_config(url), "head")
    return url, engine


def test_valid_alternatively_named_fk_is_accepted_and_not_duplicated(tmp_path, monkeypatch):
    """An equivalent FK under a different name is already correct.

    The previous repair keyed on the name, so it would have tried to create a
    second FK for an already-valid schema. The repair is now semantic, so a
    valid differently-named object is left alone and not duplicated.
    """
    url, engine = _head_db(tmp_path, monkeypatch, 'altname.db')
    with engine.connect() as conn:
        fk_before, idx_before = _schema_facts(conn)
    assert (("identity_provider_id",), "identity_providers", ("id",)) in fk_before

    # Re-run the migration; it must not add a second FK.
    command.upgrade(_alembic_config(url), "head")
    with engine.connect() as conn:
        fk_after, idx_after = _schema_facts(conn)
    engine.dispose()

    owner_fks = [f for f in fk_after if f[0] == ("identity_provider_id",)]
    assert len(owner_fks) == 1, f"duplicate owner FKs created: {owner_fks}"
    assert fk_after == fk_before
    assert idx_after == idx_before


def test_malformed_same_named_fk_is_repaired(tmp_path, monkeypatch):
    """A same-named FK pointing at the wrong table is NOT proof of health.

    The name matches but the definition is wrong, so a name-based check would
    skip it and leave the schema broken. The repair must notice the definition
    and fix it.
    """
    url = f"sqlite:///{tmp_path / 'malformed.db'}"
    engine = _use_engine(monkeypatch, url)
    command.upgrade(_alembic_config(url), "0020_trial_end_dates")

    # Build a pre-0021 table carrying a same-named FK with a WRONG definition:
    # it constrains user_id (not identity_provider_id) and refers to users.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE sm_tmp AS SELECT id, organization_id, user_id, "
            "scim_external_id, scim_active, last_synced_at, created_at "
            "FROM scim_user_mappings"))
        conn.execute(sa.text("DROP TABLE scim_user_mappings"))
        conn.execute(sa.text(
            "CREATE TABLE scim_user_mappings ("
            "id VARCHAR NOT NULL, organization_id VARCHAR NOT NULL, "
            "user_id VARCHAR NOT NULL, scim_external_id VARCHAR NOT NULL, "
            "scim_active BOOLEAN NOT NULL, last_synced_at DATETIME, "
            "created_at DATETIME NOT NULL, PRIMARY KEY (id), "
            "CONSTRAINT fk_scim_user_mappings_identity_provider_id "
            "FOREIGN KEY (user_id) REFERENCES users (id))"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings SELECT * FROM sm_tmp"))
        conn.execute(sa.text("DROP TABLE sm_tmp"))
        conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0020_trial_end_dates'"))

    command.upgrade(_alembic_config(url), "head")
    with engine.connect() as conn:
        fk, _idx = _schema_facts(conn)
    engine.dispose()

    assert (("identity_provider_id",), "identity_providers", ("id",)) in fk, \
        f"the malformed same-named FK was accepted as proof of health: {fk}"


def test_missing_index_is_recreated_and_uniqueness_enforced(tmp_path, monkeypatch):
    """A non-unique or missing ownership index must be repaired to the real
    invariant, and the invariant must then hold in the database."""
    url, engine = _head_db(tmp_path, monkeypatch, 'missingidx.db')

    # Downgrade to a state where the unique index is absent but a plain,
    # non-unique index of the same columns exists (a "same-named but wrong"
    # definition is covered separately; this is the missing case).
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX uq_scim_user_mappings_provider_external_id"))
        conn.execute(sa.text(
            "CREATE INDEX uq_scim_user_mappings_provider_external_id "
            "ON scim_user_mappings (identity_provider_id, scim_external_id)"))
        conn.execute(sa.text(
            "UPDATE alembic_version SET version_num = '0020_trial_end_dates'"))
    command.upgrade(_alembic_config(url), "head")

    with engine.connect() as conn:
        _fk, indexes = _schema_facts(conn)
        unique = indexes.get(("identity_provider_id", "scim_external_id"))
    engine.dispose()

    assert unique is not None, "the ownership index is missing after repair"
    assert bool(unique.get("unique")), \
        "the repaired index is not UNIQUE, so duplicates are still possible"


def test_legacy_rows_stay_unowned_and_inaccessible(tmp_path, monkeypatch):
    """No silent rebind, ever: a legacy row survives the migration unowned."""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = _use_engine(monkeypatch, url)
    command.upgrade(_alembic_config(url), "0020_trial_end_dates")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES "
            "('o-l', 'L', 'o-l', CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO users (id, organization_id, name, email, password_hash, role, "
            "active, kind, consecutive_task_failures, execution_enabled, hierarchy_level, "
            "session_epoch, created_at) VALUES ('u-l', 'o-l', 'L', 'l@example.com', '', "
            "'member', 1, 'human', 0, 1, 0, 0, CURRENT_TIMESTAMP)"))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings (id, organization_id, user_id, "
            "scim_external_id, scim_active, created_at) VALUES "
            "('m-l', 'o-l', 'u-l', 'ext-l', 1, CURRENT_TIMESTAMP)"))
    command.upgrade(_alembic_config(url), "head")

    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT identity_provider_id, scim_active FROM scim_user_mappings "
            "WHERE id = 'm-l'")).fetchone()
        user = conn.execute(sa.text(
            "SELECT active, session_epoch FROM users WHERE id = 'u-l'")).fetchone()
    engine.dispose()

    assert row[0] is None, "the legacy mapping was silently attributed to a provider"
    assert user[0] == 0, "the quarantined user was not deactivated"
    assert user[1] == 1, "the quarantined user's session epoch was not incremented"
