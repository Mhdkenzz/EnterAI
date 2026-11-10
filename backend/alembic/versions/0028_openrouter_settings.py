"""Add encrypted, organization-scoped OpenRouter settings."""
from alembic import op
import sqlalchemy as sa


revision = "0028_openrouter_settings"
down_revision = "0027_inbound_triage_context"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "organization_ai_settings" not in set(inspector.get_table_names()):
        op.create_table(
            "organization_ai_settings",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=False),
            sa.Column("api_key_encrypted", sa.Text(), nullable=True),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("requests_per_minute", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("organization_ai_settings")}
    if "ix_organization_ai_settings_organization_id" not in indexes:
        op.create_index("ix_organization_ai_settings_organization_id", "organization_ai_settings", ["organization_id"], unique=True)


def downgrade():
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("organization_ai_settings")}
    if "ix_organization_ai_settings_organization_id" in indexes:
        op.drop_index("ix_organization_ai_settings_organization_id", table_name="organization_ai_settings")
    if "organization_ai_settings" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("organization_ai_settings")
