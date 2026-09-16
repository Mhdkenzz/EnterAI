from fastapi.testclient import TestClient
from app.main import app
from uuid import uuid4


def test_security_headers_and_development_docs():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert client.get("/docs").status_code == 200

def test_project_document_team_and_completed_task_flow():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        user_id = client.get("/api/me", headers=headers).json()["user"]["id"]
        draft = client.post("/api/project-drafts/assist", headers=headers, files={"file": ("launch-brief.txt", b"Launch readiness\nPrepare the customer launch and final QA.", "text/plain")})
        assert draft.status_code == 201
        suffix = uuid4().hex[:8]
        team = client.post("/api/teams", headers=headers, json={"name": "QA Test Team " + suffix, "description": "Temporary test team"})
        assert team.status_code == 201
        project = client.post("/api/projects", headers=headers, json={"name": "QA Test Project " + suffix, "code": "QA" + suffix[:4].upper(), "team_id": team.json()["id"], "source_document_ids": [draft.json()["document"]["id"]]})
        assert project.status_code == 201
        project_id = project.json()["id"]
        assert project.json()["documents"][0]["id"] == draft.json()["document"]["id"]
        assert client.get("/api/teams/" + team.json()["id"] + "/projects", headers=headers).json()[0]["id"] == project_id
        task = client.post("/api/tasks", headers=headers, json={"project_id": project_id, "title": "Visible completed task", "assignee_id": user_id})
        assert task.status_code == 201
        assert client.patch("/api/tasks/" + task.json()["id"], headers=headers, json={"status": "done"}).status_code == 200
        dashboard = client.get("/api/dashboard", headers=headers).json()
        assert any(item["id"] == task.json()["id"] and item["status"] == "done" for item in dashboard["my_tasks"])
        assert dashboard["stats"]["completed_this_week"] >= 1
        duplicate = client.post("/api/projects", headers=headers, json={"name": project.json()["name"], "code": "DIFF"})
        assert duplicate.status_code == 409
        edited = client.patch("/api/projects/" + project_id, headers=headers, json={"description": "Edited from API"})
        assert edited.status_code == 200 and edited.json()["description"] == "Edited from API"


def test_xlsx_extraction_drafts_content():
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Launch plan</t></si><si><t>Owner review</t></si></sst>")
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet><sheetData><row><c t='s'><v>0</v></c><c t='s'><v>1</v></c></row></sheetData></worksheet>")
    assert "Launch plan" in __import__("app.services", fromlist=["extract_document_text"]).extract_document_text("brief.xlsx", stream.getvalue())


def test_copilot_reads_workspace_and_requires_a_signed_confirmation():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        priorities = client.post("/api/ai/plan", headers=headers, json={"message": "What should I work on today?"})
        assert priorities.status_code == 200
        assert "Today, focus" in priorities.json()["reply"]
        assert priorities.json()["actions"] == []
        assert priorities.json()["read_tools"] == ["get_projects", "get_tasks", "summarize_priorities"]

        summary = client.post("/api/ai/plan", headers=headers, json={"message": "Summarize AI Workspace"})
        assert summary.status_code == 200
        assert "AI Workspace" in summary.json()["reply"]

        proposal = client.post("/api/ai/plan", headers=headers, json={"message": "Create task prepare Copilot review for AI Workspace"})
        assert proposal.status_code == 200
        action = proposal.json()["actions"][0]
        assert action["requires_confirmation"] is True
        assert "args" not in action and "tool" not in action

        direct_write = client.post("/api/ai/confirm", headers=headers, json={"tool": "create_task", "args": {}})
        assert direct_write.status_code == 422
        confirmed = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": action["confirmation_token"]})
        assert confirmed.status_code == 200
        assert confirmed.json()["title"] == "Prepare Copilot review for AI Workspace"

        forged = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": action["confirmation_token"] + "forged"})
        assert forged.status_code == 400
