"""Least-privilege access checks shared by agent tools and admin governance."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgentAccessGrant, Project, ProjectDocument, Task, User


BASE_AGENT_TOOLS = frozenset({
    "get_projects", "get_tasks", "get_my_tasks", "get_comments", "search",
    "search_documents", "search_knowledge", "log_progress", "mark_task_complete",
    "raise_blocker", "suggest_next_step",
})
VALID_PERMISSIONS = {"project": {"read", "write"}, "document": {"read"}, "tool": {"use"}}
NEVER_AGENT_TOOLS = frozenset({"get_activity", "get_assignable_agents"})
AUDIT_ONLY_TOOLS = frozenset({"direct_api_request"})


def tool_catalog() -> set[str]:
    # Lazy import avoids a copilot <-> access-control import cycle.
    from .copilot import READ_TOOL_NAMES, WRITE_TOOL_NAMES, EXECUTION_TOOL_NAMES
    from .agent_workspace import REPO_READ_NAMES, REPO_APPLY_PATCH_NAME, REPO_PATCH_NAME
    return (set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES) | set(EXECUTION_TOOL_NAMES) | set(REPO_READ_NAMES) | {REPO_APPLY_PATCH_NAME, REPO_PATCH_NAME, "enter_ai_cli"}) - NEVER_AGENT_TOOLS


def grants_for(db: Session, agent: User, resource_type: str | None = None) -> list[AgentAccessGrant]:
    stmt = select(AgentAccessGrant).where(
        AgentAccessGrant.organization_id == agent.organization_id,
        AgentAccessGrant.agent_id == agent.id,
    )
    if resource_type:
        stmt = stmt.where(AgentAccessGrant.resource_type == resource_type)
    return db.scalars(stmt).all()


def _matching_grant(db: Session, agent: User, resource_type: str, resource_id: str, permissions: set[str]) -> AgentAccessGrant | None:
    return db.scalar(select(AgentAccessGrant).where(
        AgentAccessGrant.organization_id == agent.organization_id,
        AgentAccessGrant.agent_id == agent.id,
        AgentAccessGrant.resource_type == resource_type,
        AgentAccessGrant.resource_id == resource_id,
        AgentAccessGrant.permission.in_(permissions),
    ))


def _active_grant(db: Session, agent: User, resource_type: str, resource_id: str, permissions: set[str]) -> bool:
    return db.scalar(select(AgentAccessGrant.id).where(
        AgentAccessGrant.organization_id == agent.organization_id,
        AgentAccessGrant.agent_id == agent.id,
        AgentAccessGrant.resource_type == resource_type,
        AgentAccessGrant.resource_id == resource_id,
        AgentAccessGrant.permission.in_(permissions),
        AgentAccessGrant.revoked_at.is_(None),
    ).limit(1)) is not None


def _revoked_grant(db: Session, agent: User, resource_type: str, resource_id: str, permissions: set[str]) -> bool:
    return db.scalar(select(AgentAccessGrant.id).where(
        AgentAccessGrant.organization_id == agent.organization_id,
        AgentAccessGrant.agent_id == agent.id,
        AgentAccessGrant.resource_type == resource_type,
        AgentAccessGrant.resource_id == resource_id,
        AgentAccessGrant.permission.in_(permissions),
        AgentAccessGrant.revoked_at.is_not(None),
    ).limit(1)) is not None


def agent_tool_allowed(db: Session, agent: User, tool: str) -> bool:
    if agent.kind != "agent" or not agent.active or agent.retired_at is not None or tool in NEVER_AGENT_TOOLS:
        return False
    grant = _matching_grant(db, agent, "tool", tool, {"use"})
    if grant is not None:
        if grant.revoked_at is not None:
            return False
        if tool in {"get_direct_reports", "delegate_subtask", "delegate_task"}:
            return db.scalar(select(User.id).where(
                User.organization_id == agent.organization_id, User.kind == "agent",
                User.parent_agent_id == agent.id, User.active.is_(True), User.retired_at.is_(None),
            ).limit(1)) is not None
        return True
    if tool in {"get_direct_reports", "delegate_subtask"}:
        return db.scalar(select(User.id).where(
            User.organization_id == agent.organization_id, User.kind == "agent",
            User.parent_agent_id == agent.id, User.active.is_(True), User.retired_at.is_(None),
        ).limit(1)) is not None
    if tool in BASE_AGENT_TOOLS:
        return True
    # An explicit revoked row overrides even an otherwise available tool.
    return False


def allowed_agent_tools(db: Session, agent: User) -> set[str]:
    catalog = tool_catalog()
    return {tool for tool in catalog if agent_tool_allowed(db, agent, tool)}


def agent_project_access(db: Session, agent: User, project_id: str, permission: str = "read", task_id: str | None = None) -> bool:
    project = db.scalar(select(Project.id).where(Project.id == project_id, Project.organization_id == agent.organization_id))
    if not project or agent.kind != "agent" or not agent.active or agent.retired_at is not None:
        return False
    permissions = {"read", "write"} if permission == "read" else {"write"}
    if _active_grant(db, agent, "project", project_id, permissions):
        return True
    if _revoked_grant(db, agent, "project", project_id, permissions):
        return False
    # An agent's currently assigned task is the only implicit project access.
    if task_id:
        assigned = db.scalar(select(Task.id).where(
            Task.id == task_id, Task.project_id == project_id, Task.assignee_id == agent.id,
        ))
        if assigned:
            return True
    return False


def agent_document_access(db: Session, agent: User, document_id: str) -> bool:
    document = db.scalar(select(ProjectDocument).where(
        ProjectDocument.id == document_id,
        ProjectDocument.organization_id == agent.organization_id,
    ))
    if not document or agent.kind != "agent" or not agent.active or agent.retired_at is not None:
        return False
    if _active_grant(db, agent, "document", document_id, {"read"}):
        return True
    if _revoked_grant(db, agent, "document", document_id, {"read"}):
        return False
    if document.project_id:
        return agent_project_access(db, agent, document.project_id, "read")
    return document.uploaded_by == agent.id


def record_access_denial(db: Session, agent: User, *, tool: str, resource_type: str = "tool", reason: str = "not_granted") -> None:
    """Write a safe, durable denial event without provider arguments or source text."""
    from .observability import audit_context
    from .observability import audit
    fields = dict(
        resource_type=resource_type if resource_type in {"tool", "project", "document"} else "tool",
        tool=tool if tool in tool_catalog() or tool in AUDIT_ONLY_TOOLS else "unknown_tool",
        denial_reason=reason if reason in {"not_granted", "revoked", "out_of_scope", "invalid_resource"} else "not_granted",
    )
    if db.get_bind().dialect.name == "sqlite":
        # SQLite permits only one writer. A separate ledger connection would
        # deadlock behind the request's open transaction, so commit the compact
        # denial event with that transaction on this dialect.
        with audit_context("agent", agent.id, None):
            audit(db, agent.organization_id, agent.id, "agent_access", agent.id, "access_denied", **fields)
        db.commit()
        return
    with Session(bind=db.get_bind()) as ledger, audit_context("agent", agent.id, None):
        audit(ledger, agent.organization_id, agent.id, "agent_access", agent.id, "access_denied", **fields)
        ledger.commit()
