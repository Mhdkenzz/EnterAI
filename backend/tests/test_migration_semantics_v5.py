"""Phase 1 v5 -- migration semantics and durable tenancy invariants.

Defects pinned shut (v5 brief items 3, 4, 7):

* **Legacy bindings were indexed raw.** 0022 created its unique index over
  whatever string happened to be in the column, so `Acme.com` and
  `acme.com.` and `acme.com` were three different values, and a database
  containing all three ABORTED the upgrade on the unique index. A migration
  that cannot run is not a migration.

* **Partial indexes were called healthy on name and uniqueness alone.** A
  UNIQUE index on `domain_binding` whose WHERE clause restricts it to
  `domain_verified_at IS NULL` satisfies "unique on this column" and defeats the
  live invariant entirely -- every verified provider would then be unconstrained.
  The predicate has to be part of the shape that gets compared, normalized.

* **Mapping tenancy was not durable.** `scim_user_mappings` has independent FKs
  to `users.id` and `identity_providers.id`; nothing ties
  `mapping.organization_id` to either parent, so a cross-org mapping row is
  representable and only caught by hand-written route checks.

Every test here builds a DELIBERATE PRE-MIGRATION schema by hand. Testing
against a database already at head proves nothing about the repair paths,
because every guarded step is then a no-op.
"""
import os
from datetime import datetime, timezone

import sqlalchemy as sa
import pytest

from app.database import Base
import app.models          # noqa: F401  -- registers every table on Base.metadata
import app.sso_models      # noqa: F401


def _load(revision):
    """Import a migration module by its revision id.

    Located by path rather than as a package: the versions directory is not an
    importable package (no __init__.py), so importing it by name fails.
    """
    import importlib.util
    import pathlib
    here = pathlib.Path(__file__).parent.parent / 'alembic' / 'versions'
    for path in sorted(here.glob('*.py')):
        spec = importlib.util.spec_from_file_location(f'_mig_{path.stem}', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if getattr(mod, 'revision', None) == revision:
            return mod
    raise AssertionError(f'migration {revision} not found')


def _run(engine, revision, action='upgrade'):
    """Run one migration step against `engine` on its own connection.

    A fresh connection per step: SQLAlchemy 2 connections autobegin, so reusing
    one across create_all + upgrade raises "already initialized a Transaction".
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    mod = _load(revision)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            # Any statement issued on this connection autobegins a transaction,
            # and op.batch_alter_table's DDL needs to open its own -- which then
            # fails with "already initialized a Transaction".
            if conn.in_transaction():
                conn.commit()
            getattr(mod, action)()
            # Alembic's env.py normally owns the commit. Driving a migration by
            # hand here does not, so without this the DataManipulation
            # statements (canonicalization, quarantine) are discarded when the
            # connection closes -- the migration appears to succeed and changes
            # nothing. DDL in SQLite/PostgreSQL autocommits, which is why only
            # the row updates go missing.
            if conn.in_transaction():
                conn.commit()
    return mod


def _fresh_engine():
    """A file-backed SQLite, not `:memory:`.

    An in-memory database exists per CONNECTION, and these tests deliberately use
    several connections (setup, migration, assertion) against the same engine.
    With `sqlite:///:memory:` each one gets its own empty database, so the
    schema set up by the first vanishes before the migration looks at it. A
    temp file gives every connection the same data, which is what a real
    deployment looks like.
    """
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix='mig-v5-'), 'db.sqlite')
    return sa.create_engine(f'sqlite:///{path}', future=True)


def _head(engine):
    """An identity_providers + scim_user_mappings table built from the CURRENT
    models, i.e. what 0001_initial's create_all() leaves behind."""
    Base.metadata.create_all(engine)
    return engine


# --------------------------------------------------------------------------- #
# legacy domain_binding canonicalization
# --------------------------------------------------------------------------- #

def _legacy_idp(conn, pid, domain, verified=False):
    """Insert a legacy-shaped provider row on an OPEN connection.

    Uses the ORM so column defaults apply. These tables carry several NOT NULL
    columns with Python-side defaults (role_mapping_json, sso_only,
    consecutive_task_failures, ...), and hand-written INSERTs have to restate
    every one of them -- a raw INSERT that omits one fails on that column and
    tells you nothing about the migration behaviour actually under test.

    `domain` is written verbatim, which is the point: the migration under test
    is the one that has to canonicalize whatever raw string is here.
    """
    from app.sso_models import IdentityProvider
    payload = {
        'id': pid, 'organization_id': 'org-1', 'name': f'P{pid}',
        'protocol': 'oidc', 'domain_binding': domain, 'active': True,
        'sso_only': False, 'role_mapping_json': {},
    }
    if verified:
        payload['domain_verified_at'] = datetime.now(timezone.utc)
    conn.execute(sa.insert(IdentityProvider), [payload])


def test_upgrade_canonicalizes_legacy_bindings():
    """Each row's binding is normalized, so the unique index compares like
    with like. Row `a` is the only one that can survive here: see the duplicate
    test below for why the other two are dropped rather than collapsed."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _legacy_idp(conn, 'a', 'Acme.com')
    _run(engine, '0022_domain_ownership')
    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text(
            'SELECT id, domain_binding FROM identity_providers')).all())
    assert rows['a'] == 'acme.com', rows


def test_upgrade_canonicalizes_whitespace_and_trailing_dot():
    """Whitespace and a trailing DNS root dot are spelling noise, not a second
    claim, so a single row is normalized through them."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _legacy_idp(conn, 'a', '  Acme.com.  ')
    _run(engine, '0022_domain_ownership')
    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text(
            'SELECT id, domain_binding FROM identity_providers')).all())
    assert rows['a'] == 'acme.com', rows


