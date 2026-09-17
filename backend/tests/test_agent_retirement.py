"""Retention policy: retiring an agent must never delete it. Identity, task
history, message history, and the audit trail all have to survive -- a retired
agent just becomes permanently unable to receive new work, execute, or show up
as active. See app/hierarchy.py:retire_agent for the implementation contract."""
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.database import SessionLocal
from app.main import app
from app.models import AgentMessage, Task, User

from test_hierarchy_backend import _admin_headers_and_org, _reset_hierarchy


def _one_worker(client, headers):
    _reset_hierarchy(client, headers)
    client.patch("/api/hierarchy-config", headers=headers, json={
        "vp_count": 1, "directors_per_vp": 1, "managers_per_director": 1, "workers_per_manager": 1,
    })
    agents = client.get("/api/agents", headers=headers).json()
    return next(a for a in agents if a["hierarchy_level"] == "worker")


def test_retiring_an_agent_blocks_new_work_but_keeps_its_row_and_history():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            worker = _one_worker(client, headers)
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Done before retirement"}).json()
            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": worker["id"]})
            client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
            client.post(f"/api/agents/{worker['id']}/messages", headers=headers, json={"message": "hello before retirement"})

            retire = client.post(f"/api/admin/agents/{worker['id']}/retire", headers=headers)
            assert retire.status_code == 200
            body = retire.json()
            assert body["retired"] is True and body["retired_at"]

            # Retiring twice is rejected, not silently accepted.
            assert client.post(f"/api/admin/agents/{worker['id']}/retire", headers=headers).status_code == 409

            # Gone from the default listing...
            listed = client.get("/api/agents", headers=headers).json()
            assert all(a["id"] != worker["id"] for a in listed)
            # ...but still visible, and marked retired, for audit/history purposes.
            with_retired = client.get("/api/agents", headers=headers, params={"include_retired": True}).json()
            found = next(a for a in with_retired if a["id"] == worker["id"])
            assert found["retired"] is True

            # New work is refused.
            other_task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Should not reach a retiree"}).json()
            assign = client.post(f"/api/tasks/{other_task['id']}/assign-agent", headers=headers, json={"agent_id": worker["id"]})
            assert assign.status_code == 409
            # The same policy applies to the plain assignee_id field on task create/update.
            direct_create = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "Direct assignee_id path", "assignee_id": worker["id"]})
            assert direct_create.status_code == 409
            direct_update = client.patch(f"/api/tasks/{other_task['id']}", headers=headers, json={"assignee_id": worker["id"]})
            assert direct_update.status_code == 409
            message = client.post(f"/api/agents/{worker['id']}/messages", headers=headers, json={"message": "still there?"})
            assert message.status_code == 409

            # Its conversation history and completed-task assignment survive untouched.
            history = client.get(f"/api/agents/{worker['id']}/messages", headers=headers).json()
            assert any(m["body"] == "hello before retirement" for m in history)
            completed = client.get(f"/api/projects/{project['id']}/tasks", headers=headers).json()
            completed_task = next(t for t in completed if t["id"] == task["id"])
            assert completed_task["assignee"]["id"] == worker["id"]

        with SessionLocal() as fresh:
            row = fresh.get(User, worker["id"])
            assert row is not None
            assert row.retired_at is not None
            assert row.execution_enabled is False
            assert fresh.scalar(select(AgentMessage).where(AgentMessage.agent_id == worker["id"])) is not None
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_retired_agent_cannot_be_re_enabled_for_execution():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            worker = _one_worker(client, headers)
            client.post(f"/api/admin/agents/{worker['id']}/retire", headers=headers)
            reenable = client.patch(f"/api/admin/agents/{worker['id']}/execution", headers=headers, json={"execution_enabled": True})
            assert reenable.status_code == 409
            with SessionLocal() as fresh:
                assert fresh.get(User, worker["id"]).execution_enabled is False
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_only_a_human_admin_can_retire_an_agent():
    headers, org, db = _admin_headers_and_org()
    try:
        suffix = uuid4().hex[:8]
        member = User(organization_id=org.id, name="Member", email=f"retire-member-{suffix}@demo.enterai.com", password_hash=hash_password("x"), role="member")
        db.add(member); db.commit()
        with TestClient(app) as client:
            worker = _one_worker(client, headers)
            login = client.post("/api/auth/login", json={"email": member.email, "password": "x"})
            member_headers = {"Authorization": "Bearer " + login.json()["token"]}
            response = client.post(f"/api/admin/agents/{worker['id']}/retire", headers=member_headers)
            assert response.status_code == 403
            with SessionLocal() as fresh:
                assert fresh.get(User, worker["id"]).retired_at is None
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_shrinking_the_hierarchy_retires_descendants_instead_of_deleting_them():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
            client.patch("/api/hierarchy-config", headers=headers, json={
                "vp_count": 2, "directors_per_vp": 1, "managers_per_director": 1, "workers_per_manager": 1,
            })
            agents = client.get("/api/agents", headers=headers).json()
            removed_worker = next(a for a in agents if a["hierarchy_level"] == "worker" and a["parent_agent_id"] is not None)
            client.post(f"/api/agents/{removed_worker['id']}/messages", headers=headers, json={"message": "kept forever"})
            worker_ids = {a["id"] for a in agents if a["hierarchy_level"] == "worker"}

            client.patch("/api/hierarchy-config", headers=headers, json={"vp_count": 1})

        with SessionLocal() as fresh:
            # Every agent row from the grown tree still exists -- reconciliation
            # never deletes, it only flips retired_at on the ones over quota.
            for agent_id in worker_ids:
                row = fresh.get(User, agent_id)
                assert row is not None, "shrinking the hierarchy must not delete agent rows"
            assert fresh.scalar(select(AgentMessage).where(AgentMessage.body == "kept forever")) is not None
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_retiring_an_agent_with_a_live_task_frees_it_for_reassignment():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            worker = _one_worker(client, headers)
            project = client.get("/api/projects", headers=headers).json()[0]
            task = client.post("/api/tasks", headers=headers, json={"project_id": project["id"], "title": "In flight when retired"}).json()
            client.post(f"/api/tasks/{task['id']}/assign-agent", headers=headers, json={"agent_id": worker["id"]})

            client.post(f"/api/admin/agents/{worker['id']}/retire", headers=headers)

            after = client.get(f"/api/projects/{project['id']}/tasks", headers=headers).json()
            after_task = next(t for t in after if t["id"] == task["id"])
            assert after_task["assignee"] is None
        with SessionLocal() as fresh:
            assert fresh.get(User, worker["id"]).current_task_id is None
            assert fresh.get(Task, task["id"]).assignee_id is None
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()
