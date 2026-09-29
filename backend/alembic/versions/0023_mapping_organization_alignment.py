"""durable organization alignment for SCIM mappings

`scim_user_mappings` had two independent foreign keys -- one to `users.id` and
one to `identity_providers.id` -- and its own `organization_id`. Nothing tied
those three together, so this row is representable and, before this migration,
insertable:

    mapping.organization_id = 'org-A'
    mapping.user_id          -> a user in 'org-B'
    mapping.identity_provider_id -> a provider in 'org-C'

Every read path in app/sso_routes.py re-checked the organization by hand
(`if not u or u.organization_id != idp.organization_id: 404`). That is a
correctness burden replicated at every call site, and it is exactly the kind of
check the next write path forgets. A composite foreign key makes the database
hold the invariant instead.

Requires unique constraints on the parent column pairs, which is why this
migration adds those too: a composite FK to `(id, organization_id)` is only
valid when that pair is itself unique on the parent.

Inconsistent historical rows are quarantined, not refused. Setting
`identity_provider_id = NULL` makes the row invisible to every provider --
the established fail-closed state from 0021 -- and lets the constraint be added
without the upgrade aborting. The affected user is deactivated and its
`session_epoch` incremented, for the same reason 0021 does that to quarantined
identities: from this migration onward no provisioning path can reach the row
at all, so nothing else can revoke the access or the live sessions behind it.
"""
import sqlalchemy as sa
from alembic import op

revision = '0023_mapping_organization_alignment'
down_revision = '0022_domain_ownership'
branch_labels = None
depends_on = None

MAPPINGS = "scim_user_mappings"
USERS = "users"
PROVIDERS = "identity_providers"

# (name, table, columns) for the parent key a composite FK must reference.
#
# These are INDEXES, not table-level UniqueConstraints. SQLite only accepts a
# foreign key whose parent columns are backed by a PRIMARY KEY or a UNIQUE
# INDEX; a UNIQUE *constraint* declared in the table's CONSTRAINT clause is
# reported by the inspector as a unique constraint but is not honoured as a
# parent key, so the reference fails with "foreign key mismatch" -- and, worse,
# a same-organization row is rejected too. PostgreSQL accepts either, so the
# index form is the portable one.
PARENT_UNIQUES = [
    ("uq_users_id_organization", USERS, ["id", "organization_id"]),
    ("uq_identity_providers_id_organization", PROVIDERS, ["id", "organization_id"]),
]


def _has_unique_on(bind, table, columns) -> bool:
    """True when a UNIQUE index or constraint covers exactly these columns.

    Checked by shape, not name, for the same reason as everywhere else: a valid
    differently-named object is accepted rather than duplicated.
    """
    inspector = sa.inspect(bind)
    for index in inspector.get_indexes(table):
        if bool(index.get("unique")) and \
                tuple(index.get("column_names") or []) == tuple(columns):
            return True
    for uc in inspector.get_unique_constraints(table):
        if tuple(uc.get("column_names") or []) == tuple(columns):
            return True
    return False


def _mapping_fk_names(bind):
    """{constraint_name: (referred_table, referred_columns)} for the mapping
    table's foreign keys.

    Reported so the repair can find a correct composite constraint under ANY
    name and leave it alone. Matching on names alone is what makes a migration
    duplicate a constraint that is already right, and fails on one that is
    right under a different name.
    """
    out = {}
    for fk in sa.inspect(bind).get_foreign_keys(MAPPINGS):
        out[fk.get("name")] = (
            fk.get("referred_table"),
            tuple(fk.get("referred_columns") or []),
            tuple(fk.get("constrained_columns") or []),
        )
    return out


def _has_composite_fk(bind, constrained, referred_table, referred_columns) -> bool:
    for _name, (table, cols, constrained_cols) in _mapping_fk_names(bind).items():
        if table == referred_table and cols == tuple(referred_columns) \
                and constrained_cols == tuple(constrained):
            return True
    return False


def _quarantine_inconsistent(bind) -> None:
    """Fail-closed every cross-organization mapping, before the constraint.

    No winner is chosen and nothing is deleted: a row whose user belongs to
    another organization cannot be attributed to a correct one, and dropping
    rows would lose the audit trail of the inconsistency. Unowned (NULL) is the
    state 0021 already established, and every lookup requires an exact owner
    match, so such a row resolves for nobody.
    """
    bind.execute(sa.text(f"""
        UPDATE {MAPPINGS}
           SET identity_provider_id = NULL
         WHERE identity_provider_id IS NOT NULL
           AND (user_id, organization_id) NOT IN
               (SELECT id, organization_id FROM {USERS})
    """))
    bind.execute(sa.text(f"""
        UPDATE {MAPPINGS}
           SET identity_provider_id = NULL
         WHERE identity_provider_id IS NOT NULL
           AND (identity_provider_id, organization_id) NOT IN
               (SELECT id, organization_id FROM {PROVIDERS})
    """))
    # Also catch a mapping whose user simply vanished, which the two NOT IN
    # checks above cannot distinguish from a cross-org row.
    bind.execute(sa.text(f"""
        UPDATE {MAPPINGS}
           SET identity_provider_id = NULL
         WHERE identity_provider_id IS NOT NULL
           AND user_id NOT IN (SELECT id FROM {USERS})
    """))


