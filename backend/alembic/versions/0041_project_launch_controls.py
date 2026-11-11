"""Persist launch date and freeze rule on projects."""

from alembic import op
import sqlalchemy as sa


revision = "0041_project_launch_controls"
down_revision = "0040_transparent_agent_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
    if "launch_date" not in columns:
        op.add_column("projects", sa.Column("launch_date", sa.Date(), nullable=True))
    if "launch_freeze_rule" not in columns:
        op.add_column("projects", sa.Column("launch_freeze_rule", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
    if "launch_freeze_rule" in columns:
        op.drop_column("projects", "launch_freeze_rule")
    if "launch_date" in columns:
        op.drop_column("projects", "launch_date")
