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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error, request

import jwt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import SECRET
from .models import Activity, Comment, Notification, Project, Task, Team, User
from .services import log, sync_agent_task_pointers


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
            {"id": a.id, "action": a.action, "entity_type": a.entity_type, "detail": a.detail, "created_at": a.created_at.isoformat()}
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
    {"name": "search", "description": "Search projects and tasks by name, code, or title.",
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

# Which roles may propose (and later confirm) each write tool. Task- and comment-level
# work is open to any workspace member; team creation shapes the org chart, so it is
# admin-only. Enforced both when building the tool list offered to the model (so the
# Copilot doesn't propose something the user can't do) and again in /api/ai/confirm
# (the actual safety boundary -- a token minted before a role change must still be
# re-checked at execution time).
TOOL_ROLES: dict[str, frozenset[str]] = {
    "create_task": frozenset({"admin", "member"}),
    "update_task": frozenset({"admin", "member"}),
    "add_comment": frozenset({"admin", "member"}),
    "create_project": frozenset({"admin", "member"}),
    "update_project": frozenset({"admin", "member"}),
    "create_team": frozenset({"admin"}),
    # Only admin-equivalent identities may re-delegate work -- for agents, that's the
    # ceo/vp levels (see hierarchy.SENIOR_LEVELS), matching real reporting authority.
    "delegate_task": frozenset({"admin"}),
}


def allowed_write_tools(role: str) -> set[str]:
    return {name for name, roles in TOOL_ROLES.items() if role in roles}


def anthropic_tool_specs(role: str | None = None) -> list[dict[str, Any]]:
    allowed = allowed_write_tools(role) if role is not None else WRITE_TOOL_NAMES
    specs = [t for t in TOOL_SPECS if t["name"] in READ_TOOL_NAMES or t["name"] in allowed]
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in specs]


def openai_tool_specs(role: str | None = None) -> list[dict[str, Any]]:
    allowed = allowed_write_tools(role) if role is not None else WRITE_TOOL_NAMES
    specs = [t for t in TOOL_SPECS if t["name"] in READ_TOOL_NAMES or t["name"] in allowed]
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in specs]


def execute_read_tool(name: str, args: dict[str, Any], tools: WorkspaceTools) -> Any:
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
    raise KeyError(f"Unknown read tool {name!r}")


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
# Providers
# --------------------------------------------------------------------------

def _priority_tasks(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        (task for task in snapshot["my_tasks"] if task["status"] != "done"),
        key=lambda task: (rank.get(task["priority"], 3), task["due_date"] or "9999-12-31"),
    )


class DeterministicCopilotProvider:
    """Useful, testable offline Copilot behaviour for local development."""

    def answer(self, message: str, snapshot: dict[str, Any]) -> str:
        normalized = message.lower().strip()
        projects = snapshot["projects"]
        if any(term in normalized for term in ("what should", "work on today", "priorit", "my tasks")):
            tasks = _priority_tasks(snapshot)
            if not tasks:
                return "You have no open assigned tasks. A good next step is to review your project risks and agree the next milestone."
            items = "; ".join(
                f"{task['title']} ({task['priority'].title()} · {task['project_name']})" for task in tasks[:3]
            )
            return f"Today, focus on {items}. I ranked open work by priority and due date."
        if "summar" in normalized or "project" in normalized:
            match = next(
                (project for project in projects if project["name"].lower() in normalized or project["code"].lower() in normalized),
                None,
            )
            if match:
                task_count = len([task for task in snapshot["tasks"] if task["project_name"] == match["name"] and task["status"] != "done"])
                description = match["description"] or "No project description has been added."
                return f"{match['name']} is {match['status'].replace('_', ' ')} and {match['health'].replace('_', ' ')}. {description} It has {task_count} open task{'s' if task_count != 1 else ''}."
        at_risk = [project["name"] for project in projects if project["health"] == "at_risk"]
        if "risk" in normalized:
            return f"At-risk projects: {', '.join(at_risk) if at_risk else 'none'}. Ask me to summarize a project or plan a confirmed task."
        return "I can use your workspace data to list projects, summarize a project, or prioritize your assigned tasks. I can also propose a task, but I will never create it without your confirmation."


