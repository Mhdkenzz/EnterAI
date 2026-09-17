"""Durable privacy-minimal audit/usage ledger and execution governance."""
from alembic import op
import sqlalchemy as sa

revision = '0006_observability'
down_revision = '0005_agent_execution'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in ('organizations', 'users'):
        if 'execution_enabled' not in {c['name'] for c in sa.inspect(bind).get_columns(table)}:
            op.add_column(table, sa.Column('execution_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    existing = set(sa.inspect(bind).get_table_names())
    if 'audit_events' not in existing:
        op.create_table('audit_events',
            sa.Column('id', sa.String(), primary_key=True),
            sa.Column('organization_id', sa.String(), nullable=False),
            sa.Column('actor_id', sa.String(), nullable=True),
            sa.Column('initiator_id', sa.String(), nullable=True),
            sa.Column('source', sa.String(20), nullable=False),
            sa.Column('action', sa.String(100), nullable=False),
            sa.Column('entity_type', sa.String(40), nullable=False),
            sa.Column('entity_id', sa.String(), nullable=False),
            sa.Column('detail', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False))
        for col in ('organization_id', 'actor_id', 'source', 'action', 'entity_type', 'created_at'):
            op.create_index('ix_audit_events_'+col, 'audit_events', [col])
    if 'provider_calls' not in existing:
        op.create_table('provider_calls',
            sa.Column('id', sa.String(), primary_key=True),
            sa.Column('organization_id', sa.String(), nullable=False),
            sa.Column('actor_id', sa.String(), nullable=True),
            sa.Column('agent_id', sa.String(), nullable=True),
            sa.Column('initiator_id', sa.String(), nullable=True),
            sa.Column('source', sa.String(20), nullable=False),
            sa.Column('run_id', sa.String(), nullable=True),
            sa.Column('provider_mode', sa.String(30), nullable=False),
            sa.Column('failed', sa.Boolean(), nullable=False),
            sa.Column('duration_ms', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False))
        for col in ('organization_id', 'agent_id', 'run_id', 'created_at'):
            op.create_index('ix_provider_calls_'+col, 'provider_calls', [col])
    if 'execution_runs' not in existing:
        op.create_table('execution_runs',
            sa.Column('id', sa.String(), primary_key=True),
            sa.Column('organization_id', sa.String(), nullable=False),
            sa.Column('agent_id', sa.String(), nullable=False),
            sa.Column('outcome', sa.String(20), nullable=False),
            sa.Column('failed', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False))
        for col in ('organization_id', 'agent_id', 'created_at'):
            op.create_index('ix_execution_runs_'+col, 'execution_runs', [col])


def downgrade():
    for table in ('execution_runs', 'provider_calls', 'audit_events'):
        op.drop_table(table)
    for table in ('users', 'organizations'):
        op.drop_column(table, 'execution_enabled')
