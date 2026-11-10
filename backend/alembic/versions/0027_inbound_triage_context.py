"""Store the CEO triage summary and explicitly named concerns."""
from alembic import op
import sqlalchemy as sa


revision = "0027_inbound_triage_context"
down_revision = "0026_inbound_auto_triage"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("inbound_messages")}
    if "triage_summary" not in columns:
        op.add_column("inbound_messages", sa.Column("triage_summary", sa.Text(), nullable=True))
    if "triage_concerns" not in columns:
        op.add_column("inbound_messages", sa.Column("triage_concerns", sa.JSON(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("inbound_messages")}
    if "triage_concerns" in columns:
        op.drop_column("inbound_messages", "triage_concerns")
    if "triage_summary" in columns:
        op.drop_column("inbound_messages", "triage_summary")
