import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import create_token, hash_password
from app.copilot import (
    CopilotService,
    ProviderTurn,
    WorkspaceTools,
    allowed_write_tools,
    anthropic_tool_specs,
    create_confirmation,
    openai_tool_specs,
)
from app.database import SessionLocal
from app.main import WRITE_TOOL_HANDLERS, _consume_confirmation_once, _issue_confirmation, app
from app.models import Activity, AuditEvent, CopilotConfirmation, Organization, Project, Task, User
from app.ratelimit import SlidingWindowRateLimiter


def _login(email: str, password: str) -> dict[str, str]:
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        return {"Authorization": "Bearer " + login.json()["token"]}


def _admin_user_and_headers():
    headers = _login("admin@demo.enterai.com", "enterai-demo")
    db = SessionLocal()
    user = db.scalar(select(User).where(User.email == "admin@demo.enterai.com"))
    return db, user, headers


def _create_member(db) -> tuple[User, dict[str, str]]:
    org = db.scalar(select(Organization))
    suffix = uuid4().hex[:8]
    email = f"member-{suffix}@demo.enterai.com"
    password = "member-demo-password"
    member = User(organization_id=org.id, name="Team Member", email=email, password_hash=hash_password(password), role="member")
    db.add(member)
    db.commit()
    db.refresh(member)
    return member, _login(email, password)


def _issue(token: str, issuer: User, db) -> None:
    """Make a test proposal server-issued without exposing a production bypass."""
    _issue_confirmation(token, issuer, db)
    db.commit()


def test_tool_specs_hide_admin_only_write_tools_from_members():
    assert allowed_write_tools("admin") == {"create_task", "update_task", "add_comment", "create_project", "update_project", "create_team", "delegate_task"}
    assert allowed_write_tools("member") == {"create_task", "update_task", "add_comment", "create_project", "update_project"}
    assert "create_team" not in {spec["name"] for spec in anthropic_tool_specs("member")}
    assert "create_team" in {spec["name"] for spec in anthropic_tool_specs("admin")}
    assert "create_team" not in {spec["function"]["name"] for spec in openai_tool_specs("member")}


def test_confirm_endpoint_rejects_a_write_tool_the_role_does_not_allow():
    db, _admin, _headers = _admin_user_and_headers()
    member, member_headers = _create_member(db)
    try:
        token = create_confirmation(member, "create_team", {"name": "Should not be created " + uuid4().hex[:6]})
        _issue(token, member, db)
        with TestClient(app) as client:
            response = client.post("/api/ai/confirm", headers=member_headers, json={"confirmation_token": token})
        assert response.status_code == 403
    finally:
        db.close()


def test_confirm_endpoint_allows_a_member_to_use_a_member_permitted_tool():
    db, _admin, _headers = _admin_user_and_headers()
    member, member_headers = _create_member(db)
    try:
        with TestClient(app) as client:
            project_id = client.get("/api/projects", headers=member_headers).json()[0]["id"]
        token = create_confirmation(member, "create_task", {"project_id": project_id, "title": "Member-created task"})
        _issue(token, member, db)
        with TestClient(app) as client:
            response = client.post("/api/ai/confirm", headers=member_headers, json={"confirmation_token": token})
        assert response.status_code == 200
        assert response.json()["title"] == "Member-created task"
    finally:
        db.close()


def test_confirmation_token_can_only_be_used_once():
    db, admin, headers = _admin_user_and_headers()
    try:
        with TestClient(app) as client:
            project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
            token = create_confirmation(admin, "create_task", {"project_id": project_id, "title": "One-shot task " + uuid4().hex[:6]})
            _issue(token, admin, db)
            first = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert first.status_code == 200
            redemption = db.scalar(select(CopilotConfirmation).where(
                CopilotConfirmation.token_hash == hashlib.sha256(token.encode()).hexdigest()
            ))
            assert redemption is not None
            assert redemption.token_hash != token
            assert redemption.consumed_at is not None
            second = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert second.status_code == 409
    finally:
        db.close()


def test_confirmation_cannot_be_redeemed_by_a_different_organization():
    db, admin, headers = _admin_user_and_headers()
    try:
        project_id = db.scalar(select(Project.id).where(Project.organization_id == admin.organization_id))
        token = create_confirmation(admin, "create_task", {"project_id": project_id, "title": "Owner-only confirmation"})
        _issue(token, admin, db)
        other_suffix = uuid4().hex[:8]
        other_org = Organization(
            name="Other confirmation org " + other_suffix,
            slug="other-confirmation-org-" + other_suffix,
        )
        db.add(other_org)
        db.flush()
        other_user = User(organization_id=other_org.id, name="Other admin", email=uuid4().hex + "@example.com",
                          password_hash="unused", role="admin", kind="human")
        db.add(other_user)
        db.commit()
        other_headers = {"Authorization": "Bearer " + create_token(other_user)}
        with TestClient(app) as client:
            rejected = client.post("/api/ai/confirm", headers=other_headers, json={"confirmation_token": token})
            assert rejected.status_code == 400
            owner = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert owner.status_code == 200
    finally:
        db.close()


