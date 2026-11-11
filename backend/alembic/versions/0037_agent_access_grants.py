"""Add explicit revocable agent access grants."""

from alembic import op
import sqlalchemy as sa


revision = "0037_agent_access_grants"
down_revision = "0036_task_owner_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_access_grants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("permission", sa.String(20), nullable=False),
        sa.Column("granted_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("organization_id", "agent_id", "resource_type", "resource_id", "permission", name="uq_agent_access_grant"),
    )
    op.create_index("ix_agent_access_grants_organization_id", "agent_access_grants", ["organization_id"])
    op.create_index("ix_agent_access_grants_agent_id", "agent_access_grants", ["agent_id"])
    op.create_index("ix_agent_access_grants_org_agent", "agent_access_grants", ["organization_id", "agent_id"])
    op.create_index("ix_agent_access_grants_revoked_at", "agent_access_grants", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_access_grants_revoked_at", table_name="agent_access_grants")
    op.drop_index("ix_agent_access_grants_org_agent", table_name="agent_access_grants")
    op.drop_index("ix_agent_access_grants_agent_id", table_name="agent_access_grants")
    op.drop_index("ix_agent_access_grants_organization_id", table_name="agent_access_grants")
    op.drop_table("agent_access_grants")
