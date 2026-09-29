"""Phase 1 v6 -- migration portability, composite-FK repair, stale domain
reclaim, and real race outcomes.

Static v6 review of the v5 candidate found six verified blockers. Each test
below names the one it pins, and each is proven non-vacuous against the
candidate (the defect was reintroduced and the test observed to fail).

Blockers pinned here
--------------------
1. `0023_mapping_organization_alignment.py` wrote `active = 0` / `active = 1`
   into a BOOLEAN column. SQLite accepts that by dynamic typing; PostgreSQL
   rejects it. No test covered 0023 at all -- the v3 portability check only
   read 0021.
2. `_has_composite_fk` was called with `referred_columns` in BOTH the
   constrained and referred positions, so it compared a real child column list
   `['user_id','organization_id']` against the parent names `['id',
   'organization_id']`, never matched, and duplicated the constraint on re-run.
3. The migration tests derived their starting schema from current ORM
   metadata, which already declares the constraints, so "upgrade" repaired
   nothing and proved nothing.
4. A partial unique index cannot call a non-immutable function like now(), so
   an EXPIRED verified claim keeps occupying the exclusive slot forever and no
   legitimate re-claim can ever win it. There was no reclaim path.
5. `sso_login_start` fetched "an arbitrary active matching binding" and only
   then checked verification, so an unverified duplicate row could shadow a
   valid claimant and deny login for the whole domain.
6. Deactivating a provider left its users' sessions and mappings live; the
   brief requires the treatment to be explicit and fail-closed.

PostgreSQL
----------
No PostgreSQL server exists in this environment (no psql/postgres/initdb
binary, no Docker, nothing on 5432). PostgreSQL DDL here is COMPILED and
INSPECTED through SQLAlchemy's PostgreSQL dialect -- compile-time evidence,
not an executed migration. No test in this file claims otherwise.
"""
import importlib.util
import io
import os
import pathlib
import tempfile
import threading
import tokenize
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc
from sqlalchemy import select as sa_select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  -- registers Base metadata
import app.sso_models  # noqa: F401
from app import domain_verification
from app.database import Base, SessionLocal
from app.main import app
from app.models import Organization, User
from app.sso_auth import commit_scim, scim_create_or_update
from app.sso_models import IdentityProvider, SCIMUserMapping

