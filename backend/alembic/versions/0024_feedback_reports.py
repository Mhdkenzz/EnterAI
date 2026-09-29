"""organization-scoped demo feedback reports

The report body and diagnostics are intentionally separate. Diagnostics are
safe, allow-listed context assembled by the API; request headers, cookies,
authorization values, environment variables, and uploaded content are never
persisted here.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_feedback_reports"
down_revision = "0023_mapping_org_alignment"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 imports the shared model metadata on fresh databases, so this table
    # already exists on both SQLite and PostgreSQL in that path. Older databases
    # still need the explicit create below.
    if "feedback_reports" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "feedback_reports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("reporter_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("impact", sa.String(length=20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("page", sa.String(length=160), nullable=True),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("release_identifier", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("assignee_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_feedback_reports_organization_id", "feedback_reports", ["organization_id"])
    op.create_index("ix_feedback_reports_reporter_id", "feedback_reports", ["reporter_id"])
    op.create_index("ix_feedback_reports_category", "feedback_reports", ["category"])
    op.create_index("ix_feedback_reports_impact", "feedback_reports", ["impact"])
    op.create_index("ix_feedback_reports_release_identifier", "feedback_reports", ["release_identifier"])
    op.create_index("ix_feedback_reports_status", "feedback_reports", ["status"])
    op.create_index("ix_feedback_reports_assignee_id", "feedback_reports", ["assignee_id"])
    op.create_index("ix_feedback_reports_created_at", "feedback_reports", ["created_at"])


def downgrade():
    op.drop_table("feedback_reports")
