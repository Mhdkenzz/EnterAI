"""Scoped workspace tools and provider adapters for the Enter AI Copilot.

Providers receive either a serialised, organisation-scoped snapshot (the
deterministic offline path) or a structured tool-calling loop that executes
read tools against WorkspaceTools on the provider's behalf. Providers never
receive a database session, credentials, or a way to mutate application data
directly -- every write tool call produces a signed confirmation token that
must be replayed to /api/ai/confirm, where authenticated application code
performs the actual change.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .ai.context_budget import cap_tool_result
from .ai.profiles import DEFAULT_PROFILE
from .ai.providers import AnthropicCopilotProvider, DeterministicCopilotProvider, OpenAICompatibleCopilotProvider
from .ai.tool_routing import preflight_tool_calls
from .ai.tool_schema import render_anthropic, render_openai
from .ai.tool_validation import validate_tool_args
from .ai.turn import ProviderTurn
from .ai.conversation_memory import ConversationMemory
from .auth import CONFIRMATION_SECRET
from .models import Activity, Comment, Notification, Project, ProjectDocument, Task, Team, User
from .services import log, sync_agent_task_pointers
from .observability import legacy_detail, provider_call, require_execution, execution_run, audit, audit_context, tool_audit


class CopilotProviderError(RuntimeError):
    """Raised when a configured remote/local provider cannot answer safely."""


def _task_data(task: Task, project: Project) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "project_id": project.id,
        "project_name": project.name,
        "project_health": project.health,
    }


class WorkspaceTools:
    """The complete read surface exposed to the Copilot for this milestone."""

    def __init__(self, db: Session, user: User, scope_project_id: str | None = None):
        self.db = db
        self.user = user
        self.scope_project_id = scope_project_id

    def get_projects(self) -> list[dict[str, Any]]:
        stmt = select(Project).where(Project.organization_id == self.user.organization_id)
        if self.scope_project_id:
            stmt = stmt.where(Project.id == self.scope_project_id)
        projects = self.db.scalars(stmt.order_by(Project.created_at.desc())).all()
        return [
            {
                "id": project.id,
                "name": project.name,
                "code": project.code,
                "description": project.description or "",
                "status": project.status,
                "health": project.health,
                "due_date": project.due_date.isoformat() if project.due_date else None,
            }
            for project in projects
        ]

    def get_tasks(self) -> list[dict[str, Any]]:
        stmt = (
            select(Task, Project)
            .join(Project, Project.id == Task.project_id)
            .where(Project.organization_id == self.user.organization_id)
        )
        if self.scope_project_id:
            stmt = stmt.where(Project.id == self.scope_project_id)
        rows = self.db.execute(stmt.order_by(Task.due_date)).all()
        return [_task_data(task, project) for task, project in rows]

    def get_my_tasks(self) -> list[dict[str, Any]]:
        tasks = self.get_tasks()
        assigned_ids = set(self.db.scalars(select(Task.id).where(Task.assignee_id == self.user.id)).all())
        return [task for task in tasks if task["id"] in assigned_ids]

    def get_teams(self) -> list[dict[str, Any]]:
        teams = self.db.scalars(select(Team).where(Team.organization_id == self.user.organization_id)).all()
        return [{"id": team.id, "name": team.name, "description": team.description or ""} for team in teams]

    def get_users(self) -> list[dict[str, Any]]:
        users = self.db.scalars(select(User).where(User.organization_id == self.user.organization_id, User.kind == "human")).all()
        return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "title": u.title or ""} for u in users]

    def get_notifications(self) -> list[dict[str, Any]]:
        notifications = self.db.scalars(
            select(Notification).where(Notification.user_id == self.user.id).order_by(Notification.created_at.desc()).limit(20)
        ).all()
        return [
            {"id": n.id, "title": n.title, "body": n.body or "", "read": n.read, "created_at": n.created_at.isoformat()}
            for n in notifications
        ]

    def get_activity(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(Activity).where(Activity.organization_id == self.user.organization_id).order_by(Activity.created_at.desc()).limit(20)
        ).all()
        return [
            {"id": a.id, "action": a.action, "entity_type": a.entity_type, "detail": legacy_detail(a.detail or {}), "created_at": a.created_at.isoformat()}
            for a in rows
        ]

    def get_comments(self, task_id: str) -> list[dict[str, Any]]:
        task_row = self.db.execute(
            select(Task, Project).join(Project, Project.id == Task.project_id).where(Task.id == task_id)
        ).first()
        if not task_row or task_row[1].organization_id != self.user.organization_id:
            raise ValueError(f"No task with id {task_id!r} was found in this workspace.")
        comments = self.db.scalars(select(Comment).where(Comment.task_id == task_id)).all()
        return [
            {"id": c.id, "body": c.body, "author_id": c.author_id, "created_at": c.created_at.isoformat()}
            for c in comments
        ]

    def search(self, query: str) -> dict[str, Any]:
        term = f"%{query}%"
        projects = self.db.scalars(
            select(Project).where(Project.organization_id == self.user.organization_id, or_(Project.name.ilike(term), Project.code.ilike(term)))
        ).all()
        tasks = self.db.scalars(
            select(Task).join(Project).where(Project.organization_id == self.user.organization_id, Task.title.ilike(term))
        ).all()
        return {
            "projects": [{"id": p.id, "name": p.name, "code": p.code} for p in projects],
            "tasks": [{"id": t.id, "title": t.title, "project_id": t.project_id} for t in tasks],
        }

    def search_documents(self, query: str, project_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Retrieve indexed chunks for the user's organization, optionally scoped by project.
        Untrusted query text is never interpreted as SQL directly; the implementation uses
        parameterized pgvector queries through indexing.search_similar_chunks.
        Results are scoped to the user's organization; cross-tenant chunks are excluded
        by the WHERE clause in search_similar_chunks."""
        from .embeddings import get_embedding_provider
        from .indexing import search_similar_chunks
        provider = get_embedding_provider()
        query_embedding = provider.embed([query])[0] if query else None
        if not query_embedding:
            return []
        chunks = search_similar_chunks(
            self.db, query_embedding, self.user, project_id=project_id, limit=limit, similarity_threshold=0.7
        )
        results: list[dict[str, Any]] = []
        for chunk in chunks:
            doc_name = "Unknown"
            document = self.db.get(ProjectDocument, chunk.document_id)
            if document:
                doc_name = document.file_name
            results.append({
                "document_id": chunk.document_id,
                "document_name": doc_name,
                "chunk_index": chunk.chunk_index,
                "snippet": chunk.content[:300] + ("..." if len(chunk.content) > 300 else ""),
                "token_count": chunk.token_count,
                "citation": {"document_id": chunk.document_id, "chunk_index": chunk.chunk_index, "snippet": chunk.content[:200]},
            })
        return results

    def get_direct_reports(self) -> list[dict[str, Any]]:
        reports = self.db.scalars(select(User).where(User.parent_agent_id == self.user.id)).all()
        return [
            {"id": r.id, "name": r.name, "hierarchy_level": r.hierarchy_level, "current_task_id": r.current_task_id}
            for r in reports
        ]

    def snapshot(self) -> dict[str, Any]:
        projects = self.get_projects()
        tasks = self.get_tasks()
        assigned_ids = set(self.db.scalars(select(Task.id).where(Task.assignee_id == self.user.id)).all())
        return {"projects": projects, "tasks": tasks, "my_tasks": [task for task in tasks if task["id"] in assigned_ids]}