BACKEND = pathlib.Path(__file__).parent.parent
VERSIONS = BACKEND / "alembic" / "versions"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _load(revision: str):
    """Import a migration file by path.

    alembic/ is not a package, and a module name beginning with a digit cannot
    be imported by name at all, so this goes through the filesystem.
    """
    matches = [p for p in VERSIONS.glob(f"{revision}*.py")]
    assert len(matches) == 1, f"expected one {revision} migration, got {matches}"
    spec = importlib.util.spec_from_file_location(f"_mig_{revision}", matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(revision: str) -> str:
    matches = [p for p in VERSIONS.glob(f"{revision}*.py")]
    assert len(matches) == 1, matches
    return matches[0].read_text()


def _code_only(source: str) -> str:
    """Source with COMMENT tokens dropped, original spacing preserved.

    STRING tokens are kept on purpose: the SQL under test lives in a string
    literal, and dropping strings too would leave nothing to inspect and pass
    unconditionally. That was caught by reintroducing the defect and watching
    the check fail to notice.
    """
    kept = [tok for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type != tokenize.COMMENT]
    return tokenize.untokenize(kept)


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'V6 {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    body = response.json()
    return {'Authorization': 'Bearer ' + body['token']}, body


def _domain(tag: str) -> str:
    return f'{tag}-{uuid4().hex[:10]}.example'


def _create(client, admin, name, domain):
    created = client.post('/api/admin/sso/providers', headers=admin, json={
        'name': name, 'protocol': 'oidc', 'domain_binding': domain})
    assert created.status_code == 200, created.text
    return created.json()['id']


def _verify(client, admin, provider_id, domain):
    with mock.patch.object(domain_verification, 'lookup_txt',
                           return_value=[domain_verification.expected_txt_value(domain)]):
        return client.post(
            f'/api/admin/sso/providers/{provider_id}/domain-verification',
            headers=admin, json={'domain': domain})


def _ensure_org(conn, org):
    exists = conn.execute(sa_select(Organization).where(
        Organization.id == org)).first()
    if exists is None:
        conn.execute(sa.insert(Organization), [{'id': org, 'name': org, 'slug': org}])


def _insert_user(conn, uid, org, email, active=True):
    _ensure_org(conn, org)
    conn.execute(sa.insert(User), [{
        'id': uid, 'organization_id': org, 'name': uid, 'email': email,
        'password_hash': '', 'role': 'member', 'active': active, 'kind': 'human',
    }])


def _insert_idp(conn, pid, org, domain=None, verified_at=None, active=True):
    _ensure_org(conn, org)
    row = {'id': pid, 'organization_id': org, 'name': pid, 'protocol': 'oidc',
           'active': active, 'sso_only': False, 'role_mapping_json': {}}
    if domain is not None:
        row['domain_binding'] = domain
    if verified_at is not None:
        row['domain_verified_at'] = verified_at
    conn.execute(sa.insert(IdentityProvider), [row])


def _fresh_engine():
    """File-backed, not :memory: -- Alembic spans several connections."""
    directory = tempfile.mkdtemp()
    return sa.create_engine('sqlite:///' + os.path.join(directory, 'v6.sqlite'))


def _run(engine, revision: str):
    """Execute one migration's upgrade() against `engine`, committing."""
    module = _load(revision)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            if conn.in_transaction():
                # A statement on this connection autobegins a transaction, and
                # batch_alter_table's DDL needs to open its own. Close it first.
                conn.commit()
            module.upgrade()
            # A migration that leaves its writes uncommitted loses them when the
            # connection closes -- which silently turns every assertion into a
            # no-op pass.
            conn.commit()


# =========================================================================== #
# BLOCKER 1 -- 0023 still uses integer boolean literals
# =========================================================================== #

MIGRATIONS_WITH_SQL = [
    '0021_scim_provider_ownership',
    '0022_domain_ownership',
    '0023_mapping_organization_alignment',
]


@pytest.mark.parametrize("revision", MIGRATIONS_WITH_SQL)
def test_no_migration_assigns_an_integer_to_a_boolean_column(revision):
    """Every migration carrying SQL is inspected -- not just 0021.

    v3's check read 0021 only, so 0023 shipped `active = 0` / `active = 1`
    straight through. `users.active` is a real BOOLEAN and PostgreSQL will not
    coerce an integer literal into it. SQLite executes it happily, so a
    behavioural SQLite test cannot catch this class of bug and there is no
    PostgreSQL server here to execute against -- the source is the only place
    the defect is observable, which is why the check covers every file rather
    than the one that happened to be checked before.
    """
    code = _code_only(_source(revision))
    for literal in ('active = 0', 'active = 1',
                    'active=0', 'active=1',
                    'scim_active = 0', 'scim_active = 1',
                    'scim_active=0', 'scim_active=1'):
        assert literal not in code, (
            f"{revision} assigns an integer literal to a BOOLEAN column "
            f"({literal!r}); PostgreSQL rejects this where SQLite accepts it")


def test_0023_compiles_boolean_writes_for_postgresql():
    """The retirement UPDATE must emit a real Boolean for PostgreSQL.

    Compile-time evidence, not execution: SQLAlchemy compiles the statement
    through the PostgreSQL dialect and the bound parameter is inspected. No
    PostgreSQL server exists in this environment, so this is NOT a claim that
    the migration ran on PostgreSQL.
    """
    module = _load('0023_mapping_organization_alignment')
    captured = []

    class _Conn:
        def execute(self, stmt, params=None):
            captured.append((stmt, params))
            return mock.Mock(rowcount=0)

    module._retire_quarantined(_Conn())
    assert captured, "no retirement statement was issued"

    pg = sa.dialects.postgresql.dialect(paramstyle='pyformat')
    for stmt, params in captured:
        sql = str(stmt.compile(dialect=pg))
        assert 'active' in sql.lower()
        # No integer literal for the Boolean anywhere in the statement text:
        # the values are bound, so they cannot appear in the compiled SQL.
        for literal in ('active = 0', 'active = 1', 'active=0', 'active=1'):
            assert literal not in sql, (
                f"integer literal compiled into PostgreSQL SQL: {sql}")
        assert params is not None, (
            "the Booleans were not bound as parameters")
        assert params.get('revoked') is False, (
            f"'revoked' is not a bound False: {params}")
        assert params.get('was_active') is True, (
            f"'was_active' is not a bound True: {params}")


# =========================================================================== #
# BLOCKER 2 -- _has_composite_fk compared the wrong column lists
# =========================================================================== #

def _pre_0023_engine():
    """A schema that genuinely LACKS every constraint 0023 adds.

    The v5 tests built their starting schema from current ORM metadata, which
    already declares the composite FKs and the parent unique indexes -- so
    `upgrade()` found everything in place and repaired nothing, and the tests
    asserted against a schema the migration had never touched. This builds the
    pre-0023 shape by hand: the two single-column FKs, and no parent unique
    index on (id, organization_id), and no composite FK.
    """
    engine = _fresh_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Start from the REAL schema, then remove exactly what 0023 adds. The
        # brief's point is that a repair test must begin from a state where the
        # constraints are absent or malformed -- deriving the fixture from
        # current ORM metadata and then "upgrading" asserts against a schema
        # the migration never touched. Everything except the three objects
        # below is untouched, so nothing else can fail for unrelated reasons
        # (an earlier hand-written `users` was missing execution_enabled).
        conn.execute(sa.text("DROP INDEX IF EXISTS uq_users_id_organization"))
        conn.execute(sa.text(
            "DROP INDEX IF EXISTS uq_identity_providers_id_organization"))
        # Recreate the mapping table with only its two single-column FKs -- the
        # pre-0023 shape. DROP + CREATE rather than RENAME: SQLite's ALTER
        # TABLE RENAME rewrites references in a way that leaves the inspector
        # reporting four foreign keys, two of them unnamed.
        conn.execute(sa.text("DROP TABLE scim_user_mappings"))
        conn.execute(sa.text("""
            CREATE TABLE scim_user_mappings (
                id VARCHAR PRIMARY KEY,
                organization_id VARCHAR NOT NULL,
                user_id VARCHAR NOT NULL,
                scim_external_id VARCHAR NOT NULL,
                identity_provider_id VARCHAR,
                scim_active BOOLEAN NOT NULL DEFAULT 1,
                last_synced_at DATETIME,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (identity_provider_id)
                    REFERENCES identity_providers (id))
        """))
    return engine


def test_upgrade_adds_composite_fk_with_correct_child_and_parent_columns():
    engine = _pre_0023_engine()
    with engine.begin() as conn:
        # Fixture preconditions, asserted rather than assumed: a test that
        # "repairs" a schema which already had the constraint proves nothing.
        for pid, (table, cols) in {
            'fk_scim_mappings_user_org': ('users', ('id', 'organization_id')),
            'fk_scim_mappings_provider_org': (
                'identity_providers', ('id', 'organization_id')),
        }.items():
            assert pid not in {
                fk.get('name') for fk in sa.inspect(conn).get_foreign_keys(
                    'scim_user_mappings')}, (
                f"fixture already declares {pid}; the upgrade would be a no-op")

    _run(engine, '0023_mapping_organization_alignment')

    with engine.connect() as conn:
        fks = {fk.get('name'): fk
               for fk in sa.inspect(conn).get_foreign_keys('scim_user_mappings')}
        user_fk = fks.get('fk_scim_mappings_user_org')
        assert user_fk is not None, f"missing composite user FK: {list(fks)}"
        # The CHILD columns and the PARENT referents are different column
        # names, and getting them backwards yields an FK pointing at a column
        # that does not exist -- which SQLite reports only as a bare
        # "foreign key mismatch", and which silently accepts nothing.
        assert tuple(user_fk['constrained_columns']) == \
            ('user_id', 'organization_id'), user_fk
        assert tuple(user_fk['referred_columns']) == \
            ('id', 'organization_id'), user_fk
        assert user_fk['referred_table'] == 'users'

        provider_fk = fks.get('fk_scim_mappings_provider_org')
        assert provider_fk is not None
        assert tuple(provider_fk['constrained_columns']) == \
            ('identity_provider_id', 'organization_id'), provider_fk
        assert tuple(provider_fk['referred_columns']) == \
            ('id', 'organization_id'), provider_fk
        assert provider_fk['referred_table'] == 'identity_providers'


def test_upgrade_rejects_a_cross_org_mapping_and_accepts_a_correct_one():
    """End-state invariant: the database itself refuses the cross-org row.

    This is SQLite. SQLite does honour composite foreign keys when the parent
    key is a unique INDEX, so this is real enforcement here -- but it is NOT
    PostgreSQL evidence, and the test says so rather than implying otherwise.
    """
    engine = _pre_0023_engine()
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_user(conn, 'u-b', 'org-B', 'b@b.example')
        _insert_idp(conn, 'idp-A', 'org-A', 'a.example',
                    verified_at=datetime.now(timezone.utc))
        _insert_idp(conn, 'idp-B', 'org-B', 'b.example',
                    verified_at=datetime.now(timezone.utc))
    _run(engine, '0023_mapping_organization_alignment')

    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        good = conn.execute(sa.text(
            "INSERT INTO scim_user_mappings "
            "(id, organization_id, user_id, scim_external_id, "
            " identity_provider_id, scim_active) VALUES "
            "('m-ok', 'org-A', 'u-a', 'ext-ok', 'idp-A', 1)"))
        assert good.rowcount == 1

    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        with pytest.raises(sa_exc.IntegrityError):
            conn.execute(sa.text(
                "INSERT INTO scim_user_mappings "
                "(id, organization_id, user_id, scim_external_id, "
                " identity_provider_id, scim_active) VALUES "
                "('m-bad', 'org-A', 'u-b', 'ext-bad', 'idp-A', 1)"))


def test_rerun_does_not_duplicate_a_correct_alternate_name_fk():
    """A correct composite FK under ANY name is left alone, and re-running is
    idempotent.

    v5's call site passed `referred_columns` into the `constrained` parameter,
    so the comparison was `['id','organization_id'] == ['user_id',
    'organization_id']` -- never true. A correct pre-existing FK was therefore
    "missing" and got created a second time.
    """
    module = _load('0023_mapping_organization_alignment')
    engine = _pre_0023_engine()
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        _insert_idp(conn, 'idp-A', 'org-A', 'a.example',
                    verified_at=datetime.now(timezone.utc))
        # A correct composite FK, deliberately under a different name, added on
        # top of the pre-0023 shape.
        conn.execute(sa.text("DROP TABLE scim_user_mappings"))
        conn.execute(sa.text("""
            CREATE TABLE scim_user_mappings (
                id VARCHAR PRIMARY KEY,
                organization_id VARCHAR NOT NULL,
                user_id VARCHAR NOT NULL,
                scim_external_id VARCHAR NOT NULL,
                identity_provider_id VARCHAR,
                scim_active BOOLEAN NOT NULL DEFAULT 1,
                last_synced_at DATETIME,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (identity_provider_id)
                    REFERENCES identity_providers (id),
                CONSTRAINT some_other_name_for_the_same_thing
                    FOREIGN KEY (user_id, organization_id)
                    REFERENCES users (id, organization_id))
        """))
        conn.execute(sa.text(
            "INSERT INTO scim_user_mappings "
            "(id, organization_id, user_id, scim_external_id, "
            " identity_provider_id, scim_active) VALUES "
            "('m-1', 'org-A', 'u-a', 'ext-1', 'idp-A', 1)"))

    _run(engine, '0023_mapping_organization_alignment')

    def _composite_user_fks(conn):
        return [fk for fk in sa.inspect(conn).get_foreign_keys('scim_user_mappings')
                if fk.get('referred_table') == 'users'
                and tuple(fk.get('referred_columns') or []) == ('id', 'organization_id')]

    with engine.connect() as conn:
        composites = _composite_user_fks(conn)
        # The invariant is SEMANTIC: exactly one composite reference to
        # users(id, organization_id). Asserting on the constraint NAME instead
        # would be asserting an implementation detail -- SQLite's batch
        # recreate legitimately drops an inline constraint's name, and the
        # guarantee that matters is that the reference is present exactly once.
        assert len(composites) == 1, (
            f"expected exactly one composite users FK, got {len(composites)}: "
            f"{[ (f.get('name'), f.get('constrained_columns')) for f in composites ]}")
        assert tuple(composites[0]['constrained_columns']) == \
            ('user_id', 'organization_id'), composites[0]
        names = [fk.get('name') for fk in sa.inspect(conn).get_foreign_keys(
            'scim_user_mappings')]

    # And running the whole upgrade twice changes nothing.
    before = sorted(str(n) for n in names)
    _run(engine, '0023_mapping_organization_alignment')
    _run(engine, '0023_mapping_organization_alignment')
    with engine.connect() as conn:
        assert len(_composite_user_fks(conn)) == 1, (
            "re-running duplicated the composite FK")
        after = sorted(str(fk.get('name')) for fk in sa.inspect(conn).get_foreign_keys(
            'scim_user_mappings'))
    assert after == before, f"re-running mutated the schema: {before} -> {after}"


def test_has_composite_fk_matches_on_actual_column_lists():
    """The predicate itself, called with realistic arguments.

    Guards the specific v5 defect: the call passed the parent column names in
    the child position, so no real mapping could ever match.
    """
    module = _load('0023_mapping_organization_alignment')
    engine = _pre_0023_engine()
    with engine.begin() as conn:
        _insert_user(conn, 'u-a', 'org-A', 'a@a.example')
        conn.execute(sa.text("DROP TABLE scim_user_mappings"))
        conn.execute(sa.text("""
            CREATE TABLE scim_user_mappings (
                id VARCHAR PRIMARY KEY,
                organization_id VARCHAR NOT NULL,
                user_id VARCHAR NOT NULL,
                scim_external_id VARCHAR NOT NULL,
                identity_provider_id VARCHAR,
                scim_active BOOLEAN NOT NULL DEFAULT 1,
                last_synced_at DATETIME,
                CONSTRAINT ck_user_org
                    FOREIGN KEY (user_id, organization_id)
                    REFERENCES users (id, organization_id))
        """))

    with engine.connect() as conn:
        assert module._has_composite_fk(
            conn, ['user_id', 'organization_id'], 'users',
            ['id', 'organization_id']) is True, (
            "a correct composite FK was not recognised")
        # The single-column FK is NOT in this fixture, so asking for it must
        # report absence -- the predicate must not answer "yes" to anything.
        assert module._has_composite_fk(
            conn, ['user_id'], 'users', ['id']) is False, (
            "a single-column FK that does not exist was reported as present")
        assert module._has_composite_fk(
            conn, ['user_id', 'organization_id'], 'identity_providers',
            ['id', 'organization_id']) is False, (
            "an absent composite FK was reported as present")


# =========================================================================== #
# BLOCKER 4 -- an expired verified claim must be reclaimable
# =========================================================================== #

def test_expired_claim_is_released_so_the_domain_can_be_reclaimed():
    """A claim older than the freshness threshold stops blocking a new one.

    The exclusivity index is partial on `domain_verified_at IS NOT NULL`, and a
    partial index may not call a non-immutable function such as now(). So an
    EXPIRED row still occupies the exclusive slot and no legitimate re-claim can
    ever win it -- a real administrator who forgot to renew is locked out of
    their own domain forever, with no route to release it.

    The sound fix is not a cleverer index: it is to revoke expired claims
    before the new one is attempted.
    """
    expired = datetime.now(timezone.utc) - domain_verification.VERIFICATION_TTL \
        - timedelta(days=1)
    with TestClient(app) as client:
        admin, data = _workspace(client)
        domain = _domain('expired')
        stale_id = _create(client, admin, 'Stale', domain)
        with SessionLocal() as db:
            db.get(IdentityProvider, stale_id).domain_verified_at = expired
            db.commit()
            # Fixture precondition: the stale row really is expired.
            assert domain_verification.is_domain_verified(
                db.get(IdentityProvider, stale_id)) is False

        # A second provider must be able to take the domain over.
        taker_id = _create(client, admin, 'Taker', domain)
        assert _verify(client, admin, taker_id, domain).status_code == 200, (
            "an expired claim permanently reserved the domain")

        with SessionLocal() as db:
            taker = db.get(IdentityProvider, taker_id)
            assert taker.domain_verified_at is not None
            assert domain_verification.is_domain_verified(taker) is True
            # The expired holder is not still live.
            assert domain_verification.is_domain_verified(
                db.get(IdentityProvider, stale_id)) is False


def test_fresh_claim_is_still_exclusive():
    """The reclaim path must not let two live claims coexist."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('fresh')
        first = _create(client, admin, 'First', domain)
        assert _verify(client, admin, first, domain).status_code == 200
        second = _create(client, admin, 'Second', domain)
        assert _verify(client, admin, second, domain).status_code == 409, (
            "two live verified claims on one domain")


def test_reclaim_only_touches_the_canonical_domain():
    """Revoking expired claims must not disturb any other domain's live claim."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        expired_domain = _domain('gone')
        live_domain = _domain('alive')
        expired = datetime.now(timezone.utc) - domain_verification.VERIFICATION_TTL \
            - timedelta(days=1)

        stale_id = _create(client, admin, 'Stale', expired_domain)
        live_id = _create(client, admin, 'Live', live_domain)
        with SessionLocal() as db:
            db.get(IdentityProvider, stale_id).domain_verified_at = expired
            db.get(IdentityProvider, live_id).domain_verified_at = \
                datetime.now(timezone.utc)
            db.commit()

        taker = _create(client, admin, 'Taker', expired_domain)
        assert _verify(client, admin, taker, expired_domain).status_code == 200

        with SessionLocal() as db:
            live = db.get(IdentityProvider, live_id)
            assert domain_verification.is_domain_verified(live) is True, (
                "reclaiming one domain revoked an unrelated live claim")


# =========================================================================== #
# BLOCKER 5 -- an unverified duplicate must not shadow a valid claimant
# =========================================================================== #

def test_oidc_routing_finds_the_verified_provider_despite_an_unverified_duplicate():
    """OIDC domain routing must query only live claims.

    v5 did `select ... where domain_binding = :d and active` and then checked
    verification on the single row it happened to get. With an unverified
    duplicate row present, that arbitrary row was returned, the check failed,
    and the whole domain was denied login -- even though a valid verified
    provider existed and could have served the request.
    """
    from app import sso_oidc
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('shadow')
        good = _create(client, admin, 'Good', domain)
        assert _verify(client, admin, good, domain).status_code == 200
        # An unverified duplicate, inserted so it is not the only match.
        with SessionLocal() as db:
            _insert_idp(db, 'shadow-' + uuid4().hex[:8], db.get(
                IdentityProvider, good).organization_id, domain,
                verified_at=None, active=True)
            db.commit()

        with mock.patch.object(sso_oidc, 'build_authorization_url',
                               return_value='https://idp.invalid/authorize') as build:
            response = client.get('/api/auth/sso/start',
                                  params={'email': f'a@{domain}'},
                                  follow_redirects=False)
        assert build.call_count == 1, (
            "an unverified duplicate row shadowed the verified provider and "
            "denied login for the whole domain")
        assert response.status_code == 302


def test_oidc_routing_denies_an_expired_claim():
    """Expired is not live: routing must refuse rather than start a flow that
    provisioning would then reject."""
    from app import sso_oidc
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        domain = _domain('stale-route')
        provider_id = _create(client, admin, 'Stale', domain)
        expired = datetime.now(timezone.utc) - domain_verification.VERIFICATION_TTL \
            - timedelta(days=1)
        with SessionLocal() as db:
            db.get(IdentityProvider, provider_id).domain_verified_at = expired
            db.commit()
        with mock.patch.object(sso_oidc, 'build_authorization_url',
                               return_value='https://idp.invalid/authorize') as build:
            response = client.get('/api/auth/sso/start',
                                  params={'email': f'a@{domain}'},
                                  follow_redirects=False)
        assert build.call_count == 0, (
            "routing started a flow for an expired claim")
        assert response.status_code in (302, 303, 307), response.status_code


# =========================================================================== #
# BLOCKER 6 -- deactivation must fail closed for provider-originated access
# =========================================================================== #

def _resolve_user(scim_external_id):
    """SCIM's returned `id` is the external id, not users.id."""
    with SessionLocal() as db:
        mapping = db.scalar(sa_select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == scim_external_id))
        assert mapping is not None, f"no mapping for {scim_external_id}"
        return mapping.user_id


