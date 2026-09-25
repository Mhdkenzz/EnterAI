"""Organization names are display labels, not identifiers: drop their uniqueness.

A unique name let anyone discover which companies are customers by trying to sign
up with theirs, and blocked two unrelated organizations that share a name. The
slug stays unique.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_org_names_not_unique"
down_revision = "0018_email_outbox_revocation"
branch_labels = None
depends_on = None


def _name_constraints(bind):
    inspector = sa.inspect(bind)
    constraints = [c["name"] for c in inspector.get_unique_constraints("organizations")
                   if c["column_names"] == ["name"] and c["name"]]
    # PostgreSQL also reports each unique constraint as an index of the same name,
    # which goes away with the constraint; only standalone unique indexes remain.
    indexes = [i["name"] for i in inspector.get_indexes("organizations")
               if i["column_names"] == ["name"] and i.get("unique") and i["name"] not in constraints]
    return constraints, indexes


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite cannot drop an inline UNIQUE without rebuilding the table. SQLite is
        # only used for development and tests, whose fresh databases are created from
        # the models (which no longer declare it); production runs PostgreSQL.
        return
    constraints, indexes = _name_constraints(bind)
    for name in constraints:
        op.drop_constraint(name, "organizations", type_="unique")
    for name in indexes:
        op.drop_index(name, table_name="organizations")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not any(_name_constraints(bind)):
        op.create_unique_constraint("organizations_name_key", "organizations", ["name"])
