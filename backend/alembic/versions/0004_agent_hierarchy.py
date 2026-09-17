"""add agent identity fields and hierarchy tables"""
from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, inspect

revision = "0004_agent_hierarchy"
down_revision = "0003_task_completion"
branch_labels = None
depends_on = None


def upgrade():
    existing_user_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("users")}
    if "kind" not in existing_user_columns:
        op.add_column("users", Column("kind", String(10), nullable=False, server_default="human"))
    if "hierarchy_level" not in existing_user_columns:
        op.add_column("users", Column("hierarchy_level", String(20), nullable=True))
    if "parent_agent_id" not in existing_user_columns:
        op.add_column("users", Column("parent_agent_id", String(), ForeignKey("users.id"), nullable=True))
    if "current_task_id" not in existing_user_columns:
        op.add_column("users", Column("current_task_id", String(), ForeignKey("tasks.id"), nullable=True))
    if "last_completed_task_id" not in existing_user_columns:
        op.add_column("users", Column("last_completed_task_id", String(), ForeignKey("tasks.id"), nullable=True))

    tables = inspect(op.get_bind()).get_table_names()
    if "agent_messages" not in tables:
        op.create_table(
            "agent_messages",
            Column("id", String(), primary_key=True),
            Column("agent_id", String(), ForeignKey("users.id"), nullable=False),
            Column("author_id", String(), ForeignKey("users.id"), nullable=True),
            Column("role", String(10), nullable=False),
            Column("body", Text(), nullable=False),
            Column("created_at", DateTime(), nullable=False),
        )
        op.create_index("ix_agent_messages_agent_id", "agent_messages", ["agent_id"])

    if "hierarchy_configs" not in tables:
        op.create_table(
            "hierarchy_configs",
            Column("id", String(), primary_key=True),
            Column("organization_id", String(), ForeignKey("organizations.id"), nullable=False, unique=True),
            Column("vp_count", Integer(), nullable=False, server_default="0"),
            Column("directors_per_vp", Integer(), nullable=False, server_default="0"),
            Column("managers_per_director", Integer(), nullable=False, server_default="0"),
            Column("workers_per_manager", Integer(), nullable=False, server_default="0"),
            Column("updated_at", DateTime(), nullable=False),
        )
        op.create_index("ix_hierarchy_configs_organization_id", "hierarchy_configs", ["organization_id"])


def downgrade():
    op.drop_index("ix_hierarchy_configs_organization_id", table_name="hierarchy_configs")
    op.drop_table("hierarchy_configs")
    op.drop_index("ix_agent_messages_agent_id", table_name="agent_messages")
    op.drop_table("agent_messages")
    # SQLite must rebuild the table when removing columns referenced by FKs.
    # Other dialects retain native ALTER TABLE behavior inside this context.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_completed_task_id")
        batch.drop_column("current_task_id")
        batch.drop_column("parent_agent_id")
        batch.drop_column("hierarchy_level")
        batch.drop_column("kind")