def test_deactivation_revokes_sessions_behind_the_providers_mappings():
    """Deactivating a provider is a compromise response, so it must also retire
    the local sessions its identities hold.

    Leaving them valid means the only thing an operator can do about a
    compromised IdP is flip a flag that does nothing to the sessions already
    minted.
    """
    with TestClient(app) as client:
        admin, data = _workspace(client)
        org = data['organization']['id']
        domain = _domain('deact')
        provider_id = _create(client, admin, 'Compromised', domain)
        assert _verify(client, admin, provider_id, domain).status_code == 200
        from app.sso_routes import issue_scim_token
        with SessionLocal() as db:
            idp = db.get(IdentityProvider, provider_id)
            raw = issue_scim_token(db, idp)
            db.commit()
        headers = {'Authorization': f'Bearer {raw}'}
        created = client.post('/scim/v2/Users', headers=headers, json={
            'userName': f'user@{domain}', 'displayName': 'Victim'})
        assert created.status_code == 200, created.text
        # The SCIM response's `id` is the external id, not users.id.
        user_id = _resolve_user(created.json()['id'])

        with SessionLocal() as db:
            user = db.get(User, user_id)
            mapping = db.scalar(sa_select(SCIMUserMapping).where(
                SCIMUserMapping.user_id == user_id))
            assert user.active is True
            epoch_before = user.session_epoch or 0
            assert mapping is not None

        off = client.post(f'/api/admin/sso/providers/{provider_id}/deactivate',
                          headers=admin)
        assert off.status_code == 200, off.text

        with SessionLocal() as db:
            user = db.get(User, user_id)
            assert user.active is False, (
                "the local account behind a deactivated provider stayed active")
            assert (user.session_epoch or 0) > epoch_before, (
                "session_epoch was not bumped, so existing JWTs remain valid")

        # And the provider's own token no longer provisions.
        denied = client.post('/scim/v2/Users', headers=headers, json={
            'userName': f'second@{domain}', 'displayName': 'Another'})
        assert 400 <= denied.status_code < 500, denied.text


