from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import create_token
from app.copilot import openai_tool_specs
from app.database import SessionLocal
from app.agent_access import agent_document_access, agent_tool_allowed, record_access_denial
from app.main import app
from app.models import AgentAccessGrant, AuditEvent, Comment, Organization, Project, ProjectDocument, Task, User


def test_admin_can_inspect_grant_revoke_and_regrant_agent_access():
    db = SessionLocal()
    suffix = uuid4().hex
    org = Organization(name="Access admin test", slug=suffix)
    other_org = Organization(name="Access other tenant", slug=f"other-{suffix}")
    db.add_all([org, other_org])
    db.flush()
    admin = User(organization_id=org.id, name="Admin", email=f"admin-{suffix}@example.test", password_hash="unused", role="admin")
    member = User(organization_id=org.id, name="Member", email=f"member-{suffix}@example.test", password_hash="unused", role="member")
    agent = User(organization_id=org.id, name="Agent", email=f"agent-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
    project = Project(organization_id=org.id, name="Restricted", code="R1")
    other_project = Project(organization_id=other_org.id, name="Other tenant", code="OT")
    db.add_all([admin, member, agent, project, other_project])
    db.flush()
    doc = ProjectDocument(organization_id=org.id, project_id=project.id, uploaded_by=admin.id, file_name="brief.txt", path="safe")
    db.add(doc)
    db.commit()
    token = create_token(admin)
    headers = {"Authorization": f"Bearer {token}"}
    member_headers = {"Authorization": f"Bearer {create_token(member)}"}
    try:
        with TestClient(app) as client:
            path = f"/api/admin/agents/{agent.id}/access"
            assert client.get(path, headers=headers).status_code == 200
            assert client.get(path, headers=member_headers).status_code == 403
            assert client.post(path, headers=headers, json={"resource_type": "project", "resource_id": other_project.id, "permission": "read"}).status_code == 404
            assert client.post(path, headers=headers, json={"resource_type": "tool", "resource_id": "get_users", "permission": "use"}).status_code == 201
            assert "get_users" in {item["function"]["name"] for item in openai_tool_specs(__import__("app.agent_access", fromlist=["allowed_agent_tools"]).allowed_agent_tools(db, agent))}
            assert client.post(path, headers=headers, json={"resource_type": "document", "resource_id": doc.id, "permission": "read"}).status_code == 201
            assert agent_document_access(db, agent, doc.id)
            grants = client.get(path, headers=headers).json()["grants"]
            tool_grant = next(item for item in grants if item["resource_id"] == "get_users")
            assert client.delete(f"{path}/{tool_grant['id']}", headers=headers).status_code == 200
            db.expire_all()
            agent = db.get(User, agent.id)
            assert not agent_tool_allowed(db, agent, "get_users")
            assert client.post(path, headers=headers, json={"resource_type": "tool", "resource_id": "not_a_tool", "permission": "use"}).status_code == 404
            assert client.post(path, headers=headers, json={"resource_type": "tool", "resource_id": "get_users", "permission": "read"}).status_code == 422
    finally:
        db.rollback()
        db.close()


def test_agent_document_scope_and_denials_are_tenant_scoped_and_audited():
    db = SessionLocal()
    suffix = uuid4().hex
    org = Organization(name="Access audit test", slug=suffix)
    other_org = Organization(name="Other org", slug=f"other-{suffix}")
    db.add_all([org, other_org])
    db.flush()
    admin = User(organization_id=org.id, name="Admin", email=f"a-{suffix}@example.test", password_hash="unused", role="admin")
    agent = User(organization_id=org.id, name="Agent", email=f"b-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
    other_admin = User(organization_id=other_org.id, name="Other", email=f"c-{suffix}@example.test", password_hash="unused", role="admin")
    project = Project(organization_id=other_org.id, name="Other project", code="OTH")
    db.add_all([admin, agent, other_admin, project])
    db.flush()
    doc = ProjectDocument(organization_id=other_org.id, project_id=project.id, uploaded_by=other_admin.id, file_name="private.txt", path="private")
    db.add(doc)
    db.flush()
    record_access_denial(db, agent, tool="get_users", reason="not_granted")
    db.commit()
    try:
        assert not agent_document_access(db, agent, doc.id)
        with SessionLocal() as ledger:
            event = ledger.scalar(select(AuditEvent).where(AuditEvent.organization_id == org.id, AuditEvent.action == "access_denied"))
            assert event is not None
            assert event.detail == {"resource_type": "tool", "tool": "get_users", "denial_reason": "not_granted"}
    finally:
        db.rollback()
        db.close()


def test_ungranted_provider_tool_blocks_the_assigned_task_and_stops_the_agent():
    from app.copilot import CopilotService, ProviderTurn
    from app.execution import _run_agent_step

    class UnexpectedToolProvider:
        def user_message(self, text): return {"role": "user", "content": text}
        def assistant_message(self, turn): return {"role": "assistant", "tool_calls": turn.tool_calls, "text": turn.text}
        def tool_result_message(self, tool_call_id, content, is_error=False): return {"role": "tool", "tool_call_id": tool_call_id, "content": content, "is_error": is_error}
        def respond(self, system, messages, role=None, extra_tools=None):
            return ProviderTurn(text=None, tool_calls=[{"id": "denied", "name": "get_users", "args": {}}])

    db = SessionLocal()
    suffix = uuid4().hex
    org = Organization(name="Access stop test", slug=suffix)
    db.add(org)
    db.flush()
    agent = User(organization_id=org.id, name="Worker", email=f"worker-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker", role="member")
    project = Project(organization_id=org.id, name="Assigned", code="ASG")
    db.add_all([agent, project])
    db.flush()
    task = Task(project_id=project.id, title="Restricted task", assignee_id=agent.id, status="todo")
    db.add(task)
    db.flush()
    agent.current_task_id = task.id
    db.commit()
    service = CopilotService(db, org.id)
    service.provider = UnexpectedToolProvider()
    service.mode = "tool_calling"
    try:
        _run_agent_step(db, agent, service)
        db.refresh(task)
        db.refresh(agent)
        assert task.status == "blocked"
        assert task.blocked_reason and "needs additional access" in task.blocked_reason
        assert agent.current_task_id is None
        assert db.scalar(select(Comment.id).where(Comment.task_id == task.id)) is not None
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.organization_id == org.id, AuditEvent.action == "access_denied")) is not None
    finally:
        db.rollback()
        db.close()
