"""Auth hardening: verification, password reset, invites, onboarding."""
from alembic import op
import sqlalchemy as sa

revision = "0008_auth_hardening"
down_revision = "0007_agent_retirement"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "email_verified_at" not in user_columns:
        op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    if "session_epoch" not in user_columns:
        op.add_column("users", sa.Column("session_epoch", sa.Integer(), nullable=False, server_default="0"))
    if "onboarded_at" not in {column["name"] for column in inspector.get_columns("organizations")}:
        op.add_column("organizations", sa.Column("onboarded_at", sa.DateTime(), nullable=True))

    existing = set(inspector.get_table_names())
    if "auth_tokens" not in existing:
        op.create_table(
            "auth_tokens",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("purpose", sa.String(30), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])
        op.create_index("ix_auth_tokens_purpose", "auth_tokens", ["purpose"])
        op.create_index("ix_auth_tokens_token_hash", "auth_tokens", ["token_hash"], unique=True)
    if "invites" not in existing:
        op.create_table(
            "invites",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("role", sa.String(30), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("invited_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("accepted_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_invites_organization_id", "invites", ["organization_id"])
        op.create_index("ix_invites_email", "invites", ["email"])
        op.create_index("ix_invites_token_hash", "invites", ["token_hash"], unique=True)


def downgrade():
    op.drop_table("invites")
    op.drop_table("auth_tokens")
    # SQLite rebuilds the table to drop a column; other dialects use ALTER.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("session_epoch")
        batch.drop_column("email_verified_at")
    with op.batch_alter_table("organizations") as batch:
        batch.drop_column("onboarded_at")