def test_canonical_spelling_variants_across_rows_are_still_a_duplicate():
    """Three rows, three different spellings, one domain. Normalizing them
    together CREATES a unique-index collision, so the migration cannot keep all
    three -- and must not pick a winner. Each loses the binding."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        for pid, raw in [('a', 'Acme.com'), ('b', ' acme.com. '), ('c', 'ACME.COM')]:
            _legacy_idp(conn, pid, raw)
    _run(engine, '0022_domain_ownership')          # must not raise
    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text(
            'SELECT id, domain_binding FROM identity_providers')).all())
    assert all(v is None for v in rows.values()), (
        f"the migration chose a winner among conflicting claims: {rows}")


def test_upgrade_does_not_abort_on_duplicate_legacy_bindings():
    """The database may ALREADY contain exact duplicates. An upgrade that dies
    on its own index cannot be deployed to a real instance."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        # Drop the current unique index so the duplicates can exist, as they
        # would on a pre-0022 database where the column was never indexed.
        conn.execute(sa.text('DROP INDEX IF EXISTS uq_identity_providers_domain_binding'))
        for pid in ('a', 'b', 'c'):
            _legacy_idp(conn, pid, 'acme.com')
    _run(engine, '0022_domain_ownership')          # must not raise

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            'SELECT id, domain_binding, domain_verified_at '
            'FROM identity_providers ORDER BY id')).all()
    survivors = {r[0]: (r[1], r[2]) for r in rows}
    assert survivors['a'][0] == 'acme.com'
    # No guessing a winner: the duplicate is resolved fail-closed, unverified.
    for pid, (domain, verified_at) in survivors.items():
        assert verified_at is None, (
            f"row {pid} was marked verified during a duplicate reconciliation")


def test_upgrade_fails_closed_on_malformed_legacy_bindings():
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _legacy_idp(conn, 'good', 'acme.com')
        _legacy_idp(conn, 'bad', 'not a domain !!')
        _legacy_idp(conn, 'bare', 'acme')
    _run(engine, '0022_domain_ownership')
    with engine.connect() as conn:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute(sa.text(
            'SELECT id, domain_binding, domain_verified_at '
            'FROM identity_providers')).all()}
    assert rows['good'][0] == 'acme.com'
    for pid in ('bad', 'bare'):
        assert rows[pid][0] is None, (
            f"{pid} kept the unparseable binding {rows[pid][0]!r}; a binding that "
            "cannot be canonicalized must not survive to be verified")
        assert rows[pid][1] is None


