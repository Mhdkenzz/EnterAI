"""Extend provider_calls with the fields needed to actually answer "which
model/provider handled this call, how many tokens did it use, did the
compatibility adapter fire" -- today the table only records provider_mode
(deterministic/tool_calling), duration, and pass/fail, so an operator has no
way to see what was actually asked or by which model even in aggregate.

All columns are additive and nullable/defaulted, so every existing row (and
every existing INSERT that doesn't pass the new fields) keeps working
unchanged. `timed_out` and `retries` are included now, defaulted to
False/0, as the schema's forward-compatible place for a future retry/timeout-
taxonomy pass to write into -- no retry logic exists yet, so nothing writes a
non-default value into them today; that's an honest placeholder, not a claim
this migration adds retry behavior.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_provider_call_observability"
down_revision = "0014_audit_aggregation_unique"
branch_labels = None
depends_on = None

_NEW_COLUMNS = [
    ("provider", sa.String(40)),
    ("model", sa.String(120)),
    ("prompt_tokens", sa.Integer()),
    ("completion_tokens", sa.Integer()),
    ("reasoning_tokens", sa.Integer()),
    ("rounds", sa.Integer()),
    ("adapter_used", sa.String(40)),
    ("error_kind", sa.String(60)),
]


def upgrade():
    bind = op.get_bind()
    existing_cols = {c["name"] for c in sa.inspect(bind).get_columns("provider_calls")}
    for name, col_type in _NEW_COLUMNS:
        if name not in existing_cols:
            op.add_column("provider_calls", sa.Column(name, col_type, nullable=True))
    if "timed_out" not in existing_cols:
        op.add_column("provider_calls", sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "retries" not in existing_cols:
        op.add_column("provider_calls", sa.Column("retries", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    for name, _ in _NEW_COLUMNS + [("timed_out", None), ("retries", None)]:
        op.drop_column("provider_calls", name)
