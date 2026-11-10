"""Add selectable hosted AI provider while preserving saved OpenRouter keys."""
from alembic import op
import sqlalchemy as sa


revision = "0031_ai_provider_selection"
down_revision = "0030_task_blockers"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "organization_ai_settings" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("organization_ai_settings")}
    if "provider" not in columns:
        # Existing encrypted credentials belong to OpenRouter. Preserve their
        # meaning so they are never accidentally sent to IFM after an upgrade.
        op.add_column("organization_ai_settings", sa.Column(
            "provider", sa.String(length=30), nullable=False, server_default="openrouter",
        ))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "organization_ai_settings" in set(inspector.get_table_names()):
        columns = {column["name"] for column in inspector.get_columns("organization_ai_settings")}
        if "provider" in columns:
            op.drop_column("organization_ai_settings", "provider")
