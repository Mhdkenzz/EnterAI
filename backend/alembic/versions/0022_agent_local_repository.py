"""Enable organization-scoped local repository tools for agents."""
from alembic import op
import sqlalchemy as sa

revision = "0022_agent_local_repository"
down_revision = "0021_ceo_full_access"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("organizations")}
    if "agent_workspace_enabled" not in columns:
        op.add_column(
            "organizations",
            sa.Column("agent_workspace_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("organizations")}
    if "agent_workspace_enabled" in columns:
        op.drop_column("organizations", "agent_workspace_enabled")
