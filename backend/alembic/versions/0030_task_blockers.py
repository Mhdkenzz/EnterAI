"""Persist a human-visible reason when an agent pauses blocked work."""
from alembic import op
import sqlalchemy as sa


revision = "0030_task_blockers"
down_revision = "0029_task_dependencies"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("tasks")}
    if "blocked_reason" not in columns:
        op.add_column("tasks", sa.Column("blocked_reason", sa.Text(), nullable=True))


def downgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("tasks")}
    if "blocked_reason" in columns:
        op.drop_column("tasks", "blocked_reason")
