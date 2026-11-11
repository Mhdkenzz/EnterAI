from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth import create_token
from app.database import SessionLocal
from app.main import app
from app.models import Activity, AuditEvent, Comment, Decision, Organization, Project, Task, User


def _headers(user):
    return {"Authorization": "Bearer " + create_token(user)}


def _workspace():
    db = SessionLocal()
    suffix = uuid4().hex
    org = Organization(name="Agent transparency", slug="agent-transparency-" + suffix)
    db.add(org)
    db.flush()
    admin = User(organization_id=org.id, name="Workspace Admin", email=f"admin-{suffix}@example.test", password_hash="unused", role="admin", kind="human")
    member = User(organization_id=org.id, name="Workspace Member", email=f"member-{suffix}@example.test", password_hash="unused", role="member", kind="human")
    worker = User(organization_id=org.id, name="Worker Agent", email=f"worker-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="senior_manager")
    colleague = User(organization_id=org.id, name="Other Agent", email=f"other-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
    db.add_all([admin, member, worker, colleague])
    db.flush()
    project = Project(organization_id=org.id, name="Transparency Project", code="TRN", owner_id=admin.id)
    db.add(project)
    db.flush()
    active = Task(project_id=project.id, title="Agent implementation", assignee_id=worker.id, status="in_progress")
    colleague_task = Task(project_id=project.id, title="Colleague private task", assignee_id=colleague.id, status="in_progress")
    completed = Task(project_id=project.id, title="Finished agent report", assignee_id=worker.id, status="done", completed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    blocked = Task(project_id=project.id, title="Blocked agent item", assignee_id=worker.id, status="blocked", blocked_reason="Needs a human decision")
    review = Task(project_id=project.id, title="Agent proposal", assignee_id=worker.id, status="review")
    db.add_all([active, colleague_task, completed, blocked, review])
    db.flush()
    db.add_all([
        Decision(organization_id=org.id, project_id=project.id, title="Use staged rollout", body="Roll out in two steps.", created_by=admin.id),
        Activity(organization_id=org.id, actor_id=worker.id, entity_type="task", entity_id=active.id, action="agent_progress", detail={"task_id": active.id}),
        Activity(organization_id=org.id, actor_id=colleague.id, entity_type="task", entity_id=colleague_task.id, action="agent_progress", detail={"task_id": colleague_task.id}),
        AuditEvent(organization_id=org.id, actor_id=worker.id, source="agent", entity_type="execution_run", entity_id="run-own", action="execution_started", detail={"task_id": active.id, "project_id": project.id, "outcome": "started"}),
        AuditEvent(organization_id=org.id, actor_id=worker.id, source="agent", entity_type="agent_tool", entity_id="tool-own", action="tool_result", detail={"task_id": active.id, "project_id": project.id, "tool": "search", "outcome": "failed"}),
        AuditEvent(organization_id=org.id, actor_id=colleague.id, source="agent", entity_type="execution_run", entity_id="run-other", action="execution_started", detail={"task_id": colleague_task.id, "project_id": project.id, "outcome": "started"}),
        Comment(task_id=active.id, author_id=worker.id, body="Worker progress update"),
        Comment(task_id=colleague_task.id, author_id=colleague.id, body="Colleague confidential update"),
    ])
    db.commit()
    return db, org, project, admin, worker, colleague, active, colleague_task, completed, blocked, review, member


def _cleanup(db, org_id):
    task_ids = db.scalars(select(Task.id).join(Project).where(Project.organization_id == org_id)).all()
    if task_ids:
        db.execute(delete(Comment).where(Comment.task_id.in_(task_ids)))
        db.execute(delete(Task).where(Task.id.in_(task_ids)))
    db.execute(delete(Activity).where(Activity.organization_id == org_id))
    db.execute(delete(AuditEvent).where(AuditEvent.organization_id == org_id))
    db.execute(delete(Decision).where(Decision.organization_id == org_id))
    db.execute(delete(Project).where(Project.organization_id == org_id))
    db.execute(delete(User).where(User.organization_id == org_id))
    db.execute(delete(Organization).where(Organization.id == org_id))
    db.commit()


def test_humans_can_manage_agent_jobs_and_get_a_scoped_team_digest():
    db, org, project, admin, worker, _, active, _, completed, blocked, review, member = _workspace()
    try:
        with TestClient(app) as client:
            headers = _headers(admin)
            response = client.get("/api/agent-work", headers=headers)
            assert response.status_code == 200
            jobs = {job["id"]: job for job in response.json()}
            assert jobs[active.id]["status"] == "active"
            assert jobs[completed.id]["status"] == "completed"
            assert jobs[blocked.id]["status"] == "blocked"
            assert jobs[review.id]["status"] == "awaiting_approval"
            member_headers = _headers(member)
            member_jobs = client.get("/api/agent-work", headers=member_headers)
            assert member_jobs.status_code == 200
            assert all(not job["can_control"] for job in member_jobs.json())
            assert client.post(f"/api/tasks/{active.id}/agent-control", headers=member_headers, json={"action": "pause"}).status_code == 403
            digest = client.get("/api/team-digest?days=7", headers=headers)
            assert digest.status_code == 200
            body = digest.json()
            assert any(row["title"] == completed.title for row in body["completed_work"])
            assert any(row["title"] == "Use staged rollout" for row in body["decisions"])
            assert any(row["title"] == blocked.title for row in body["blockers"])

            assert client.post(f"/api/tasks/{active.id}/agent-control", headers=headers, json={"action": "pause"}).status_code == 200
            paused_jobs = {job["id"]: job for job in client.get("/api/agent-work", headers=headers).json()}
            assert paused_jobs[active.id]["status"] == "paused"
            assert client.post(f"/api/tasks/{active.id}/agent-control", headers=headers, json={"action": "resume"}).status_code == 200
            assert client.post(f"/api/tasks/{blocked.id}/agent-control", headers=headers, json={"action": "redirect", "instructions": "Retry after the human review is complete."}).status_code == 200
            assert client.post(f"/api/tasks/{blocked.id}/agent-control", headers=headers, json={"action": "reassign", "assignee_id": admin.id}).status_code == 200
            assert blocked.id not in {job["id"] for job in client.get("/api/agent-work", headers=headers).json()}
            assert client.post(f"/api/tasks/{active.id}/agent-control", headers=headers, json={"action": "redirect", "instructions": "Focus on documenting the remaining implementation."}).status_code == 200
            assert client.post(f"/api/tasks/{active.id}/agent-control", headers=headers, json={"action": "cancel", "reason": "Scope was withdrawn."}).status_code == 200

            feed = client.get("/api/activity?limit=100", headers=headers)
            assert feed.status_code == 200
            feed_rows = feed.json()
            assert any(row["action"] == "agent_job_cancelled" and row["task_title"] == active.title for row in feed_rows)
            assert any(row["summary"] == "recorded progress" and row["created_at"] for row in feed_rows)
            assert any(row["summary"] == "used search · failed" for row in feed_rows)

    finally:
        _cleanup(db, org.id)
        db.close()


def test_agent_activity_feed_only_shows_its_assigned_tasks():
    db, org, _, _, worker, colleague, active, colleague_task, *_ = _workspace()
    try:
        with TestClient(app) as client:
            response = client.get("/api/activity?limit=100", headers=_headers(worker))
            assert client.get("/api/agent-work", headers=_headers(worker)).status_code == 403
            assert client.get("/api/team-digest", headers=_headers(worker)).status_code == 403
        assert response.status_code == 200
        text = response.text
        assert active.title in text
        assert "Worker progress update" in text
        assert colleague_task.title not in text
        assert "Colleague confidential update" not in text
        assert "run-other" not in text
    finally:
        _cleanup(db, org.id)
        db.close()
