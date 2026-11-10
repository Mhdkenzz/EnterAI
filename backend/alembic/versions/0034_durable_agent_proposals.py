"""Persist execution-time agent proposals for human review."""
from alembic import op
import sqlalchemy as sa

revision = "0034_durable_agent_proposals"
down_revision = "0033_slack_workspace_tenant_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "copilot_confirmations" not in tables:
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("copilot_confirmations")}
    additions = [
        ("token_ciphertext", sa.Column("token_ciphertext", sa.Text(), nullable=True)),
        ("label", sa.Column("label", sa.String(240), nullable=True)),
        ("details", sa.Column("details", sa.JSON(), nullable=True)),
        ("task_id", sa.Column("task_id", sa.String(), nullable=True)),
        ("rejected_at", sa.Column("rejected_at", sa.DateTime(), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column("copilot_confirmations", column)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("copilot_confirmations")}
    if "ix_copilot_confirmations_task_id" not in indexes:
        op.create_index("ix_copilot_confirmations_task_id", "copilot_confirmations", ["task_id"])
    if "ix_copilot_confirmations_rejected_at" not in indexes:
        op.create_index("ix_copilot_confirmations_rejected_at", "copilot_confirmations", ["rejected_at"])
    foreign_keys = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("copilot_confirmations")}
    if "fk_copilot_confirmations_task_id_tasks" not in foreign_keys:
        with op.batch_alter_table("copilot_confirmations") as batch:
            batch.create_foreign_key("fk_copilot_confirmations_task_id_tasks", "tasks", ["task_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    if "copilot_confirmations" not in set(sa.inspect(bind).get_table_names()):
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("copilot_confirmations")}
    for name in ("ix_copilot_confirmations_task_id", "ix_copilot_confirmations_rejected_at"):
        if name in indexes:
            op.drop_index(name, table_name="copilot_confirmations")
    with op.batch_alter_table("copilot_confirmations") as batch:
        for name in ("token_ciphertext", "label", "details", "task_id", "rejected_at"):
            if name in {column["name"] for column in sa.inspect(bind).get_columns("copilot_confirmations")}:
                batch.drop_column(name)
