from fastapi.testclient import TestClient
from sqlalchemy import select

from app.copilot import CopilotProviderError, CopilotService, ProviderTurn
from app.database import SessionLocal
from app.execution import MAX_CONSECUTIVE_FAILURES, run_execution_tick
import app.execution as execution
from app.main import app
from app.models import Comment, Organization, Task, User


def _admin_headers_and_org():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
    db = SessionLocal()
    org = db.scalar(select(Organization))
    return headers, org, db


def _reset_hierarchy(client, headers):
    client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 0, "directors_per_vp": 0, "managers_per_director": 0, "workers_per_manager": 0})


def _agent_with_task(client, headers, db, vp_count=1, directors_per_vp=0):
    """Creates a fresh CEO (+ optional director-line) agent and gives the CEO a task."""
    _reset_hierarchy(client, headers)
    client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": vp_count, "directors_per_vp": directors_per_vp})
    agents = client.get("/api/agents", headers=headers).json()
    ceo = next(a for a in agents if a["hierarchy_level"] == "ceo")
    project = client.get("/api/projects", headers=headers).json()[0]
    task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Autonomous work item", "description": "Do the thing."}).json()
    client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": ceo["id"]})
    return db.get(User, ceo["id"]), db.get(Task, task["id"])


class _ScriptedProvider:
    """Mirrors a real tool-calling provider; ignores extra_tools beyond recording them."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def respond(self, system, messages, role=None, extra_tools=None):
        self.calls += 1
        return self._turns.pop(0)

    def assistant_message(self, turn):
        return {"role": "assistant", "tool_calls": turn.tool_calls, "text": turn.text}

    def tool_result_message(self, tool_call_id, content, is_error=False):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content, "is_error": is_error}

    def user_message(self, text):
        return {"role": "user", "content": text}


class _FailingProvider(_ScriptedProvider):
    def __init__(self):
        super().__init__([])

    def respond(self, system, messages, role=None, extra_tools=None):
        self.calls += 1
        raise CopilotProviderError("offline for this test")


def _service_with(provider):
    service = CopilotService.__new__(CopilotService)
    service.provider = provider
    service.mode = "tool_calling"
    return service


def test_background_loop_never_starts_under_the_deterministic_default():
    """Regression test: the scheduler must not spawn a task (and therefore a thread and
    DB session on its first tick) when the configured provider can't tool-call anyway --
    every TestClient startup across the whole suite hits this exact path, and it
    previously added real background-thread/DB overhead to every single test."""
    execution.stop_background_loop()
    assert execution._background_task is None
    execution.start_background_loop()
    try:
        assert execution._background_task is None
    finally:
        execution.stop_background_loop()


def test_background_loop_starts_when_a_tool_calling_provider_is_configured(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_API_KEY", "test-key")
    execution.stop_background_loop()
    try:
        execution.start_background_loop()
        assert execution._background_task is not None
    finally:
        execution.stop_background_loop()
        assert execution._background_task is None


def test_execution_tick_is_a_noop_under_the_deterministic_default():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        db.refresh(agent)
        stepped = run_execution_tick(db)  # no `service` override -> real CopilotService() -> deterministic by default
        assert stepped == 0
        db.refresh(agent)
        assert agent.last_execution_at is None
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_log_progress_executes_immediately_as_a_task_comment_and_agent_message():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "c1", "name": "log_progress", "args": {"note": "Reviewed the requirements."}}]),
            ProviderTurn(text="Made a first pass; will continue next cycle.", tool_calls=[]),
        ])
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 1

        db.refresh(agent)
        db.refresh(task)
        assert agent.consecutive_task_failures == 0
        assert agent.last_execution_at is not None
        comments = db.scalars(select(Comment).where(Comment.task_id == task.id)).all()
        assert any(c.body == "Reviewed the requirements." and c.author_id == agent.id for c in comments)

        with TestClient(app) as client:
            history = client.get(f"/api/agents/{agent.id}/messages", headers=headers).json()
        assert any(m["role"] == "agent" and "continue next cycle" in m["body"] for m in history)
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_mark_task_complete_finishes_the_task_and_moves_the_agents_pointers():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        provider = _ScriptedProvider([
            ProviderTurn(text="Done -- shipped it.", tool_calls=[{"id": "c1", "name": "mark_task_complete", "args": {}}]),
        ])
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 1

        db.refresh(agent)
        db.refresh(task)
        assert task.status == "done"
        assert agent.current_task_id is None
        assert agent.last_completed_task_id == task.id
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_a_proposed_write_during_execution_mints_a_confirmation_instead_of_executing():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
            project = client.get("/api/projects", headers=headers).json()[0]
        provider = _ScriptedProvider([
            ProviderTurn(text="I'll also file a follow-up task.", tool_calls=[{"id": "c1", "name": "create_task", "args": {"project_id": project["id"], "title": "Follow-up from autonomous work"}}]),
        ])
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 1

        titles = [t.title for t in db.scalars(select(Task)).all()]
        assert "Follow-up from autonomous work" not in titles  # never executes without confirmation

        with TestClient(app) as client:
            history = client.get(f"/api/agents/{agent.id}/messages", headers=headers).json()
        assert any("follow-up" in m["body"].lower() for m in history)
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_a_worker_level_agent_cannot_have_an_admin_only_tool_executed_during_execution():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1, "directors_per_vp": 1, "managers_per_director": 1, "workers_per_manager": 1})
            worker = next(a for a in client.get("/api/agents", headers=headers).json() if a["hierarchy_level"] == "worker")
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Worker task"}).json()
            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": worker["id"]})
        worker_row = db.get(User, worker["id"])
        assert worker_row.role == "member"
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "c1", "name": "create_team", "args": {"name": "Escalated team"}}]),
            ProviderTurn(text="I can't create a team, so I'll keep working on my task instead.", tool_calls=[]),
        ])
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 1
        assert not db.scalars(select(Task).where(Task.title == "Escalated team")).all()

        with TestClient(app) as client:
            history = client.get(f"/api/agents/{worker['id']}/messages", headers=headers).json()
        assert any("keep working" in m["body"] for m in history)
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_circuit_breaker_stops_polling_an_agent_after_repeated_provider_failures():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        provider = _FailingProvider()
        service = _service_with(provider)

        # cooldown_seconds=0 opts out of the anti-double-step window, which would
        # otherwise skip these deliberately back-to-back ticks; the circuit breaker
        # is what is under test here, not the tick spacing.
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            assert run_execution_tick(db, service, cooldown_seconds=0) == 1
        db.refresh(agent)
        assert agent.consecutive_task_failures == MAX_CONSECUTIVE_FAILURES
        calls_before = provider.calls

        assert run_execution_tick(db, service, cooldown_seconds=0) == 0  # circuit breaker excludes it now
        assert provider.calls == calls_before  # never even attempted

        with TestClient(app) as client:
            status = next(a for a in client.get("/api/agents", headers=headers).json() if a["id"] == agent.id)
        assert status["is_blocked"] is True
        assert status["is_working"] is False
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_daily_call_limit_skips_an_agent_that_has_exhausted_it():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        for _ in range(execution.DAILY_CALL_LIMIT):
            execution._daily_limiter.allow(agent.id)
        provider = _FailingProvider()  # would raise if ever called
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 0
        assert provider.calls == 0
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_reassigning_a_task_resets_the_circuit_breaker():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        agent.consecutive_task_failures = MAX_CONSECUTIVE_FAILURES
        db.commit()

        with TestClient(app) as client:
            client.post(f"/api/tasks/{task.id}/assign-agent", headers=headers, json={"agent_id": agent.id})
        db.refresh(agent)
        assert agent.consecutive_task_failures == 0
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_messaging_an_agent_resets_the_circuit_breaker():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        agent.consecutive_task_failures = MAX_CONSECUTIVE_FAILURES
        db.commit()

        with TestClient(app) as client:
            client.post(f"/api/agents/{agent.id}/messages", headers=headers, json={"message": "How's it going?"})
        db.refresh(agent)
        assert agent.consecutive_task_failures == 0
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()
