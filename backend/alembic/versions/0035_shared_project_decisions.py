"""Add durable project decisions linked to discussion and work."""

from alembic import op
import sqlalchemy as sa

revision = "0035_shared_project_decisions"
down_revision = "0034_durable_agent_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The historical 0001 migration builds its table set from current ORM
    # metadata on fresh databases, so a new model may already exist by the time
    # this revision runs. Upgrades from established databases still need the
    # explicit table creation below.
    if sa.inspect(op.get_bind()).has_table("decisions"):
        return
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("source_comment_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_comment_id"], ["comments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_comment_id", name="uq_decisions_source_comment_id"),
    )
    op.create_index("ix_decisions_organization_id", "decisions", ["organization_id"])
    op.create_index("ix_decisions_project_id", "decisions", ["project_id"])
    op.create_index("ix_decisions_task_id", "decisions", ["task_id"])
    op.create_index("ix_decisions_created_at", "decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_decisions_created_at", table_name="decisions")
    op.drop_index("ix_decisions_task_id", table_name="decisions")
    op.drop_index("ix_decisions_project_id", table_name="decisions")
    op.drop_index("ix_decisions_organization_id", table_name="decisions")
    op.drop_table("decisions")
