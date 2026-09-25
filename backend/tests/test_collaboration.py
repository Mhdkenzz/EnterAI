"""People hear about work assigned to them, and uploaded files can be opened again."""
from fastapi.testclient import TestClient

from app.main import app
from test_observability import identity, workspace


def _project(c, headers):
    return c.post("/api/projects", headers=headers, json={"name": "Launch", "code": "LCH"}).json()


def test_assignment_and_comments_reach_the_assignees_inbox():
    with TestClient(app) as c:
        admin, account = workspace(c)
        member_id, member = identity(account["organization"]["id"], "member")
        project = _project(c, admin)
        task = c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": "Book venue", "assignee_id": member_id}).json()
        c.post(f"/api/tasks/{task['id']}/comments", headers=admin, json={"body": "Deposit is due Friday"})
        inbox = c.get("/api/notifications", headers=member).json()
        titles = [n["title"] for n in inbox]
        assert any("assigned you “Book venue”" in t for t in titles)
        assert any("commented on “Book venue”" in t for t in titles)
        assert all(n["href"] == f"project:{project['id']}" for n in inbox)
        # The actor is not notified about their own actions.
        assert c.get("/api/notifications", headers=admin).json() == []


def test_a_blank_task_title_is_refused():
    with TestClient(app) as c:
        admin, _ = workspace(c)
        project = _project(c, admin)
        assert c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": ""}).status_code == 422
        task = c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": "Real"}).json()
        assert c.patch(f"/api/tasks/{task['id']}", headers=admin, json={"title": ""}).status_code == 422


def test_attachments_are_listed_and_download_as_files_not_pages():
    with TestClient(app) as c:
        admin, account = workspace(c)
        project = _project(c, admin)
        task = c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": "Spec"}).json()
        c.post(f"/api/tasks/{task['id']}/attachments", headers=admin,
               files={"file": ("evil.html", b"<script>alert(1)</script>", "text/html")})
        listed = c.get(f"/api/tasks/{task['id']}/attachments", headers=admin).json()
        assert [a["file_name"] for a in listed] == ["evil.html"]
        download = c.get(f"/api/attachments/{listed[0]['id']}/download", headers=admin)
        assert download.status_code == 200 and download.content == b"<script>alert(1)</script>"
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")
        # Another workspace cannot fetch it by id.
        other, _ = workspace(c)
        assert c.get(f"/api/attachments/{listed[0]['id']}/download", headers=other).status_code == 404


def test_project_documents_download_within_their_organization_only():
    with TestClient(app) as c:
        admin, _ = workspace(c)
        project = _project(c, admin)
        doc = c.post(f"/api/projects/{project['id']}/documents", headers=admin,
                     files={"file": ("brief.txt", b"Launch brief", "text/plain")}).json()
        assert c.get(f"/api/project-documents/{doc['id']}/download", headers=admin).content == b"Launch brief"
        other, _ = workspace(c)
        assert c.get(f"/api/project-documents/{doc['id']}/download", headers=other).status_code == 404


def test_the_only_admin_cannot_delete_their_own_account():
    with TestClient(app) as c:
        admin, _ = workspace(c)
        response = c.post("/api/privacy/delete-account", headers=admin, json={"password": "test-password"})
        assert response.status_code == 409 and "only administrator" in response.json()["detail"]


def test_search_treats_wildcards_literally_and_is_bounded():
    with TestClient(app) as c:
        admin, _ = workspace(c)
        project = _project(c, admin)
        c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": "Reach 100% coverage"})
        c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": "Unrelated"})
        found = c.get("/api/search", headers=admin, params={"q": "100%"}).json()["tasks"]
        assert [t["title"] for t in found] == ["Reach 100% coverage"]
        assert c.get("/api/search", headers=admin, params={"q": "x" * 201}).status_code == 422


def test_a_proposed_task_says_which_project_it_lands_in():
    with TestClient(app) as c:
        admin, _ = workspace(c)
        c.post("/api/projects", headers=admin, json={"name": "Alpha", "code": "ALP"})
        beta = c.post("/api/projects", headers=admin, json={"name": "Beta launch", "code": "BET"}).json()
        plan = c.post("/api/ai/plan", headers=admin, json={"message": "create task Book venue for Beta launch", "project_id": beta["id"]}).json()
        action = plan["actions"][0]
        assert {"label": "Project", "value": "Beta launch"} in action["details"]
        assert any(d["label"] == "Title" for d in action["details"])