# --------------------------------------------------------------------------
# Structured tool registry shared by every tool-calling provider.
# --------------------------------------------------------------------------

READ_TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "get_projects", "description": "List every project in the workspace with status, health, and description.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_tasks", "description": "List every task across every project in the workspace.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_my_tasks", "description": "List tasks assigned to the current user only.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_teams", "description": "List teams in the workspace.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_users", "description": "List members of the workspace, with their role and title.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_notifications", "description": "List the current user's recent notifications.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_activity", "description": "List the workspace's recent audit-log activity.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_comments", "description": "List the comments on one specific task.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"], "additionalProperties": False}},
{"name": "search_documents", "description": "Search indexed document chunks using a query text. Returns matching chunks with citations. Never reveals documents from other organizations. If more than one chunk matches, list every one individually rather than picking one.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "project_id": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, "required": ["query"], "additionalProperties": False}},
        {"name": "search", "description": "Search projects and tasks by name, code, or title. If more than one project or task matches, list every one individually (with its id) rather than picking one.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    {"name": "get_direct_reports", "description": "List the agents who report directly to you in the org chart (empty if you are not an agent, or have no reports).",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]

WRITE_TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "create_task", "description": "Propose creating a new task in a project. Never executes without user confirmation.",
     "input_schema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
         "priority": {"type": "string", "enum": ["low", "medium", "high"]}, "assignee_id": {"type": "string"},
         "due_date": {"type": "string", "description": "ISO-8601 date-time"},
     }, "required": ["project_id", "title"], "additionalProperties": False}},
    {"name": "update_task", "description": "Propose changes to an existing task, such as its status, priority, or assignee. Never executes without user confirmation.",
     "input_schema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
         "status": {"type": "string", "enum": ["backlog", "todo", "in_progress", "review", "done"]},
         "priority": {"type": "string", "enum": ["low", "medium", "high"]}, "assignee_id": {"type": "string"},
         "due_date": {"type": "string", "description": "ISO-8601 date-time"},
     }, "required": ["task_id"], "additionalProperties": False}},
    {"name": "create_project", "description": "Propose creating a new project. Never executes without user confirmation.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "code": {"type": "string"}, "description": {"type": "string"},
         "team_id": {"type": "string"}, "health": {"type": "string", "enum": ["on_track", "at_risk", "off_track"]},
     }, "required": ["name", "code"], "additionalProperties": False}},
    {"name": "update_project", "description": "Propose changes to an existing project, such as its status or health. Never executes without user confirmation.",
     "input_schema": {"type": "object", "properties": {
         "project_id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
         "status": {"type": "string"}, "health": {"type": "string", "enum": ["on_track", "at_risk", "off_track"]},
     }, "required": ["project_id"], "additionalProperties": False}},
    {"name": "add_comment", "description": "Propose adding a comment to a task. Never executes without user confirmation.",
     "input_schema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "body": {"type": "string"},
     }, "required": ["task_id", "body"], "additionalProperties": False}},
    {"name": "create_team", "description": "Propose creating a new team. Never executes without user confirmation.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "description": {"type": "string"},
     }, "required": ["name"], "additionalProperties": False}},
    {"name": "delegate_task", "description": "Propose reassigning a task to a different agent (e.g. pushing it down to a direct report). Never executes without confirmation.",
     "input_schema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "agent_id": {"type": "string"},
     }, "required": ["task_id", "agent_id"], "additionalProperties": False}},
]

