"""Add organization-scoped source messages for request intake."""
from alembic import op
import sqlalchemy as sa


revision = "0023_inbound_messages"
down_revision = "0022_agent_local_repository"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "inbound_messages" not in tables:
        op.create_table(
            "inbound_messages",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False),
            sa.Column("external_id", sa.String(length=500), nullable=True),
            sa.Column("thread_id", sa.String(length=500), nullable=True),
            sa.Column("sender", sa.String(length=500), nullable=True),
            sa.Column("subject", sa.String(length=500), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("source_url", sa.String(length=2000), nullable=True),
            sa.Column("received_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "source", "external_id", name="uq_inbound_message_source_id"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("inbound_messages")}
    for name, column in (("ix_inbound_messages_organization_id", "organization_id"),
                         ("ix_inbound_messages_received_at", "received_at"),
                         ("ix_inbound_messages_status", "status")):
        if name not in indexes:
            op.create_index(name, "inbound_messages", [column])


def downgrade():
    op.drop_index("ix_inbound_messages_status", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_received_at", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_organization_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")
