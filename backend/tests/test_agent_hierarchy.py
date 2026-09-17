from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import hash_password
from app.copilot import WorkspaceTools
from app.database import SessionLocal
from app.main import app
from app.models import AgentMessage, HierarchyConfig, Organization, Project, Task, User
from app.services import ensure_hierarchy_config


def _admin_headers_and_org():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
    db = SessionLocal()
    org = db.scalar(select(Organization))
    return headers, org, db


def test_hierarchy_config_defaults_to_all_zero_and_is_idempotent():
    headers, org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            response = client.get("/api/hierarchy-config", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"vp_count": 0, "directors_per_vp": 0, "managers_per_director": 0, "workers_per_manager": 0}

        count_before = len(db.scalars(select(HierarchyConfig).where(HierarchyConfig.organization_id == org.id)).all())
        ensure_hierarchy_config(db, org.id)
        ensure_hierarchy_config(db, org.id)
        db.commit()
        count_after = len(db.scalars(select(HierarchyConfig).where(HierarchyConfig.organization_id == org.id)).all())
        assert count_before == count_after == 1
    finally:
        db.close()


def test_agents_endpoint_is_empty_until_agents_exist():
    headers, _org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            response = client.get("/api/agents", headers=headers)
        assert response.status_code == 200
        assert response.json() == []
    finally:
        db.close()


def _make_agent(db, org_id, name, level, parent_id=None, current_task_id=None, last_completed_task_id=None):
    agent = User(
        organization_id=org_id, name=name, email=f"{uuid4().hex[:10]}@agents.internal",
        password_hash=hash_password(uuid4().hex), kind="agent", hierarchy_level=level,
        parent_agent_id=parent_id, current_task_id=current_task_id, last_completed_task_id=last_completed_task_id,
        title=level.replace("_", " ").title(), avatar=level[:2].upper(),
    )
    db.add(agent)
    db.flush()
    return agent


def _delete_agents(db, *agents):
    """These tests build agents directly via SQLAlchemy, bypassing hierarchy
    reconciliation -- clean them up so they don't linger in the shared demo org and
    pollute other tests' /api/agents listings (delete children before parents)."""
    for agent in agents:
        for message in db.scalars(select(AgentMessage).where(AgentMessage.agent_id == agent.id)).all():
            db.delete(message)
    for agent in agents:
        db.delete(agent)
    db.commit()


def test_agents_endpoint_reports_hierarchy_links_and_current_and_last_task():
    headers, org, db = _admin_headers_and_org()
    try:
        project = db.scalar(select(Project).where(Project.organization_id == org.id))
        tasks = db.scalars(select(Task).where(Task.project_id == project.id)).all()
        current_task, completed_task = tasks[0], tasks[1]

        ceo = _make_agent(db, org.id, "Enter AI CEO Agent", "ceo")
        vp = _make_agent(db, org.id, "Enter AI VP Agent", "vp", parent_id=ceo.id,
                          current_task_id=current_task.id, last_completed_task_id=completed_task.id)
        db.commit()

        with TestClient(app) as client:
            response = client.get("/api/agents", headers=headers)
        assert response.status_code == 200
        by_id = {a["id"]: a for a in response.json()}
        assert set(by_id) == {ceo.id, vp.id}
        assert by_id[ceo.id]["hierarchy_level"] == "ceo"
        assert by_id[ceo.id]["parent_agent_id"] is None
        assert by_id[vp.id]["hierarchy_level"] == "vp"
        assert by_id[vp.id]["parent_agent_id"] == ceo.id
        assert by_id[vp.id]["current_task"]["id"] == current_task.id
        assert by_id[vp.id]["last_completed_task"]["id"] == completed_task.id
        assert by_id[ceo.id]["current_task"] is None
    finally:
        _delete_agents(db, vp, ceo)
        db.close()


def test_agents_are_excluded_from_the_human_user_list_and_copilot_read_tool():
    headers, org, db = _admin_headers_and_org()
    try:
        agent = _make_agent(db, org.id, "Enter AI Worker Agent", "worker")
        db.commit()

        with TestClient(app) as client:
            response = client.get("/api/users", headers=headers)
        assert response.status_code == 200
        assert all(u["id"] != agent.id for u in response.json())

        admin = db.scalar(select(User).where(User.email == "admin@demo.enterai.com"))
        tools = WorkspaceTools(db, admin)
        assert all(u["id"] != agent.id for u in tools.get_users())
    finally:
        _delete_agents(db, agent)
        db.close()


def test_agent_messages_persist_independently_of_the_general_copilot_transcript():
    headers, org, db = _admin_headers_and_org()
    try:
        admin = db.scalar(select(User).where(User.email == "admin@demo.enterai.com"))
        agent = _make_agent(db, org.id, "Enter AI Director Agent", "director")
        db.add(AgentMessage(agent_id=agent.id, author_id=admin.id, role="user", body="What's the status?"))
        db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body="On track."))
        db.commit()

        history = db.scalars(select(AgentMessage).where(AgentMessage.agent_id == agent.id).order_by(AgentMessage.created_at)).all()
        assert [m.role for m in history] == ["user", "agent"]
        assert history[0].author_id == admin.id
        assert history[1].author_id is None
    finally:
        _delete_agents(db, agent)
        db.close()
