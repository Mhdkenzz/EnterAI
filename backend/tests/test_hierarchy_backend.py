from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.copilot import ProviderTurn, WorkspaceTools, read_agent_confirmation
from app.copilot import CopilotService
from app.database import SessionLocal
from app.hierarchy import MAX_HIERARCHY_AGENTS, desired_counts
from app.main import app
from app.models import Organization, User


def _admin_headers_and_org():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
    db = SessionLocal()
    org = db.scalar(select(Organization))
    return headers, org, db


def _reset_hierarchy(client, headers):
    """Tests run against a shared demo DB; zero the hierarchy out first so each test
    starts from a known, empty tree regardless of what earlier tests configured."""
    client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 0, "directors_per_vp": 0, "managers_per_director": 0, "workers_per_manager": 0})


def test_desired_counts_multiply_top_down_and_zero_out_below_a_zero_level():
    assert desired_counts(2, 3, 4, 5) == {"ceo": 1, "vp": 2, "director": 6, "senior_manager": 24, "worker": 120}
    assert desired_counts(0, 3, 4, 5) == {"ceo": 0, "vp": 0, "director": 0, "senior_manager": 0, "worker": 0}


def test_hierarchy_config_update_requires_admin_role():
    headers, org, db = _admin_headers_and_org()
    try:
        suffix = uuid4().hex[:8]
        member = User(organization_id=org.id, name="Member", email=f"m-{suffix}@demo.enterai.com", password_hash=hash_password("x"), role="member")
        db.add(member); db.commit()
        with TestClient(app) as client:
            login = client.post("/api/auth/login", json={"email": member.email, "password": "x"})
            member_headers = {"Authorization": "Bearer " + login.json()["token"]}
            response = client.patch("/api/hierarchy-config", headers=member_headers, json={"vp_count": 1})
            assert response.status_code == 403
    finally:
        db.close()


def test_hierarchy_config_update_rejects_configurations_over_the_agent_cap():
    headers, _org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            response = client.patch("/api/hierarchy-config", headers=headers, json={
                "vp_count": 10, "directors_per_vp": 10, "managers_per_director": 10, "workers_per_manager": 10,
            })
        assert response.status_code == 422
        assert str(MAX_HIERARCHY_AGENTS) in response.json()["detail"]
    finally:
        db.close()


def test_hierarchy_config_reconciles_agents_to_the_configured_shape_and_shrinks_cleanly():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            grown = client.patch("/api/hierarchy-config", headers=headers, json={
                "vp_count": 2, "directors_per_vp": 1, "managers_per_director": 1, "workers_per_manager": 1,
            })
            assert grown.status_code == 200
            assert grown.json() == {"vp_count": 2, "directors_per_vp": 1, "managers_per_director": 1, "workers_per_manager": 1}

            agents_response = client.get("/api/agents", headers=headers)
        agents = agents_response.json()
        by_level: dict[str, list] = {}
        for a in agents:
            by_level.setdefault(a["hierarchy_level"], []).append(a)
        assert len(by_level["ceo"]) == 1
        assert len(by_level["vp"]) == 2
        assert len(by_level["director"]) == 2
        assert len(by_level["senior_manager"]) == 2
        assert len(by_level["worker"]) == 2
        # every director's parent is one of the two VPs; every VP's parent is the one CEO
        ceo_id = by_level["ceo"][0]["id"]
        assert all(vp["parent_agent_id"] == ceo_id for vp in by_level["vp"])
        vp_ids = {vp["id"] for vp in by_level["vp"]}
        assert all(director["parent_agent_id"] in vp_ids for director in by_level["director"])

        with TestClient(app) as client:
            shrunk = client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1})
            assert shrunk.status_code == 200
            remaining = client.get("/api/agents", headers=headers).json()
        remaining_by_level: dict[str, list] = {}
        for a in remaining:
            remaining_by_level.setdefault(a["hierarchy_level"], []).append(a)
        assert len(remaining_by_level["ceo"]) == 1
        assert len(remaining_by_level["vp"]) == 1
        assert len(remaining_by_level["director"]) == 1
        assert len(remaining_by_level["senior_manager"]) == 1
        assert len(remaining_by_level["worker"]) == 1
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_shrinking_the_hierarchy_unassigns_tasks_held_by_removed_agents():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 2})
            agents = client.get("/api/agents", headers=headers).json()
            vp = next(a for a in agents if a["hierarchy_level"] == "vp")
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Owned by a VP that will be removed"}).json()
            assigned = client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": vp["id"]})
            assert assigned.status_code == 200
            assert assigned.json()["assignee"]["id"] == vp["id"]

            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 0})
            after = client.get(f"/api/projects/{project['id']}/tasks", headers=headers).json()
        after_task = next(t for t in after if t["id"] == task["id"])
        assert after_task["assignee"] is None
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_assign_agent_endpoint_sets_and_moves_current_task():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1, "directors_per_vp": 1})
            agents = client.get("/api/agents", headers=headers).json()
            vp = next(a for a in agents if a["hierarchy_level"] == "vp")
            director = next(a for a in agents if a["hierarchy_level"] == "director")
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Reassignable task"}).json()

            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": vp["id"]})
            after_first = client.get("/api/agents", headers=headers).json()
            vp_after_first = next(a for a in after_first if a["id"] == vp["id"])
            assert vp_after_first["current_task"]["id"] == task["id"]

            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": director["id"]})
            after_second = client.get("/api/agents", headers=headers).json()
            vp_after_second = next(a for a in after_second if a["id"] == vp["id"])
            director_after_second = next(a for a in after_second if a["id"] == director["id"])
        assert vp_after_second["current_task"] is None
        assert director_after_second["current_task"]["id"] == task["id"]
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_completing_a_task_updates_the_agents_current_and_last_completed_pointers():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1})
            worker = next(a for a in client.get("/api/agents", headers=headers).json() if a["hierarchy_level"] == "vp")
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Finish me"}).json()
            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": worker["id"]})

            client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
            after = next(a for a in client.get("/api/agents", headers=headers).json() if a["id"] == worker["id"])
        assert after["current_task"] is None
        assert after["last_completed_task"]["id"] == task["id"]
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_agent_chat_persists_conversation_and_is_readable_afterwards():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1})
            agent = next(a for a in client.get("/api/agents", headers=headers).json() if a["hierarchy_level"] == "vp")

            sent = client.post(f"/api/agents/{agent['id']}/messages", headers=headers, json={"message": "What is at risk?"})
            assert sent.status_code == 201
            assert "risk" in sent.json()["reply"].lower() or "none" in sent.json()["reply"].lower()

            history = client.get(f"/api/agents/{agent['id']}/messages", headers=headers).json()
        assert [m["role"] for m in history] == ["user", "agent"]
        assert history[0]["body"] == "What is at risk?"
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_get_direct_reports_lists_an_agents_children():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 2})
            agents = client.get("/api/agents", headers=headers).json()
        ceo = next(a for a in agents if a["hierarchy_level"] == "ceo")
        vp_ids = {a["id"] for a in agents if a["hierarchy_level"] == "vp"}

        ceo_row = db.get(User, ceo["id"])
        tools = WorkspaceTools(db, ceo_row)
        reports = tools.get_direct_reports()
        assert {r["id"] for r in reports} == vp_ids
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


