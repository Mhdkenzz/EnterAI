from fastapi.testclient import TestClient
from sqlalchemy import select

from app.copilot import CopilotProviderError, CopilotService, ProviderTurn
from app.database import SessionLocal
from app.execution import MAX_CONSECUTIVE_FAILURES, run_execution_tick
import app.execution as execution
from app.main import app
from app.models import AgentAccessGrant, AuditEvent, Comment, Organization, Project, Task, User
from app.ratelimit import SlidingWindowRateLimiter
import pytest


@pytest.fixture(autouse=True)
def isolate_agent_daily_budget(monkeypatch):
    """Each test gets a fresh rolling budget despite reusing seeded agent IDs."""
    monkeypatch.setattr(execution, "_daily_limiter", SlidingWindowRateLimiter(execution.DAILY_CALL_LIMIT, 86400))


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
    agent = db.get(User, ceo["id"])
    # The shared demo CEO may still have earlier test work queued. Select this
    # fixture's task explicitly so each execution test targets its own record.
    agent.current_task_id = task["id"]
    agent.last_execution_at = None  # don't carry scheduler cooldown across tests
    agent.daily_limit_reached_at = None
    db.commit()
    return agent, db.get(Task, task["id"])


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


def test_provider_rate_limit_pauses_without_blocking_the_task():
    from app.ratelimit import AIProviderRateLimitExceeded

    class RateLimitedProvider(_ScriptedProvider):
        def respond(self, *args, **kwargs):
            raise AIProviderRateLimitExceeded()

    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        service = _service_with(RateLimitedProvider([]))
        for _ in range(MAX_CONSECUTIVE_FAILURES + 1):
            run_execution_tick(db, service, cooldown_seconds=0)
        db.refresh(agent)
        db.refresh(task)
        assert agent.consecutive_task_failures == 0
        assert task.status == "in_progress"
    finally:
        if 'task' in locals():
            task.status = "done"
            db.commit()
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def _service_with(provider):
    service = CopilotService.__new__(CopilotService)
    service.provider = provider
    service.mode = "tool_calling"
    return service


def _grant_agent(db, org_id, agent_id, resource_type, resource_id, permission):
    existing = db.scalar(select(AgentAccessGrant).where(
        AgentAccessGrant.organization_id == org_id, AgentAccessGrant.agent_id == agent_id,
        AgentAccessGrant.resource_type == resource_type, AgentAccessGrant.resource_id == resource_id,
        AgentAccessGrant.permission == permission,
    ))
    if existing:
        existing.revoked_at = existing.revoked_by = None
        db.commit()
        return
    admin_id = db.scalar(select(User.id).where(
        User.organization_id == org_id, User.kind == "human", User.role == "admin", User.active.is_(True),
    ))
    db.add(AgentAccessGrant(organization_id=org_id, agent_id=agent_id,
        resource_type=resource_type, resource_id=resource_id, permission=permission,
        granted_by=admin_id))
    db.commit()


def test_background_loop_never_starts_under_the_deterministic_default():
    """Regression test: the scheduler must not spawn a task (and therefore a thread and
    DB session on its first tick) when the configured provider can't tool-call anyway --
    every TestClient startup across the whole suite hits this exact path, and it
    previously added real background-thread/DB overhead to every single test."""
    import asyncio
    asyncio.run(execution.stop_background_loop())
    assert execution._background_task is None
    execution.start_background_loop()
    try:
        assert execution._background_task is None
    finally:
        asyncio.run(execution.stop_background_loop())


def test_background_loop_starts_when_a_tool_calling_provider_is_configured(monkeypatch):
    import asyncio
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_API_KEY", "test-key")
    async def exercise_lifecycle():
        await execution.stop_background_loop()
        execution.start_background_loop()
        assert execution._background_task is not None
        await execution.stop_background_loop()
        assert execution._background_task is None
    asyncio.run(exercise_lifecycle())


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


