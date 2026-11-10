"""Persist editable, approval-gated plans from incoming requests."""
from alembic import op
import sqlalchemy as sa


revision = "0024_inbound_plans"
down_revision = "0023_inbound_messages"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "inbound_plans" not in tables:
        op.create_table(
            "inbound_plans",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=False),
            sa.Column("inbound_message_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("proposal", sa.JSON(), nullable=False),
            sa.Column("planner_provider", sa.String(length=40), nullable=False),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("reviewed_by", sa.String(), nullable=True),
            sa.Column("project_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["inbound_message_id"], ["inbound_messages.id"]),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
            sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("inbound_plans")}
    for name, column in (("ix_inbound_plans_organization_id", "organization_id"),
                         ("ix_inbound_plans_inbound_message_id", "inbound_message_id"),
                         ("ix_inbound_plans_status", "status")):
        if name not in indexes:
            op.create_index(name, "inbound_plans", [column])


def downgrade():
    op.drop_index("ix_inbound_plans_status", table_name="inbound_plans")
    op.drop_index("ix_inbound_plans_inbound_message_id", table_name="inbound_plans")
    op.drop_index("ix_inbound_plans_organization_id", table_name="inbound_plans")
    op.drop_table("inbound_plans")
