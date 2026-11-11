"""Verify separate human accounts see shared tenant work without cross-tenant access."""
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.database import SessionLocal
from app.main import app
from app.models import Comment, Organization, Project, Task, User


def test_distinct_human_members_have_organization_wide_read_access():
    suffix = uuid4().hex
    password = "Synthetic-test-password-42!"
    with SessionLocal() as db:
        organization = Organization(name="Visibility test", slug=f"visibility-{suffix}")
        foreign_org = Organization(name="Other workspace", slug=f"other-{suffix}")
        db.add_all([organization, foreign_org])
        db.flush()
        alice = User(organization_id=organization.id, name="Alice", email=f"alice-{suffix}@example.test", password_hash=hash_password(password), role="member", kind="human")
        bob = User(organization_id=organization.id, name="Bob", email=f"bob-{suffix}@example.test", password_hash=hash_password(password), role="member", kind="human")
        agent = User(organization_id=organization.id, name="AI Worker", email=f"agent-{suffix}@example.test", password_hash="unused", kind="agent", hierarchy_level="worker")
        db.add_all([alice, bob, agent])
        db.flush()
        shared = Project(organization_id=organization.id, name="Shared project", code="SHARED")
        unassigned = Project(organization_id=organization.id, name="Other project", code="OTHER")
        foreign = Project(organization_id=foreign_org.id, name="Foreign project", code="FOREIGN")
        db.add_all([shared, unassigned, foreign])
        db.flush()
        alice_task = Task(project_id=shared.id, title="Alice task", assignee_id=alice.id, reporter_id=alice.id)
        bob_task = Task(project_id=shared.id, title="Bob task", assignee_id=bob.id, reporter_id=bob.id)
        foreign_task = Task(project_id=foreign.id, title="Foreign task")
        db.add_all([alice_task, bob_task, foreign_task])
        db.flush()
        db.add(Comment(task_id=bob_task.id, author_id=bob.id, body="Bob's task comment"))
        db.commit()
        ids = {"alice": alice.id, "bob": bob.id, "shared": shared.id, "unassigned": unassigned.id,
               "foreign": foreign.id, "alice_task": alice_task.id, "bob_task": bob_task.id,
               "foreign_task": foreign_task.id, "agent": agent.id}
        emails = (alice.email, bob.email)

    # Separate clients own separate session cookies and authenticate normally.
    with TestClient(app) as alice_client, TestClient(app) as bob_client:
        for client, email, expected in ((alice_client, emails[0], ids["alice"]), (bob_client, emails[1], ids["bob"])):
            assert client.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
            assert client.get("/api/me").json()["user"]["id"] == expected
            projects = client.get("/api/projects")
            assert projects.status_code == 200
            assert {p["id"] for p in projects.json()} == {ids["shared"], ids["unassigned"]}
            tasks = client.get(f"/api/projects/{ids['shared']}/tasks")
            assert tasks.status_code == 200
            assert {t["id"] for t in tasks.json()} == {ids["alice_task"], ids["bob_task"]}
            search = client.get("/api/search", params={"q": "task"})
            assert search.status_code == 200
            assert {t["id"] for t in search.json()["tasks"]} == {ids["alice_task"], ids["bob_task"]}
            assert client.get(f"/api/tasks/{ids['bob_task']}").status_code == 200
            comments = client.get(f"/api/tasks/{ids['bob_task']}/comments")
            assert comments.status_code == 200
            assert [c["body"] for c in comments.json()] == ["Bob's task comment"]
            assert client.get(f"/api/projects/{ids['foreign']}").status_code == 404
            assert client.get(f"/api/tasks/{ids['foreign_task']}").status_code == 404
            agents = client.get("/api/agents")
            assert agents.status_code == 200
            assert ids["agent"] in {a["id"] for a in agents.json()}

        assert {t["id"] for t in alice_client.get("/api/dashboard").json()["my_tasks"]} == {ids["alice_task"]}
        assert {t["id"] for t in bob_client.get("/api/dashboard").json()["my_tasks"]} == {ids["bob_task"]}