def test_concurrent_confirmation_redemption_has_exactly_one_winner():
    """An issued proposal is atomically consumed once across database sessions."""
    db, admin, _headers = _admin_user_and_headers()
    try:
        # _issue commits, which expires ORM attributes. Never share the issuing
        # session's ORM object with worker threads; workers receive immutable IDs.
        subject_id = admin.id
        organization_id = admin.organization_id
        project_id = db.scalar(select(Project.id).where(Project.organization_id == organization_id))
        token = create_confirmation(admin, "create_task", {"project_id": project_id, "title": "Concurrent " + uuid4().hex[:6]})
        _issue(token, admin, db)
        barrier = Barrier(2)

        def redeem() -> bool:
            worker_db = SessionLocal()
            try:
                barrier.wait()
                return _consume_confirmation_once(token, subject_id, organization_id, "create_task", worker_db)
            finally:
                worker_db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: redeem(), range(2)))

        assert sorted(outcomes) == ["consumed", "used"]
        with SessionLocal() as verify_db:
            rows = verify_db.scalars(select(CopilotConfirmation).where(
                CopilotConfirmation.token_hash == hashlib.sha256(token.encode()).hexdigest()
            )).all()
        assert len(rows) == 1
        assert rows[0].consumed_at is not None
    finally:
        db.close()


def test_confirmed_write_is_recorded_with_before_and_after_state():
    db, admin, headers = _admin_user_and_headers()
    try:
        with TestClient(app) as client:
            project_id = client.get("/api/projects", headers=headers).json()[0]["id"]
            before_project = client.get(f"/api/projects/{project_id}", headers=headers).json()
            token = create_confirmation(admin, "update_project", {"project_id": project_id, "health": "off_track"})
            _issue(token, admin, db)
            response = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert response.status_code == 200

        entry = db.scalar(
            select(Activity)
            .where(Activity.entity_type == "copilot", Activity.action == "confirmed")
            .order_by(Activity.created_at.desc())
        )
        assert entry is not None
        assert entry.detail["tool"] == "update_project"
        assert entry.detail["before"]["health"] == before_project["health"]
        assert entry.detail["after"]["health"] == "off_track"
    finally:
        db.close()


def test_non_issued_signed_confirmation_cannot_execute_a_write():
    """A signature alone is never authority: only /api/ai/plan issues pending state."""
    db, admin, headers = _admin_user_and_headers()
    try:
        project_id = db.scalar(select(Project.id).where(Project.organization_id == admin.organization_id))
        title = "Non-issued signed confirmation " + uuid4().hex[:8]
        token = create_confirmation(admin, "create_task", {"project_id": project_id, "title": title})
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        before = db.scalar(select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.title == title))
        assert db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash)) is None
        with TestClient(app) as client:
            rejected = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
        after = db.scalar(select(func.count()).select_from(Task).where(Task.project_id == project_id, Task.title == title))
        assert rejected.status_code == 400
        assert after == before
        assert db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash)) is None
    finally:
        db.close()


def test_plan_issues_pending_confirmation_then_consumes_it_once():
    """The live plan endpoint persists pending state before returning a token."""
    db, admin, headers = _admin_user_and_headers()
    try:
        project = db.scalar(select(Project).where(Project.organization_id == admin.organization_id))
        with TestClient(app) as client:
            plan = client.post("/api/ai/plan", headers=headers, json={
                "message": "Create task issued lifecycle " + uuid4().hex[:8], "project_id": project.id,
            })
            assert plan.status_code == 200, plan.text
            token = plan.json()["actions"][0]["confirmation_token"]
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            pending = db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash))
            assert pending is not None
            assert pending.subject_id == admin.id
            assert pending.organization_id == admin.organization_id
            assert pending.tool == "create_task"
            assert pending.consumed_at is None
            confirmed = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert confirmed.status_code == 200
            db.expire_all()
            consumed = db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash))
            assert consumed is not None and consumed.consumed_at is not None
            replay = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert replay.status_code == 409
    finally:
        db.close()


