from fastapi.testclient import TestClient
from app.main import app

def test_project_document_team_and_completed_task_flow():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        user_id = client.get("/api/me", headers=headers).json()["user"]["id"]
        draft = client.post("/api/project-drafts/assist", headers=headers, files={"file": ("launch-brief.txt", b"Launch readiness\nPrepare the customer launch and final QA.", "text/plain")})
        assert draft.status_code == 201
        team = client.post("/api/teams", headers=headers, json={"name": "QA Test Team", "description": "Temporary test team"})
        assert team.status_code == 201
        project = client.post("/api/projects", headers=headers, json={"name": "QA Test Project", "code": "QATP", "team_id": team.json()["id"], "source_document_ids": [draft.json()["document"]["id"]]})
        assert project.status_code == 201
        project_id = project.json()["id"]
        assert project.json()["documents"][0]["id"] == draft.json()["document"]["id"]
        assert client.get("/api/teams/" + team.json()["id"] + "/projects", headers=headers).json()[0]["id"] == project_id
        task = client.post("/api/tasks", headers=headers, json={"project_id": project_id, "title": "Visible completed task", "assignee_id": user_id})
        assert task.status_code == 201
        assert client.patch("/api/tasks/" + task.json()["id"], headers=headers, json={"status": "done"}).status_code == 200
        dashboard = client.get("/api/dashboard", headers=headers).json()
        assert any(item["id"] == task.json()["id"] and item["status"] == "done" for item in dashboard["my_tasks"])
