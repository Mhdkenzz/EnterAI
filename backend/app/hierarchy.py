"""Reconciles actual agent (User kind="agent") rows to match an org's HierarchyConfig.

The desired shape is a strict top-down multiplication: vp_count VPs under the one
CEO, directors_per_vp Directors under *each* VP, managers_per_director Senior
Managers under *each* Director, workers_per_manager Workers under *each* Senior
Manager. Reconciliation is incremental (existing agents are kept where possible,
identified by creation order) rather than wipe-and-rebuild, so routine count tweaks
don't discard an agent's identity, current work, or conversation history.

Shrinking never deletes a surplus agent: it retires it (see `retire_agent`). Retired
agents keep their row, tasks, messages, and audit trail forever, but stop counting
towards a level's quota, stop appearing as active agents, and can never again be
assigned work, execute, or be re-enabled -- see `execution_allowed` (observability.py),
`assign_task_to_agent` and `send_agent_message` (main.py).
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from .time_utils import utcnow_naive

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .observability import audit
from .models import HierarchyConfig, Notification, Project, Task, User
from .services import log

LEVEL_TITLES = {
    "ceo": "Chief Executive Officer",
    "vp": "Vice President",
    "director": "Director",
    "senior_manager": "Senior Manager",
    "worker": "Worker",
}
# Bound the number of active agent rows a single hierarchy update can create.
# This keeps a configuration mistake from expanding into thousands of agents.
MAX_HIERARCHY_AGENTS = 250
# Levels senior enough to act with admin-equivalent tool permissions (see TOOL_ROLES
# in copilot.py); everyone else gets the same permissions as a human team member.
SENIOR_LEVELS = {"ceo", "vp"}
def desired_counts(vp_count: int, directors_per_vp: int, managers_per_director: int, workers_per_manager: int) -> dict[str, int]:
    vp = max(vp_count, 0)
    director = vp * max(directors_per_vp, 0)
    manager = director * max(managers_per_director, 0)
    worker = manager * max(workers_per_manager, 0)
    # The CEO is the workspace's conversational entry point, even before an
    # organization chooses to configure any reports beneath them.
    return {"ceo": 1, "vp": vp, "director": director, "senior_manager": manager, "worker": worker}


def _create_agent(db: Session, org_id: str, level: str, parent_id: str | None, name: str) -> User:
    agent = User(
        organization_id=org_id, name=name, email=f"agent-{uuid4().hex}@agents.internal",
        password_hash=hash_password(uuid4().hex), role="admin" if level in SENIOR_LEVELS else "member",
        title=LEVEL_TITLES[level], avatar="".join(word[0] for word in name.split() if word)[:2].upper() or "AI",
        kind="agent", hierarchy_level=level, parent_agent_id=parent_id,
    )
    db.add(agent)
    db.flush()
    audit(db, org_id, None, "agent", agent.id, "created")
    return agent


def retire_agent(db: Session, agent: User, actor_id: str | None = None) -> None:
    """Retire the identity and hand all unfinished owned work to an active human."""
    if agent.retired_at is not None:
        return
    tasks = db.scalars(select(Task).where(
        Task.assignee_id == agent.id, Task.status != "done",
    )).all()
    for task in tasks:
        project = db.get(Project, task.project_id)
        reporter = db.get(User, task.reporter_id) if task.reporter_id else None
        project_owner = db.get(User, project.owner_id) if project and project.owner_id else None
        human_owner = next((person for person in (reporter, project_owner)
                            if person and person.kind == "human" and person.active
                            and person.organization_id == agent.organization_id), None)
        if human_owner is None:
            human_owner = db.scalar(select(User).where(
                User.organization_id == agent.organization_id, User.kind == "human",
                User.role == "admin", User.active.is_(True),
            ).order_by(User.created_at, User.id).limit(1))
        if human_owner is None:
            # Preserve the durable assignee rather than erase the only known owner.
            continue
        task.assignee_id = human_owner.id
        if task.status == "in_progress":
            task.status = "blocked"
            task.blocked_reason = "The assigned agent was retired. A human needs to review the current state before work resumes."
            task.escalation_category = "other"
        log(db, agent.organization_id, actor_id, "task", task.id, "task_reassigned",
            from_assignee_id=agent.id, to_assignee_id=human_owner.id,
            assignment_reason="agent_retired")
        db.add(Notification(
            user_id=human_owner.id,
            title=f"Work reassigned after {agent.name} was retired: {task.title}"[:220],
            body=task.blocked_reason if task.status == "blocked" else "This task now needs your ownership.",
            href=f"project:{task.project_id}",
        ))
    agent.current_task_id = None
    agent.retired_at = utcnow_naive()
    agent.execution_enabled = False
    audit(db, agent.organization_id, actor_id, "agent", agent.id, "retired")
    db.flush()


def _retire_with_descendants(db: Session, agent: User) -> None:
    """A hierarchy shrink removes a whole reporting slot at once, so descendants of
    the removed role have nowhere left to report and retire along with it."""
    for child in db.scalars(select(User).where(User.parent_agent_id == agent.id, User.retired_at.is_(None))).all():
        _retire_with_descendants(db, child)
    retire_agent(db, agent)


def _sync_level(db: Session, org_id: str, level: str, parents: list[User | None], per_parent: int) -> list[User]:
    """Ensure each agent in `parents` has exactly `per_parent` children at `level`
    (parents=[None] for the root CEO slot). Returns the level's final agent list,
    grouped by parent in the same order as `parents`."""
    result: list[User] = []
    for parent in parents:
        parent_id = parent.id if parent else None
        existing = db.scalars(
            select(User)
            .where(User.organization_id == org_id, User.kind == "agent", User.hierarchy_level == level,
                   User.parent_agent_id == parent_id, User.retired_at.is_(None))
            .order_by(User.created_at)
        ).all()
        if len(existing) > per_parent:
            for extra in existing[per_parent:]:
                _retire_with_descendants(db, extra)
            existing = list(existing[:per_parent])
        while len(existing) < per_parent:
            index = len(existing) + 1
            name = LEVEL_TITLES[level] if per_parent == 1 and parent is None else f"{LEVEL_TITLES[level]} {index}"
            existing.append(_create_agent(db, org_id, level, parent_id, name))
        result.extend(existing)
    return result


def reconcile_agents(db: Session, org_id: str) -> None:
    config = db.scalar(select(HierarchyConfig).where(HierarchyConfig.organization_id == org_id))
    if not config:
        return
    vp_count, directors_per_vp = max(config.vp_count, 0), max(config.directors_per_vp, 0)
    managers_per_director, workers_per_manager = max(config.managers_per_director, 0), max(config.workers_per_manager, 0)
    ceo_list = _sync_level(db, org_id, "ceo", [None], 1)
    vp_list = _sync_level(db, org_id, "vp", ceo_list, vp_count)
    director_list = _sync_level(db, org_id, "director", vp_list, directors_per_vp)
    manager_list = _sync_level(db, org_id, "senior_manager", director_list, managers_per_director)
    _sync_level(db, org_id, "worker", manager_list, workers_per_manager)
    db.commit()
