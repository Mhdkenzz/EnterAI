"""SSO/SCIM (Phase 14) and enterprise controls (Phase 15) tables.

sso_models.py and enterprise_models.py define these on the shared Base, but
nothing imports either module during a migration run, so Base.metadata never
picked them up and `alembic upgrade head` never created them on any real
database -- the same gap 0010_billing's docstring documents for billing_models.
Every route in sso_routes.py/enterprise_routes.py that touches these tables
500s with "no such table" until this migration runs. Follows the same
idempotent-guard style as every migration since 0008.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_sso_enterprise"
down_revision = "0011_privacy"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "identity_providers" not in existing:
        op.create_table(
            "identity_providers",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("protocol", sa.String(20), nullable=False, server_default="oidc"),
            sa.Column("sso_url", sa.String(500), nullable=True),
            sa.Column("acs_url", sa.String(500), nullable=True),
            sa.Column("entity_id", sa.String(255), nullable=True),
            sa.Column("certificate_pem", sa.Text(), nullable=True),
            sa.Column("client_id", sa.String(255), nullable=True),
            sa.Column("client_secret_hash", sa.String(255), nullable=True),
            sa.Column("scim_token_hash", sa.String(255), nullable=True),
            sa.Column("domain_binding", sa.String(255), nullable=True),
            sa.Column("sso_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("role_mapping_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_identity_providers_organization_id", "identity_providers", ["organization_id"])
        op.create_index("ix_identity_providers_domain_binding", "identity_providers", ["domain_binding"])
        op.create_index("ix_identity_providers_scim_token_hash", "identity_providers", ["scim_token_hash"])

    if "scim_user_mappings" not in existing:
        op.create_table(
            "scim_user_mappings",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("scim_external_id", sa.String(255), nullable=False),
            sa.Column("scim_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_synced_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_scim_user_mappings_organization_id", "scim_user_mappings", ["organization_id"])
        op.create_index("ix_scim_user_mappings_user_id", "scim_user_mappings", ["user_id"])
        op.create_index("ix_scim_user_mappings_scim_external_id", "scim_user_mappings", ["scim_external_id"])

    if "ip_allowlists" not in existing:
        op.create_table(
            "ip_allowlists",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("cidr", sa.String(60), nullable=False),
            sa.Column("description", sa.String(255), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_ip_allowlists_organization_id", "ip_allowlists", ["organization_id"])

    if "session_policies" not in existing:
        op.create_table(
            "session_policies",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
            sa.Column("max_session_days", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("sso_only_enforced", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_session_policies_organization_id", "session_policies", ["organization_id"], unique=True)

    if "enterprise_audit_aggregations" not in existing:
        op.create_table(
            "enterprise_audit_aggregations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("action", sa.String(100), nullable=False),
            sa.Column("date_bucket", sa.String(20), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_updated", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_enterprise_audit_aggregations_organization_id", "enterprise_audit_aggregations", ["organization_id"])
        op.create_index("ix_enterprise_audit_aggregations_action", "enterprise_audit_aggregations", ["action"])
        op.create_index("ix_enterprise_audit_aggregations_date_bucket", "enterprise_audit_aggregations", ["date_bucket"])

    _backfill_missing_subscriptions(bind)


def _backfill_missing_subscriptions(bind) -> None:
    """Every organization must have exactly one OrganizationSubscription row, or
    BillingService.enforce_plan_limit (wired into /api/ai/plan and agent
    messaging in this same change) denies ALL usage for any org without one --
    see its "no subscription = deny" rule and ensure_trial_subscription's
    docstring in billing_service.py. New organizations get one at registration;
    this backfills every organization that already existed before that wiring
    landed, so this migration does not lock every existing customer out of
    Copilot/agent messaging the moment it deploys. A trial row with no plan_id
    is uncapped, matching today's unmetered behavior."""
    import uuid
    from datetime import datetime, timezone

    if "organization_subscriptions" not in set(sa.inspect(bind).get_table_names()):
        return  # pragma: no cover -- 0010_billing always runs before this migration
    org_ids = {row[0] for row in bind.execute(sa.text("SELECT id FROM organizations"))}
    subscribed_ids = {row[0] for row in bind.execute(sa.text("SELECT organization_id FROM organization_subscriptions"))}
    missing = org_ids - subscribed_ids
    if not missing:
        return
    now = datetime.now(timezone.utc)
    insert_stmt = sa.text(
        "INSERT INTO organization_subscriptions (id, organization_id, status, created_at) "
        "VALUES (:id, :organization_id, 'trial', :created_at)"
    )
    for organization_id in missing:
        bind.execute(insert_stmt, {"id": str(uuid.uuid4()), "organization_id": organization_id, "created_at": now})


def downgrade():
    op.drop_table("enterprise_audit_aggregations")
    op.drop_table("session_policies")
    op.drop_table("ip_allowlists")
    op.drop_table("scim_user_mappings")
    op.drop_table("identity_providers")