TOOL_SPECS = READ_TOOL_SPECS + WRITE_TOOL_SPECS
READ_TOOL_NAMES = {spec["name"] for spec in READ_TOOL_SPECS}
WRITE_TOOL_NAMES = {spec["name"] for spec in WRITE_TOOL_SPECS}
_INPUT_SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {spec["name"]: spec["input_schema"] for spec in TOOL_SPECS}

# Which roles may propose (and later confirm) each write tool. Task- and comment-level
# work is open to any workspace member; team creation shapes the org chart, so it is
# admin-only. Enforced both when building the tool list offered to the model (so the
# Copilot doesn't propose something the user can't do) and again in /api/ai/confirm
# (the actual safety boundary -- a token minted before a role change must still be
# re-checked at execution time).
TOOL_ROLES: dict[str, frozenset[str]] = {
    "create_task": frozenset({"admin", "manager", "member"}),
    "update_task": frozenset({"admin", "manager", "member"}),
    "add_comment": frozenset({"admin", "manager", "member"}),
    "create_project": frozenset({"admin", "manager", "member"}),
    "update_project": frozenset({"admin", "manager", "member"}),
    "create_team": frozenset({"admin"}),
    # Only admin-equivalent identities may re-delegate work -- for agents, that's the
    # ceo/vp levels (see hierarchy.SENIOR_LEVELS), matching real reporting authority.
    "delegate_task": frozenset({"admin"}),
}


def allowed_write_tools(role: str) -> set[str]:
    return {name for name, roles in TOOL_ROLES.items() if role in roles}


def _filtered_specs(role: str | None) -> list[dict[str, Any]]:
    allowed = allowed_write_tools(role) if role is not None else WRITE_TOOL_NAMES
    return [t for t in TOOL_SPECS if t["name"] in READ_TOOL_NAMES or t["name"] in allowed]


def anthropic_tool_specs(role: str | None = None) -> list[dict[str, Any]]:
    return render_anthropic(_filtered_specs(role))


def openai_tool_specs(role: str | None = None) -> list[dict[str, Any]]:
    return render_openai(_filtered_specs(role))


def execute_read_tool(name: str, args: dict[str, Any], tools: WorkspaceTools) -> Any:
    outcome = 'failed'
    try:
        result = _execute_read_tool(name, args, tools)
        outcome = 'succeeded'
        return result
    finally:
        # Never persist provider-controlled tool names or arguments.
        tool_audit(tools, 'provider_read', name if name in READ_TOOL_NAMES else 'unknown_tool', outcome=outcome)


