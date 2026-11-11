from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.copilot import CopilotProviderError, WorkspaceTools, _raise_task_blocker, _suggest_project_next_step
from app.database import SessionLocal
from app.main import app
from app.models import Activity, AgentNextStep, Project, ProjectMilestone, Task, User


def _workspace(client: TestClient):
    key = uuid4().hex
    registration = client.post("/api/auth/register", json={
        "organization_name": f"Coordination {key}", "name": "Project Owner",
        "email": f"coord-{key}@example.com", "password": f"Coord-password-{key}",
    })
    assert registration.status_code == 200, registration.text
    headers = {"Authorization": f"Bearer {registration.json()['token']}"}
    return registration.json(), headers


def test_project_goal_is_measurable_and_metric_updates_are_tracked():
    with TestClient(app) as client:
        _, headers = _workspace(client)
        incomplete = client.post("/api/projects", headers=headers, json={
            "name": "Incomplete goal", "code": "IG",
            "north_star_goal": "Improve activation",
        })
        assert incomplete.status_code == 422

        created = client.post("/api/projects", headers=headers, json={
            "name": "Activation", "code": "ACT",
            "north_star_goal": "Help new customers reach value quickly",
            "success_metric": "Weekly activated customers", "metric_unit": "customers",
            "baseline_value": 10, "current_value": 20, "target_value": 50,
        })
        assert created.status_code == 201, created.text
        project = created.json()
        assert project["north_star"]["progress"] == 25

        updated = client.patch(f"/api/projects/{project['id']}", headers=headers, json={"current_value": 30})
        assert updated.status_code == 200, updated.text
        assert updated.json()["north_star"]["progress"] == 50
        with SessionLocal() as db:
            assert db.scalar(select(Activity).where(
                Activity.entity_type == "project", Activity.entity_id == project["id"],
                Activity.action == "north_star_metric_updated",
            )) is not None


def test_tasks_link_to_same_project_milestones_and_show_owner_progress():
    with TestClient(app) as client:
        _, headers = _workspace(client)
        project = client.post("/api/projects", headers=headers, json={"name": "Launch", "code": "LCH"}).json()
        other_project = client.post("/api/projects", headers=headers, json={"name": "Support", "code": "SUP"}).json()
        milestone_response = client.post(f"/api/projects/{project['id']}/milestones", headers=headers, json={
            "title": "Private beta", "description": "Recruit and onboard pilot customers.",
        })
        assert milestone_response.status_code == 201, milestone_response.text
        milestone = milestone_response.json()
        task = client.post("/api/tasks", headers=headers, json={
            "project_id": project["id"], "title": "Prepare pilot checklist", "milestone_id": milestone["id"],
        })
        assert task.status_code == 201, task.text
        assert task.json()["milestone_title"] == "Private beta"
        cross_project = client.post("/api/tasks", headers=headers, json={
            "project_id": other_project["id"], "title": "Wrong project link", "milestone_id": milestone["id"],
        })
        assert cross_project.status_code == 422
        detail = client.get(f"/api/projects/{project['id']}", headers=headers).json()
        assert detail["milestones"][0]["task_count"] == 1
        assert detail["milestones"][0]["tasks"][0]["assignee_name"] == "Project Owner"


def test_agent_suggests_next_step_without_stopping_and_human_approves_or_rejects():
    with TestClient(app) as client:
        registration, headers = _workspace(client)
        account = registration
        project = client.post("/api/projects", headers=headers, json={
            "name": "Onboarding", "code": "ONB", "north_star_goal": "Increase new-user activation",
            "success_metric": "Activated users", "metric_unit": "users", "baseline_value": 0,
            "current_value": 10, "target_value": 100,
        }).json()
        milestone = client.post(f"/api/projects/{project['id']}/milestones", headers=headers,
            json={"title": "First-run experience"}).json()
        task = client.post("/api/tasks", headers=headers, json={
            "project_id": project["id"], "title": "Review onboarding funnel", "status": "in_progress",
            "milestone_id": milestone["id"],
        }).json()
        db = SessionLocal()
        try:
            agent = User(organization_id=account["organization"]["id"], name="Growth analyst",
                email=f"agent-{uuid4().hex}@example.com", password_hash="unused", kind="agent",
                hierarchy_level="worker", role="member", execution_enabled=True, current_task_id=task["id"])
            db.add(agent); db.flush()
            source_task = db.get(Task, task["id"])
            source_task.assignee_id = agent.id
            db.commit(); db.refresh(agent); db.refresh(source_task)
            tools = WorkspaceTools(db, agent, project["id"], source_task.id)
            proposal = _suggest_project_next_step(tools, source_task, {
                "title": "Test a shorter onboarding path", "description": "Run a small pilot with fewer setup steps.",
                "rationale": "The funnel review found drop-off before users reach the first activation event.",
                "priority": "high", "milestone_id": milestone["id"],
            })
            db.commit()
            suggestion_id = proposal["id"]
            assert source_task.status == "in_progress"
            with pytest.raises(CopilotProviderError, match="already exists or is already suggested"):
                _suggest_project_next_step(tools, source_task, {
                    "title": "Test a shorter onboarding path", "description": "Duplicate suggestion.",
                    "rationale": "This repeats a suggestion already awaiting human review.", "priority": "medium",
                })
            _raise_task_blocker(tools, source_task,
                "The product team must choose between activation speed and collecting optional setup details.",
                "strategic_decision")
            db.commit()
            db.refresh(source_task)
            assert source_task.status == "blocked"
            assert source_task.escalation_category == "strategic_decision"
        finally:
            db.close()

        pending = client.get(f"/api/projects/{project['id']}/next-steps", headers=headers)
        assert pending.status_code == 200
        assert pending.json()[0]["id"] == suggestion_id
        approved = client.post(f"/api/projects/{project['id']}/next-steps/{suggestion_id}/approve", headers=headers)
        assert approved.status_code == 201, approved.text
        created_task = approved.json()
        assert created_task["milestone_id"] == milestone["id"]
        assert created_task["priority"] == "high"
        assert created_task["requires_human_approval"] is True
        assert created_task["assignee"]["kind"] == "human"
        assert client.get(f"/api/projects/{project['id']}/next-steps", headers=headers).json() == []

        with SessionLocal() as db:
            suggestion = db.get(AgentNextStep, suggestion_id)
            assert suggestion.status == "approved"
            assert suggestion.created_task_id == created_task["id"]
