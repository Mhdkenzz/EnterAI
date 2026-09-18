"""Billing tables (plans, subscriptions, usage, webhook events).

billing_models.py defines these on the shared Base, but nothing imports that
module during a migration run, so Base.metadata never picked them up and
`alembic upgrade head` never created them on any real database -- Phase 12
billing has had no schema in any migrated environment, including CI and
production. This migration creates them explicitly, following the same
idempotent-guard style as every other post-0001 migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_billing"
down_revision = "0009_rag_chunks"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "billing_plans" not in existing:
        op.create_table(
            "billing_plans",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("stripe_plan_id", sa.String(255), nullable=False, unique=True),
            sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
            sa.Column("seat_limit", sa.Integer(), nullable=True),
            sa.Column("ai_usage_limit", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )

    if "organization_subscriptions" not in existing:
        op.create_table(
            "organization_subscriptions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("stripe_subscription_id", sa.String(255), nullable=True, unique=True),
            sa.Column("stripe_customer_id", sa.String(255), nullable=True),
            sa.Column("plan_id", sa.String(), sa.ForeignKey("billing_plans.id"), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="trial"),
            sa.Column("current_period_start", sa.DateTime(), nullable=True),
            sa.Column("current_period_end", sa.DateTime(), nullable=True),
            sa.Column("trial_ends_at", sa.DateTime(), nullable=True),
            sa.Column("canceled_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_organization_subscriptions_organization_id", "organization_subscriptions", ["organization_id"])

    if "usage_records" not in existing:
        op.create_table(
            "usage_records",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("metric", sa.String(50), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_usage_records_organization_id", "usage_records", ["organization_id"])

    if "billing_webhook_events" not in existing:
        op.create_table(
            "billing_webhook_events",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("stripe_event_id", sa.String(255), nullable=False, unique=True),
            sa.Column("event_type", sa.String(50), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    op.drop_table("billing_webhook_events")
    op.drop_index("ix_usage_records_organization_id", table_name="usage_records")
    op.drop_table("usage_records")
    op.drop_index("ix_organization_subscriptions_organization_id", table_name="organization_subscriptions")
    op.drop_table("organization_subscriptions")
    op.drop_table("billing_plans")