def test_upgrade_never_marks_a_legacy_binding_verified():
    """A typed-in legacy binding is an assertion, not a proof."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _legacy_idp(conn, 'a', 'acme.com')
    _run(engine, '0022_domain_ownership')
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            'SELECT COUNT(*) FROM identity_providers '
            'WHERE domain_verified_at IS NOT NULL')).scalar() == 0


# --------------------------------------------------------------------------- #
# partial-index shape: the WHERE predicate is part of the invariant
# --------------------------------------------------------------------------- #

def _index_predicate(conn, name):
    """The raw CREATE INDEX text, which carries the WHERE clause verbatim.

    Reflection is not enough: SQLite's inspector frequently reports no
    predicate for a partial index at all, and 'no predicate' must never be read
    as 'the right predicate'.
    """
    rows = conn.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"),
        {'n': name}).scalar()
    return rows


def test_wrong_predicate_index_is_rebuilt_not_accepted():
    """A UNIQUE index on domain_binding restricted to the WRONG rows satisfies
    every name-and-uniqueness check while enforcing nothing for a verified
    provider. It must be dropped and rebuilt."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        conn.execute(sa.text('DROP INDEX IF EXISTS uq_identity_providers_domain_binding'))
        conn.execute(sa.text(
            'CREATE UNIQUE INDEX uq_identity_providers_domain_binding '
            'ON identity_providers (domain_binding) '
            'WHERE domain_verified_at IS NULL'))       # exactly backwards
    _run(engine, '0022_domain_ownership')
    with engine.connect() as conn:
        sql = _index_predicate(conn, 'uq_identity_providers_domain_binding')
    assert sql is not None
    lowered = sql.lower()
    assert 'active' in lowered, f"the exclusivity predicate ignores activeness: {sql}"
    assert 'domain_verified_at is not null' in lowered, (
        f"the exclusivity predicate ignores verification: {sql}")


def test_correctly_predicated_index_under_any_name_is_accepted():
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        conn.execute(sa.text('DROP INDEX IF EXISTS uq_identity_providers_domain_binding'))
        conn.execute(sa.text(
            'CREATE UNIQUE INDEX some_other_name ON identity_providers '
            '(domain_binding) WHERE domain_binding IS NOT NULL '
            'AND active AND domain_verified_at IS NOT NULL'))
    _run(engine, '0022_domain_ownership')
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='identity_providers' AND sql IS NOT NULL "
            "AND sql LIKE '%UNIQUE%domain_binding%'")).all()]
    assert names == ['some_other_name'], (
        f"a semantically correct index was duplicated or rebuilt: {names}")


def test_0021_also_rejects_a_wrong_predicate_ownership_index():
    """Same class of defect in 0021's provider/external-id uniqueness."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        conn.execute(sa.text(
            'DROP INDEX IF EXISTS uq_scim_user_mappings_provider_external_id'))
        conn.execute(sa.text(
            'CREATE UNIQUE INDEX uq_scim_user_mappings_provider_external_id '
            'ON scim_user_mappings (identity_provider_id, scim_external_id) '
            'WHERE scim_active = 0'))   # constrains only DEPROVISIONED rows
    _run(engine, '0021_scim_provider_ownership')
    with engine.connect() as conn:
        sql = _index_predicate(conn, 'uq_scim_user_mappings_provider_external_id')
    assert sql is not None
    assert 'identity_provider_id is not null' in sql.lower(), (
        f"the ownership uniqueness predicate does not restrict to owned rows: {sql}")


def test_downgrade_round_trips_after_repair():
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        conn.execute(sa.text('DROP INDEX IF EXISTS uq_identity_providers_domain_binding'))
        conn.execute(sa.text(
            'CREATE UNIQUE INDEX uq_identity_providers_domain_binding '
            'ON identity_providers (domain_binding) WHERE domain_verified_at IS NULL'))
        _legacy_idp(conn, 'a', 'Acme.com')
    _run(engine, '0022_domain_ownership')
    _run(engine, '0022_domain_ownership', 'downgrade')   # must not raise
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            'SELECT COUNT(*) FROM identity_providers')).scalar() == 1


# --------------------------------------------------------------------------- #
# durable cross-organization tenancy
# --------------------------------------------------------------------------- #

def _insert_user(conn, uid, org, email, active=True):
    """Insert a user on an OPEN connection, letting the ORM supply defaults.

    A raw INSERT has to restate every NOT NULL column with a Python-side
    default (consecutive_task_failures, session_epoch, ...), and the resulting
    failure names that column rather than the migration behaviour under test.
    """
    from app.models import User
    _ensure_org(conn, org)
    conn.execute(sa.insert(User), [{
        'id': uid, 'organization_id': org, 'name': uid, 'email': email,
        'password_hash': '', 'role': 'member', 'active': active, 'kind': 'human',
    }])


def _ensure_org(conn, org):
    """organization_id carries a real FK, so the parent row has to exist."""
    from app.models import Organization
    exists = conn.execute(sa.select(Organization).where(
        Organization.id == org)).first()
    if exists is None:
        conn.execute(sa.insert(Organization), [{'id': org, 'name': org, 'slug': org}])


def _insert_idp(conn, pid, org):
    from app.sso_models import IdentityProvider
    _ensure_org(conn, org)
    conn.execute(sa.insert(IdentityProvider), [{
        'id': pid, 'organization_id': org, 'name': pid, 'protocol': 'oidc',
        'active': True, 'sso_only': False, 'role_mapping_json': {},
    }])


def _insert_mapping(conn, mid, org, user_id, external_id, provider_id):
    from app.sso_models import SCIMUserMapping
    conn.execute(sa.insert(SCIMUserMapping), [{
        'id': mid, 'organization_id': org, 'user_id': user_id,
        'scim_external_id': external_id, 'identity_provider_id': provider_id,
        'scim_active': True,
    }])


def test_composite_fk_rejects_the_cross_org_row():
    """mapping.organization_id = 'org-A' pointing at a user in 'org-B' must be
    refused by the database, not by a route check that the next write path can
    forget."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_user(conn, 'u-b', 'org-B', 'b@b.example')
        _insert_idp(conn, 'idp-A', 'org-A')
    _run(engine, '0023_mapping_organization_alignment')
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        from sqlalchemy.exc import IntegrityError as SAIntegrityError
        with pytest.raises(SAIntegrityError):
            _insert_mapping(conn, 'm-1', 'org-A', 'u-b', 'ext', 'idp-A')


