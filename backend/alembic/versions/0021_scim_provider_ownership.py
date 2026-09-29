"""SCIM mappings are owned by an identity provider (Phase 1, group 1).

`scim_user_mappings` had no provider identity: every lookup in sso_routes.py,
sso_auth.py and sso_oidc.py was scoped by `organization_id` +
`scim_external_id` alone, so two providers in one organization shared a single
identity namespace. A SCIM bearer token minted for provider A could read,
overwrite, deactivate, reactivate, role-escalate, and enumerate identities
owned by provider B. The boundary was enforced nowhere durable, so the fix
needs to live in the schema as well as in the query layer.

Migration safety for pre-existing rows
--------------------------------------
Existing rows carry no owner, and there is no correct value to infer for them:
the column is new, so no row ever recorded which provider created it, and
`identity_providers` rows have no creation timestamp that can be ordered
against a mapping's `created_at` with any reliability. Backfilling "the first
active provider in the organization" -- the obvious non-NULL option -- would
silently transfer those identities to an arbitrary tenant-configured IdP,
re-creating exactly the cross-provider disclosure this migration exists to
close, at a moment an administrator is not watching.

So this migration deliberately leaves every legacy row NULL, meaning
"unowned", and the application layer treats NULL as matching no provider:

  * an unowned mapping is invisible to SCIM list/GET/PUT/PATCH and to OIDC
    subject resolution, because every one of those paths now requires an exact
    `identity_provider_id` match;
  * no current write path can create one: `scim_create_or_update` now requires
    a live provider owned by `org_id` and refuses otherwise, so NULL owners
    arise only from this migration, never from a live SCIM POST/PUT.

Recovery of a legacy identity -- stated precisely
-------------------------------------------------
An earlier draft of this file claimed the owning IdP could recover a legacy
identity by re-POSTing it. That is wrong, and the correction is load-bearing:
the mapping is invisible to every provider, so the POST takes the *create*
branch, and `users.email` is globally unique, so the pre-existing account's
email is rejected with 409 before any new mapping can be created. The identity
therefore does NOT self-recover. It stays quarantined, and the only mechanism
that can rebind it is an operator deliberately setting
`scim_user_mappings.identity_provider_id` to the correct provider, having
established out of band which provider that is. There is no automatic or
silent binding path, and this migration will not invent one: inferring an owner
is precisely the cross-tenant disclosure the migration exists to prevent.

The operational cost is real and is accepted deliberately: after this migration
an existing SCIM deployment has quarantined identities that its IdP can no
longer edit until an operator rebinds them. Deployments that cannot accept that
should assess the affected row count before upgrading. The fails-closed
alternative -- guessing an owner -- fails open, into cross-tenant disclosure,
which is worse for a security boundary and is not recoverable after the fact.

Uniqueness for owned identities
-------------------------------
A partial unique index on (identity_provider_id, scim_external_id) WHERE
identity_provider_id IS NOT NULL makes "one external id per provider" durable
in the schema rather than only in the query layer, so a second write path cannot
create a duplicate that `scim_mapping_for_provider`'s db.scalar would silently
collapse. The predicate is what allows the legacy NULL-owner rows above to
coexist: they are outside the constraint by design.
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_scim_provider_ownership"
down_revision = "0020_trial_end_dates"
branch_labels = None
depends_on = None


def _has_correct_owner_fk(bind) -> bool:
    """True when identity_provider_id already has the FK we intend.

    Shape, not name. Accepts a correct FK whatever it is called (so no
    duplicate is created) and rejects a same-named one that constrains the
    wrong column or references the wrong table.
    """
    for fk in sa.inspect(bind).get_foreign_keys("scim_user_mappings"):
        if (tuple(fk.get("constrained_columns") or []) ==
                ("identity_provider_id",)
                and fk.get("referred_table") == "identity_providers"
                and tuple(fk.get("referred_columns") or []) == ("id",)):
            return True
    return False


def _normalize_predicate(where):
    """A comparable form of a partial-index WHERE clause.

    Reflection yields a string, a TextClause, or nothing; spellings differ by
    whitespace, case, parenthesis and redundant IS NOT NULL. All normalized
    away so a semantically identical clause compares equal.

    The `is None` chain is required: a TextClause has no defined truth value,
    so evaluating one with `or` raises TypeError.
    """
    if where is None:
        return None
    text = str(getattr(where, "text", where))
    return " ".join(text.lower().replace("(", " ").replace(")", " ").split())


def _catalog_predicates(bind, table) -> dict:
    """{name: predicate_text} from the database's own catalog.

    SQLite's inspector often reports NO predicate for a partial index, so
    reflection alone would make a correct index look wrong (and get rebuilt on
    every run) and a wrong-predicate index look right. sqlite_master carries
    the literal CREATE INDEX text, predicate included.
    """
    try:
        rows = bind.execute(sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = :t"), {'t': table}).all()
    except Exception:
        return {}
    out = {}
    for name, sql in rows:
        if not sql:
            continue
        marker = sql.upper().find(" WHERE ")
        out[name] = sql[marker:] if marker != -1 else None
    return out


def _has_owner_index(bind, columns, unique=None, where=None) -> bool:
    """True when an index on exactly these columns satisfies the requirement.

    `unique` and `where` are checked only when given. Matching on shape rather
    than name is what keeps a valid differently-named index from being
    duplicated, and stops a same-named wrong-shape one being accepted.

    The `where` predicate IS part of the match, and this was the v5 defect: the
    predicate argument was accepted and then ignored, on the stated reasoning
    that SQLite does not reflect it. That reasoning was half right -- reflection
    is unreliable -- but the conclusion drawn from it was wrong. An index
    restricted to a single fixed boolean value is "unique on these columns" and
    enforces nothing for the rows that matter. The fix is not to skip the
    comparison but to get the predicate from the catalog, which is authoritative
    on both SQLite and PostgreSQL, and fall back to reflection.

    A missing predicate is a MISMATCH when one was required: no WHERE clause
    means unique over the whole column, which is a different invariant.
    """
    table = "scim_user_mappings"
    catalog = _catalog_predicates(bind, table)
    reflected = {}
    for index in sa.inspect(bind).get_indexes(table):
        options = index.get("dialect_options") or {}
        predicate = options.get("sqlite_where")
        if predicate is None:
            predicate = options.get("postgresql_where")
        reflected[index.get("name")] = predicate

    expected = _normalize_predicate(where) if where is not None else None
    for name, index in ((i.get("name"), i)
                        for i in sa.inspect(bind).get_indexes(table)):
        if tuple(index.get("column_names") or []) != tuple(columns):
            continue
        if unique is not None and bool(index.get("unique")) != unique:
            continue
        if expected is None:
            return True
        for candidate in (reflected.get(name), catalog.get(name)):
            if candidate is None:
                continue
            if _normalize_predicate(candidate) == expected:
                return True
    return False


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "scim_user_mappings" not in set(inspector.get_table_names()):
        return  # pragma: no cover -- 0012_sso_enterprise always runs first

    columns = {c["name"] for c in inspector.get_columns("scim_user_mappings")}
    if "identity_provider_id" not in columns:
        op.add_column(
            "scim_user_mappings",
            sa.Column("identity_provider_id", sa.String(), nullable=True),
        )

    # The FK and the indexes are each repaired independently of the column.
    # Creating them inside the add_column branch (as the first draft of this
    # migration did) meant a database that already had the column -- e.g. one
    # written by a run interrupted between add_column and create_foreign_key,
    # or by the model-level create_all in 0001_initial -- never received them,
    # and the migration still reported success. That is a silently
    # under-migrated database, so each piece is now checked on its own.
    #
    # SQLite cannot ALTER to add a foreign key, so the FK goes through batch
    # mode (which recreates the table); PostgreSQL takes the real constraint.
    fk_name = "fk_scim_user_mappings_identity_provider_id"
    # Semantic, not name-based. A same-named FK that constrains the wrong
    # column or points at the wrong table is NOT proof of health -- keying on
    # the name accepted a broken constraint and left the schema broken. And a
    # correct FK under a different name is already fine, so matching on shape
    # avoids creating a duplicate. SQLite cannot ALTER to add a foreign key, so
    # the FK goes through batch mode (recreating the table); PostgreSQL takes
    # the real constraint.
    if not _has_correct_owner_fk(bind):
        for fk in sa.inspect(bind).get_foreign_keys("scim_user_mappings"):
            # Drop a same-named constraint whose definition is wrong, so it can
            # be replaced rather than left in place alongside a correct one.
            if fk.get("name") == fk_name:
                with op.batch_alter_table("scim_user_mappings") as batch_op:
                    batch_op.drop_constraint(fk_name, type_="foreignkey")
        with op.batch_alter_table("scim_user_mappings") as batch_op:
            batch_op.create_foreign_key(
                fk_name,
                "identity_providers",
                ["identity_provider_id"],
                ["id"],
            )

    # Same rule for the indexes: presence by shape, not by name.
    if not _has_owner_index(bind, ["identity_provider_id"]):
        for i in sa.inspect(bind).get_indexes("scim_user_mappings"):
            if i.get("name") == "ix_scim_user_mappings_identity_provider_id":
                op.drop_index("ix_scim_user_mappings_identity_provider_id",
                              table_name="scim_user_mappings")
        op.create_index(
            "ix_scim_user_mappings_identity_provider_id",
            "scim_user_mappings",
            ["identity_provider_id"],
        )

    # One external id per provider, enforced by the database. Partial on
    # IS NOT NULL so the legacy NULL-owner rows left above may coexist --
    # a plain unique index would not distinguish them either, but the
    # predicate states the intent explicitly and keeps the constraint from
    # ever applying to quarantine rows.
    if not _has_owner_index(
            bind, ["identity_provider_id", "scim_external_id"], unique=True,
            where="identity_provider_id IS NOT NULL"):
        for i in sa.inspect(bind).get_indexes("scim_user_mappings"):
            if i.get("name") == "uq_scim_user_mappings_provider_external_id":
                op.drop_index("uq_scim_user_mappings_provider_external_id",
                              table_name="scim_user_mappings")
        op.create_index(
            "uq_scim_user_mappings_provider_external_id",
            "scim_user_mappings",
            ["identity_provider_id", "scim_external_id"],
            unique=True,
            sqlite_where=sa.text("identity_provider_id IS NOT NULL"),
            postgresql_where=sa.text("identity_provider_id IS NOT NULL"),
        )

    _retire_quarantined_identities(bind)


def _retire_quarantined_identities(bind) -> None:
    """Revoke access for every user behind a NULL-owner mapping.

    Quarantining the mapping is not enough on its own. From this migration
    onward the mapping is invisible to every provider, so no SCIM or OIDC call
    can ever deactivate, re-enable, or rotate that account again -- there is no
    remaining code path that can revoke it. If the User row is left active with
    its existing session_epoch untouched, then:

      * every session cookie issued before the migration stays valid, for an
        identity the operator can no longer manage through the product; and
      * the SCIM surface looks locked down while the actual access is still
        wide open.

    So the migration deactivates those users and bumps session_epoch, which is
    the same mechanism `deactivate_scim_user` uses on the live PATCH path. The
    epoch bump is what actually kills the already-issued tokens; `active=False`
    alone would leave them working.

    Deliberately NOT done here: attributing the mapping to a provider, and
    re-provisioning or deleting the user. Both would be guesses about which
    provider owns the identity, which is the cross-tenant disclosure this
    migration exists to prevent.

    Only users still active are touched, and the epoch is incremented
    unconditionally for those, so a re-run of the migration on an
    already-processed database is a no-op rather than an epoch inflation.
    Owned mappings are untouched -- those are still fully manageable.
    """
    if "users" not in set(sa.inspect(bind).get_table_names()):
        return  # pragma: no cover -- 0001_initial always runs first
    columns = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    if "session_epoch" not in columns or "active" not in columns:
        return  # pragma: no cover -- defensive

    # Boolean literals, not integers. `active = 0` is accepted by SQLite's
    # dynamic typing and rejected by PostgreSQL, where users.active is a
    # BOOLEAN column and an integer is not a valid assignment -- this migration
    # would fail on the deployment target and pass in every SQLite test. Bound
    # parameters keep the driver in control of the literal.
    result = bind.execute(sa.text(
        "UPDATE users SET active = :retired, "
        "session_epoch = COALESCE(session_epoch, 0) + 1 "
        "WHERE active = :still_live AND id IN ("
        "  SELECT user_id FROM scim_user_mappings WHERE identity_provider_id IS NULL"
        ")"), {"retired": False, "still_live": True})
    # Surfaced rather than swallowed: an operator needs to know how many
    # identities this migration revoked in order to plan the rebind.
    op.get_context().impl.static_output(
        f"0021: revoked access for {result.rowcount} quarantined identity(ies); "
        "their mappings remain unowned and must be rebound by an operator"
    )


def downgrade():
    bind = op.get_bind()
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("scim_user_mappings")}
    for index_name in ("uq_scim_user_mappings_provider_external_id",
                       "ix_scim_user_mappings_identity_provider_id"):
        if index_name in indexes:
            op.drop_index(index_name, table_name="scim_user_mappings")
    # Drop the column together with the FK that references it. On SQLite a bare
    # `ALTER TABLE ... DROP COLUMN` fails with "unknown column identity_provider_id
    # in foreign key definition" whenever a foreign key still names it, so the
    # batch path (which recreates the table from the reflected definition minus
    # the column) is required, not merely stylistic.
    #
    # The named drop_constraint is only issued where the name survived: batch
    # operations against a reflected SQLite table cannot resolve a constraint
    # name, and asking for one that is not there raises "No such constraint".
    # The column drop is what actually removes the FK on SQLite, because the
    # recreated table is built from the remaining columns.
    foreign_keys = {fk.get("name") for fk in
                    sa.inspect(bind).get_foreign_keys("scim_user_mappings")}
    with op.batch_alter_table("scim_user_mappings") as batch_op:
        if "fk_scim_user_mappings_identity_provider_id" in foreign_keys:
            batch_op.drop_constraint(
                "fk_scim_user_mappings_identity_provider_id", type_="foreignkey"
            )
        batch_op.drop_column("identity_provider_id")