def test_delegation_persists_before_the_next_provider_turn(monkeypatch):
    def fail_loudly(_bind, _org, _agent, error):
        raise error
    monkeypatch.setattr(execution, "scheduler_failure", fail_loudly)
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        report = db.scalar(select(User).where(User.parent_agent_id == agent.id, User.retired_at.is_(None)))
        _grant_agent(db, org.id, agent.id, "tool", "delegate_subtask", "use")
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "delegate", "name": "delegate_subtask", "args": {"agent_id": report.id, "title": "Prepare checklist", "description": "Draft the checklist for review.", "priority": "medium"}}]),
            ProviderTurn(text="Delegated the checklist.", tool_calls=[]),
        ])
        assert run_execution_tick(db, _service_with(provider)) == 1
        db.expire_all()
        child = db.scalar(select(Task).where(Task.parent_id == task.id, Task.title == "Prepare checklist"))
        assert child is not None and child.assignee_id == report.id
        assert provider.calls == 2
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_unexpected_execution_failures_eventually_block_the_task():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        class BrokenService:
            mode = "tool_calling"
            def run_execution_step(self, tools, assigned_task):
                raise RuntimeError("failure")
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            assert run_execution_tick(db, BrokenService(), cooldown_seconds=0) == 1
        db.refresh(task)
        db.refresh(agent)
        assert task.status == "blocked"
        assert agent.current_task_id == task.id
        assert run_execution_tick(db, BrokenService(), cooldown_seconds=0) == 0
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_manager_can_read_several_reports_before_completing():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        turns = [ProviderTurn(text=None, tool_calls=[{"id": str(i), "name": "get_comments", "args": {"task_id": task.id}}]) for i in range(5)]
        turns.append(ProviderTurn(text="Reviewed.", tool_calls=[{"id": "done", "name": "mark_task_complete", "args": {"result": "Reviewed the team reports and reconciled the schedule, budget, and next steps."}}]))
        assert run_execution_tick(db, _service_with(_ScriptedProvider(turns)), cooldown_seconds=0) == 1
        db.refresh(task)
        assert task.status == "done"
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_agent_can_raise_blocker_without_deadlocking_audit():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        provider = _ScriptedProvider([ProviderTurn(text=None, tool_calls=[{"id": "block", "name": "raise_blocker", "args": {"reason": "A required approval is missing; an administrator must provide it."}}])])
        assert run_execution_tick(db, _service_with(provider), cooldown_seconds=0) == 1
        db.refresh(task)
        assert task.status == "blocked"
        assert "approval" in task.blocked_reason
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_agent_resumes_with_its_own_recorded_progress():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        db.add(Comment(task_id=task.id, author_id=agent.id, body="Prepared the schedule for the next step."))
        db.commit()
        class CapturingProvider(_ScriptedProvider):
            def respond(self, system, messages, role=None, extra_tools=None):
                self.initial = messages[0]["content"]
                return super().respond(system, messages, role, extra_tools)
        provider = CapturingProvider([ProviderTurn(text="Continuing later.", tool_calls=[])])
        assert run_execution_tick(db, _service_with(provider), cooldown_seconds=0) == 1
        assert "Prepared the schedule for the next step." in provider.initial
        assert "untrusted task data" in provider.initial
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_background_agents_complete_independent_tasks_concurrently(monkeypatch):
    import threading
    from uuid import uuid4
    db = SessionLocal()
    try:
        org = Organization(name="Parallel test", slug=f"parallel-{uuid4().hex}")
        db.add(org)
        db.flush()
        project = Project(organization_id=org.id, name="Parallel", code="PARALLEL")
        db.add(project)
        db.flush()
        agents = [User(organization_id=org.id, name=f"Parallel {i}", email=f"{uuid4().hex}@agents.internal", password_hash="unused", kind="agent", hierarchy_level="worker", role="member") for i in range(3)]
        db.add_all(agents)
        db.flush()
        tasks = [Task(project_id=project.id, title=f"Parallel task {i}", assignee_id=agent.id) for i, agent in enumerate(agents)]
        db.add_all(tasks)
        db.commit()
        ids = {agent.id for agent in agents}
        barrier = threading.Barrier(3, timeout=10)
        class ParallelProvider(_ScriptedProvider):
            def respond(self, system, messages, role=None, extra_tools=None):
                barrier.wait()
                return ProviderTurn(text="Done.", tool_calls=[{"id": "done", "name": "mark_task_complete", "args": {"result": "Completed the assigned work and recorded a concrete result for review."}}])
        monkeypatch.setattr(execution, "_eligible_agents", lambda session: session.scalars(select(User).where(User.id.in_(ids))).all())
        monkeypatch.setattr(execution, "CopilotService", lambda *args: _service_with(ParallelProvider([])))
        assert execution._run_background_tick() == 3
        db.expire_all()
        assert all(db.get(Task, task.id).status == "done" for task in tasks)
    finally:
        db.close()


