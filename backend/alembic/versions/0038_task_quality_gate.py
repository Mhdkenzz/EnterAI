"""Add task acceptance, QA evidence, checks, and peer review state."""

from alembic import op
import sqlalchemy as sa


revision = "0038_task_quality_gate"
down_revision = "0037_agent_access_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The historical SQLite bootstrap creates tables from current model metadata.
    # Add only missing columns so both that path and older installed databases work.
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tasks")}
    columns = [
        sa.Column("qa_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
        *(sa.Column(name, sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
          for name in ("acceptance_criteria", "review_rubric", "qa_evidence", "qa_checks")),
        sa.Column("qa_checked_at", sa.DateTime(), nullable=True),
        sa.Column("peer_review_status", sa.String(length=30), nullable=False, server_default="not_requested"),
        sa.Column("peer_review_agent_id", sa.String(), sa.ForeignKey("users.id", name="fk_tasks_peer_review_agent_id_users"), nullable=True),
        sa.Column("peer_review_task_id", sa.String(), sa.ForeignKey("tasks.id", name="fk_tasks_peer_review_task_id_tasks"), nullable=True),
        sa.Column("peer_review_summary", sa.Text(), nullable=True),
        sa.Column("peer_review_decision", sa.Text(), nullable=True),
    ]
    missing = [column for column in columns if column.name not in existing]
    if op.get_bind().dialect.name == "sqlite" and missing:
        # SQLite cannot ALTER TABLE to add foreign-key constraints. Batch mode
        # rebuilds the table while preserving existing columns and relationships.
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for column in missing:
                batch.add_column(column)
    else:
        for column in missing:
            op.add_column("tasks", column)


def downgrade() -> None:
    names = ("peer_review_decision", "peer_review_summary", "peer_review_task_id", "peer_review_agent_id",
             "peer_review_status", "qa_checked_at", "qa_checks", "qa_evidence", "review_rubric",
             "acceptance_criteria", "requires_human_approval", "qa_required")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("tasks", recreate="always") as batch:
            for name in names:
                batch.drop_column(name)
        return
    op.drop_constraint("fk_tasks_peer_review_task_id_tasks", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_peer_review_agent_id_users", "tasks", type_="foreignkey")
    for name in names:
        op.drop_column("tasks", name)
