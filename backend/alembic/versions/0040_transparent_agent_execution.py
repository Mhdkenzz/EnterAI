"""Add visible agent job controls and redirection history pointers."""

from alembic import op
import sqlalchemy as sa


revision = "0040_transparent_agent_execution"
down_revision = "0039_project_north_star_coordination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tasks")}
    additions = (
        sa.Column("agent_job_state", sa.String(20), nullable=True),
        sa.Column("agent_job_reason", sa.Text(), nullable=True),
        sa.Column("agent_job_changed_at", sa.DateTime(), nullable=True),
        sa.Column("agent_job_changed_by", sa.String(), nullable=True),
        sa.Column("agent_directive", sa.Text(), nullable=True),
        sa.Column("agent_directed_at", sa.DateTime(), nullable=True),
        sa.Column("agent_directed_by", sa.String(), nullable=True),
    )
    missing = [column for column in additions if column.name not in columns]
    if missing and op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for column in missing:
                batch.add_column(column)
    else:
        for column in missing:
            op.add_column("tasks", column)
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("tasks")}
    if "ix_tasks_agent_job_state" not in indexes:
        op.create_index("ix_tasks_agent_job_state", "tasks", ["agent_job_state"])


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("tasks")}
    if "ix_tasks_agent_job_state" in indexes:
        op.drop_index("ix_tasks_agent_job_state", table_name="tasks")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tasks")}
    remove = [name for name in (
        "agent_directed_by", "agent_directed_at", "agent_directive", "agent_job_changed_by",
        "agent_job_changed_at", "agent_job_reason", "agent_job_state",
    ) if name in columns]
    if remove and op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for name in remove:
                batch.drop_column(name)
    else:
        for name in remove:
            op.drop_column("tasks", name)
