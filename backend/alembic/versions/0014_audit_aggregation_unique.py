"""Unique constraint on enterprise_audit_aggregations(organization_id, action,
date_bucket) -- 0012 created this table with no way to detect a concurrent
duplicate. observability._bump_audit_aggregation's select-then-insert-or-update
is not atomic on its own: two concurrent audit() calls for the same
org/action/day can each see "no existing row" and both insert, producing two
rows (count=1 each) instead of one row (count=2). The constraint turns that
race into a real, catchable IntegrityError instead of silent undercounting.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_audit_aggregation_unique"
down_revision = "0013_oidc_login"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "uq_enterprise_audit_aggregations_org_action_bucket"


def upgrade():
    bind = op.get_bind()
    existing_constraints = {c["name"] for c in sa.inspect(bind).get_unique_constraints("enterprise_audit_aggregations")}
    if CONSTRAINT_NAME not in existing_constraints:
        # A concurrent-insert race before this migration could already have
        # produced duplicate (organization_id, action, date_bucket) rows;
        # collapse those into one (summed count) before the constraint can be
        # added, or the ALTER itself fails.
        op.execute(sa.text("""
            DELETE FROM enterprise_audit_aggregations
            WHERE id NOT IN (
                SELECT MIN(id) FROM enterprise_audit_aggregations
                GROUP BY organization_id, action, date_bucket
            )
        """))
        with op.batch_alter_table("enterprise_audit_aggregations") as batch_op:
            batch_op.create_unique_constraint(CONSTRAINT_NAME, ["organization_id", "action", "date_bucket"])


def downgrade():
    with op.batch_alter_table("enterprise_audit_aggregations") as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
