"""track task completion timestamps"""
from alembic import op
from sqlalchemy import Column, DateTime, inspect

revision = "0003_task_completion"
down_revision = "0002_project_documents"
branch_labels = None
depends_on = None


def upgrade():
    if any(column["name"] == "completed_at" for column in inspect(op.get_bind()).get_columns("tasks")):
        return
    op.add_column("tasks", Column("completed_at", DateTime(), nullable=True))


def downgrade():
    op.drop_column("tasks", "completed_at")
