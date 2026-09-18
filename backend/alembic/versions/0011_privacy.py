"""Phase 13: privacy, deletion, and retention.

Adds anonymization/consent columns to users, and the retention_policies /
deletion_requests tables. Follows 0008_auth_hardening's guarded-idempotent style,
not Base.metadata.create_all -- see 0010_billing's docstring for why relying on
implicit metadata registration silently drops tables that nothing imports at
migration time.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_privacy"
down_revision = "0010_billing"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "anonymized_at" not in user_columns:
        op.add_column("users", sa.Column("anonymized_at", sa.DateTime(), nullable=True))
    if "tos_accepted_at" not in user_columns:
        op.add_column("users", sa.Column("tos_accepted_at", sa.DateTime(), nullable=True))
    if "tos_version" not in user_columns:
        op.add_column("users", sa.Column("tos_version", sa.String(20), nullable=True))

    existing = set(inspector.get_table_names())

    if "retention_policies" not in existing:
        op.create_table(
            "retention_policies",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
            sa.Column("chat_retention_days", sa.Integer(), nullable=True),
            sa.Column("audit_retention_days", sa.Integer(), nullable=True),
            sa.Column("activity_retention_days", sa.Integer(), nullable=True),
            sa.Column("document_retention_days", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if "deletion_requests" not in existing:
        op.create_table(
            "deletion_requests",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), nullable=False),
            sa.Column("requested_by", sa.String(), nullable=False),
            sa.Column("target_type", sa.String(20), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("reason", sa.String(500), nullable=True),
            sa.Column("requested_at", sa.DateTime(), nullable=False),
            sa.Column("scheduled_for", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("canceled_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_deletion_requests_organization_id", "deletion_requests", ["organization_id"])
        op.create_index("ix_deletion_requests_target_id", "deletion_requests", ["target_id"])
        op.create_index("ix_deletion_requests_status", "deletion_requests", ["status"])
        op.create_index("ix_deletion_requests_scheduled_for", "deletion_requests", ["scheduled_for"])


def downgrade():
    op.drop_table("deletion_requests")
    op.drop_table("retention_policies")
    op.drop_column("users", "tos_version")
    op.drop_column("users", "tos_accepted_at")
    op.drop_column("users", "anonymized_at")
