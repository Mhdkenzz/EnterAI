"""Phase 13: privacy, deletion, and retention -- service-level behavior.

Covers deletion completeness, export scoping (cross-org/cross-user isolation),
audit continuity across anonymization, and retention cleanup. Route-level auth
and re-auth gating live in test_privacy_routes.py.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session():
    from app.database import Base
    import app.models  # noqa: F401 -- registers every table on Base.metadata before create_all
    import app.privacy_models  # noqa: F401
    import app.billing_models  # noqa: F401
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _org(db, name="Org"):
    from app.models import Organization
    org = Organization(name=name, slug=name.lower().replace(" ", "-") + "-" + uuid4().hex[:8])
    db.add(org); db.flush()
    return org


def _user(db, org, *, email=None, role="member", kind="human"):
    from app.models import User
    u = User(organization_id=org.id, name="Member", email=email or f"{uuid4().hex}@example.com",
             password_hash="x", role=role, kind=kind)
    db.add(u); db.flush()
    return u


def _project(db, org):
    from app.models import Project
    p = Project(organization_id=org.id, name="P", code="P1")
    db.add(p); db.flush()
    return p


def _task(db, project, *, assignee=None, reporter=None):
    from app.models import Task
    t = Task(project_id=project.id, title="T", assignee_id=assignee.id if assignee else None,
             reporter_id=reporter.id if reporter else None)
    db.add(t); db.flush()
    return t


# --------------------------------------------------------------------------- #
# Anonymization
# --------------------------------------------------------------------------- #

def test_anonymize_user_scrubs_pii_but_preserves_id_and_references(db_session):
    from app.privacy_service import anonymize_user
    from app.models import Comment
    org = _org(db_session)
    user = _user(db_session, org, email="real.person@example.com")
    project = _project(db_session, org)
    task = _task(db_session, project, reporter=user)
    comment = Comment(task_id=task.id, author_id=user.id, body="hi")
    db_session.add(comment); db_session.flush()
    original_id = user.id

    anonymize_user(db_session, user, actor_id=user.id)

    assert user.id == original_id  # row survives, id unchanged
    assert "real.person" not in user.email
    assert user.name != "Member"
    assert user.password_hash != "x"
    assert user.active is False
    assert user.anonymized_at is not None
    # every FK that pointed at this user still resolves
    assert db_session.get(Comment, comment.id).author_id == user.id
    assert task.reporter_id == user.id


def test_anonymize_user_bumps_session_epoch_to_kill_live_tokens(db_session):
    from app.privacy_service import anonymize_user
    org = _org(db_session)
    user = _user(db_session, org)
    before = user.session_epoch or 0
    anonymize_user(db_session, user, actor_id=user.id)
    assert (user.session_epoch or 0) == before + 1


def test_anonymize_user_is_idempotent(db_session):
    from app.privacy_service import anonymize_user
    org = _org(db_session)
    user = _user(db_session, org)
    anonymize_user(db_session, user, actor_id=user.id)
    first_email, first_epoch = user.email, user.session_epoch
    anonymize_user(db_session, user, actor_id=user.id)  # second call must no-op
    assert user.email == first_email
    assert user.session_epoch == first_epoch


def test_audit_events_survive_user_anonymization(db_session):
    """Audit continuity: an action a user took before deletion must still show up
    in the audit trail afterward, still attributed to that (now anonymized) id."""
    from app.privacy_service import anonymize_user
    from app.services import log
    from app.models import AuditEvent
    org = _org(db_session)
    user = _user(db_session, org)
    log(db_session, org.id, user.id, "task", "task-123", "created")
    db_session.flush()

    anonymize_user(db_session, user, actor_id=user.id)

    events = db_session.scalars(select(AuditEvent).where(AuditEvent.actor_id == user.id)).all()
    assert len(events) >= 1
    assert any(e.action == "created" and e.entity_id == "task-123" for e in events)


# --------------------------------------------------------------------------- #
# Export scoping
# --------------------------------------------------------------------------- #

def test_export_user_data_excludes_other_users_content(db_session):
    from app.privacy_service import export_user_data
    from app.models import Comment
    org = _org(db_session)
    me = _user(db_session, org, email="me@example.com")
    someone_else = _user(db_session, org, email="other@example.com")
    project = _project(db_session, org)
    my_task = _task(db_session, project, reporter=me)
    their_task = _task(db_session, project, reporter=someone_else)
    db_session.add(Comment(task_id=my_task.id, author_id=me.id, body="mine"))
    db_session.add(Comment(task_id=their_task.id, author_id=someone_else.id, body="theirs"))
    db_session.flush()

    export = export_user_data(db_session, me)

    task_ids = {t["id"] for t in export["tasks"]}
    assert my_task.id in task_ids
    assert their_task.id not in task_ids
    bodies = {c["body"] for c in export["comments"]}
    assert bodies == {"mine"}


def test_export_organization_data_excludes_other_organizations(db_session):
    from app.privacy_service import export_organization_data
    org_a = _org(db_session, "Org A")
    org_b = _org(db_session, "Org B")
    user_a = _user(db_session, org_a, email="a@example.com")
    user_b = _user(db_session, org_b, email="b@example.com")
    project_a = _project(db_session, org_a)
    project_b = _project(db_session, org_b)
    _task(db_session, project_a, reporter=user_a)
    _task(db_session, project_b, reporter=user_b)

    export = export_organization_data(db_session, org_a.id)

    assert export["organization"]["id"] == org_a.id
    assert {u["id"] for u in export["users"]} == {user_a.id}
    assert {p["id"] for p in export["projects"]} == {project_a.id}
    task_ids = {t["id"] for t in export["tasks"]}
    assert task_ids  # at least this org's task is present
    for t in export["tasks"]:
        assert t["project_id"] == project_a.id


# --------------------------------------------------------------------------- #
# Organization deletion completeness
# --------------------------------------------------------------------------- #

def test_delete_organization_data_removes_everything_scoped_to_it(db_session):
    from app.privacy_service import delete_organization_data
    from app.models import (AgentMessage, Attachment, AuditEvent, Comment, Organization,
                            Project, ProjectDocument, Task, Team, User)
    from app.services import log
    org = _org(db_session)
    user = _user(db_session, org)
    agent = _user(db_session, org, email="agent@example.com", kind="agent")
    from app.models import Team as TeamModel
    team = TeamModel(organization_id=org.id, name="Team")
    db_session.add(team); db_session.flush()
    project = _project(db_session, org)
    project.team_id = team.id
    project.owner_id = user.id
    task = _task(db_session, project, assignee=user, reporter=user)
    user.current_task_id = task.id
    db_session.add(Comment(task_id=task.id, author_id=user.id, body="c"))
    db_session.add(Attachment(task_id=task.id, uploaded_by=user.id, file_name="f", path="/f"))
    db_session.add(ProjectDocument(organization_id=org.id, project_id=project.id, uploaded_by=user.id,
                                   file_name="d.txt", path="/d.txt", extracted_text="hi"))
    db_session.add(AgentMessage(agent_id=agent.id, author_id=user.id, role="user", body="hi"))
    log(db_session, org.id, user.id, "task", task.id, "created")
    db_session.flush()

    delete_organization_data(db_session, org.id)

    assert db_session.get(Organization, org.id) is None
    assert db_session.scalars(select(User).where(User.organization_id == org.id)).all() == []
    assert db_session.scalars(select(Task).where(Task.project_id == project.id)).all() == []
    assert db_session.scalars(select(Project).where(Project.organization_id == org.id)).all() == []
    assert db_session.scalars(select(Team).where(Team.organization_id == org.id)).all() == []
    assert db_session.scalars(select(Comment).where(Comment.task_id == task.id)).all() == []
    assert db_session.scalars(select(Attachment).where(Attachment.task_id == task.id)).all() == []
    assert db_session.scalars(select(ProjectDocument).where(ProjectDocument.organization_id == org.id)).all() == []
    assert db_session.scalars(select(AgentMessage).where(AgentMessage.agent_id == agent.id)).all() == []
    assert db_session.scalars(select(AuditEvent).where(AuditEvent.organization_id == org.id)).all() == []


def test_delete_organization_data_does_not_touch_other_organizations(db_session):
    from app.privacy_service import delete_organization_data
    from app.models import Organization, Task, User
    org_a = _org(db_session, "Org A")
    org_b = _org(db_session, "Org B")
    user_b = _user(db_session, org_b)
    project_b = _project(db_session, org_b)
    task_b = _task(db_session, project_b, reporter=user_b)

    delete_organization_data(db_session, org_a.id)

    assert db_session.get(Organization, org_b.id) is not None
    assert db_session.get(User, user_b.id) is not None
    assert db_session.get(Task, task_b.id) is not None


# --------------------------------------------------------------------------- #
# Deletion-request workflow
# --------------------------------------------------------------------------- #

def test_request_deletion_is_idempotent_per_target(db_session):
    from app.privacy_service import request_deletion
    org = _org(db_session)
    user = _user(db_session, org)
    first = request_deletion(db_session, organization_id=org.id, requested_by=user.id,
                             target_type="user", target_id=user.id)
    second = request_deletion(db_session, organization_id=org.id, requested_by=user.id,
                              target_type="user", target_id=user.id)
    assert first.id == second.id


def test_cancel_deletion_only_works_while_pending(db_session):
    from app.privacy_service import cancel_deletion, request_deletion
    org = _org(db_session)
    user = _user(db_session, org)
    request = request_deletion(db_session, organization_id=org.id, requested_by=user.id,
                               target_type="user", target_id=user.id)
    cancel_deletion(db_session, request, actor_id=user.id)
    assert request.status == "canceled"
    with pytest.raises(ValueError):
        cancel_deletion(db_session, request, actor_id=user.id)


def test_execute_due_deletions_ignores_requests_still_in_their_grace_period(db_session):
    from app.privacy_service import execute_due_deletions, request_deletion
    org = _org(db_session)
    user = _user(db_session, org)
    request_deletion(db_session, organization_id=org.id, requested_by=user.id,
                     target_type="user", target_id=user.id, grace_days=14)
    db_session.commit()
    result = execute_due_deletions(db_session, now=datetime.now(timezone.utc))
    assert result == {"users_anonymized": 0, "organizations_deleted": 0}
    assert user.anonymized_at is None


def test_execute_due_deletions_anonymizes_a_due_user_request(db_session):
    from app.privacy_service import execute_due_deletions, request_deletion
    org = _org(db_session)
    user = _user(db_session, org)
    request_deletion(db_session, organization_id=org.id, requested_by=user.id,
                     target_type="user", target_id=user.id, grace_days=1)
    db_session.commit()
    result = execute_due_deletions(db_session, now=datetime.now(timezone.utc) + timedelta(days=2))
    assert result["users_anonymized"] == 1
    assert user.anonymized_at is not None


def test_execute_due_deletions_deletes_a_due_organization_request(db_session):
    from app.privacy_service import execute_due_deletions, request_deletion
    from app.models import Organization
    org = _org(db_session)
    org_id = org.id
    admin = _user(db_session, org, role="admin")
    request_deletion(db_session, organization_id=org_id, requested_by=admin.id,
                     target_type="organization", target_id=org_id, grace_days=1)
    db_session.commit()
    result = execute_due_deletions(db_session, now=datetime.now(timezone.utc) + timedelta(days=2))
    assert result["organizations_deleted"] == 1
    # execute_due_deletions committed internally, expiring `org` -- look it up by
    # the id captured before deletion rather than touching the expired instance.
    assert db_session.get(Organization, org_id) is None


# --------------------------------------------------------------------------- #
# Retention cleanup
# --------------------------------------------------------------------------- #

def _policy(db, org, **kwargs):
    from app.privacy_models import RetentionPolicy
    p = RetentionPolicy(organization_id=org.id, **kwargs)
    db.add(p); db.flush()
    return p


def test_retention_cleanup_purges_old_chats_but_keeps_recent_ones(db_session):
    from app.privacy_service import run_retention_cleanup
    from app.models import AgentMessage
    org = _org(db_session)
    agent = _user(db_session, org, kind="agent")
    _policy(db_session, org, chat_retention_days=30)
    old = AgentMessage(agent_id=agent.id, role="user", body="old")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=60)
    recent = AgentMessage(agent_id=agent.id, role="user", body="recent")
    db_session.add_all([old, recent]); db_session.commit()

    totals = run_retention_cleanup(db_session)

    assert totals["chats"] == 1
    remaining = db_session.scalars(select(AgentMessage).where(AgentMessage.agent_id == agent.id)).all()
    assert [m.body for m in remaining] == ["recent"]


def test_retention_cleanup_purges_old_audit_events(db_session):
    from app.privacy_service import run_retention_cleanup
    from app.models import AuditEvent
    org = _org(db_session)
    _policy(db_session, org, audit_retention_days=90)
    old = AuditEvent(organization_id=org.id, source="human", action="x", entity_type="task", entity_id="1")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=200)
    db_session.add(old); db_session.commit()

    totals = run_retention_cleanup(db_session)

    assert totals["audit"] == 1
    assert db_session.scalars(select(AuditEvent).where(AuditEvent.organization_id == org.id)).all() == []


def test_retention_cleanup_purges_old_documents_and_their_chunks(db_session):
    from app.privacy_service import run_retention_cleanup
    from app.models import DocumentChunk, ProjectDocument
    org = _org(db_session)
    user = _user(db_session, org)
    _policy(db_session, org, document_retention_days=30)
    doc = ProjectDocument(organization_id=org.id, uploaded_by=user.id, file_name="old.txt",
                          path="/old.txt", extracted_text="x")
    doc.created_at = datetime.now(timezone.utc) - timedelta(days=90)
    db_session.add(doc); db_session.flush()
    doc_id = doc.id
    chunk = DocumentChunk(organization_id=org.id, document_id=doc_id, chunk_index=0,
                          content="x", token_count=1)
    db_session.add(chunk); db_session.commit()

    totals = run_retention_cleanup(db_session)

    # run_retention_cleanup committed internally, expiring `doc` -- use the id
    # captured before deletion rather than touching the expired instance.
    assert totals["documents"] == 1
    assert db_session.scalars(select(ProjectDocument).where(ProjectDocument.id == doc_id)).all() == []
    assert db_session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)).all() == []


def test_retention_cleanup_leaves_orgs_without_a_policy_untouched(db_session):
    from app.privacy_service import run_retention_cleanup
    from app.models import AuditEvent
    org = _org(db_session)  # no RetentionPolicy row at all
    old = AuditEvent(organization_id=org.id, source="human", action="x", entity_type="task", entity_id="1")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=1000)
    db_session.add(old); db_session.commit()

    run_retention_cleanup(db_session)

    assert db_session.scalars(select(AuditEvent).where(AuditEvent.organization_id == org.id)).all() != []


def test_retention_cleanup_is_scoped_per_organization(db_session):
    """One org's retention policy must never purge another org's data."""
    from app.privacy_service import run_retention_cleanup
    from app.models import AuditEvent
    org_a = _org(db_session, "Org A")
    org_b = _org(db_session, "Org B")
    _policy(db_session, org_a, audit_retention_days=1)
    # org_b has no policy at all
    old_a = AuditEvent(organization_id=org_a.id, source="human", action="x", entity_type="task", entity_id="1")
    old_a.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    old_b = AuditEvent(organization_id=org_b.id, source="human", action="x", entity_type="task", entity_id="1")
    old_b.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    db_session.add_all([old_a, old_b]); db_session.commit()

    run_retention_cleanup(db_session)

    assert db_session.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_a.id)).all() == []
    assert len(db_session.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_b.id)).all()) == 1
