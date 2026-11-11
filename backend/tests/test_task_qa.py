from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Task, User


def _workspace(client: TestClient):
    key = uuid4().hex
    registration = client.post("/api/auth/register", json={
        "organization_name": f"QA workspace {key}", "name": "QA Admin",
        "email": f"qa-{key}@example.com", "password": f"QA-password-{key}",
    })
    assert registration.status_code == 200, registration.text
    token = registration.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    project = client.post("/api/projects", headers=headers,
                          json={"name": f"QA project {key}", "code": f"QA{key[:6].upper()}"})
    assert project.status_code == 201, project.text
    return registration.json(), headers, project.json()


def _task(client, headers, project, **overrides):
    body = {
        "project_id": project["id"], "title": "Deliver a verified change",
        "qa_required": True,
        "acceptance_criteria": ["The agreed behavior is implemented", "No regression is introduced"],
        "review_rubric": ["Evidence supports each criterion", "No unresolved blocker remains"],
    }
    body.update(overrides)
    response = client.post("/api/tasks", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_high_priority_tasks_require_criteria_qa_and_explicit_human_approval():
    with TestClient(app) as client:
        _, headers, project = _workspace(client)
        missing_plan = client.post("/api/tasks", headers=headers, json={
            "project_id": project["id"], "title": "High-impact delivery", "priority": "high",
        })
        assert missing_plan.status_code == 422

        task = _task(client, headers, project, priority="high")
        assert task["qa_required"] is True
        assert task["requires_human_approval"] is True

        attempted_bypass = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={
            "priority": "high", "qa_required": False, "requires_human_approval": False,
        })
        assert attempted_bypass.status_code == 200, attempted_bypass.text
        assert attempted_bypass.json()["qa_required"] is True
        assert attempted_bypass.json()["requires_human_approval"] is True

        premature_completion = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
        assert premature_completion.status_code == 409


def test_qa_gate_requires_evidence_and_passed_checks_before_human_completion():
    with TestClient(app) as client:
        _, headers, project = _workspace(client)
        task = _task(client, headers, project)
        reviewed = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "review"})
        assert reviewed.status_code == 200, reviewed.text

        checks = client.post(f"/api/tasks/{task['id']}/qa/run", headers=headers)
        assert checks.status_code == 200
        assert any(item["name"] == "evidence" and not item["passed"] for item in checks.json()["qa_checks"])
        blocked = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
        assert blocked.status_code == 409

        saved = client.patch(f"/api/tasks/{task['id']}", headers=headers,
                             json={"qa_evidence": ["Automated regression validation passed; observed output matches the acceptance criteria."]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["qa_checked_at"] is not None
        assert all(item["passed"] for item in saved.json()["qa_checks"])
        completed = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
        assert completed.status_code == 200, completed.text


def test_high_priority_completion_preserves_passed_qa_checks():
    with TestClient(app) as client:
        _, headers, project = _workspace(client)
        task = _task(client, headers, project, priority="high")
        evidence = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={
            "qa_evidence": ["Automated release checks passed and the result was reviewed."],
        })
        assert evidence.status_code == 200, evidence.text
        submitted = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "review"})
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["qa_checked_at"] is not None
        assert all(item["passed"] for item in submitted.json()["qa_checks"])

        completed = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
        assert completed.status_code == 200, completed.text
        assert completed.json()["qa_checked_at"] is not None
        assert all(item["passed"] for item in completed.json()["qa_checks"])


def test_peer_review_assigns_a_different_agent_and_human_decides_after_submission():
    with TestClient(app) as client:
        registration, headers, project = _workspace(client)
        task = _task(client, headers, project, status="review")
        evidence = client.patch(f"/api/tasks/{task['id']}", headers=headers,
                                json={"qa_evidence": ["Build and consistency evidence are attached."]})
        assert evidence.status_code == 200, evidence.text
        db = SessionLocal()
        try:
            reviewer = User(
                organization_id=registration["organization"]["id"], name="Independent reviewer",
                email=f"reviewer-{uuid4().hex}@example.test", password_hash="unused",
                kind="agent", hierarchy_level="worker", role="member",
            )
            db.add(reviewer)
            db.commit()
            db.refresh(reviewer)
            reviewer_id = reviewer.id
        finally:
            db.close()

        requested = client.post(f"/api/tasks/{task['id']}/qa/peer-review", headers=headers,
                                json={"reviewer_agent_id": reviewer_id})
        assert requested.status_code == 200, requested.text
        result = requested.json()
        assert result["task"]["peer_review_status"] == "pending"
        assert result["review_task"]["assignee"]["id"] == reviewer_id
        assert "Acceptance criteria" in result["review_task"]["description"]
        assert "Review rubric" in result["review_task"]["description"]

        decision = client.post(f"/api/tasks/{task['id']}/qa/peer-review/decision", headers=headers,
                               json={"outcome": "approved", "summary": "Review is complete and the evidence supports the rubric."})
        assert decision.status_code == 409
        cleanup = SessionLocal()
        try:
            pending_review = cleanup.get(Task, result["review_task"]["id"])
            assert pending_review is not None
            reviewer_row = cleanup.get(User, reviewer_id)
            assert reviewer_row is not None

            from app.copilot import CopilotService, ProviderTurn
            from app.execution import _run_agent_step

            class ReviewProvider:
                def respond(self, system, messages, role=None, extra_tools=None):
                    return ProviderTurn(text=None, tool_calls=[{
                        "id": "peer-review", "name": "mark_task_complete",
                        "args": {"result": "Reviewed each acceptance criterion and rubric item. Evidence supports approval; no unresolved issues were found."},
                    }])
                def assistant_message(self, turn):
                    return {"role": "assistant", "tool_calls": turn.tool_calls, "text": turn.text}
                def tool_result_message(self, tool_call_id, content, is_error=False):
                    return {"role": "tool", "tool_call_id": tool_call_id, "content": content, "is_error": is_error}
                def user_message(self, text): return {"role": "user", "content": text}

            service = CopilotService.__new__(CopilotService)
            service.provider = ReviewProvider()
            service.mode = "tool_calling"
            _run_agent_step(cleanup, reviewer_row, service)
            cleanup.refresh(pending_review)
            parent = cleanup.get(Task, task["id"])
            assert pending_review.status == "done"
            assert parent.peer_review_status == "submitted"
            assert parent.peer_review_summary and "acceptance criterion" in parent.peer_review_summary
        finally:
            cleanup.close()

        decision = client.post(f"/api/tasks/{task['id']}/qa/peer-review/decision", headers=headers,
                               json={"outcome": "approved", "summary": "Review is complete and the evidence supports the rubric."})
        assert decision.status_code == 200, decision.text
        assert decision.json()["peer_review_status"] == "approved"
        assert decision.json()["qa_checked_at"] is not None
        assert all(item["passed"] for item in decision.json()["qa_checks"])
        completed = client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "done"})
        assert completed.status_code == 200, completed.text


def test_qa_configuration_is_restricted_and_high_impact_work_implies_qa():
    with TestClient(app) as client:
        _, headers, project = _workspace(client)
        missing = client.post("/api/tasks", headers=headers, json={
            "project_id": project["id"], "title": "Sensitive release", "requires_human_approval": True,
        })
        assert missing.status_code == 422
        task = _task(client, headers, project)
        assert task["qa_required"] is True
        assert task["requires_human_approval"] is False
