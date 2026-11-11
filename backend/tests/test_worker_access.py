from uuid import uuid4
from datetime import datetime

import pytest

from app.copilot import AgentAccessDenied, WorkspaceTools, _repo_tools_enabled, _worker_write_in_scope, _execute_read_tool
from app.database import SessionLocal
from app.models import AgentAccessGrant, Comment, Organization, Project, Task, User
from app.auth import create_token
from app.main import app
from fastapi.testclient import TestClient


def test_agents_read_assigned_work_and_only_explicitly_granted_projects():
    db = SessionLocal()
    suffix = uuid4().hex
    try:
        org = Organization(name="Scope test", slug=suffix)
        db.add(org)
        db.flush()
        manager = User(organization_id=org.id, name="Manager", email=f"manager-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="senior_manager")
        admin = User(organization_id=org.id, name="Admin", email=f"admin-{suffix}@example.test", password_hash="unused", kind="human", role="admin")
        worker = User(organization_id=org.id, name="Worker", email=f"worker-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
        other = User(organization_id=org.id, name="Other", email=f"other-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
        db.add_all([manager, worker, other, admin])
        db.flush()
        visible = Project(organization_id=org.id, name="Visible", code="VIS", description="Private project brief")
        hidden = Project(organization_id=org.id, name="Hidden", code="HID")
        db.add_all([visible, hidden])
        db.flush()
        own = Task(project_id=visible.id, title="Own task", assignee_id=worker.id)
        colleague = Task(project_id=visible.id, title="Colleague task", assignee_id=other.id)
        secret = Task(project_id=hidden.id, title="Hidden task", assignee_id=other.id)
        db.add_all([own, colleague, secret])
        db.flush()
        db.add_all([Comment(task_id=own.id, author_id=worker.id, body="Own comment"), Comment(task_id=colleague.id, author_id=other.id, body="Private comment")])
        db.flush()

        tools = WorkspaceTools(db, worker)
        assert [t["id"] for t in tools.get_tasks()] == [own.id]
        assert [p["id"] for p in tools.get_projects()] == [visible.id]
        assert tools.get_projects()[0]["description"] == ""
        assert [t["id"] for t in tools.search("task")["tasks"]] == [own.id]
        assert tools.search("Hidden")["projects"] == []
        assert tools.get_comments(own.id)[0]["body"] == "Own comment"
        with pytest.raises(AgentAccessDenied):
            tools.get_comments(colleague.id)
        assert tools.search_documents("anything") == []
        assert tools.get_activity() == tools.get_users() == tools.get_teams() == tools.get_notifications() == []
        assert _repo_tools_enabled(db, worker) is False
        assert _worker_write_in_scope(tools, {"task_id": own.id})
        assert not _worker_write_in_scope(tools, {"task_id": colleague.id})
        assert not _worker_write_in_scope(tools, {"project_id": hidden.id})
        assert not _worker_write_in_scope(tools, {"assignee_id": other.id})
        assert WorkspaceTools(db, worker, hidden.id).get_tasks() == []
        manager_tools = WorkspaceTools(db, manager)
        assert manager_tools.get_tasks() == []
        assert manager_tools.get_projects() == []
        grant = AgentAccessGrant(organization_id=org.id, agent_id=manager.id,
            resource_type="project", resource_id=visible.id, permission="read", granted_by=admin.id)
        db.add(grant)
        db.flush()
        assert {task["id"] for task in manager_tools.get_tasks()} == {own.id, colleague.id}
        assert [project["id"] for project in manager_tools.get_projects()] == [visible.id]
        grant.revoked_by, grant.revoked_at = admin.id, datetime.utcnow()
        db.flush()
        assert manager_tools.get_tasks() == []
        assert [item["id"] for item in _execute_read_tool("get_assignable_agents", {"query": "Worker"}, WorkspaceTools(db, admin))] == [worker.id]
        assert _execute_read_tool("get_assignable_agents", {}, tools) == []
    finally:
        db.rollback()
        db.close()


def test_worker_session_cannot_bypass_assigned_task_tools():
    db = SessionLocal()
    suffix = uuid4().hex
    try:
        org = Organization(name="Worker session test", slug=suffix)
        db.add(org)
        db.flush()
        worker = User(organization_id=org.id, name="Worker", email=f"worker-session-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
        db.add(worker)
        db.commit()
        token = create_token(worker)
        with TestClient(app) as client:
            for path in ("/api/me", "/api/projects", "/api/search?q=task", "/api/agents"):
                assert client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    finally:
        db.delete(worker)
        db.flush()  # PostgreSQL enforces the organization foreign key
        db.delete(org)
        db.commit()
        db.close()
