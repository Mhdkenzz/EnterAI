"""Scoped workspace tools and provider adapters for the Enter AI Copilot.

Providers receive a serialised, organisation-scoped snapshot only.  They never
receive a database session, credentials, or a way to mutate application data.
All reads and writes remain in authenticated application code.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error, request

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, Task, User


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

    def __init__(self, db: Session, user: User):
        self.db = db
        self.user = user

    def get_projects(self) -> list[dict[str, Any]]:
        projects = self.db.scalars(
            select(Project)
            .where(Project.organization_id == self.user.organization_id)
            .order_by(Project.created_at.desc())
        ).all()
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
        rows = self.db.execute(
            select(Task, Project)
            .join(Project, Project.id == Task.project_id)
            .where(Project.organization_id == self.user.organization_id)
            .order_by(Task.due_date)
        ).all()
        return [_task_data(task, project) for task, project in rows]

    def get_my_tasks(self) -> list[dict[str, Any]]:
        tasks = self.get_tasks()
        assigned_ids = set(self.db.scalars(select(Task.id).where(Task.assignee_id == self.user.id)).all())
        return [task for task in tasks if task["id"] in assigned_ids]

    def snapshot(self) -> dict[str, Any]:
        projects = self.get_projects()
        tasks = self.get_tasks()
        assigned_ids = set(self.db.scalars(select(Task.id).where(Task.assignee_id == self.user.id)).all())
        return {"projects": projects, "tasks": tasks, "my_tasks": [task for task in tasks if task["id"] in assigned_ids]}


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


class OpenAICompatibleCopilotProvider:
    """Adapter for LM Studio or any OpenAI-compatible hosted chat endpoint."""

    def __init__(self):
        self.base_url = os.getenv("AI_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
        self.model = os.getenv("AI_MODEL", "local-model")
        self.api_key = os.getenv("AI_API_KEY", "")
        self.timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))

    def answer(self, message: str, snapshot: dict[str, Any]) -> str:
        instructions = (
            "You are Enter AI Copilot. Answer only from the JSON workspace snapshot. "
            "Do not claim to take actions, invent data, expose IDs, or give database instructions. "
            "Keep the answer concise and useful."
        )
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": f"Workspace snapshot:\n{json.dumps(snapshot)}\n\nQuestion: {message}"},
                ],
                "temperature": 0.2,
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        outgoing = request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:  # nosec B310 -- URL is explicit operator configuration
                payload = json.loads(response.read().decode())
            content = payload["choices"][0]["message"]["content"].strip()
            if not content:
                raise CopilotProviderError("The configured AI provider returned an empty response.")
            return content[:6000]
        except (error.URLError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise CopilotProviderError("The configured AI provider is unavailable. Check AI_PROVIDER settings and try again.") from exc


class CopilotService:
    def __init__(self):
        provider_name = os.getenv("AI_PROVIDER", "deterministic").strip().lower()
        self.provider = OpenAICompatibleCopilotProvider() if provider_name in {"openai_compatible", "lmstudio"} else DeterministicCopilotProvider()

    def plan(self, message: str, tools: WorkspaceTools) -> dict[str, Any]:
        snapshot = tools.snapshot()
        reply = self.provider.answer(message, _provider_snapshot(snapshot))
        actions = self._proposed_actions(message, snapshot, tools.user)
        return {"reply": reply, "actions": actions, "read_tools": ["get_projects", "get_tasks", "summarize_priorities"]}

    def _proposed_actions(self, message: str, snapshot: dict[str, Any], user: User) -> list[dict[str, Any]]:
        normalized = message.lower()
        if not any(trigger in normalized for trigger in ("create task", "add task", "make a task")):
            return []
        target = next((project for project in snapshot["projects"] if project["name"].lower() in normalized), snapshot["projects"][0] if snapshot["projects"] else None)
        if not target:
            return []
        title = re.sub(r"\b(create|add|make)( a)? task\b", "", message, flags=re.I).strip(" :.-")[:140] or "New task"
        args = {"project_id": target["id"], "title": title[:1].upper() + title[1:], "priority": "medium"}
        return [{"label": f"Create task: {args['title']}", "confirmation_token": create_confirmation(user, "create_task", args), "requires_confirmation": True}]


def _provider_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Minimise provider context while preserving the authenticated server's IDs."""
    return {
        "projects": [{key: value for key, value in project.items() if key != "id"} for project in snapshot["projects"]],
        "tasks": [{key: value for key, value in task.items() if key not in {"id", "project_id"}} for task in snapshot["tasks"]],
        "my_tasks": [{key: value for key, value in task.items() if key not in {"id", "project_id"}} for task in snapshot["my_tasks"]],
    }


def create_confirmation(user: User, tool: str, args: dict[str, Any]) -> str:
    secret = os.getenv("JWT_SECRET", "dev-secret-change-me")
    return jwt.encode(
        {"kind": "copilot_confirmation", "sub": user.id, "org": user.organization_id, "tool": tool, "args": args, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        secret,
        algorithm="HS256",
    )


def read_confirmation(token: str, user: User) -> tuple[str, dict[str, Any]]:
    secret = os.getenv("JWT_SECRET", "dev-secret-change-me")
    try:
        claim = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ValueError("This Copilot confirmation has expired or is invalid. Ask again to create a new proposal.") from exc
    if claim.get("kind") != "copilot_confirmation" or claim.get("sub") != user.id or claim.get("org") != user.organization_id:
        raise ValueError("This Copilot confirmation does not belong to your workspace.")
    tool, args = claim.get("tool"), claim.get("args")
    if tool not in {"create_task", "update_task"} or not isinstance(args, dict):
        raise ValueError("This Copilot proposal is not supported.")
    return tool, args
