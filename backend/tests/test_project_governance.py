"""What managers can do that members cannot, deleting and archiving projects, and removing people."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Attachment, Comment, Project, Task, User
from test_observability import identity, workspace


def _project_with_work(c, headers):
    p = c.post("/api/projects", headers=headers, json={"name": "Doomed", "code": "DOOM"}).json()
    parent = c.post("/api/tasks", headers=headers, json={"project_id": p["id"], "title": "Parent"}).json()
    child = c.post("/api/tasks", headers=headers, json={"project_id": p["id"], "title": "Child", "parent_id": parent["id"]}).json()
    c.post(f"/api/tasks/{child['id']}/comments", headers=headers, json={"body": "note"})
    c.post(f"/api/tasks/{parent['id']}/attachments", headers=headers, files={"file": ("a.txt", b"x", "text/plain")})
    c.post(f"/api/projects/{p['id']}/documents", headers=headers, files={"file": ("brief.txt", b"Launch brief", "text/plain")})
    return p, [parent["id"], child["id"]]


def test_a_manager_deletes_a_project_with_everything_in_it():
    with TestClient(app) as c:
        admin, account = workspace(c)
        _id, manager = identity(account["organization"]["id"], "manager")
        project, task_ids = _project_with_work(c, admin)
        assert c.delete(f"/api/projects/{project['id']}", headers=manager).status_code == 204
        with SessionLocal() as db:
            assert db.get(Project, project["id"]) is None
            assert not db.scalars(select(Task).where(Task.id.in_(task_ids))).all()
            assert not db.scalars(select(Comment).where(Comment.task_id.in_(task_ids))).all()
            assert not db.scalars(select(Attachment).where(Attachment.task_id.in_(task_ids))).all()


def test_members_cannot_delete_or_archive_projects_or_others_tasks():
    with TestClient(app) as c:
        admin, account = workspace(c)
        _id, member = identity(account["organization"]["id"], "member")
        project, task_ids = _project_with_work(c, admin)
        assert c.delete(f"/api/projects/{project['id']}", headers=member).status_code == 403
        assert c.patch(f"/api/projects/{project['id']}", headers=member, json={"status": "archived"}).status_code == 403
        assert c.delete(f"/api/tasks/{task_ids[0]}", headers=member).status_code == 403
        own = c.post("/api/tasks", headers=member, json={"project_id": project["id"], "title": "Mine"}).json()
        assert c.delete(f"/api/tasks/{own['id']}", headers=member).status_code == 204
        assert c.post("/api/teams", headers=member, json={"name": "Nope"}).status_code == 403


def test_archiving_is_reversible_by_a_manager():
    with TestClient(app) as c:
        admin, account = workspace(c)
        _id, manager = identity(account["organization"]["id"], "manager")
        project, _ = _project_with_work(c, admin)
        assert c.patch(f"/api/projects/{project['id']}", headers=manager, json={"status": "archived"}).json()["status"] == "archived"
        assert c.patch(f"/api/projects/{project['id']}", headers=manager, json={"status": "active"}).json()["status"] == "active"
        assert c.post("/api/teams", headers=manager, json={"name": "Ops"}).status_code == 201


def test_an_admin_removes_a_member_who_is_signed_out_everywhere():
    with TestClient(app) as c:
        admin, account = workspace(c)
        member_id, member = identity(account["organization"]["id"], "member")
        assert c.get("/api/me", headers=member).status_code == 200
        removed = c.patch(f"/api/admin/users/{member_id}/active", headers=admin, json={"active": False})
        assert removed.status_code == 200 and removed.json()["active"] is False
        assert c.get("/api/me", headers=member).status_code == 401
        assert member_id not in [u["id"] for u in c.get("/api/users", headers=admin).json()]
        project = c.post("/api/projects", headers=admin, json={"name": "P", "code": "PP"}).json()
        assert c.post("/api/tasks", headers=admin, json={"project_id": project["id"], "title": "T", "assignee_id": member_id}).status_code == 409
        assert c.patch(f"/api/admin/users/{member_id}/active", headers=admin, json={"active": True}).json()["active"] is True


def test_an_admin_cannot_remove_themselves():
    with TestClient(app) as c:
        admin, account = workspace(c)
        response = c.patch(f"/api/admin/users/{account['user']['id']}/active", headers=admin, json={"active": False})
        assert response.status_code == 409
