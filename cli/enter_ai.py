#!/usr/bin/env python3
"""Command-line client for the Enter AI project-management API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://localhost:8000/api"
CONFIG_PATH = Path.home() / ".config" / "enter-ai" / "config.json"


class CLIError(Exception):
    """An expected, user-facing command error."""


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)


def clean_url(value: str) -> str:
    return value.rstrip("/")


@dataclass
class Client:
    base_url: str
    token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        file_path: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data: bytes | None = None

        if file_path:
            data, content_type = multipart_body(file_path)
            headers["Content-Type"] = content_type
        elif payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        request = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode()
                return json.loads(body) if body else None
        except HTTPError as error:
            body = error.read().decode(errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body or error.reason
            raise CLIError(f"API returned {error.code}: {detail}") from error
        except URLError as error:
            raise CLIError(f"Cannot reach Enter AI at {self.base_url}: {error.reason}") from error


def multipart_body(file_path: str) -> tuple[bytes, str]:
    path = Path(file_path)
    if not path.is_file():
        raise CLIError(f"Attachment not found: {path}")
    boundary = "----EnterAIUploadBoundary"
    pieces = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(pieces), f"multipart/form-data; boundary={boundary}"


def print_value(value: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, default=str))
        return
    if value is None:
        return
    if isinstance(value, list):
        if not value:
            print("No results.")
            return
        if all(isinstance(item, dict) for item in value):
            print_table(value)
            return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (list, dict)):
                print(f"{label(key)}:")
                print_value(item)
            else:
                print(f"{label(key)}: {display(item)}")
        return
    print(value)


def print_table(rows: list[dict[str, Any]]) -> None:
    keys = list(dict.fromkeys(key for row in rows for key in row))
    preferred = [key for key in ("id", "name", "code", "title", "status", "priority", "health", "progress", "read", "description", "due_date") if key in keys]
    keys = preferred + [key for key in keys if key not in preferred and key not in {"owner", "assignee", "created_at", "updated_at", "avatar", "href", "body", "labels", "detail"}]
    keys = keys[:8]
    widths = {key: max(len(label(key)), *(len(display(row.get(key, ""))) for row in rows)) for key in keys}
    print("  ".join(label(key).ljust(widths[key]) for key in keys))
    print("  ".join("-" * widths[key] for key in keys))
    for row in rows:
        print("  ".join(display(row.get(key, "")).ljust(widths[key]) for key in keys))


def label(value: str) -> str:
    return value.replace("_", " ").title()


def display(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or "-")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value).replace("\n", " ")[:48]


def confirm(prompt: str, args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def require(value: str | None, prompt: str, secret: bool = False) -> str:
    if value:
        return value
    value = getpass(prompt) if secret else input(prompt)
    if not value:
        raise CLIError("A value is required.")
    return value


def parse_labels(value: str | None) -> list[str] | None:
    return [item.strip() for item in value.split(",") if item.strip()] if value is not None else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enter-ai", description="Manage Enter AI from your terminal.")
    parser.add_argument("--api-url", help="API root (default: http://localhost:8000/api)")
    parser.add_argument("--token", help="Override the saved access token")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--yes", action="store_true", help="Skip the local confirmation prompt for write commands")
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth", help="Sign in, register, or manage credentials")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login", help="Sign in and store a token")
    login.add_argument("--email")
    login.add_argument("--password")
    register = auth_commands.add_parser("register", help="Create an organization and admin account")
    register.add_argument("--organization")
    register.add_argument("--name")
    register.add_argument("--email")
    register.add_argument("--password")
    auth_commands.add_parser("logout", help="Remove the saved token")
    auth_commands.add_parser("whoami", help="Show the active account")

    commands.add_parser("dashboard", help="Show dashboard data")
    projects = commands.add_parser("projects", help="Manage projects")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list", help="List projects")
    show = project_commands.add_parser("show", help="Show a project")
    show.add_argument("project_id")
    create = project_commands.add_parser("create", help="Create a project")
    create.add_argument("--name", required=True); create.add_argument("--code", required=True)
    create.add_argument("--description"); create.add_argument("--team-id"); create.add_argument("--health", default="on_track")
    create.add_argument("--color", default="#8b5cf6"); create.add_argument("--due-date")
    project_tasks = project_commands.add_parser("tasks", help="List a project's tasks")
    project_tasks.add_argument("project_id")

    tasks = commands.add_parser("tasks", help="Manage tasks, subtasks, comments, and attachments")
    task_commands = tasks.add_subparsers(dest="task_command", required=True)
    task_create = task_commands.add_parser("create", help="Create a task or subtask")
    task_create.add_argument("--project-id", required=True); task_create.add_argument("--title", required=True)
    task_create.add_argument("--description"); task_create.add_argument("--parent-id"); task_create.add_argument("--status", default="todo")
    task_create.add_argument("--priority", default="medium"); task_create.add_argument("--assignee-id"); task_create.add_argument("--due-date"); task_create.add_argument("--labels")
    update = task_commands.add_parser("update", help="Update a task")
    update.add_argument("task_id"); update.add_argument("--title"); update.add_argument("--description"); update.add_argument("--status")
    update.add_argument("--priority"); update.add_argument("--assignee-id"); update.add_argument("--due-date"); update.add_argument("--labels")
    delete = task_commands.add_parser("delete", help="Delete a task")
    delete.add_argument("task_id")
    comments = task_commands.add_parser("comments", help="List task comments")
    comments.add_argument("task_id")
    comment = task_commands.add_parser("comment", help="Add a task comment")
    comment.add_argument("task_id"); comment.add_argument("body")
    attach = task_commands.add_parser("attach", help="Upload a file attachment")
    attach.add_argument("task_id"); attach.add_argument("file")

    teams = commands.add_parser("teams", help="List or create teams")
    team_commands = teams.add_subparsers(dest="team_command", required=True)
    team_commands.add_parser("list", help="List teams")
    team_create = team_commands.add_parser("create", help="Create a team")
    team_create.add_argument("--name", required=True); team_create.add_argument("--description")
    commands.add_parser("users", help="List organization users")

    notifications = commands.add_parser("notifications", help="View or mark notifications read")
    notification_commands = notifications.add_subparsers(dest="notification_command", required=True)
    notification_commands.add_parser("list", help="List notifications")
    notification_read = notification_commands.add_parser("read", help="Mark a notification read")
    notification_read.add_argument("notification_id")
    activity = commands.add_parser("activity", help="Show activity history")
    activity.add_argument("--limit", type=int, default=20)
    search = commands.add_parser("search", help="Search projects and tasks")
    search.add_argument("query")
    commands.add_parser("risks", help="List at-risk projects")

    ai = commands.add_parser("ai", help="Plan work with Enter AI")
    ai_commands = ai.add_subparsers(dest="ai_command", required=True)
    ask = ai_commands.add_parser("ask", help="Ask AI to read work or propose actions")
    ask.add_argument("message")
    ask.add_argument("--confirm", action="store_true", help="Run proposed actions after an explicit confirmation prompt")
    return parser


def task_payload(args: argparse.Namespace, creating: bool = False) -> dict[str, Any]:
    fields = ("title", "description", "status", "priority", "assignee_id", "due_date")
    values = {field: getattr(args, field) for field in fields if creating or getattr(args, field) is not None}
    labels = parse_labels(args.labels)
    if labels is not None:
        values["labels"] = labels
    if creating:
        values["project_id"] = args.project_id
        values["parent_id"] = args.parent_id
    return values


def execute(args: argparse.Namespace, client: Client) -> Any:
    command = args.command
    if command == "auth":
        if args.auth_command == "login":
            result = client.request("POST", "/auth/login", {"email": require(args.email, "Email: "), "password": require(args.password, "Password: ", True)})
            persist_token(result["token"], client.base_url)
            return {"message": f"Signed in as {result['user']['name']}", "organization": result["organization"]["name"]}
        if args.auth_command == "register":
            result = client.request("POST", "/auth/register", {"organization_name": require(args.organization, "Organization: "), "name": require(args.name, "Name: "), "email": require(args.email, "Email: "), "password": require(args.password, "Password: ", True)})
            persist_token(result["token"], client.base_url)
            return {"message": f"Created {result['organization']['name']} and signed in", "user": result["user"]["name"]}
        if args.auth_command == "logout":
            config = load_config(); config.pop("token", None); save_config(config)
            return {"message": "Signed out."}
        return client.request("GET", "/me")

    ensure_authenticated(client)
    if command == "dashboard": return client.request("GET", "/dashboard")
    if command == "projects":
        if args.project_command == "list": return client.request("GET", "/projects")
        if args.project_command == "show": return client.request("GET", f"/projects/{args.project_id}")
        if args.project_command == "tasks": return client.request("GET", f"/projects/{args.project_id}/tasks")
        payload = {"name": args.name, "code": args.code, "description": args.description, "team_id": args.team_id, "health": args.health, "color": args.color, "due_date": args.due_date}
        return write(args, "Create this project?", client, "POST", "/projects", payload)
    if command == "tasks":
        if args.task_command == "create": return write(args, "Create this task?", client, "POST", "/tasks", task_payload(args, True))
        if args.task_command == "update":
            payload = task_payload(args)
            if not payload: raise CLIError("Provide at least one task field to update.")
            return write(args, "Update this task?", client, "PATCH", f"/tasks/{args.task_id}", payload)
        if args.task_command == "delete": return write(args, "Permanently delete this task?", client, "DELETE", f"/tasks/{args.task_id}")
        if args.task_command == "comments": return client.request("GET", f"/tasks/{args.task_id}/comments")
        if args.task_command == "comment": return write(args, "Add this comment?", client, "POST", f"/tasks/{args.task_id}/comments", {"body": args.body})
        return write(args, "Upload this attachment?", client, "POST", f"/tasks/{args.task_id}/attachments", file_path=args.file)
    if command == "teams":
        if args.team_command == "list": return client.request("GET", "/teams")
        return write(args, "Create this team?", client, "POST", "/teams", {"name": args.name, "description": args.description})
    if command == "users": return client.request("GET", "/users")
    if command == "notifications":
        if args.notification_command == "list": return client.request("GET", "/notifications")
        return write(args, "Mark this notification as read?", client, "PATCH", f"/notifications/{args.notification_id}/read", {})
    if command == "activity": return client.request("GET", "/activity")[:args.limit]
    if command == "search": return client.request("GET", f"/search?{urlencode({'q': args.query})}")
    if command == "risks": return client.request("GET", "/risks")
    if command == "ai":
        plan = client.request("POST", "/ai/plan", {"message": args.message})
        if not args.confirm or not plan.get("actions"):
            return plan
        if not confirm("Run all proposed AI actions?", args):
            return {"message": "No changes made.", "plan": plan}
        results = [client.request("POST", "/ai/confirm", {"tool": action["tool"], "args": action["args"]}) for action in plan["actions"]]
        return {"message": plan["reply"], "results": results}
    raise CLIError("Unsupported command.")


def write(args: argparse.Namespace, prompt: str, client: Client, method: str, path: str, payload: dict[str, Any] | None = None, file_path: str | None = None) -> Any:
    if not confirm(prompt, args):
        return {"message": "No changes made."}
    return client.request(method, path, payload, file_path)


def persist_token(token: str, base_url: str) -> None:
    config = load_config(); config.update({"token": token, "api_url": base_url}); save_config(config)


def ensure_authenticated(client: Client) -> None:
    if not client.token:
        raise CLIError("Sign in first: enter-ai auth login")


def normalize_global_args(argv: list[str]) -> list[str]:
    """Allow global output and connection flags before or after a subcommand."""
    globals_first: list[str] = []
    command_args: list[str] = []
    index = 0
    value_flags = {"--api-url", "--token"}
    boolean_flags = {"--json", "--yes"}
    while index < len(argv):
        item = argv[index]
        if item in boolean_flags:
            globals_first.append(item)
        elif item in value_flags:
            if index + 1 >= len(argv):
                command_args.append(item)
            else:
                globals_first.extend((item, argv[index + 1]))
                index += 1
        else:
            command_args.append(item)
        index += 1
    return globals_first + command_args


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(normalize_global_args(argv or sys.argv[1:]))
    config = load_config()
    base_url = clean_url(args.api_url or os.getenv("ENTER_AI_API_URL") or config.get("api_url") or DEFAULT_API_URL)
    token = args.token or os.getenv("ENTER_AI_TOKEN") or config.get("token")
    try:
        result = execute(args, Client(base_url, token))
        print_value(result, args.json)
        return 0
    except CLIError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