def test_same_org_mapping_is_still_accepted():
    """The constraint must not refuse legitimate rows, or the migration would
    break every working deployment."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_idp(conn, 'idp-A', 'org-A')
    _run(engine, '0023_mapping_organization_alignment')
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        _insert_mapping(conn, 'm-ok', 'org-A', 'u-a', 'ext', 'idp-A')
        assert conn.execute(sa.text(
            'SELECT COUNT(*) FROM scim_user_mappings WHERE id = :i'),
            {'i': 'm-ok'}).scalar() == 1


def test_cross_org_provider_is_also_rejected():
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_idp(conn, 'idp-B', 'org-B')
    _run(engine, '0023_mapping_organization_alignment')
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        from sqlalchemy.exc import IntegrityError as SAIntegrityError
        with pytest.raises(SAIntegrityError):
            _insert_mapping(conn, 'm-2', 'org-A', 'u-a', 'ext', 'idp-B')


def test_migration_quarantines_inconsistent_rows_instead_of_failing():
    """An existing cross-org row must not abort the migration. It becomes
    unowned (NULL), which is the established fail-closed state, and the affected
    user's sessions are retired by the same rule 0021 applies."""
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_user(conn, 'u-b', 'org-B', 'b@b.example')
        _insert_idp(conn, 'idp-A', 'org-A')
        _insert_idp(conn, 'idp-B', 'org-B')
        _insert_mapping(conn, 'm-1', 'org-A', 'u-b', 'ext', 'idp-B')
    _run(engine, '0023_mapping_organization_alignment')          # must not raise
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT identity_provider_id FROM scim_user_mappings WHERE id='m-1'")).one()
    assert row[0] is None, (
        "a cross-org mapping kept its owner through the migration; it must be "
        "quarantined as unowned, not left pointing across organizations")


def test_inconsistent_mapping_deactivates_the_affected_user():
    engine = _head(_fresh_engine())
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_user(conn, 'u-b', 'org-B', 'b@b.example')
        _insert_idp(conn, 'idp-A', 'org-A')
        _insert_idp(conn, 'idp-B', 'org-B')
        _insert_mapping(conn, 'm-1', 'org-A', 'u-b', 'ext', 'idp-B')
    _run(engine, '0023_mapping_organization_alignment')
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            'SELECT active, session_epoch FROM users WHERE id = :i'),
            {'i': 'u-b'}).one()
    assert row[0] in (0, False), (
        "the user behind a cross-org mapping stayed active")
    assert (row[1] or 0) >= 1, (
        "the user behind a cross-org mapping kept its live sessions")
