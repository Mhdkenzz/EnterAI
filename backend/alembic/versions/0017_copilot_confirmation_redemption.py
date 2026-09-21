"""Persist server-issued Copilot confirmation lifecycle across API replicas."""
from alembic import op
import sqlalchemy as sa

revision = "0017_copilot_confirmation_redeem"
down_revision = "0016_document_chunk_provenance"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 creates every model currently imported by app.models for SQLite
    # tests, while PostgreSQL upgrades may reach this migration without it.
    # Keep both paths idempotent.
    bind = op.get_bind()
    if "copilot_confirmations" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "copilot_confirmations",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=False),
            sa.Column("tool", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_copilot_confirmations_token_hash", "copilot_confirmations", ["token_hash"], unique=True)
        op.create_index("ix_copilot_confirmations_subject_id", "copilot_confirmations", ["subject_id"], unique=False)
        op.create_index("ix_copilot_confirmations_organization_id", "copilot_confirmations", ["organization_id"], unique=False)
        op.create_index("ix_copilot_confirmations_expires_at", "copilot_confirmations", ["expires_at"], unique=False)
        op.create_index("ix_copilot_confirmations_consumed_at", "copilot_confirmations", ["consumed_at"], unique=False)


def downgrade():
    if "copilot_confirmations" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("copilot_confirmations")
