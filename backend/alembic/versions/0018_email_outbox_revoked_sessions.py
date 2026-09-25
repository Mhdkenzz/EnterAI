"""Retry queue for undelivered email; revocation list for signed-out sessions."""
from alembic import op
import sqlalchemy as sa

revision = "0018_email_outbox_revocation"
down_revision = "0017_copilot_confirmation_redeem"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 creates every model imported by app.models on a fresh SQLite database,
    # so both tables may already exist; keep the migration idempotent like 0017.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "email_outbox" not in tables:
        op.create_table(
            "email_outbox",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("recipient", sa.String(length=320), nullable=False),
            sa.Column("subject", sa.String(length=300), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("last_error", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_email_outbox_status", "email_outbox", ["status"], unique=False)
        op.create_index("ix_email_outbox_next_attempt_at", "email_outbox", ["next_attempt_at"], unique=False)
    if "revoked_sessions" not in tables:
        op.create_table(
            "revoked_sessions",
            sa.Column("jti", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("jti"),
        )
        op.create_index("ix_revoked_sessions_expires_at", "revoked_sessions", ["expires_at"], unique=False)


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "revoked_sessions" in tables:
        op.drop_table("revoked_sessions")
    if "email_outbox" in tables:
        op.drop_table("email_outbox")