# =========================================================================== #
# BLOCKER 6b / item 4 -- a real race, with a barrier and a specific outcome
# =========================================================================== #

def test_concurrent_scim_provision_has_one_winner_and_no_orphans():
    """Two threads, a real barrier, a specific required outcome.

    v5's race tests used mocks that asserted "some exception happened", which a
    broken implementation passes by accident. This one requires: exactly one
    200, the other a 4xx that is not 500, and no user row left behind without
    its mapping.
    """
    engine = _fresh_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    domain = _domain('race-v6')
    with Session() as setup:
        org = 'org-race-v6'
        _ensure_org(setup, org)
        provider = IdentityProvider(
            organization_id=org, name='P', protocol='oidc', active=True,
            domain_binding=domain, domain_verified_at=domain_verification.utcnow())
        setup.add(provider)
        setup.commit()
        provider_id = provider.id

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()
    contested = f'contested@{domain}'

    def provision(index):
        session = Session()
        try:
            barrier.wait(timeout=10)
            user = scim_create_or_update(session, org, 'shared-ext', contested,
                                         'X', source_provider_id=provider_id)
            commit_scim(session)
            with lock:
                outcomes.append(('ok', user.id))
        except Exception as exc:  # noqa: BLE001 -- the outcome is what matters
            status = getattr(exc, 'status_code', None)
            with lock:
                outcomes.append(
                    ('error', status, type(exc).__name__, str(exc)[:120]))
        finally:
            session.close()

    threads = [threading.Thread(target=provision, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(t.is_alive() for t in threads), "a worker deadlocked"

    with lock:
        recorded = list(outcomes)
    winners = [o for o in recorded if o[0] == 'ok']
    losers = [o for o in recorded if o[0] == 'error']
    assert len(winners) == 1, (
        f"expected exactly one winner, got {recorded}")
    assert len(losers) == 1, f"expected exactly one loser, got {recorded}"

    status, name, message = losers[0][1], losers[0][2], losers[0][3]
    assert status is not None and 400 <= status < 500, (
        f"the loser surfaced as {name}({status}): {message} -- a 500 or an "
        f"unclassified error is exactly the defect under test")
    assert 'IntegrityError' not in name, (
        f"the raw IntegrityError escaped: {name}")

    with Session() as verify:
        users = verify.scalars(sa_select(User).where(
            User.email == contested)).all()
        mappings = verify.scalars(sa_select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == 'shared-ext')).all()
        assert len(users) == 1, f"expected one user row, got {len(users)}"
        assert len(mappings) == 1, f"expected one mapping row, got {len(mappings)}"
        assert users[0].id == mappings[0].user_id, (
            "an orphaned user or mapping was left behind")
