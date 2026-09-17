"""Reconciles actual agent (User kind="agent") rows to match an org's HierarchyConfig.

The desired shape is a strict top-down multiplication: vp_count VPs under the one
CEO, directors_per_vp Directors under *each* VP, managers_per_director Senior
Managers under *each* Director, workers_per_manager Workers under *each* Senior
Manager. Reconciliation is incremental (existing agents are kept where possible,
identified by creation order) rather than wipe-and-rebuild, so routine count tweaks
don't discard an agent's identity, current work, or conversation history.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .models import AgentMessage, HierarchyConfig, Task, User

LEVEL_TITLES = {
    "ceo": "Chief Executive Officer",
    "vp": "Vice President",
    "director": "Director",
    "senior_manager": "Senior Manager",
    "worker": "Worker",
}
# Levels senior enough to act with admin-equivalent tool permissions (see TOOL_ROLES
# in copilot.py); everyone else gets the same permissions as a human team member.
SENIOR_LEVELS = {"ceo", "vp"}
MAX_HIERARCHY_AGENTS = 200


def desired_counts(vp_count: int, directors_per_vp: int, managers_per_director: int, workers_per_manager: int) -> dict[str, int]:
    vp = max(vp_count, 0)
    director = vp * max(directors_per_vp, 0)
    manager = director * max(managers_per_director, 0)
    worker = manager * max(workers_per_manager, 0)
    return {"ceo": 1 if vp > 0 else 0, "vp": vp, "director": director, "senior_manager": manager, "worker": worker}


def _create_agent(db: Session, org_id: str, level: str, parent_id: str | None, name: str) -> User:
    agent = User(
        organization_id=org_id, name=name, email=f"agent-{uuid4().hex}@agents.internal",
        password_hash=hash_password(uuid4().hex), role="admin" if level in SENIOR_LEVELS else "member",
        title=LEVEL_TITLES[level], avatar="".join(word[0] for word in name.split() if word)[:2].upper() or "AI",
        kind="agent", hierarchy_level=level, parent_agent_id=parent_id,
    )
    db.add(agent)
    db.flush()
    return agent


def _delete_agent_and_descendants(db: Session, agent: User) -> None:
    for child in db.scalars(select(User).where(User.parent_agent_id == agent.id)).all():
        _delete_agent_and_descendants(db, child)
    for task in db.scalars(select(Task).where(Task.assignee_id == agent.id)).all():
        task.assignee_id = None
    for message in db.scalars(select(AgentMessage).where(AgentMessage.agent_id == agent.id)).all():
        db.delete(message)
    db.delete(agent)
    db.flush()


def _sync_level(db: Session, org_id: str, level: str, parents: list[User | None], per_parent: int) -> list[User]:
    """Ensure each agent in `parents` has exactly `per_parent` children at `level`
    (parents=[None] for the root CEO slot). Returns the level's final agent list,
    grouped by parent in the same order as `parents`."""
    result: list[User] = []
    for parent in parents:
        parent_id = parent.id if parent else None
        existing = db.scalars(
            select(User)
            .where(User.organization_id == org_id, User.kind == "agent", User.hierarchy_level == level, User.parent_agent_id == parent_id)
            .order_by(User.created_at)
        ).all()
        if len(existing) > per_parent:
            for extra in existing[per_parent:]:
                _delete_agent_and_descendants(db, extra)
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
    ceo_list = _sync_level(db, org_id, "ceo", [None], 1 if vp_count > 0 else 0)
    vp_list = _sync_level(db, org_id, "vp", ceo_list, vp_count)
    director_list = _sync_level(db, org_id, "director", vp_list, directors_per_vp)
    manager_list = _sync_level(db, org_id, "senior_manager", director_list, managers_per_director)
    _sync_level(db, org_id, "worker", manager_list, workers_per_manager)
    db.commit()
