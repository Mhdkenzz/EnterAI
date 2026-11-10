"""Persist connected Gmail accounts, encrypted credentials and OAuth state."""
from alembic import op
import sqlalchemy as sa


revision = "0025_gmail_integrations"
down_revision = "0024_inbound_plans"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "integration_connections" not in tables:
        op.create_table(
        "integration_connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("external_account_id", sa.String(length=500), nullable=False),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("access_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("refresh_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("history_cursor", sa.String(length=100), nullable=True),
        sa.Column("connected_by", sa.String(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connected_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "provider", "external_account_id", name="uq_integration_org_provider_account"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("integration_connections")}
    if "ix_integration_connections_organization_id" not in indexes:
        op.create_index("ix_integration_connections_organization_id", "integration_connections", ["organization_id"])
    if "ix_integration_connections_status" not in indexes:
        op.create_index("ix_integration_connections_status", "integration_connections", ["status"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "integration_oauth_states" not in tables:
        op.create_table(
        "integration_oauth_states",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("integration_oauth_states")}
    if "ix_integration_oauth_states_organization_id" not in indexes:
        op.create_index("ix_integration_oauth_states_organization_id", "integration_oauth_states", ["organization_id"])
    if "ix_integration_oauth_states_expires_at" not in indexes:
        op.create_index("ix_integration_oauth_states_expires_at", "integration_oauth_states", ["expires_at"])


def downgrade():
    op.drop_index("ix_integration_oauth_states_expires_at", table_name="integration_oauth_states")
    op.drop_index("ix_integration_oauth_states_organization_id", table_name="integration_oauth_states")
    op.drop_table("integration_oauth_states")
    op.drop_index("ix_integration_connections_status", table_name="integration_connections")
    op.drop_index("ix_integration_connections_organization_id", table_name="integration_connections")
    op.drop_table("integration_connections")
