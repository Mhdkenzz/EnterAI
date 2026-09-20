"""Entity grounding guards.

Prevent silent substitution of nonexistent entities with unrelated real data.
Used in read-tool paths and in normalization of results for multi-step tracking.

Rules enforced at the application layer (not model-level, since model-level
failures remain possible):
- Explicit ID: exact match or ValueError (not found)
- Name-based search: return candidates with IDs; if ambiguous, expose IDs
- No fuzzy substitution allowed in any tool result processing
"""
from __future__ import annotations
from typing import Any


def resolve_project_by_id(db, user, project_id: str):
    """Exact-match only. Raises ValueError if not found or cross-tenant."""
    from sqlalchemy import select
    from .models import Project
    row = db.execute(
        select(Project).where(Project.id == project_id, Project.organization_id == user.organization_id)
    ).scalar_one_or_none()
    if not row:
        raise ValueError(f"No project with id {project_id!r} was found in this workspace.")
    return row


def resolve_task_by_id(db, user, task_id: str):
    from sqlalchemy import select
    from .models import Task, Project
    row = db.execute(
        select(Task, Project)
        .join(Project, Project.id == Task.project_id)
        .where(Task.id == task_id, Project.organization_id == user.organization_id)
    ).first()
    if not row or row[1].organization_id != user.organization_id:
        raise ValueError(f"No task with id {task_id!r} was found in this workspace.")
    return row


def resolve_user_by_name(db, user, name_substring: str):
    """Returns list of candidates (never substitutes). Used for ambiguity exposure."""
    from sqlalchemy import select
    from .models import User
    candidates = db.scalars(
        select(User).where(
            User.organization_id == user.organization_id,
            User.name.ilike(f"%{name_substring}%"),
            User.kind == "human",
        )
    ).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role} for u in candidates]
