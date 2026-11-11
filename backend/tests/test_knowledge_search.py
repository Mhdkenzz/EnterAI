"""End-to-end contract tests for authorized, source-linked workspace search."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import create_token
from app.database import engine, get_db
from app.main import app
from app.models import AgentAccessGrant, Attachment, Comment, Organization, Project, ProjectDocument, Task, User


@pytest.fixture
def knowledge_workspace():
    """Use a savepoint-backed disposable fixture, including commits made by routes."""
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    suffix = uuid4().hex
    marker = f"KNOWLEDGE{suffix[:12].upper()}"
    password_hash = "unused-test-password-hash"

    org = Organization(name=f"Knowledge Org {suffix}", slug=f"knowledge-{suffix}")
    other_org = Organization(name=f"Other Knowledge Org {suffix}", slug=f"other-knowledge-{suffix}")
    db.add_all([org, other_org])
    db.flush()

    admin = User(organization_id=org.id, name="Knowledge Admin", email=f"admin-{suffix}@example.test",
                 password_hash=password_hash, role="admin", kind="human")
    manager = User(organization_id=org.id, name="Knowledge Manager", email=f"manager-{suffix}@example.test",
                   password_hash=password_hash, role="manager", kind="human")
    alice = User(organization_id=org.id, name="Alice", email=f"alice-{suffix}@example.test",
                 password_hash=password_hash, role="member", kind="human")
    bob = User(organization_id=org.id, name="Bob", email=f"bob-{suffix}@example.test",
               password_hash=password_hash, role="member", kind="human")
    outsider = User(organization_id=other_org.id, name="Outsider", email=f"outsider-{suffix}@example.test",
                    password_hash=password_hash, role="admin", kind="human")
    db.add_all([admin, manager, alice, bob, outsider])
    db.flush()

    owned_project = Project(organization_id=org.id, owner_id=alice.id, name=f"Alice owned {suffix}", code=f"AO{suffix[:6].upper()}", description=f"{marker} owned project summary")
    assigned_project = Project(organization_id=org.id, owner_id=bob.id, name=f"Alice assigned {suffix}", code=f"AA{suffix[:6].upper()}", description=f"{marker} shared project summary")
    bob_project = Project(organization_id=org.id, owner_id=bob.id, name=f"Bob private {suffix}", code=f"BP{suffix[:6].upper()}", description=f"{marker} private project summary")
    unassigned_project = Project(organization_id=org.id, owner_id=admin.id, name=f"Unassigned {suffix}", code=f"UA{suffix[:6].upper()}", description=f"{marker} unassigned project summary")
    foreign_project = Project(organization_id=other_org.id, owner_id=outsider.id, name=f"Foreign {suffix}", code=f"FO{suffix[:6].upper()}", description=f"{marker} foreign project summary")
    db.add_all([owned_project, assigned_project, bob_project, unassigned_project, foreign_project])
    db.flush()

    alice_task = Task(project_id=assigned_project.id, title=f"{marker} Alice task", assignee_id=alice.id, reporter_id=admin.id)
    bob_task = Task(project_id=assigned_project.id, title=f"{marker} Bob task", assignee_id=bob.id, reporter_id=bob.id)
    bob_private_task = Task(project_id=bob_project.id, title=f"{marker} Bob private task", assignee_id=bob.id, reporter_id=bob.id)
    unassigned_task = Task(project_id=unassigned_project.id, title=f"{marker} Unassigned task", reporter_id=admin.id)
    foreign_task = Task(project_id=foreign_project.id, title=f"{marker} Foreign task", assignee_id=outsider.id, reporter_id=outsider.id)
    db.add_all([alice_task, bob_task, bob_private_task, unassigned_task, foreign_task])
    db.flush()

    alice_comment = Comment(task_id=alice_task.id, author_id=alice.id, body=f"{marker} Alice discussion: approved the rollout window.")
    bob_comment = Comment(task_id=bob_task.id, author_id=bob.id, body=f"{marker} Bob discussion: private schedule details.")
    db.add_all([alice_comment, bob_comment])
    db.flush()

    alice_attachment = Attachment(task_id=alice_task.id, uploaded_by=alice.id, file_name=f"{marker}-alice-attachment.txt",
                                  path=f"test/{suffix}/alice-attachment", content_type="text/plain")
    bob_attachment = Attachment(task_id=bob_task.id, uploaded_by=bob.id, file_name=f"{marker}-bob-attachment.txt",
                                path=f"test/{suffix}/bob-attachment", content_type="text/plain")
    db.add_all([alice_attachment, bob_attachment])

    alice_project_document = ProjectDocument(organization_id=org.id, project_id=assigned_project.id,
        uploaded_by=alice.id, file_name=f"{marker}-alice-project.txt", path=f"test/{suffix}/alice-project",
        content_type="text/plain", extracted_text=f"{marker} Alice project knowledge.")
    bob_project_document = ProjectDocument(organization_id=org.id, project_id=bob_project.id,
        uploaded_by=bob.id, file_name=f"{marker}-bob-project.txt", path=f"test/{suffix}/bob-project",
        content_type="text/plain", extracted_text=f"{marker} Bob project confidential knowledge.")
    alice_private_document = ProjectDocument(organization_id=org.id, project_id=None,
        uploaded_by=alice.id, file_name=f"{marker}-alice-unassigned.txt", path=f"test/{suffix}/alice-unassigned",
        content_type="text/plain", extracted_text=f"{marker} Alice uploader-only confidential knowledge.")
    bob_private_document = ProjectDocument(organization_id=org.id, project_id=None,
        uploaded_by=bob.id, file_name=f"{marker}-bob-unassigned.txt", path=f"test/{suffix}/bob-unassigned",
        content_type="text/plain", extracted_text=f"{marker} Bob uploader-only confidential knowledge.")
    foreign_document = ProjectDocument(organization_id=other_org.id, project_id=foreign_project.id,
        uploaded_by=outsider.id, file_name=f"{marker}-foreign.txt", path=f"test/{suffix}/foreign",
        content_type="text/plain", extracted_text=f"{marker} cross-organization confidential knowledge.")
    db.add_all([alice_project_document, bob_project_document, alice_private_document,
                bob_private_document, foreign_document])
    db.flush()

    ids = {
        "org": org.id, "admin": admin.id, "manager": manager.id, "alice": alice.id, "bob": bob.id,
        "outsider": outsider.id, "owned_project": owned_project.id, "assigned_project": assigned_project.id,
        "bob_project": bob_project.id, "unassigned_project": unassigned_project.id,
        "foreign_project": foreign_project.id, "alice_task": alice_task.id, "bob_task": bob_task.id,
        "bob_private_task": bob_private_task.id, "unassigned_task": unassigned_task.id,
        "foreign_task": foreign_task.id, "alice_comment": alice_comment.id, "bob_comment": bob_comment.id,
        "alice_attachment": alice_attachment.id, "bob_attachment": bob_attachment.id,
        "alice_project_document": alice_project_document.id, "bob_project_document": bob_project_document.id,
        "alice_private_document": alice_private_document.id, "bob_private_document": bob_private_document.id,
        "foreign_document": foreign_document.id,
    }
    tokens = {key: create_token(user) for key, user in {
        "admin": admin, "manager": manager, "alice": alice, "bob": bob, "outsider": outsider,
    }.items()}
    db.commit()
    db.close()

    def override_get_db():
        request_db = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {"marker": marker, "ids": ids, "tokens": tokens, "connection": connection}
    finally:
        app.dependency_overrides.pop(get_db, None)
        transaction.rollback()
        connection.close()


class _TestStorage:
    def read(self, _path: str) -> bytes:
        return b"authorized fixture file"


def _headers(workspace, person: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {workspace['tokens'][person]}"}


def _assert_source_fields(items: list[dict]) -> None:
    required = {"id", "title", "snippet", "created_at", "project_id", "task_id", "source_ref"}
    for item in items:
        assert required <= item.keys(), item
        assert item["id"]
        assert item["title"]
        assert item["snippet"]
        assert item["created_at"]
        assert item["source_ref"]


def test_search_returns_typed_sources_and_explicit_decisions_keep_comment_provenance(knowledge_workspace):
    workspace = knowledge_workspace
    ids = workspace["ids"]
    client = TestClient(app)
    try:
        headers = _headers(workspace, "alice")
        capture = client.post(
            f"/api/tasks/{ids['alice_task']}/decisions/from-comment/{ids['alice_comment']}",
            headers=headers,
        )
        assert capture.status_code in {200, 201}, capture.text
        created_decision = capture.json()
        assert created_decision["source_comment_id"] == ids["alice_comment"]

        linked = client.get(f"/api/tasks/{ids['alice_task']}/decisions", headers=headers)
        assert linked.status_code == 200, linked.text
        decisions = linked.json()
        if isinstance(decisions, dict):
            decisions = decisions.get("decisions", [])
        assert any(item["source_comment_id"] == ids["alice_comment"] for item in decisions)
        decision = next(item for item in decisions if item["source_comment_id"] == ids["alice_comment"])
        assert decision["title"] and decision["body"]
        assert decision["created_at"] and decision["source_ref"]

        response = client.get("/api/search", params={"q": workspace["marker"]}, headers=_headers(workspace, "admin"))
        assert response.status_code == 200, response.text
        result = response.json()
        for category in ("projects", "tasks", "documents", "attachments", "discussions", "decisions"):
            assert isinstance(result.get(category), list), result
        assert result["projects"] and result["tasks"]  # Keep legacy result groups.
        for category in ("documents", "attachments", "discussions", "decisions"):
            _assert_source_fields(result[category])

        assert ids["alice_project_document"] in {item["id"] for item in result["documents"]}
        assert ids["bob_project_document"] in {item["id"] for item in result["documents"]}
        assert ids["alice_attachment"] in {item["id"] for item in result["attachments"]}
        assert ids["bob_attachment"] in {item["id"] for item in result["attachments"]}
        assert ids["alice_comment"] in {item["id"] for item in result["discussions"]}
        assert ids["bob_comment"] in {item["id"] for item in result["discussions"]}
        assert decision["id"] in {item["id"] for item in result["decisions"]}

        # Manager access is workspace-wide within the tenant, including unassigned docs.
        manager_search = client.get("/api/search", params={"q": workspace["marker"]}, headers=_headers(workspace, "manager"))
        assert manager_search.status_code == 200, manager_search.text
        assert ids["alice_private_document"] in {item["id"] for item in manager_search.json()["documents"]}
        assert ids["bob_private_document"] in {item["id"] for item in manager_search.json()["documents"]}
    finally:
        client.close()


def test_member_search_and_document_download_respect_project_and_uploader_scope(knowledge_workspace, monkeypatch):
    workspace = knowledge_workspace
    ids = workspace["ids"]
    monkeypatch.setattr("app.main.get_storage", lambda: _TestStorage())
    client = TestClient(app)
    try:
        alice = client.get("/api/search", params={"q": workspace["marker"]}, headers=_headers(workspace, "alice"))
        assert alice.status_code == 200, alice.text
        alice_result = alice.json()
        assert {p["id"] for p in alice_result["projects"]} == {ids["owned_project"], ids["assigned_project"], ids["bob_project"], ids["unassigned_project"]}
        assert {t["id"] for t in alice_result["tasks"]} == {ids["alice_task"], ids["bob_task"], ids["bob_private_task"], ids["unassigned_task"]}
        assert ids["alice_project_document"] in {item["id"] for item in alice_result["documents"]}
        assert ids["alice_private_document"] in {item["id"] for item in alice_result["documents"]}
        assert ids["bob_project_document"] in {item["id"] for item in alice_result["documents"]}
        assert ids["bob_private_document"] not in {item["id"] for item in alice_result["documents"]}
        assert ids["foreign_document"] not in {item["id"] for item in alice_result["documents"]}
        assert ids["alice_comment"] in {item["id"] for item in alice_result["discussions"]}
        assert ids["bob_comment"] in {item["id"] for item in alice_result["discussions"]}
        assert ids["alice_attachment"] in {item["id"] for item in alice_result["attachments"]}
        assert ids["bob_attachment"] in {item["id"] for item in alice_result["attachments"]}

        serialized = str(alice_result).casefold()
        assert "bob project confidential knowledge" in serialized
        assert "bob uploader-only confidential knowledge" not in serialized
        assert "cross-organization confidential knowledge" not in serialized

        bob_headers = _headers(workspace, "bob")
        assert client.get(f"/api/project-documents/{ids['alice_private_document']}/download", headers=bob_headers).status_code == 404
        assert client.get(f"/api/project-documents/{ids['foreign_document']}/download", headers=bob_headers).status_code == 404
        assert client.get(f"/api/project-documents/{ids['bob_private_document']}/download", headers=_headers(workspace, "alice")).status_code == 404
        assert client.get(f"/api/project-documents/{ids['alice_private_document']}/download", headers=_headers(workspace, "alice")).status_code == 200

        for manager in ("admin", "manager"):
            admin_headers = _headers(workspace, manager)
            assert client.get(f"/api/project-documents/{ids['alice_private_document']}/download", headers=admin_headers).status_code == 200
            assert client.get(f"/api/project-documents/{ids['foreign_document']}/download", headers=admin_headers).status_code == 404

        outsider_result = client.get("/api/search", params={"q": workspace["marker"]}, headers=_headers(workspace, "outsider"))
        assert outsider_result.status_code == 200, outsider_result.text
        outsider_items = outsider_result.json()
        assert {item["id"] for item in outsider_items["projects"]} == {ids["foreign_project"]}
        assert {item["id"] for item in outsider_items["tasks"]} == {ids["foreign_task"]}
        assert ids["alice_project_document"] not in {item["id"] for item in outsider_items["documents"]}
    finally:
        client.close()


def test_member_cannot_attach_another_users_private_document_to_a_new_project(knowledge_workspace):
    workspace = knowledge_workspace
    ids = workspace["ids"]
    client = TestClient(app)
    try:
        response = client.post("/api/projects", headers=_headers(workspace, "alice"), json={
            "name": "Alice knowledge project",
            "code": f"AK{uuid4().hex[:8].upper()}",
            "source_document_ids": [ids["alice_private_document"], ids["bob_private_document"]],
        })
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]

        with Session(bind=engine) as db:
            assert db.get(ProjectDocument, ids["alice_private_document"]).project_id == project_id
            assert db.get(ProjectDocument, ids["bob_private_document"]).project_id is None
    finally:
        client.close()


def test_worker_knowledge_search_is_project_scoped_and_tracks_citations(knowledge_workspace):
    from app.copilot import WorkspaceTools, execute_read_tool

    workspace = knowledge_workspace
    ids = workspace["ids"]
    with Session(bind=workspace["connection"], join_transaction_mode="create_savepoint") as db:
        worker = User(
            organization_id=ids["org"], name="Assigned worker", email=f"worker-{uuid4().hex}@example.test",
            password_hash="unused", kind="agent", hierarchy_level="worker",
        )
        db.add(worker)
        db.flush()
        worker_task = Task(
            project_id=ids["assigned_project"], title=f"{workspace['marker']} worker assigned task",
            assignee_id=worker.id, reporter_id=ids["admin"],
        )
        db.add(worker_task)
        db.flush()
        worker_attachment = Attachment(
            task_id=worker_task.id, uploaded_by=worker.id,
            file_name=f"{workspace['marker']}-worker-attachment.txt", path="test/worker-attachment",
        )
        db.add(worker_attachment)
        db.flush()
        db.add(AgentAccessGrant(
            organization_id=ids["org"], agent_id=worker.id, resource_type="document",
            resource_id=ids["alice_project_document"], permission="read", granted_by=ids["admin"],
        ))
        db.flush()

        tools = WorkspaceTools(db, worker, scope_project_id=ids["assigned_project"])
        results = execute_read_tool("search_knowledge", {"query": workspace["marker"]}, tools)
        assert worker_task.id in {item["id"] for item in results if item["type"] == "task"}
        assert ids["alice_task"] not in {item["id"] for item in results if item["type"] == "task"}
        assert ids["bob_private_task"] not in {item["id"] for item in results if item["type"] == "task"}
        assert ids["alice_project_document"] in {item["id"] for item in results if item["type"] == "document"}
        assert ids["alice_private_document"] not in {item["id"] for item in results if item["type"] == "document"}
        visible_attachments = [item for item in results if item["type"] == "attachment"]
        assert [item["id"] for item in visible_attachments] == [worker_attachment.id]
        assert visible_attachments[0]["source_ref"] == f"/api/attachments/{worker_attachment.id}/download"
        assert results and all(item["created_at"] and item["source_ref"] for item in results)
        assert tools.used_sources and all(source["source_ref"] for source in tools.used_sources)
