"""Opt-in direct workspace UI actions for the CEO agent."""
from alembic import op
import sqlalchemy as sa

revision = "0021_ceo_full_access"
down_revision = "0020_trial_end_dates"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations",
        sa.Column("ceo_full_access_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("organizations", "ceo_full_access_enabled")
