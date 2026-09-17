"""add agent autonomous-execution tracking columns"""
from alembic import op
from sqlalchemy import Column, DateTime, Integer, inspect

revision = "0005_agent_execution"
down_revision = "0004_agent_hierarchy"
branch_labels = None
depends_on = None


def upgrade():
    existing_user_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("users")}
    if "consecutive_task_failures" not in existing_user_columns:
        op.add_column("users", Column("consecutive_task_failures", Integer, nullable=False, server_default="0"))
    if "last_execution_at" not in existing_user_columns:
        op.add_column("users", Column("last_execution_at", DateTime, nullable=True))


def downgrade():
    op.drop_column("users", "last_execution_at")
    op.drop_column("users", "consecutive_task_failures")
