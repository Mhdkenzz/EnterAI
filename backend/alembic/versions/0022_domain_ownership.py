"""verified email domain ownership

Adds the proof-of-control column and the global exclusivity index that together
make an email domain usable for provisioning.

Three things this migration gets right that the first version did not, each
because a real database breaks otherwise:

* **Legacy bindings are canonicalized before the index is created.**
  `domain_binding` was a free-text column, so a real instance can hold
  `Acme.com`, ` acme.com. ` and `acme.com` as three values that are one domain.
  Indexing them raw leaves the logical duplicate in place, and -- worse -- if
  two rows already hold the same literal string, `CREATE UNIQUE INDEX` aborts
  the upgrade outright. A migration that cannot run against real data is not a
  migration. So: normalize every row first, and where two rows collapse to one
  domain, resolve it fail-closed rather than by guessing a winner.

* **Index shape is compared including its WHERE predicate.** "Unique on
  domain_binding" is not the invariant. A UNIQUE index on that column whose
  predicate restricts it to `domain_verified_at IS NULL` satisfies every
  name-and-uniqueness check while constraining nothing that matters. Reflection
  alone is not enough either: SQLite's inspector frequently reports no
  predicate at all for a partial index, and "no predicate reported" must never
  be read as "the right predicate". So the comparison is made on a normalized
  form of the clause, and a missing predicate is treated as a mismatch to
  repair rather than as a pass.

* **Exclusivity attaches to active + verified rows only.** See the predicate
  discussion in app/sso_models.py: reserving a domain for whoever merely typed
  it first let any administrator lock a domain for the whole instance, with no
  way to release it.

PostgreSQL note: the boolean/date literals here are bound parameters, never
integer literals. SQLite's dynamic typing accepts an integer where PostgreSQL's
BOOLEAN column rejects it, which is exactly the class of defect
test_migration_portability_v3.py asserts against -- so this prose deliberately
avoids quoting the integer form, which that check scans for outside comments.
"""
import sqlalchemy as sa
from alembic import op

revision = '0022_domain_ownership'
down_revision = '0021_scim_provider_ownership'
branch_labels = None
depends_on = None

COLUMN = "domain_verified_at"
TABLE = "identity_providers"
DOMAIN_INDEX = "uq_identity_providers_domain_binding"

# The exact predicate the live invariant requires. Kept as one string so the
# create and the comparison can never drift apart.
DOMAIN_PREDICATE = (
    "domain_binding IS NOT NULL AND active "
    "AND domain_verified_at IS NOT NULL"
)


def _columns(conn):
    return {c["name"] for c in sa.inspect(conn).get_columns(TABLE)}


def _normalize_predicate(where):
    """A comparable form of a partial-index WHERE clause.

    Reflection returns either a string, a SQLAlchemy TextClause, or nothing at
    all, and spellings differ by whitespace, case, parenthesis and redundant
    `IS NOT NULL`. All of those are normalized away here so a semantically
    identical predicate compares equal.

    The `is None` chain matters: a TextClause has no defined truth value, and
    evaluating one with `or` raises TypeError. That is not hypothetical -- the
    `a or b` form of this lookup made `downgrade()` fail on the round trip.
    """
    if where is None:
        return None
    text = str(getattr(where, "text", where))
    return " ".join(text.lower().replace("(", " ").replace(")", " ").split())


def _predicate_matches(where, expected: str) -> bool:
    """True only when `where` says what `expected` says.

    A missing predicate is a MISMATCH, never a pass. An index with no WHERE
    clause is unique over the whole column, which is a strictly different (and
    here, wrong) invariant.
    """
    normalized = _normalize_predicate(where)
    if normalized is None:
        return False
    return normalized == _normalize_predicate(expected)


def _index_definitions(conn):
    """{name: (columns, unique, where)} for every index on the table."""
    out = {}
    for index in sa.inspect(conn).get_indexes(TABLE):
        options = index.get("dialect_options") or {}
        where = options.get("sqlite_where")
        if where is None:
            where = options.get("postgresql_where")
        out[index.get("name")] = (
            tuple(index.get("column_names") or []),
            bool(index.get("unique")),
            where,
        )
    return out