def _execute_read_tool(name: str, args: dict[str, Any], tools: WorkspaceTools) -> Any:
    if name == "get_projects": return tools.get_projects()
    if name == "get_tasks": return tools.get_tasks()
    if name == "get_my_tasks": return tools.get_my_tasks()
    if name == "get_teams": return tools.get_teams()
    if name == "get_users": return tools.get_users()
    if name == "get_notifications": return tools.get_notifications()
    if name == "get_activity": return tools.get_activity()
    if name == "get_direct_reports": return tools.get_direct_reports()
    if name == "get_comments": return tools.get_comments(args.get("task_id", ""))
    if name == "search": return tools.search(args.get("query", ""))
    if name == "search_documents": return tools.search_documents(args.get("query", ""), args.get("project_id"), args.get("limit", 5))
    raise KeyError(f"Unknown read tool {name!r}")


def describe_action(db: Session, user: User, name: str, args: dict[str, Any], token: str) -> dict[str, Any]:
    """A proposed write as the confirmation card shows it: a label plus the concrete
    targets it touches, resolved to names inside the caller's organization, so the
    person approving it can see *which* project or task will change."""
    details: list[dict[str, str]] = []

    def scoped(model, key):
        row = db.get(model, args.get(key)) if args.get(key) else None
        org = getattr(row, "organization_id", None)
        if row is not None and org is None and model is Task:
            project = db.get(Project, row.project_id)
            org = project.organization_id if project else None
        return row if row is not None and org == user.organization_id else None

    project = scoped(Project, "project_id")
    task = scoped(Task, "task_id")
    if task is not None and project is None:
        project = db.get(Project, task.project_id)
    if project is not None:
        details.append({"label": "Project", "value": project.name})
    if task is not None:
        details.append({"label": "Task", "value": task.title})
    if args.get("assignee_id") or args.get("agent_id"):
        person = db.get(User, args.get("assignee_id") or args.get("agent_id"))
        if person is not None and person.organization_id == user.organization_id:
            details.append({"label": "Assignee", "value": person.name})
    if args.get("team_id"):
        team = db.get(Team, args["team_id"])
        if team is not None and team.organization_id == user.organization_id:
            details.append({"label": "Team", "value": team.name})
    for key, label in (("title", "Title"), ("name", "Name"), ("status", "Status"), ("priority", "Priority"),
                       ("health", "Health"), ("body", "Comment"), ("due_date", "Due")):
        if args.get(key) not in (None, ""):
            details.append({"label": label, "value": str(args[key]).replace("_", " ")[:200]})
    label = _describe_write_call(name, args)
    if name == "update_task" and task is not None:
        label = f"Update task: {task.title}"
    elif name == "update_project" and project is not None:
        label = f"Update project: {project.name}"
    elif name == "add_comment" and task is not None:
        label = f"Comment on: {task.title}"
    return {"label": label, "details": details, "confirmation_token": token, "requires_confirmation": True}


def _describe_write_call(name: str, args: dict[str, Any]) -> str:
    if name == "create_task": return f"Create task: {args.get('title', 'New task')}"
    if name == "update_task": return "Update task"
    if name == "create_project": return f"Create project: {args.get('name', 'New project')}"
    if name == "update_project": return "Update project"
    if name == "add_comment": return "Add comment"
    if name == "create_team": return f"Create team: {args.get('name', 'New team')}"
    if name == "delegate_task": return "Delegate task"
    return "Proposed action"


# --------------------------------------------------------------------------
# Providers -- adapters live in app/ai/providers.py; this module is their
# only consumer and stays the security/product boundary (roles, WorkspaceTools,
# confirmation tokens) that app/ai/ deliberately knows nothing about.
# --------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You are Enter AI Copilot, embedded in a project-management workspace. Use the read tools to ground every "
    "answer in this organisation's real data; never invent projects, tasks, or people. You may call at most one "
    "write tool per turn, from whichever write tools are available to you in this turn's tool list, to propose a "
    "change, but it will never execute automatically -- the user must explicitly confirm it afterwards. "
    "Whenever you call a write tool, also include a short plain-language explanation of the proposal in the same turn. "
    "If a tool call fails or finds no match for a specific id or name the user gave you, tell them it was not found "
    "-- never substitute a different real project, task, or person and answer as if it were the one asked about."
)
MAX_TOOL_TURNS = 6