@dataclass
class ProviderTurn:
    """One model turn: free text and/or a batch of tool calls to act on."""

    text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class AnthropicCopilotProvider:
    """Structured tool-calling adapter for the Anthropic Messages API."""

    def __init__(self):
        self.base_url = os.getenv("AI_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.model = os.getenv("AI_MODEL", "claude-sonnet-5")
        self.api_key = os.getenv("AI_API_KEY", "")
        self.timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))
        self.version = os.getenv("AI_ANTHROPIC_VERSION", "2023-06-01")

    def respond(self, system: str, messages: list[dict[str, Any]], role: str | None = None, extra_tools: list[dict[str, Any]] | None = None) -> ProviderTurn:
        if not self.api_key:
            raise CopilotProviderError("AI_API_KEY is not configured. Set it to use AI_PROVIDER=anthropic.")
        body = json.dumps({
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": messages,
            "tools": anthropic_tool_specs(role) + list(extra_tools or []),
        }).encode()
        headers = {"Content-Type": "application/json", "x-api-key": self.api_key, "anthropic-version": self.version}
        outgoing = request.Request(f"{self.base_url}/v1/messages", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:  # nosec B310 -- URL is explicit operator configuration
                payload = json.loads(response.read().decode())
        except (error.URLError, ValueError) as exc:
            raise CopilotProviderError("The configured AI provider is unavailable. Check AI_PROVIDER settings and try again.") from exc
        if "error" in payload:
            raise CopilotProviderError(payload["error"].get("message", "The configured AI provider returned an error."))
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({"id": block.get("id"), "name": block.get("name"), "args": block.get("input") or {}})
        return ProviderTurn(text="\n".join(part for part in text_parts if part).strip() or None, tool_calls=tool_calls)

    def assistant_message(self, turn: ProviderTurn) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if turn.text:
            blocks.append({"type": "text", "text": turn.text})
        for call in turn.tool_calls:
            blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
        return {"role": "assistant", "content": blocks}

    def tool_result_message(self, tool_call_id: str, content: Any, is_error: bool = False) -> dict[str, Any]:
        text = content if isinstance(content, str) else json.dumps(content)
        block = {"type": "tool_result", "tool_use_id": tool_call_id, "content": text}
        if is_error:
            block["is_error"] = True
        return {"role": "user", "content": [block]}

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}


