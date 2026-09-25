"""Give open-ended trials an end date.

Trials used to be unlimited and never expire. Existing trial organizations get a
full trial from the day this ships (TRIAL_DAYS, default 14) rather than being cut
off by a rule they never saw.
"""
import os
from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision = "0020_trial_end_dates"
down_revision = "0019_org_names_not_unique"
branch_labels = None
depends_on = None


def upgrade():
    ends = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=int(os.getenv("TRIAL_DAYS", "14")))
    op.get_bind().execute(
        sa.text("UPDATE organization_subscriptions SET trial_ends_at = :ends "
                "WHERE status = 'trial' AND trial_ends_at IS NULL"),
        {"ends": ends},
    )


def downgrade():
    pass  # an end date is harmless to keep; there is no way to tell backfilled rows apart