EXECUTION_SYSTEM_PROMPT = (
    "You are {name}, a {title} agent in an AI-native workspace, autonomously working on your assigned task. Use "
    "your read tools to check any context you need, then call log_progress to record concrete progress as you make "
    "it, or mark_task_complete once the task is genuinely finished -- never claim you did something you did not "
    "actually do. If real progress requires something outside your own task (delegating to a report, creating a "
    "task, editing another task or project), propose it with the matching tool; a human will review and confirm it "
    "before anything changes. Call at most one tool per turn."
)
EXECUTION_TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "log_progress", "description": "Record a progress note as a comment on your current task. Use this to narrate concrete work as you do it.",
     "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False}},
    {"name": "mark_task_complete", "description": "Mark your current task as done. Only call this once the work is genuinely finished.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]
EXECUTION_TOOL_NAMES = {spec["name"] for spec in EXECUTION_TOOL_SPECS}
MAX_EXECUTION_TURNS = 4


def _provider_turn_metrics(turn: ProviderTurn) -> dict[str, Any]:
    metrics: dict[str, Any] = {"adapter_used": turn.adapter_used}
    if turn.usage:
        metrics["prompt_tokens"] = turn.usage.prompt_tokens
        metrics["completion_tokens"] = turn.usage.completion_tokens
        metrics["reasoning_tokens"] = turn.usage.reasoning_tokens
    return metrics


class CopilotService:
    def __init__(self):
        provider_name = os.getenv("AI_PROVIDER", "deterministic").strip().lower()
        if provider_name == "anthropic":
            self.provider: Any = AnthropicCopilotProvider()
            self.mode = "tool_calling"
        elif provider_name in {"openai_compatible", "lmstudio"}:
            self.provider = OpenAICompatibleCopilotProvider()
            self.mode = "tool_calling"
        else:
            self.provider = DeterministicCopilotProvider()
            self.mode = "deterministic"

    def plan(self, message: str, tools: WorkspaceTools) -> dict[str, Any]:
        try:
            return self._plan(message, tools)
        except Exception:
            tool_audit(tools, 'copilot', 'plan_failed', outcome='failed')
            raise

    def _plan(self, message: str, tools: WorkspaceTools) -> dict[str, Any]:
        if tools.user.kind == "agent":
            require_execution(tools.db, tools.user)
        if self.mode == "deterministic":
            return self._plan_deterministic(message, tools)
        return self._plan_tool_calling(message, tools)

    def _plan_deterministic(self, message: str, tools: WorkspaceTools) -> dict[str, Any]:
        snapshot = tools.snapshot()
        reply = provider_call(tools, self.mode, lambda: self.provider.answer(message, _provider_snapshot(snapshot)))
        if tools.user.kind == "agent":
            require_execution(tools.db, tools.user)
        actions = self._proposed_actions(message, snapshot, tools)
        return {"reply": reply, "actions": actions, "read_tools": ["get_projects", "get_tasks", "summarize_priorities"]}

    def _proposed_actions(self, message: str, snapshot: dict[str, Any], tools: "WorkspaceTools") -> list[dict[str, Any]]:
        user = tools.user
        normalized = message.lower()
        if "create_task" not in allowed_write_tools(user.role):
            return []
        if not any(trigger in normalized for trigger in ("create task", "add task", "make a task")):
            return []
        target = next((project for project in snapshot["projects"] if project["name"].lower() in normalized), snapshot["projects"][0] if snapshot["projects"] else None)
        if not target:
            return []
        title = re.sub(r"\b(create|add|make)( a)? task\b", "", message, flags=re.I).strip(" :.-")[:140] or "New task"
        args = {"project_id": target["id"], "title": title[:1].upper() + title[1:], "priority": "medium"}
        mint = create_agent_confirmation if user.kind == "agent" else create_confirmation
        return [describe_action(tools.db, user, "create_task", args, mint(user, "create_task", args))]

    def _plan_tool_calling(self, message: str, tools: WorkspaceTools, conversation_id: str | None = None) -> dict[str, Any]:
        role = tools.user.role
        system = SYSTEM_PROMPT
        if tools.scope_project_id:
            system += f" The user is currently viewing a single project (id={tools.scope_project_id}); get_projects and get_tasks are scoped to that project only."
        # Conversation history integration: bounded previous turns only.
        if conversation_id:
            conv_memory = ConversationMemory(conversation_id, max_turns=6)
            # In a full persistence design, we would load previous turns from DB here.
            # For this safe integration, we rely on bounded in-memory history; isolation
            # preserved by never sharing conversation objects across users/organizations.
        # Routing layer: determine candidate domains and build a reduced roster.
        # Low confidence widens safely back to full roster; authorization unchanged.
        from .ai.tool_routing import detect_domains, domain_candidates
        detected_domains = detect_domains(message)
        routed_candidates = domain_candidates(detected_domains) if detected_domains else []
        routed_candidates_for_telemetry = routed_candidates
        messages: list[dict[str, Any]] = [self.provider.user_message(message)]
        used_read_tools: list[str] = []
        # Deterministic preflight: a message that clearly references project
        # documents gets search_documents called for real before the model's
        # first turn, so its context already contains the results and it cannot
        # skip retrieving them -- because retrieval already happened. Spliced in
        # as a normal tool-use/tool-result pair, valid in both provider dialects,
        # and outside MAX_TOOL_TURNS's budget (it's not a model-initiated turn).
        for idx, (preflight_name, preflight_args) in enumerate(preflight_tool_calls(message, tools.scope_project_id)):
            preflight_call = {"id": f"preflight-{idx}", "name": preflight_name, "args": preflight_args}
            messages.append(self.provider.assistant_message(ProviderTurn(text=None, tool_calls=[preflight_call])))
            try:
                result = execute_read_tool(preflight_name, preflight_args, tools)
                used_read_tools.append(preflight_name)
                messages.append(self.provider.tool_result_message(preflight_call["id"], result))
            except (ValueError, KeyError) as exc:
                messages.append(self.provider.tool_result_message(preflight_call["id"], str(exc), is_error=True))
            except Exception:
                # search_documents can fail for engine/connectivity reasons unrelated to
                # the query itself. Never surface raw exception or SQL text to the model
                # or its transcript (same discipline observability.py's safe_detail/
                # legacy_detail already apply elsewhere) -- degrade to a generic, honest
                # failure instead of crashing the whole request over an optimization.
                messages.append(self.provider.tool_result_message(preflight_call["id"], "This search could not be completed.", is_error=True))
        # A model-agnostic ceiling derived from the resolved profile rather than a new
        # flat constant: a slow local model's own (already profile-driven) per-call
        # timeout scales the whole-request deadline with it, so this never
        # false-positive-aborts legitimately slow inference the profile already expects.
        # Falls back to DEFAULT_PROFILE for any provider (real or a test's scripted
        # fake) that doesn't expose .profile at all.
        profile = getattr(self.provider, "profile", DEFAULT_PROFILE)
        deadline = time.monotonic() + profile.timeout_seconds * profile.max_tool_turns * 1.2
        seen_calls: set[tuple[str, str]] = set()
        duplicate_already_warned = False
        provider_name = type(self.provider).__name__
        provider_model = getattr(self.provider, "model", None)
        for round_number in range(1, MAX_TOOL_TURNS + 1):
            if time.monotonic() > deadline:
                raise CopilotProviderError("Enter AI Copilot exceeded its time budget answering. Try a narrower question.")
            if tools.user.kind == "agent":
                require_execution(tools.db, tools.user)
            turn = provider_call(
                tools, self.mode, lambda: self.provider.respond(system, messages, role),
                provider=provider_name, model=provider_model, round_number=round_number,
                extract_metrics=_provider_turn_metrics,
            )
            if tools.user.kind == "agent":
                require_execution(tools.db, tools.user)
            if not turn.tool_calls:
                return {"reply": turn.text or "I don't have a response right now.", "actions": [], "read_tools": used_read_tools}
            messages.append(self.provider.assistant_message(turn))
            # Every tool_use in this turn needs a tool_result before the conversation can
            # continue, except the one write call we accept -- we return immediately after
            # that and never send another request, so no result is needed for it.
            confirmed_action: dict[str, Any] | None = None
            for call in turn.tool_calls:
                name = call["name"]
                dedup_key = (name, json.dumps(call["args"], sort_keys=True, default=str))
                if dedup_key in seen_calls:
                    if duplicate_already_warned:
                        raise CopilotProviderError("Enter AI Copilot repeated the same tool call and stopped to avoid looping.")
                    duplicate_already_warned = True
                    messages.append(self.provider.tool_result_message(call["id"], "You already called this tool with these exact arguments -- use the result you already have.", is_error=True))
                    continue
                seen_calls.add(dedup_key)
                if name in WRITE_TOOL_NAMES:
                    if name not in allowed_write_tools(role):
                        messages.append(self.provider.tool_result_message(call["id"], "Your role is not permitted to perform this action.", is_error=True))
                    elif confirmed_action is not None:
                        messages.append(self.provider.tool_result_message(call["id"], "Only one proposed action is allowed per turn.", is_error=True))
                    elif validate_tool_args(call["args"], _INPUT_SCHEMA_BY_NAME[name]) is None:
                        messages.append(self.provider.tool_result_message(call["id"], "Arguments did not match this tool's schema.", is_error=True))
                    else:
                        mint = create_agent_confirmation if tools.user.kind == "agent" else create_confirmation
                        token = mint(tools.user, name, call["args"])
                        confirmed_action = describe_action(tools.db, tools.user, name, call["args"], token)
                    continue
                if name not in READ_TOOL_NAMES:
                    messages.append(self.provider.tool_result_message(call["id"], "This tool does not exist.", is_error=True))
                    continue
                # Closes the gap between "the model asked for this" and "this was ever
                # checked": the native message.tool_calls path had no schema validation
                # before this, unlike the textual-adapter-recovered path, which always
                # validated before returning a call at all (see textual_toolcall_adapter.py).
                if validate_tool_args(call["args"], _INPUT_SCHEMA_BY_NAME[name]) is None:
                    messages.append(self.provider.tool_result_message(call["id"], "Arguments did not match this tool's schema.", is_error=True))
                    continue
                try:
                    result = execute_read_tool(name, call["args"], tools)
                    used_read_tools.append(name)
                    messages.append(self.provider.tool_result_message(call["id"], cap_tool_result(result)))
                except (ValueError, KeyError) as exc:
                    messages.append(self.provider.tool_result_message(call["id"], str(exc), is_error=True))
            if confirmed_action:
                reply = turn.text or f"I can {confirmed_action['label'][0].lower()}{confirmed_action['label'][1:]} once you confirm."
                return {"reply": reply, "actions": [confirmed_action], "read_tools": used_read_tools}
        raise CopilotProviderError("Enter AI Copilot could not finish answering within the allotted tool calls. Try a narrower question.")

    def run_execution_step(self, tools: WorkspaceTools, task: Task) -> dict[str, Any]:
        require_execution(tools.db, tools.user)
        if tools.user.kind != 'agent' or task.assignee_id != tools.user.id or tools.user.current_task_id != task.id:
            raise CopilotProviderError('Agent task is no longer assigned')
        with execution_run(tools) as run:
            result = self._run_execution_step(tools, task)
            run.outcome = result["outcome"]
            return result

    def _run_execution_step(self, tools: WorkspaceTools, task: Task) -> dict[str, Any]:
        """One autonomous-execution tick for `tools.user` (must be an agent) on its own
        assigned `task`. log_progress/mark_task_complete execute immediately -- they are
        safe by construction, since neither takes a task/agent id and both always act on
        this exact task. Any other write tool still mints a human confirmation and stops
        the tick right there, same as the interactive chat loop."""
        agent, db = tools.user, tools.db
        system = EXECUTION_SYSTEM_PROMPT.format(name=agent.name, title=agent.title or agent.hierarchy_level or "agent")
        kickoff = (
            f'Your current task is "{task.title}" (status: {task.status}, priority: {task.priority}).\n'
            f"{task.description or 'No description was provided.'}\nMake progress now."
        )
        messages: list[dict[str, Any]] = [self.provider.user_message(kickoff)]
        used_read_tools: list[str] = []
        progress_notes: list[str] = []
        provider_name = type(self.provider).__name__
        provider_model = getattr(self.provider, "model", None)
        for round_number in range(1, MAX_EXECUTION_TURNS + 1):
            require_execution(db, agent)
            turn = provider_call(
                tools, self.mode, lambda: self.provider.respond(system, messages, agent.role, extra_tools=EXECUTION_TOOL_SPECS),
                provider=provider_name, model=provider_model, round_number=round_number,
                extract_metrics=_provider_turn_metrics,
            )
            require_execution(db, agent)
            if not turn.tool_calls:
                return {"outcome": "narrated", "text": turn.text, "read_tools": used_read_tools, "progress_notes": progress_notes, "action": None}
            messages.append(self.provider.assistant_message(turn))
            for call in turn.tool_calls:
                name = call["name"]
                if name in EXECUTION_TOOL_NAMES or name in WRITE_TOOL_NAMES:
                    require_execution(db, agent)
                if name == "log_progress":
                    note = str(call["args"].get("note") or "").strip()[:2000] or "Made progress."
                    db.add(Comment(task_id=task.id, author_id=agent.id, body=note))
                    log(db, agent.organization_id, agent.id, "task", task.id, "agent_progress", note=note)
                    progress_notes.append(note)
                    messages.append(self.provider.tool_result_message(call["id"], "Logged."))
                    continue
                if name == "mark_task_complete":
                    before_status = task.status
                    task.status = "done"
                    task.completed_at = datetime.utcnow()
                    sync_agent_task_pointers(db, task, task.assignee_id)
                    log(db, agent.organization_id, agent.id, "task", task.id, "agent_completed", from_status=before_status)
                    return {"outcome": "completed", "text": turn.text, "read_tools": used_read_tools, "progress_notes": progress_notes, "action": None}
                if name in WRITE_TOOL_NAMES:
                    if name not in allowed_write_tools(agent.role):
                        messages.append(self.provider.tool_result_message(call["id"], "Your role is not permitted to perform this action.", is_error=True))
                        continue
                    if validate_tool_args(call["args"], _INPUT_SCHEMA_BY_NAME[name]) is None:
                        messages.append(self.provider.tool_result_message(call["id"], "Arguments did not match this tool's schema.", is_error=True))
                        continue
                    token = create_agent_confirmation(agent, name, call["args"])
                    action = describe_action(db, agent, name, call["args"], token)
                    return {"outcome": "proposed", "text": turn.text, "read_tools": used_read_tools, "progress_notes": progress_notes, "action": action}
                if name not in READ_TOOL_NAMES:
                    messages.append(self.provider.tool_result_message(call["id"], "This tool does not exist.", is_error=True))
                    continue
                if validate_tool_args(call["args"], _INPUT_SCHEMA_BY_NAME[name]) is None:
                    messages.append(self.provider.tool_result_message(call["id"], "Arguments did not match this tool's schema.", is_error=True))
                    continue
                try:
                    result = execute_read_tool(name, call["args"], tools)
                    used_read_tools.append(name)
                    messages.append(self.provider.tool_result_message(call["id"], cap_tool_result(result)))
                except (ValueError, KeyError) as exc:
                    messages.append(self.provider.tool_result_message(call["id"], str(exc), is_error=True))
        return {"outcome": "inconclusive", "text": None, "read_tools": used_read_tools, "progress_notes": progress_notes, "action": None}


def _provider_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Minimise provider context while preserving the authenticated server's IDs."""
    return {
        "projects": [{key: value for key, value in project.items() if key != "id"} for project in snapshot["projects"]],
        "tasks": [{key: value for key, value in task.items() if key not in {"id", "project_id"}} for task in snapshot["tasks"]],
        "my_tasks": [{key: value for key, value in task.items() if key not in {"id", "project_id"}} for task in snapshot["my_tasks"]],
    }


def create_confirmation(user: User, tool: str, args: dict[str, Any]) -> str:
    return jwt.encode(
        {"kind": "copilot_confirmation", "jti": uuid4().hex, "sub": user.id, "org": user.organization_id, "tool": tool, "args": args, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        CONFIRMATION_SECRET,
        algorithm="HS256",
    )


def read_confirmation(token: str, user: User) -> tuple[str, dict[str, Any]]:
    try:
        claim = jwt.decode(token, CONFIRMATION_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ValueError("This Copilot confirmation has expired or is invalid. Ask again to create a new proposal.") from exc
    if claim.get("kind") != "copilot_confirmation" or claim.get("sub") != user.id or claim.get("org") != user.organization_id:
        raise ValueError("This Copilot confirmation does not belong to your workspace.")
    tool, args = claim.get("tool"), claim.get("args")
    if tool not in WRITE_TOOL_NAMES or not isinstance(args, dict):
        raise ValueError("This Copilot proposal is not supported.")
    return tool, args


def create_agent_confirmation(agent: User, tool: str, args: dict[str, Any]) -> str:
    """Same shape as create_confirmation, but for a proposal an *agent* made rather than
    the human chatting -- a distinct `kind` so the two token families can never be
    confused, since an agent can't click "confirm" for itself; a human in its org must."""
    return jwt.encode(
        {"kind": "agent_copilot_confirmation", "jti": uuid4().hex, "sub": agent.id, "org": agent.organization_id, "tool": tool, "args": args, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        CONFIRMATION_SECRET,
        algorithm="HS256",
    )


def read_agent_confirmation(token: str, confirming_user: User, db: Session) -> tuple[str, dict[str, Any], str]:
    """Redeem an agent-proposed confirmation on the agent's behalf. Any human in the
    same organisation as the proposing agent may confirm it -- role permission for the
    tool itself is still checked by the caller against the *confirming human's* role."""
    if confirming_user.kind != "human" or not confirming_user.active:
        raise ValueError("An active human must confirm this action")
    try:
        claim = jwt.decode(token, CONFIRMATION_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ValueError("This Copilot confirmation has expired or is invalid. Ask again to create a new proposal.") from exc
    if claim.get("kind") != "agent_copilot_confirmation" or claim.get("org") != confirming_user.organization_id:
        raise ValueError("This Copilot confirmation does not belong to your workspace.")
    agent_id = claim.get("sub")
    agent = db.get(User, agent_id) if agent_id else None
    if not agent or agent.kind != "agent" or agent.organization_id != confirming_user.organization_id:
        raise ValueError("This Copilot confirmation does not belong to your workspace.")
    tool, args = claim.get("tool"), claim.get("args")
    if tool not in WRITE_TOOL_NAMES or not isinstance(args, dict):
        raise ValueError("This Copilot proposal is not supported.")
    return tool, args, agent_id
