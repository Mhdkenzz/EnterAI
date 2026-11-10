"""Add explicit task prerequisites for ordered work."""
from alembic import op
import sqlalchemy as sa

revision = "0029_task_dependencies"
down_revision = "0028_openrouter_settings"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "task_dependencies" not in set(inspector.get_table_names()):
        op.create_table(
            "task_dependencies",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("depends_on_task_id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency_pair"),
        )
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("task_dependencies")}
    if "ix_task_dependencies_task_id" not in indexes:
        op.create_index("ix_task_dependencies_task_id", "task_dependencies", ["task_id"])
    if "ix_task_dependencies_depends_on_task_id" not in indexes:
        op.create_index("ix_task_dependencies_depends_on_task_id", "task_dependencies", ["depends_on_task_id"])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "task_dependencies" not in set(inspector.get_table_names()):
        return
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("task_dependencies")}
    if "ix_task_dependencies_task_id" in indexes:
        op.drop_index("ix_task_dependencies_task_id", table_name="task_dependencies")
    if "ix_task_dependencies_depends_on_task_id" in indexes:
        op.drop_index("ix_task_dependencies_depends_on_task_id", table_name="task_dependencies")
    op.drop_table("task_dependencies")