class OpenAICompatibleCopilotProvider:
    """Structured tool-calling adapter for LM Studio or any OpenAI-compatible endpoint."""

    def __init__(self):
        self.base_url = os.getenv("AI_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
        self.model = os.getenv("AI_MODEL", "local-model")
        self.api_key = os.getenv("AI_API_KEY", "")
        self.timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))

    def respond(self, system: str, messages: list[dict[str, Any]], role: str | None = None, extra_tools: list[dict[str, Any]] | None = None) -> ProviderTurn:
        extra = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in (extra_tools or [])]
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": openai_tool_specs(role) + extra,
            "tool_choice": "auto",
            "temperature": 0.2,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        outgoing = request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:  # nosec B310 -- URL is explicit operator configuration
                payload = json.loads(response.read().decode())
            choice = payload["choices"][0]["message"]
        except (error.URLError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise CopilotProviderError("The configured AI provider is unavailable. Check AI_PROVIDER settings and try again.") from exc
        text = (choice.get("content") or "").strip() or None
        tool_calls: list[dict[str, Any]] = []
        for call in choice.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            tool_calls.append({"id": call.get("id"), "name": fn.get("name"), "args": args})
        return ProviderTurn(text=text, tool_calls=tool_calls)

    def assistant_message(self, turn: ProviderTurn) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if turn.tool_calls:
            message["tool_calls"] = [
                {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"])}}
                for call in turn.tool_calls
            ]
        return message

    def tool_result_message(self, tool_call_id: str, content: Any, is_error: bool = False) -> dict[str, Any]:
        text = content if isinstance(content, str) else json.dumps(content)
        if is_error:
            text = f"Error: {text}"
        return {"role": "tool", "tool_call_id": tool_call_id, "content": text}

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}


SYSTEM_PROMPT = (
    "You are Enter AI Copilot, embedded in a project-management workspace. Use the read tools to ground every "
    "answer in this organisation's real data; never invent projects, tasks, or people. You may call at most one "
    "write tool per turn, from whichever write tools are available to you in this turn's tool list, to propose a "
    "change, but it will never execute automatically -- the user must explicitly confirm it afterwards. "
    "Whenever you call a write tool, also include a short plain-language explanation of the proposal in the same turn."
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
        if self.mode == "deterministic":
            return self._plan_deterministic(message, tools)
        return self._plan_tool_calling(message, tools)

    def _plan_deterministic(self, message: str, tools: WorkspaceTools) -> dict[str, Any]:
        snapshot = tools.snapshot()
        reply = self.provider.answer(message, _provider_snapshot(snapshot))
        actions = self._proposed_actions(message, snapshot, tools.user)
        return {"reply": reply, "actions": actions, "read_tools": ["get_projects", "get_tasks", "summarize_priorities"]}

    def _proposed_actions(self, message: str, snapshot: dict[str, Any], user: User) -> list[dict[str, Any]]:
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
        return [{"label": f"Create task: {args['title']}", "confirmation_token": mint(user, "create_task", args), "requires_confirmation": True}]

    def _plan_tool_calling(self, message: str, tools: WorkspaceTools) -> dict[str, Any]:
        role = tools.user.role
        system = SYSTEM_PROMPT
        if tools.scope_project_id:
            system += f" The user is currently viewing a single project (id={tools.scope_project_id}); get_projects and get_tasks are scoped to that project only."
        messages: list[dict[str, Any]] = [self.provider.user_message(message)]
        used_read_tools: list[str] = []
        for _ in range(MAX_TOOL_TURNS):
            turn = self.provider.respond(system, messages, role)
            if not turn.tool_calls:
                return {"reply": turn.text or "I don't have a response right now.", "actions": [], "read_tools": used_read_tools}
            messages.append(self.provider.assistant_message(turn))
            # Every tool_use in this turn needs a tool_result before the conversation can
            # continue, except the one write call we accept -- we return immediately after
            # that and never send another request, so no result is needed for it.
            confirmed_action: dict[str, Any] | None = None
            for call in turn.tool_calls:
                name = call["name"]
                if name in WRITE_TOOL_NAMES:
                    if name not in allowed_write_tools(role):
                        messages.append(self.provider.tool_result_message(call["id"], "Your role is not permitted to perform this action.", is_error=True))
                    elif confirmed_action is not None:
                        messages.append(self.provider.tool_result_message(call["id"], "Only one proposed action is allowed per turn.", is_error=True))
                    else:
                        mint = create_agent_confirmation if tools.user.kind == "agent" else create_confirmation
                        token = mint(tools.user, name, call["args"])
                        label = _describe_write_call(name, call["args"])
                        confirmed_action = {"label": label, "confirmation_token": token, "requires_confirmation": True}
                    continue
                if name not in READ_TOOL_NAMES:
                    messages.append(self.provider.tool_result_message(call["id"], "This tool does not exist.", is_error=True))
                    continue
                try:
                    result = execute_read_tool(name, call["args"], tools)
                    used_read_tools.append(name)
                    messages.append(self.provider.tool_result_message(call["id"], result))
                except (ValueError, KeyError) as exc:
                    messages.append(self.provider.tool_result_message(call["id"], str(exc), is_error=True))
            if confirmed_action:
                reply = turn.text or f"I can {confirmed_action['label'][0].lower()}{confirmed_action['label'][1:]} once you confirm."
                return {"reply": reply, "actions": [confirmed_action], "read_tools": used_read_tools}
        raise CopilotProviderError("Enter AI Copilot could not finish answering within the allotted tool calls. Try a narrower question.")

    def run_execution_step(self, tools: WorkspaceTools, task: Task) -> dict[str, Any]:
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
        for _ in range(MAX_EXECUTION_TURNS):
            turn = self.provider.respond(system, messages, agent.role, extra_tools=EXECUTION_TOOL_SPECS)
            if not turn.tool_calls:
                return {"outcome": "narrated", "text": turn.text, "read_tools": used_read_tools, "progress_notes": progress_notes, "action": None}
            messages.append(self.provider.assistant_message(turn))
            for call in turn.tool_calls:
                name = call["name"]
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
                    token = create_agent_confirmation(agent, name, call["args"])
                    action = {"label": _describe_write_call(name, call["args"]), "confirmation_token": token, "requires_confirmation": True}
                    return {"outcome": "proposed", "text": turn.text, "read_tools": used_read_tools, "progress_notes": progress_notes, "action": action}
                if name not in READ_TOOL_NAMES:
                    messages.append(self.provider.tool_result_message(call["id"], "This tool does not exist.", is_error=True))
                    continue
                try:
                    result = execute_read_tool(name, call["args"], tools)
                    used_read_tools.append(name)
                    messages.append(self.provider.tool_result_message(call["id"], result))
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
        {"kind": "copilot_confirmation", "sub": user.id, "org": user.organization_id, "tool": tool, "args": args, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        SECRET,
        algorithm="HS256",
    )


def read_confirmation(token: str, user: User) -> tuple[str, dict[str, Any]]:
    try:
        claim = jwt.decode(token, SECRET, algorithms=["HS256"])
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
        {"kind": "agent_copilot_confirmation", "sub": agent.id, "org": agent.organization_id, "tool": tool, "args": args, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        SECRET,
        algorithm="HS256",
    )


def read_agent_confirmation(token: str, confirming_user: User, db: Session) -> tuple[str, dict[str, Any], str]:
    """Redeem an agent-proposed confirmation on the agent's behalf. Any human in the
    same organisation as the proposing agent may confirm it -- role permission for the
    tool itself is still checked by the caller against the *confirming human's* role."""
    try:
        claim = jwt.decode(token, SECRET, algorithms=["HS256"])
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
