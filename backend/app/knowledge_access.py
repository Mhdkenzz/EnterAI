"""Reusable query scopes for shared project knowledge.

These helpers return SQL expressions and selectable project IDs so callers can
apply authorization in the database query itself. They deliberately fail closed
for inactive users and identity types without an explicit visibility policy.
"""
from __future__ import annotations

from sqlalchemy import and_, false, not_, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import ColumnElement, Select

from .models import AgentAccessGrant, Project, ProjectDocument, Task, User


def is_workspace_manager(user: User) -> bool:
    """Return whether this identity has workspace-wide knowledge access.

    Active human admins/managers receive manager-level access. Human member and
    viewer access to shared projects is handled separately by query scopes.
    Agents never inherit workspace-wide visibility from their hierarchy level.
    Retired or inactive identities always fail closed.
    """
    if not user.active or user.retired_at is not None:
        return False
    if user.kind == "human":
        return user.role in {"admin", "manager"}
    return False


def visible_project_ids(
    db: Session,
    user: User,
    scope_project_id: str | None = None,
) -> Select[tuple[str]]:
    """Build a selectable of project IDs visible to ``user``.

    Human members and managers see shared organization projects. AI workers see
    only projects with a task assigned to them. A supplied project scope can
    only narrow access.
    """
    # Keep the session in the signature so this helper composes consistently with
    # the other request-scoped query helpers; the selectable executes on the
    # caller's session when used in a WHERE/IN clause.
    _ = db
    stmt = select(Project.id).where(Project.organization_id == user.organization_id)
    if is_workspace_manager(user):
        pass
    elif user.active and user.kind == "human" and user.role in {"member", "viewer"}:
        # Human project records are shared across the workspace. Organization
        # level documents still apply the uploader-only rule below.
        pass
    elif user.active and user.kind == "agent" and user.retired_at is None:
        assigned_projects = select(Task.project_id).where(Task.assignee_id == user.id)
        readable_grants = select(AgentAccessGrant.resource_id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "project",
            AgentAccessGrant.permission.in_(["read", "write"]),
            AgentAccessGrant.revoked_at.is_(None),
        )
        revoked_grant = aliased(AgentAccessGrant)
        active_same_project = select(revoked_grant.id).where(
            revoked_grant.organization_id == user.organization_id,
            revoked_grant.agent_id == user.id,
            revoked_grant.resource_type == "project",
            revoked_grant.resource_id == AgentAccessGrant.resource_id,
            revoked_grant.permission.in_(["read", "write"]),
            revoked_grant.revoked_at.is_(None),
        ).exists()
        revoked_projects = select(AgentAccessGrant.resource_id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "project",
            AgentAccessGrant.revoked_at.is_not(None),
            not_(active_same_project),
        )
        stmt = stmt.where(or_(Project.id.in_(assigned_projects), Project.id.in_(readable_grants)))
        stmt = stmt.where(Project.id.not_in(revoked_projects))
    else:
        stmt = stmt.where(false())
    if scope_project_id is not None:
        stmt = stmt.where(Project.id == scope_project_id)
    return stmt


def task_is_visible(db: Session, user: User) -> ColumnElement[bool]:
    """Return a SQL predicate for task rows visible to ``user``.

    Apply as ``select(Task).where(task_is_visible(db, user))``. Project scope
    enforces tenant and project access; worker rows are limited to assignments.
    """
    project_scope = Task.project_id.in_(visible_project_ids(db, user))
    if is_workspace_manager(user):
        return project_scope
    if user.active and user.kind == "human" and user.role in {"member", "viewer"}:
        return project_scope
    if user.active and user.kind == "agent" and user.retired_at is None:
        granted_projects = select(AgentAccessGrant.resource_id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "project",
            AgentAccessGrant.permission.in_(["read", "write"]),
            AgentAccessGrant.revoked_at.is_(None),
        )
        revoked_grant = aliased(AgentAccessGrant)
        active_same_project = select(revoked_grant.id).where(
            revoked_grant.organization_id == user.organization_id,
            revoked_grant.agent_id == user.id,
            revoked_grant.resource_type == "project",
            revoked_grant.resource_id == AgentAccessGrant.resource_id,
            revoked_grant.permission.in_(["read", "write"]),
            revoked_grant.revoked_at.is_(None),
        ).exists()
        revoked_projects = select(AgentAccessGrant.resource_id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "project",
            AgentAccessGrant.revoked_at.is_not(None),
            not_(active_same_project),
        )
        own_task = Task.assignee_id == user.id
        granted_task = Task.project_id.in_(granted_projects)
        return project_scope & or_(and_(own_task, Task.project_id.not_in(revoked_projects)), granted_task)
    return false()


def document_is_visible(db: Session, user: User) -> ColumnElement[bool]:
    """Return a SQL predicate for project-document rows visible to ``user``.

    Project-attached documents inherit project visibility. Organization-level
    documents with no project are visible only to their uploader and workspace
    managers, preventing unscoped files from leaking through unified search.
    """
    if is_workspace_manager(user):
        return ProjectDocument.organization_id == user.organization_id
    if not user.active or user.kind not in {"human", "agent"}:
        return false()
    if user.kind == "agent":
        doc_grant = select(AgentAccessGrant.id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "document",
            AgentAccessGrant.resource_id == ProjectDocument.id,
            AgentAccessGrant.permission == "read",
            AgentAccessGrant.revoked_at.is_(None),
        ).exists()
        doc_revoked = select(AgentAccessGrant.id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "document",
            AgentAccessGrant.resource_id == ProjectDocument.id,
            AgentAccessGrant.revoked_at.is_not(None),
        ).exists()
        explicitly_readable_projects = select(AgentAccessGrant.resource_id).where(
            AgentAccessGrant.organization_id == user.organization_id,
            AgentAccessGrant.agent_id == user.id,
            AgentAccessGrant.resource_type == "project",
            AgentAccessGrant.permission.in_(["read", "write"]),
            AgentAccessGrant.revoked_at.is_(None),
        )
        project_visible = ProjectDocument.project_id.in_(explicitly_readable_projects)
        return (ProjectDocument.organization_id == user.organization_id) & or_(doc_grant, and_(project_visible, not_(doc_revoked)))
    visible_projects = ProjectDocument.project_id.in_(visible_project_ids(db, user))
    private_unscoped = (
        ProjectDocument.project_id.is_(None)
        & (ProjectDocument.uploaded_by == user.id)
    )
    return (ProjectDocument.organization_id == user.organization_id) & (
        visible_projects | private_unscoped
    )
