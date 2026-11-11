"""Give existing unowned tasks an accountable workspace owner."""

from alembic import op
import sqlalchemy as sa


revision = "0036_task_owner_backfill"
down_revision = "0035_shared_project_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve a living requester first, then the project owner, then the
    # organization's first active administrator. This changes only tasks whose
    # owner is missing and does not rewrite completed tasks that have an owner.
    op.execute(sa.text("""
        UPDATE tasks
        SET assignee_id = COALESCE(
            (SELECT requester.id FROM users AS requester
             WHERE requester.id = tasks.reporter_id AND requester.active IS TRUE
               AND requester.retired_at IS NULL LIMIT 1),
            (SELECT owner.id FROM projects
             JOIN users AS owner ON owner.id = projects.owner_id
             WHERE projects.id = tasks.project_id AND owner.active IS TRUE
               AND owner.retired_at IS NULL LIMIT 1),
            (SELECT admin.id FROM projects
             JOIN users AS admin ON admin.organization_id = projects.organization_id
             WHERE projects.id = tasks.project_id AND admin.kind = 'human'
               AND admin.role = 'admin' AND admin.active IS TRUE
             ORDER BY admin.created_at, admin.id LIMIT 1)
        )
        WHERE tasks.assignee_id IS NULL
    """))


def downgrade() -> None:
    # Ownership values written by this migration are indistinguishable from later
    # user assignments, so downgrade deliberately preserves them.
    pass