def test_failed_confirm_handler_consumes_issued_token_without_partial_writes(monkeypatch):
    """A failed handler stays fail-closed: no retry after the durable consume point."""
    db, admin, headers = _admin_user_and_headers()
    calls = 0

    def fail_after_consumption(_args, _user, _db):
        nonlocal calls
        calls += 1
        raise TypeError("test-only handler failure")

    monkeypatch.setitem(WRITE_TOOL_HANDLERS, "create_task", fail_after_consumption)
    try:
        project = db.scalar(select(Project).where(Project.organization_id == admin.organization_id))
        with TestClient(app) as client:
            plan = client.post("/api/ai/plan", headers=headers, json={
                "message": "Create task failed-handler " + uuid4().hex[:8], "project_id": project.id,
            })
            assert plan.status_code == 200, plan.text
            token = plan.json()["actions"][0]["confirmation_token"]
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            pending = db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash))
            assert pending is not None and pending.consumed_at is None
            tasks_before = db.scalar(select(func.count()).select_from(Task).where(Task.project_id == project.id))
            activity_before = db.scalar(select(func.count()).select_from(Activity).where(Activity.organization_id == admin.organization_id))
            audit_before = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.organization_id == admin.organization_id))

            failed = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert failed.status_code == 422
            assert calls == 1
            db.expire_all()
            consumed = db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash))
            assert consumed is not None and consumed.consumed_at is not None
            assert db.scalar(select(func.count()).select_from(Task).where(Task.project_id == project.id)) == tasks_before
            assert db.scalar(select(func.count()).select_from(Activity).where(Activity.organization_id == admin.organization_id)) == activity_before
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.organization_id == admin.organization_id)) == audit_before

            replay = client.post("/api/ai/confirm", headers=headers, json={"confirmation_token": token})
            assert replay.status_code == 409
            assert calls == 1
            assert client.get("/api/me", headers=headers).status_code == 200
        assert db.execute(select(func.count()).select_from(User)).scalar() >= 1
    finally:
        db.close()


def test_workspace_tools_scoped_to_a_project_only_see_that_projects_data():
    db, admin, _headers = _admin_user_and_headers()
    try:
        unscoped = WorkspaceTools(db, admin)
        all_projects = unscoped.get_projects()
        assert len(all_projects) > 1
        target = all_projects[0]

        scoped = WorkspaceTools(db, admin, scope_project_id=target["id"])
        scoped_projects = scoped.get_projects()
        assert [p["id"] for p in scoped_projects] == [target["id"]]

        scoped_tasks = scoped.get_tasks()
        assert all(task["project_id"] == target["id"] for task in scoped_tasks)
        assert scoped_tasks
        assert len(scoped_tasks) < len(unscoped.get_tasks())
    finally:
        db.close()


def test_plan_endpoint_rejects_a_project_id_outside_the_users_organization():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        response = client.post("/api/ai/plan", headers=headers, json={"message": "hi", "project_id": "does-not-exist"})
        assert response.status_code == 404


class _RoleAwareScriptedProvider:
    def __init__(self, turns):
        self._turns = list(turns)
        self.roles_seen = []

    def respond(self, system, messages, role=None):
        self.roles_seen.append(role)
        return self._turns.pop(0)

    def assistant_message(self, turn):
        return {"role": "assistant", "tool_calls": turn.tool_calls, "text": turn.text}

    def tool_result_message(self, tool_call_id, content, is_error=False):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content, "is_error": is_error}

    def user_message(self, text):
        return {"role": "user", "content": text}


def test_tool_calling_loop_blocks_a_member_from_proposing_an_admin_only_tool():
    db, admin, _headers = _admin_user_and_headers()
    member, _member_headers = _create_member(db)
    try:
        tools = WorkspaceTools(db, member)
        provider = _RoleAwareScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "call_1", "name": "create_team", "args": {"name": "Nope"}}]),
            ProviderTurn(text="I can't create a team for you.", tool_calls=[]),
        ])
        service = CopilotService.__new__(CopilotService)
        service.provider = provider
        service.mode = "tool_calling"
        result = service.plan("Create a team called Nope", tools)
        assert result["actions"] == []
        assert result["reply"] == "I can't create a team for you."
        assert provider.roles_seen == ["member", "member"]
    finally:
        db.close()


def test_sliding_window_rate_limiter_blocks_bursts_and_recovers_after_the_window():
    limiter = SlidingWindowRateLimiter(max_calls=3, window_seconds=60)
    assert limiter.allow("user-1", now=0) is True
    assert limiter.allow("user-1", now=1) is True
    assert limiter.allow("user-1", now=2) is True
    assert limiter.allow("user-1", now=3) is False
    assert limiter.allow("user-2", now=3) is True
    assert limiter.allow("user-1", now=61) is True
