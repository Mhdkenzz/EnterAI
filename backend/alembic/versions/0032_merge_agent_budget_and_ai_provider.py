"""Join the agent budget and AI provider selection revisions."""

revision = "0032_merge_agent_budget_and_ai_provider"
down_revision = ("0031_agent_daily_limit", "0031_ai_provider_selection")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