def _retire_quarantined(bind) -> None:
    """Revoke access for every user behind a now-unowned mapping.

    Same reasoning as 0021's _retire_quarantined_identities: once the mapping
    is unowned, no provisioning path can ever reach it again, so
    if the User stayed active with its existing session_epoch, every cookie
    issued before this migration would remain valid forever with no way left to
    invalidate it. Deactivating and bumping the epoch is the only remaining
    revocation point.

    PostgreSQL portability
    ----------------------
    `users.active` is a real BOOLEAN. SQLite accepts an integer literal where a
    BOOLEAN is expected, but PostgreSQL does not, so every Boolean written here is
    a BOUND PARAMETER rather than a literal spliced into the SQL text. That is not
    a stylistic choice: an integer literal here is a migration that succeeds in
    development and aborts on the first real deployment.
    """
    # Booleans are BOUND PARAMETERS, never literals in the SQL text.
    # `users.active` is a real BOOLEAN: SQLite coerces an integer for it,
    # PostgreSQL refuses to, so a literal here is a migration that passes in
    # development and aborts on the first real deployment. The session_epoch
    # bump is likewise passed as a parameter rather than arithmetic written
    # into the statement, keeping every value dialect-neutral.
    bind.execute(
        sa.text(f"""
            UPDATE {USERS}
               SET active = :revoked,
                   session_epoch = COALESCE(session_epoch, 0) + :epoch_bump
             WHERE id IN (SELECT user_id FROM {MAPPINGS}
                           WHERE identity_provider_id IS NULL)
               AND active = :was_active
        """),
        {'revoked': False, 'epoch_bump': 1, 'was_active': True},
    )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if MAPPINGS not in set(inspector.get_table_names()):
        return  # pragma: no cover -- 0012_sso_enterprise always runs first

    _quarantine_inconsistent(bind)
    _retire_quarantined(bind)

    # Parent uniqueness first: a composite FK is invalid without it. SQLite
    # reports "foreign key mismatch" rather than a clear error when the parent
    # key is not actually unique, so this ordering is load-bearing.
    for name, table, columns in PARENT_UNIQUES:
        if not _has_unique_on(bind, table, columns):
            op.create_index(name, table, columns, unique=True)
    # The batch recreations above leave the recreated tables' data in place but
    # SQLite may not have re-applied the constraint to a table whose rows were
    # copied; verify rather than assume, because "migration appeared to run" is
    # exactly the failure mode this file is about.
    for name, table, columns in PARENT_UNIQUES:
        if not _has_unique_on(bind, table, columns):
            raise RuntimeError(
                f"{name} was not created on {table}; a composite foreign key "
                "cannot reference a non-unique parent key")

    # Then the composite constraints themselves. (local columns, referred
    # columns) -- these differ, and passing the local list for both produces an
    # FK pointing at users(user_id, organization_id), which does not exist, and
    # which SQLite reports only as a bare "foreign key mismatch".
    wanted = [
        ("fk_scim_mappings_user_org", USERS, ["id", "organization_id"]),
        ("fk_scim_mappings_provider_org", PROVIDERS, ["id", "organization_id"]),
    ]
    for name, referred_table, referred_columns in wanted:
        local_columns = _local_for(referred_table, referred_columns)
        # The CHILD column list and the PARENT referent list are different
        # names. v5 passed `referred_columns` into the `constrained` parameter,
        # so this compared ['id','organization_id'] against the real
        # ['user_id','organization_id'], never matched, and created a SECOND
        # copy of a constraint that was already correct.
        if _has_composite_fk(bind, local_columns, referred_table,
                             referred_columns):
            continue
        with op.batch_alter_table(MAPPINGS) as batch:
            batch.create_foreign_key(
                name, referred_table,
                # local (constrained) columns
                local_columns,
                # referred (parent) columns
                referred_columns,
            )


def _local_for(referred_table, referred_columns):
    """The local column names for a composite reference to `referred_table`.

    'users' and 'identity_providers' name the same pair differently --
    (user_id, organization_id) versus (identity_provider_id, organization_id).
    Keeping the mapping explicit means the FK cannot be built with the parent's
    column names on the child side, which is what produced a silently invalid
    constraint before.
    """
    if referred_table == USERS:
        return ["user_id", "organization_id"]
    if referred_table == PROVIDERS:
        return ["identity_provider_id", "organization_id"]
    raise AssertionError(f"unhandled referred table: {referred_table}")


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if MAPPINGS not in set(inspector.get_table_names()):
        return  # pragma: no cover

    wanted = [
        ("fk_scim_mappings_user_org", USERS, ["id", "organization_id"]),
        ("fk_scim_mappings_provider_org", PROVIDERS, ["id", "organization_id"]),
    ]
    existing = _mapping_fk_names(bind)
    for name, referred_table, referred_columns in wanted:
        for constraint_name, (table, cols, _constrained) in existing.items():
            if table == referred_table and cols == tuple(referred_columns):
                with op.batch_alter_table(MAPPINGS) as batch:
                    batch.drop_constraint(constraint_name, type_="foreignkey")
                break

    for name, table, _columns in PARENT_UNIQUES:
        # Dropped only when this migration is the thing that created it; a
        # same-named object that predates us is left in place.
        try:
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(name, type_="unique")
        except Exception:
            continue