class _ScriptedProvider:
    def __init__(self, turns):
        self._turns = list(turns)

    def respond(self, system, messages, role=None):
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


def test_delegate_task_tool_end_to_end_agent_proposes_human_confirms():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1, "directors_per_vp": 1})
            agents = client.get("/api/agents", headers=headers).json()
            ceo = next(a for a in agents if a["hierarchy_level"] == "ceo")
            vp = next(a for a in agents if a["hierarchy_level"] == "vp")
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Delegate me"}).json()
            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": ceo["id"]})

        ceo_row = db.get(User, ceo["id"])
        tools = WorkspaceTools(db, ceo_row)
        provider = _ScriptedProvider([
            ProviderTurn(text="Delegating to my VP.", tool_calls=[{"id": "call_1", "name": "delegate_task", "args": {"task_id": task["id"], "agent_id": vp["id"]}}]),
        ])
        result = _service_with(provider).plan("Delegate this to my VP", tools)
        token = result["actions"][0]["confirmation_token"]
        import pytest
        with pytest.raises(ValueError, match="active human"):
            read_agent_confirmation(token, ceo_row, db)
        human = db.scalar(select(User).where(
            User.organization_id == org.id, User.email == "admin@demo.enterai.com",
            User.kind == "human", User.active.is_(True)))
        assert human is not None
        tool, args, proposer_agent_id = read_agent_confirmation(token, human, db)
        assert tool == "delegate_task" and proposer_agent_id == ceo["id"]

        with TestClient(app) as client:
            confirmed = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
        assert confirmed.status_code == 200
        assert confirmed.json()["assignee"]["id"] == vp["id"]
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_a_worker_level_agent_cannot_propose_delegate_task():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1, "directors_per_vp": 1, "managers_per_director": 1, "workers_per_manager": 1})
            worker = next(a for a in client.get("/api/agents", headers=headers).json() if a["hierarchy_level"] == "worker")
        worker_row = db.get(User, worker["id"])
        assert worker_row.role == "member"
        tools = WorkspaceTools(db, worker_row)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "call_1", "name": "delegate_task", "args": {"task_id": "whatever", "agent_id": "whatever"}}]),
            ProviderTurn(text="I can't delegate work.", tool_calls=[]),
        ])
        result = _service_with(provider).plan("Delegate this somewhere", tools)
        assert result["actions"] == []
        assert result["reply"] == "I can't delegate work."
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()
