from uuid import uuid4

from fastapi.testclient import TestClient

from app.copilot import (
    READ_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    CopilotProviderError,
    CopilotService,
    ProviderTurn,
    TOOL_SPECS,
    WorkspaceTools,
    anthropic_tool_specs,
    execute_read_tool,
    openai_tool_specs,
    read_confirmation,
)
from app.database import SessionLocal
from app.main import app
from app.models import User


def _login_and_load_user():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        user_id = login.json()["user"]["id"]
    db = SessionLocal()
    user = db.get(User, user_id)
    return db, user


def test_tool_specs_cover_read_and_write_surface_with_valid_shapes():
    names = {spec["name"] for spec in TOOL_SPECS}
    assert READ_TOOL_NAMES | WRITE_TOOL_NAMES == names
    assert READ_TOOL_NAMES.isdisjoint(WRITE_TOOL_NAMES)
    for spec in TOOL_SPECS:
        assert spec["description"]
        assert spec["input_schema"]["type"] == "object"

    anthropic_specs = anthropic_tool_specs()
    assert {spec["name"] for spec in anthropic_specs} == names
    openai_specs = openai_tool_specs()
    assert {spec["function"]["name"] for spec in openai_specs} == names


def test_execute_read_tool_dispatches_every_declared_read_tool():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        assert isinstance(execute_read_tool("get_projects", {}, tools), list)
        assert isinstance(execute_read_tool("get_tasks", {}, tools), list)
        assert isinstance(execute_read_tool("get_my_tasks", {}, tools), list)
        assert isinstance(execute_read_tool("get_teams", {}, tools), list)
        assert isinstance(execute_read_tool("get_users", {}, tools), list)
        assert isinstance(execute_read_tool("get_notifications", {}, tools), list)
        assert isinstance(execute_read_tool("get_activity", {}, tools), list)
        result = execute_read_tool("search", {"query": "Launch"}, tools)
        assert any(p["code"] == "LAUNCH" for p in result["projects"])
        try:
            execute_read_tool("get_comments", {"task_id": "does-not-exist"}, tools)
            assert False, "expected ValueError for a task outside the workspace"
        except ValueError:
            pass
        try:
            execute_read_tool("not_a_real_tool", {}, tools)
            assert False, "expected KeyError for an unknown tool"
        except KeyError:
            pass
    finally:
        db.close()


class _ScriptedProvider:
    """Replays a fixed sequence of ProviderTurn objects, mirroring a real tool-calling provider."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.seen_messages = []

    def respond(self, system, messages, role=None):
        self.seen_messages.append(list(messages))
        self.seen_role = role
        return self._turns.pop(0)

    def assistant_message(self, turn):
        return {"role": "assistant", "tool_calls": turn.tool_calls, "text": turn.text}

    def tool_result_message(self, tool_call_id, content, is_error=False):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content, "is_error": is_error}

    def user_message(self, text):
        return {"role": "user", "content": text}


def _service_with(provider):
    service = CopilotService.__new__(CopilotService)
    service.provider = provider
    service.mode = "tool_calling"
    return service


def test_tool_calling_loop_executes_read_tools_then_returns_final_text():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "call_1", "name": "get_projects", "args": {}}]),
            ProviderTurn(text="You have three projects.", tool_calls=[]),
        ])
        result = _service_with(provider).plan("What projects do we have?", tools)
        assert result["reply"] == "You have three projects."
        assert result["actions"] == []
        assert result["read_tools"] == ["get_projects"]
        assert len(provider.seen_messages) == 2
    finally:
        db.close()


def test_tool_calling_loop_stops_on_write_tool_and_issues_a_confirmation_token():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([
            ProviderTurn(
                text="I'll create that task once you confirm.",
                tool_calls=[{"id": "call_1", "name": "create_task", "args": {"project_id": "proj-1", "title": "Ship the release"}}],
            ),
        ])
        result = _service_with(provider).plan("Create a task to ship the release", tools)
        assert result["reply"] == "I'll create that task once you confirm."
        assert len(result["actions"]) == 1
        action = result["actions"][0]
        assert action["requires_confirmation"] is True
        assert action["label"] == "Create task: Ship the release"
        tool, args = read_confirmation(action["confirmation_token"], user)
        assert tool == "create_task"
        assert args == {"project_id": "proj-1", "title": "Ship the release"}
    finally:
        db.close()


def test_tool_calling_loop_reports_unknown_and_out_of_scope_tool_errors_without_crashing():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "call_1", "name": "get_comments", "args": {"task_id": "missing"}}]),
            ProviderTurn(text=None, tool_calls=[{"id": "call_2", "name": "delete_everything", "args": {}}]),
            ProviderTurn(text="I could not complete that request.", tool_calls=[]),
        ])
        result = _service_with(provider).plan("Do something unsupported", tools)
        assert result["reply"] == "I could not complete that request."
        first_result_message = provider.seen_messages[1][-1]
        assert first_result_message["is_error"] is True
    finally:
        db.close()


def test_tool_calling_loop_gives_up_after_the_turn_budget():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        endless = ProviderTurn(text=None, tool_calls=[{"id": "call_1", "name": "get_projects", "args": {}}])
        provider = _ScriptedProvider([endless] * 10)
        try:
            _service_with(provider).plan("Loop forever", tools)
            assert False, "expected CopilotProviderError once the tool-turn budget is exhausted"
        except CopilotProviderError:
            pass
    finally:
        db.close()


def test_confirm_endpoint_executes_every_generalised_write_tool():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        user_id = client.get("/api/me", headers=headers).json()["user"]["id"]
        projects = client.get("/api/projects", headers=headers).json()
        project_id = projects[0]["id"]
        task_id = client.get(f"/api/projects/{project_id}/tasks", headers=headers).json()[0]["id"]

        from app.copilot import create_confirmation
        from app.models import User as UserModel

        db = SessionLocal()
        user = db.get(UserModel, user_id)
        suffix = uuid4().hex[:8]

        # /api/ai/confirm dispatches to the target route function directly, so the response
        # carries the confirm route's own status code (200), not the target route's decorator.
        team_token = create_confirmation(user, "create_team", {"name": "Tool-Calling Test Team " + suffix})
        created_team = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": team_token})
        assert created_team.status_code == 200
        assert created_team.json()["name"] == "Tool-Calling Test Team " + suffix

        project_token = create_confirmation(user, "create_project", {"name": "Tool-Calling Test Project " + suffix, "code": "TC" + suffix[:4].upper()})
        created_project = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": project_token})
        assert created_project.status_code == 200

        update_token = create_confirmation(user, "update_project", {"project_id": project_id, "health": "at_risk"})
        updated_project = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": update_token})
        assert updated_project.status_code == 200
        assert updated_project.json()["health"] == "at_risk"

        comment_token = create_confirmation(user, "add_comment", {"task_id": task_id, "body": "Checked in via Copilot"})
        added_comment = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": comment_token})
        assert added_comment.status_code == 200
        assert added_comment.json()["body"] == "Checked in via Copilot"

        db.close()
