"""Track automatic request triage and retry state."""
from alembic import op
import sqlalchemy as sa


revision = "0026_inbound_auto_triage"
down_revision = "0025_gmail_integrations"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("inbound_messages")}
    definitions = {
        "triage_status": sa.Column("triage_status", sa.String(length=30), nullable=False, server_default="not_started"),
        "triage_category": sa.Column("triage_category", sa.String(length=40), nullable=True),
        "triage_reason": sa.Column("triage_reason", sa.Text(), nullable=True),
        "triage_attempts": sa.Column("triage_attempts", sa.Integer(), nullable=False, server_default="0"),
        "triage_next_attempt_at": sa.Column("triage_next_attempt_at", sa.DateTime(), nullable=True),
    }
    for name, definition in definitions.items():
        if name not in columns:
            op.add_column("inbound_messages", definition)
    indexes = {index["name"] for index in inspector.get_indexes("inbound_messages")}
    if "ix_inbound_messages_triage_status" not in indexes:
        op.create_index("ix_inbound_messages_triage_status", "inbound_messages", ["triage_status"])
    op.execute("UPDATE inbound_messages SET triage_status = 'pending' WHERE source = 'gmail' AND status = 'new'")


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("inbound_messages")}
    indexes = {index["name"] for index in inspector.get_indexes("inbound_messages")}
    if "ix_inbound_messages_triage_status" in indexes:
        op.drop_index("ix_inbound_messages_triage_status", table_name="inbound_messages")
    for name in ("triage_next_attempt_at", "triage_attempts", "triage_reason", "triage_category", "triage_status"):
        if name in columns:
            op.drop_column("inbound_messages", name)
