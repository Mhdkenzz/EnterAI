"""Agent retirement: retire instead of delete, preserving identity and history."""
from alembic import op
import sqlalchemy as sa

revision = '0007_agent_retirement'
down_revision = '0006_observability'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'retired_at' not in {c['name'] for c in sa.inspect(bind).get_columns('users')}:
        op.add_column('users', sa.Column('retired_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('users', 'retired_at')
