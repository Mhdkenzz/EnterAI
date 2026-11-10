"""Track when an agent reaches its shared daily execution budget."""
from alembic import op
import sqlalchemy as sa

revision = "0031_agent_daily_limit"
down_revision = "0030_task_blockers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "daily_limit_reached_at" not in columns:
        op.add_column("users", sa.Column("daily_limit_reached_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "daily_limit_reached_at" in columns:
        op.drop_column("users", "daily_limit_reached_at")
