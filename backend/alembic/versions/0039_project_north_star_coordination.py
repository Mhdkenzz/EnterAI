"""Add measurable project goals, milestones and agent coordination records."""

from alembic import op
import sqlalchemy as sa


revision = "0039_project_north_star_coordination"
down_revision = "0038_task_quality_gate"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    project_columns = _columns("projects")
    for column in (
        sa.Column("north_star_goal", sa.Text(), nullable=True),
        sa.Column("success_metric", sa.String(180), nullable=True),
        sa.Column("metric_unit", sa.String(60), nullable=True),
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("target_value", sa.Float(), nullable=True),
    ):
        if column.name not in project_columns:
            op.add_column("projects", column)

    if not sa.inspect(op.get_bind()).has_table("project_milestones"):
        op.create_table(
            "project_milestones",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(180), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("due_date", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_project_milestones_organization_id", "project_milestones", ["organization_id"])
        op.create_index("ix_project_milestones_project_id", "project_milestones", ["project_id"])
        op.create_index("ix_project_milestones_project", "project_milestones", ["project_id", "created_at"])

    task_columns = _columns("tasks")
    missing_task_columns = []
    if "milestone_id" not in task_columns:
        missing_task_columns.append(sa.Column(
            "milestone_id", sa.String(), sa.ForeignKey("project_milestones.id", name="fk_tasks_milestone_id_project_milestones", ondelete="SET NULL"), nullable=True,
        ))
    if "escalation_category" not in task_columns:
        missing_task_columns.append(sa.Column("escalation_category", sa.String(32), nullable=True))
    if missing_task_columns:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("tasks", recreate="always") as batch:
                for column in missing_task_columns:
                    batch.add_column(column)
        else:
            for column in missing_task_columns:
                op.add_column("tasks", column)
    if "milestone_id" in {column.name for column in missing_task_columns}:
        op.create_index("ix_tasks_milestone_id", "tasks", ["milestone_id"])

    if not sa.inspect(op.get_bind()).has_table("agent_next_steps"):
        op.create_table(
            "agent_next_steps",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_task_id", sa.String(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("milestone_id", sa.String(), sa.ForeignKey("project_milestones.id", ondelete="SET NULL"), nullable=True),
            sa.Column("agent_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(240), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("priority", sa.String(20), nullable=False, server_default="medium"),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("created_task_id", sa.String(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        )
        for name, columns in (
            ("ix_agent_next_steps_organization_id", ["organization_id"]),
            ("ix_agent_next_steps_project_id", ["project_id"]),
            ("ix_agent_next_steps_source_task_id", ["source_task_id"]),
            ("ix_agent_next_steps_agent_id", ["agent_id"]),
            ("ix_agent_next_steps_status", ["status"]),
            ("ix_agent_next_steps_org_project_status", ["organization_id", "project_id", "status"]),
        ):
            op.create_index(name, "agent_next_steps", columns)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("agent_next_steps"):
        for name in (
            "ix_agent_next_steps_org_project_status", "ix_agent_next_steps_status", "ix_agent_next_steps_agent_id",
            "ix_agent_next_steps_source_task_id", "ix_agent_next_steps_project_id", "ix_agent_next_steps_organization_id",
        ):
            op.drop_index(name, table_name="agent_next_steps")
        op.drop_table("agent_next_steps")

    task_columns = _columns("tasks")
    if "milestone_id" in task_columns:
        op.drop_index("ix_tasks_milestone_id", table_name="tasks")
    drop_task_columns = [name for name in ("escalation_category", "milestone_id") if name in task_columns]
    if drop_task_columns:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("tasks", recreate="always") as batch:
                for name in drop_task_columns:
                    batch.drop_column(name)
        else:
            if "milestone_id" in drop_task_columns:
                milestone_fk = next((fk for fk in sa.inspect(op.get_bind()).get_foreign_keys("tasks")
                                     if fk.get("referred_table") == "project_milestones"
                                     and fk.get("constrained_columns") == ["milestone_id"]), None)
                if milestone_fk and milestone_fk.get("name"):
                    op.drop_constraint(milestone_fk["name"], "tasks", type_="foreignkey")
            for name in drop_task_columns:
                op.drop_column("tasks", name)

    if sa.inspect(op.get_bind()).has_table("project_milestones"):
        for name in ("ix_project_milestones_project", "ix_project_milestones_project_id", "ix_project_milestones_organization_id"):
            op.drop_index(name, table_name="project_milestones")
        op.drop_table("project_milestones")

    project_columns = _columns("projects")
    for name in ("target_value", "current_value", "baseline_value", "metric_unit", "success_metric", "north_star_goal"):
        if name in project_columns:
            op.drop_column("projects", name)