def test_completion_without_a_result_does_not_finish_the_task():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "c1", "name": "mark_task_complete", "args": {}}]),
            ProviderTurn(text="I still need to produce the deliverable.", tool_calls=[]),
        ])
        assert run_execution_tick(db, _service_with(provider)) == 1
        db.refresh(task)
        assert task.status != "done"
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
            ProviderTurn(text="Done.", tool_calls=[{"id": "c1", "name": "mark_task_complete", "args": {"result": "Drafted the release checklist: verify rollout, monitor errors, and prepare rollback steps."}}]),
        ])
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 1

        db.refresh(agent)
        db.refresh(task)
        assert task.status == "done"
        assert db.scalar(select(Comment).where(Comment.task_id == task.id, Comment.author_id == agent.id)).body.startswith("Completed work:")
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
        _grant_agent(db, org.id, agent.id, "project", project["id"], "write")
        _grant_agent(db, org.id, agent.id, "tool", "create_task", "use")
        provider = _ScriptedProvider([
            ProviderTurn(text="I'll also file a follow-up task.", tool_calls=[{"id": "c1", "name": "create_task", "args": {"project_id": project["id"], "title": "Follow-up from autonomous work"}}]),
        ])
        stepped = run_execution_tick(db, _service_with(provider))
        assert stepped == 1

        titles = [t.title for t in db.scalars(select(Task)).all()]
        assert "Follow-up from autonomous work" not in titles  # never executes without confirmation
        db.refresh(task)
        assert task.status == "review"
        assert run_execution_tick(db, _service_with(_ScriptedProvider([]))) == 0

        with TestClient(app) as client:
            history = client.get(f"/api/agents/{agent.id}/messages", headers=headers).json()
            proposals = client.get("/api/agent-proposals", headers=headers).json()
            agent_state = next(a for a in client.get("/api/agents", headers=headers).json() if a["id"] == agent.id)
        assert any("follow-up" in m["body"].lower() for m in history)
        assert agent_state["is_awaiting_approval"] is True
        assert agent_state["is_working"] is False
        proposal = next(p for p in proposals if p["task_id"] == task.id)
        assert proposal["label"]
        with TestClient(app) as client:
            response = client.post("/api/ai/confirm", headers=headers,
                                   json={"confirmation_token": proposal["confirmation_token"]})
            assert response.status_code == 200, response.text
        db.refresh(task)
        assert task.status == "in_progress"
        assert "Follow-up from autonomous work" in [t.title for t in db.scalars(select(Task)).all()]
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_rejecting_an_execution_proposal_pauses_its_task():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
            project = client.get("/api/projects", headers=headers).json()[0]
        _grant_agent(db, org.id, agent.id, "project", project["id"], "write")
        _grant_agent(db, org.id, agent.id, "tool", "create_task", "use")
        provider = _ScriptedProvider([
            ProviderTurn(text="I can add a follow-up.", tool_calls=[{"id": "c1", "name": "create_task", "args": {"project_id": project["id"], "title": "Rejected follow-up"}}]),
        ])
        assert run_execution_tick(db, _service_with(provider)) == 1
        with TestClient(app) as client:
            proposals = client.get("/api/agent-proposals", headers=headers).json()
            proposal = next(p for p in proposals if p["task_id"] == task.id)
            response = client.post(f"/api/agent-proposals/{proposal['id']}/reject", headers=headers)
            assert response.status_code == 200, response.text
        db.refresh(task)
        assert task.status == "blocked"
        assert "rejected" in task.blocked_reason.lower()
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_an_agent_stops_and_blocks_when_it_attempts_an_ungranted_tool():
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
            ProviderTurn(text="This response must never be requested after access was denied.", tool_calls=[]),
        ])
        stepped = run_execution_tick(db, _service_with(provider), only_agent_id=worker["id"])
        assert stepped == 1
        assert not db.scalars(select(Task).where(Task.title == "Escalated team")).all()
        task_row = db.get(Task, task["id"])
        assert task_row.status == "blocked"
        assert "needs additional access" in task_row.blocked_reason
        assert provider.calls == 1
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.organization_id == org.id, AuditEvent.action == "access_denied"))

        with TestClient(app) as client:
            history = client.get(f"/api/agents/{worker['id']}/messages", headers=headers).json()
        assert not any("must never be requested" in m["body"] for m in history)
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
        db.refresh(task)
        assert agent.consecutive_task_failures == MAX_CONSECUTIVE_FAILURES
        assert task.status == "blocked"
        assert task.blocked_reason and "failed repeatedly" in task.blocked_reason
        calls_before = provider.calls

        assert run_execution_tick(db, service, cooldown_seconds=0) == 0  # circuit breaker excludes it now
        assert provider.calls == calls_before  # never even attempted

        with TestClient(app) as client:
            status = next(a for a in client.get("/api/agents", headers=headers).json() if a["id"] == agent.id)
            reopened = client.patch(f"/api/tasks/{task.id}", headers=headers, json={"status": "todo"})
        assert reopened.status_code == 200
        db.refresh(agent)
        assert agent.consecutive_task_failures == 0
        assert status["is_blocked"] is True
        assert status["is_working"] is False
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_repeated_narration_without_progress_blocks_the_task_for_human_review():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
        provider = _ScriptedProvider([
            ProviderTurn(text="I am still considering this task.", tool_calls=[])
            for _ in range(MAX_CONSECUTIVE_FAILURES)
        ])
        service = _service_with(provider)
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            assert run_execution_tick(db, service, cooldown_seconds=0) == 1
        db.refresh(agent)
        db.refresh(task)
        assert task.status == "blocked"
        assert task.blocked_reason and "without recording progress" in task.blocked_reason
        assert agent.consecutive_task_failures == MAX_CONSECUTIVE_FAILURES
        assert run_execution_tick(db, service, cooldown_seconds=0) == 0
        assert provider.calls == MAX_CONSECUTIVE_FAILURES
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
        db.refresh(agent)
        assert agent.daily_limit_reached_at is not None
        with TestClient(app) as client:
            status = next(a for a in client.get("/api/agents", headers=headers).json() if a["id"] == agent.id)
            notifications = client.get("/api/notifications", headers=headers).json()
        assert status["is_rate_limited"] is True
        assert any("daily limit" in notice["title"].lower() for notice in notifications)
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_reassigning_a_task_resets_the_circuit_breaker():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, task = _agent_with_task(client, headers, db)
            target = next(a for a in client.get("/api/agents", headers=headers).json() if a["id"] != agent.id)
        target_agent = db.get(User, target["id"])
        agent.consecutive_task_failures = MAX_CONSECUTIVE_FAILURES
        target_agent.consecutive_task_failures = MAX_CONSECUTIVE_FAILURES
        db.commit()

        with TestClient(app) as client:
            response = client.post(f"/api/tasks/{task.id}/assign-agent", headers=headers, json={"agent_id": target_agent.id})
            assert response.status_code == 200
        db.refresh(agent)
        db.refresh(target_agent)
        assert agent.consecutive_task_failures == MAX_CONSECUTIVE_FAILURES
        assert target_agent.consecutive_task_failures == 0
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
