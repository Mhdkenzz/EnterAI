"""Allow one active Slack bot installation to own a workspace across tenants."""
from alembic import op
import sqlalchemy as sa

revision = "0033_slack_workspace_tenant_isolation"
down_revision = "0032_merge_agent_budget_and_ai_provider"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_connected_slack_workspace"
PREDICATE = "provider = 'slack' AND status = 'connected'"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "integration_connections" not in tables:
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("integration_connections")}
    if INDEX_NAME in indexes:
        return
    conflicts = bind.execute(sa.text(
        "SELECT external_account_id FROM integration_connections "
        "WHERE provider = 'slack' AND status = 'connected' "
        "GROUP BY external_account_id HAVING count(*) > 1 LIMIT 1"
    )).first()
    if conflicts:
        raise RuntimeError(
            "Multiple organizations have an active connection to the same Slack workspace. "
            "Disconnect duplicate Slack bot connections before upgrading."
        )
    op.create_index(
        INDEX_NAME, "integration_connections", ["external_account_id"], unique=True,
        postgresql_where=sa.text(PREDICATE), sqlite_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("integration_connections")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="integration_connections")
