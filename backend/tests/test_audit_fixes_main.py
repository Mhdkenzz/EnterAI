from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User


def _admin_headers(client):
    login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def test_sso_only_account_login_is_refused_like_an_unknown_address():
    with TestClient(app) as client:
        suffix = uuid4().hex[:8]
        db = SessionLocal()
        try:
            org = db.scalar(select(Organization))
            # SSO and SCIM-provisioned users carry an empty password hash.
            db.add(User(organization_id=org.id, name="SSO Only", email=f"sso-{suffix}@example.com", password_hash="", role="member"))
            db.commit()
        finally:
            db.close()
        sso = client.post("/api/auth/login", json={"email": f"sso-{suffix}@example.com", "password": "anything-at-all"})
        unknown = client.post("/api/auth/login", json={"email": f"nobody-{suffix}@example.com", "password": "anything-at-all"})
        assert sso.status_code == 401
        assert sso.json()["detail"] == "Incorrect email or password"
        assert sso.status_code == unknown.status_code and sso.json() == unknown.json()


def test_project_named_with_a_wildcard_does_not_collide_and_exact_duplicates_still_do():
    with TestClient(app) as client:
        headers = _admin_headers(client)
        assert len(client.get("/api/projects", headers=headers).json()) >= 1
        suffix = uuid4().hex[:6].upper()
        # A LIKE pattern of "%" matches every existing project, so this used to 409.
        wildcard = client.post("/api/projects", headers=headers, json={"name": "%", "code": "PCT" + suffix})
        try:
            assert wildcard.status_code == 201, wildcard.text
            # Exact name, different case, different code: still a duplicate.
            base = client.post("/api/projects", headers=headers, json={"name": "Percent Check " + suffix, "code": "PCA" + suffix})
            assert base.status_code == 201, base.text
            try:
                duplicate = client.post("/api/projects", headers=headers, json={"name": "percent check " + suffix, "code": "PCB" + suffix})
                assert duplicate.status_code == 409
                clash = client.post("/api/projects", headers=headers, json={"name": "Other " + suffix, "code": "pca" + suffix})
                assert clash.status_code == 409
            finally:
                client.delete("/api/projects/" + base.json()["id"], headers=headers)
        finally:
            if wildcard.status_code == 201:
                client.delete("/api/projects/" + wildcard.json()["id"], headers=headers)


def test_task_status_and_priority_outside_the_known_values_are_rejected():
    with TestClient(app) as client:
        headers = _admin_headers(client)
        suffix = uuid4().hex[:6].upper()
        project = client.post("/api/projects", headers=headers, json={"name": "Task Limits " + suffix, "code": "TL" + suffix})
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        assert client.post("/api/tasks", headers=headers, json={"project_id": project_id, "title": "Bad status", "status": "shipped"}).status_code == 422
        assert client.post("/api/tasks", headers=headers, json={"project_id": project_id, "title": "Bad priority", "priority": "critical"}).status_code == 422
        task = client.post("/api/tasks", headers=headers, json={"project_id": project_id, "title": "Good values", "status": "backlog", "priority": "low"})
        assert task.status_code == 201, task.text
        assert client.patch("/api/tasks/" + task.json()["id"], headers=headers, json={"status": "archived"}).status_code == 422
        assert client.patch("/api/tasks/" + task.json()["id"], headers=headers, json={"priority": "critical"}).status_code == 422


def test_comment_body_has_an_upper_bound():
    with TestClient(app) as client:
        headers = _admin_headers(client)
        projects = client.get("/api/projects", headers=headers).json()
        task = client.post("/api/tasks", headers=headers, json={"project_id": projects[0]["id"], "title": "Comment limits"})
        assert task.status_code == 201, task.text
        comments_url = "/api/tasks/" + task.json()["id"] + "/comments"
        assert client.post(comments_url, headers=headers, json={"body": "x" * 10001}).status_code == 422
        assert client.post(comments_url, headers=headers, json={"body": "x" * 10000}).status_code == 201


def test_project_color_must_be_a_six_digit_hex_value():
    with TestClient(app) as client:
        headers = _admin_headers(client)
        suffix = uuid4().hex[:6].upper()
        assert client.post("/api/projects", headers=headers, json={"name": "Color Check " + suffix, "code": "CC" + suffix, "color": "red"}).status_code == 422
        assert client.post("/api/projects", headers=headers, json={"name": "Color Short " + suffix, "code": "CS" + suffix, "color": "#fff"}).status_code == 422
        project = client.post("/api/projects", headers=headers, json={"name": "Color Ok " + suffix, "code": "CO" + suffix, "color": "#38bdf8"})
        assert project.status_code == 201, project.text
        assert client.patch("/api/projects/" + project.json()["id"], headers=headers, json={"color": "#12345"}).status_code == 422
        client.delete("/api/projects/" + project.json()["id"], headers=headers)


def test_project_patch_with_null_name_or_code_is_422_not_500():
    with TestClient(app) as client:
        headers = _admin_headers(client)
        suffix = uuid4().hex[:6].upper()
        created = client.post("/api/projects", headers=headers, json={"name": "Null Patch " + suffix, "code": "NP" + suffix})
        assert created.status_code == 201, created.text
        try:
            for field in ("name", "code"):
                response = client.patch("/api/projects/" + created.json()["id"], headers=headers, json={field: None})
                assert response.status_code == 422, response.text
        finally:
            client.delete("/api/projects/" + created.json()["id"], headers=headers)
