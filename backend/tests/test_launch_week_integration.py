"""Launch-week project workflows across separate requests and team roles."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Notification, Task
from test_observability import identity, workspace


def test_launch_week_checklist_persistence_decisions_billing_and_digest():
    with TestClient(app) as client:
        admin_headers, account = workspace(client)
        org_id = account["organization"]["id"]
        admin_id = account["user"]["id"]
        member_id, member_headers = identity(org_id, "member")
        agent_id, _ = identity(org_id, "admin", "agent")

        # Monday: a complete launch checklist has an accountable owner and deadline
        # on every item, created through the same project/task API used by the UI.
        project_response = client.post("/api/projects", headers=admin_headers, json={
            "name": "Launch Week Simulation", "code": "LWS",
            "launch_date": "2027-03-19",
            "launch_freeze_rule": "Freeze changes 48 hours before launch; only approved fixes may ship.",
        })
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()
        checklist = [
            ("Confirm launch scope", admin_id, "2027-03-15"),
            ("Complete release QA", agent_id, "2027-03-17"),
            ("Approve rollback plan", member_id, "2027-03-18"),
        ]
        created = []
        for title, owner_id, due in checklist:
            response = client.post("/api/tasks", headers=admin_headers, json={
                "project_id": project["id"], "title": title, "assignee_id": owner_id,
                "due_date": f"{due}T00:00:00",
            })
            assert response.status_code == 201, response.text
            row = response.json()
            assert row["assignee"]["id"] == owner_id
            assert row["due_date"].startswith(due)
            created.append(row)
        assert len(created) == 3

        # Tuesday: a fresh client/session reloads the saved launch controls.
        with TestClient(app) as resumed_session:
            resumed = resumed_session.get(f"/api/projects/{project['id']}", headers=admin_headers)
        assert resumed.status_code == 200
        assert resumed.json()["launch_date"] == "2027-03-19"
        assert resumed.json()["launch_freeze_rule"] == project["launch_freeze_rule"]

        # Wednesday: a human confirms a decision from the discussion, which is
        # durably tied to its affected task and then reflected in the task deadline.
        decision_task = created[1]
        comment = client.post(f"/api/tasks/{decision_task['id']}/comments", headers=admin_headers, json={
            "body": "Confirmed team decision: keep the launch on March 19 and begin freeze on March 17.",
        })
        assert comment.status_code == 201, comment.text
        decision_response = client.post(
            f"/api/tasks/{decision_task['id']}/decisions/from-comment/{comment.json()['id']}",
            headers=admin_headers,
        )
        assert decision_response.status_code == 201, decision_response.text
        decision = decision_response.json()
        assert decision["task_id"] == decision_task["id"]
        assert decision["source_comment_id"] == comment.json()["id"]
        revised = client.patch(f"/api/tasks/{decision_task['id']}", headers=admin_headers, json={
            "due_date": "2027-03-17T00:00:00",
        })
        assert revised.status_code == 200, revised.text
        assert revised.json()["due_date"].startswith("2027-03-17")

        # Thursday: a normal human worker cannot read billing data; the denial
        # contains no account, subscription, or payment details.
        denied = client.get("/api/billing", headers=member_headers)
        assert denied.status_code == 403
        assert denied.json() == {"detail": "Human administrator required"}
        assert "subscription" not in denied.text.lower()
        assert "payment" not in denied.text.lower()

        # Friday: digest reads remain read-only and repeatable. Include real agent
        # work so the summary has completed work and the confirmed decision.
        with SessionLocal() as db:
            task = db.get(Task, decision_task["id"])
            task.status = "done"
            task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            before = db.query(Notification).filter(Notification.user_id.in_([admin_id, member_id])).count()
        first = client.get("/api/team-digest?days=7", headers=admin_headers)
        second = client.get("/api/team-digest?days=7", headers=admin_headers)
        assert first.status_code == second.status_code == 200
        digest = first.json()
        assert any(item["task_id"] == decision_task["id"] for item in digest["completed_work"])
        assert any(item["id"] == decision["id"] for item in digest["decisions"])
        assert [(item["task_id"], item["title"]) for item in digest["completed_work"]] == [
            (item["task_id"], item["title"]) for item in second.json()["completed_work"]
        ]
        with SessionLocal() as db:
            after = db.query(Notification).filter(Notification.user_id.in_([admin_id, member_id])).count()
        assert after == before