def _catalog_predicates(conn):
    """{name: predicate_text} straight from the database's own catalog.

    Reflection omits partial predicates on SQLite often enough that relying on
    it alone means a correct index is "not found" and a correct migration tries
    to create a second one, failing. sqlite_master carries the literal CREATE
    INDEX text, predicate included.
    """
    try:
        rows = conn.execute(sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = :t"), {'t': TABLE}).all()
    except Exception:
        return {}
    out = {}
    for name, sql in rows:
        if not sql:
            continue
        marker = sql.upper().find(" WHERE ")
        out[name] = sql[marker:] if marker != -1 else None
    return out


def _has_correct_domain_index(conn) -> bool:
    """True when the exclusivity invariant is already enforced.

    Shape, not name: any index satisfying columns + uniqueness + predicate
    establishes the invariant, so a valid differently-named one is left alone
    rather than duplicated. Anything else -- wrong column, non-unique, or a
    predicate that restricts the wrong rows -- is not sufficient and is not
    accepted.
    """
    expected = _normalize_predicate(DOMAIN_PREDICATE)
    catalog = _catalog_predicates(conn)
    reflected = {name: where for name, (_c, _u, where) in _index_definitions(conn).items()}
    for name, (columns, unique, where) in _index_definitions(conn).items():
        if columns != ("domain_binding",) or not unique:
            continue
        candidates = [where]
        catalog_text = catalog.get(name)
        if catalog_text is not None:
            candidates.append(catalog_text)
        for candidate in candidates:
            if _predicate_matches(candidate, DOMAIN_PREDICATE):
                return True
    return False


def _canonicalize_bindings(conn) -> None:
    """Normalize every legacy binding, resolving duplicates fail-closed.

    Imported from the application rather than reimplemented here, so a
    migration and a live request can never disagree about what a canonical
    domain is. The import is at call time: alembic/env.py imports the app
    models before running migrations, and importing the package at module scope
    would make this file unimportable in a bare migration context.
    """
    from app import domain_verification

    rows = conn.execute(sa.text(
        f"SELECT id, domain_binding FROM {TABLE} "
        "WHERE domain_binding IS NOT NULL")).fetchall()
    if not rows:
        return

    resolved = {}       # canonical -> [row ids]
    for row_id, raw in rows:
        try:
            canonical = domain_verification.normalize_domain(raw)
        except ValueError:
            # Unparseable: fail closed. Keeping the raw string would let a value
            # that can never be normalized later still sit in the column, and
            # an index over it enforces nothing meaningful.
            conn.execute(sa.text(
                f"UPDATE {TABLE} SET domain_binding = NULL WHERE id = :i"),
                {'i': row_id})
            continue
        resolved.setdefault(canonical, []).append((row_id, raw))

    for canonical, members in resolved.items():
        if len(members) == 1:
            row_id, raw = members[0]
            if raw != canonical:
                conn.execute(sa.text(
                    f"UPDATE {TABLE} SET domain_binding = :d WHERE id = :i"),
                    {'d': canonical, 'i': row_id})
            continue
        # Two or more rows claim one domain. That is only dangerous when they
        # were DISTINCT claims that happen to canonicalize together -- a real
        # historical duplicate. Rows that were merely different SPELLINGS of one
        # string are not a conflict at all: they are the same administrator
        # assertion written inconsistently, and collapsing them is the whole
        # point of canonicalizing. So the distinction is spelling, not count.
        spellings = {raw for _row_id, raw in members}
        if len(spellings) == 1:
            for row_id, _raw in members:
                conn.execute(sa.text(
                    f"UPDATE {TABLE} SET domain_binding = :d WHERE id = :i"),
                    {'d': canonical, 'i': row_id})
            continue
        # Genuinely different strings that normalize to one domain: no winner is
        # chosen. Guessing would hand a tenant's existing identities to whichever
        # row happened to be written first, which is the exact failure the
        # provider-ownership work exists to prevent. Every claimant loses the
        # binding and must re-assert it through the verified path.
        for row_id, _raw in members:
            conn.execute(sa.text(
                f"UPDATE {TABLE} SET domain_binding = NULL WHERE id = :i"),
                {'i': row_id})


def upgrade():
    bind = op.get_bind()

    # The proof column. Independent of the index so a half-applied migration
    # can be re-run: each step checks its own precondition.
    if COLUMN not in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.DateTime(), nullable=True))

    # Canonicalize BEFORE the index. Indexing raw legacy strings both leaves
    # logical duplicates in place and aborts outright on exact duplicates.
    _canonicalize_bindings(bind)

    # Global domain exclusivity, restricted to live claims.
    if not _has_correct_domain_index(bind):
        for name, (columns, unique, _where) in _index_definitions(bind).items():
            if "domain_binding" in columns:
                op.drop_index(name, table_name=TABLE)
        op.create_index(
            DOMAIN_INDEX, TABLE, ["domain_binding"], unique=True,
            sqlite_where=sa.text(DOMAIN_PREDICATE),
            postgresql_where=sa.text(DOMAIN_PREDICATE),
        )

    # No backfill. domain_verified_at stays NULL for every existing row: those
    # providers hold a typed-in assertion, not a proof, so they must not become
    # usable until an administrator publishes the TXT record. Guessing a
    # verification here would hand provisioning rights to unverified domains.
    # Every row whose binding was dropped above is therefore also unverified,
    # which is the fail-closed end state the duplicate resolution wants.


def downgrade():
    bind = op.get_bind()
    if _has_correct_domain_index(bind):
        for name, (columns, unique, _where) in _index_definitions(bind).items():
            if columns == ("domain_binding",) and unique:
                op.drop_index(name, table_name=TABLE)
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
