"""Data export, account/org deletion, and retention cleanup (Phase 13).

Two different things are both called "deletion" here, deliberately kept distinct:

* A **user** is anonymized in place (see `anonymize_user`), never row-deleted --
  the same reasoning as `hierarchy.retire_agent` for agents: tasks, comments,
  documents, and audit rows that reference this user's id must keep working and
  stay attributable to *someone*, not go orphaned or get silently reassigned to a
  different real person.
* An **organization** is hard-deleted (see `delete_organization_data`): once the
  tenant itself is gone there is nothing left that needs its data to survive, and
  keeping it around would be exactly the orphaned-PII outcome deletion exists to
  prevent. Deletion walks child tables before parents in the same dependency
  order 0001_initial's downgrade() already established for this schema's FK
  cycles (users<->tasks, projects->teams, etc).

Both paths go through a DeletionRequest with a grace period, so a self-service
delete can be undone before it runs (`request_deletion` / `cancel_deletion` /
`execute_due_deletions`).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from .auth import hash_password
from .models import (Activity, AgentMessage, Attachment, AuditEvent, AuthToken, Comment,
                     DocumentChunk, ExecutionRun, HierarchyConfig, Invite, Notification,
                     Organization, Project, ProjectDocument, ProviderCall, Task, Team, User)
from .privacy_models import DeletionRequest, RetentionPolicy
from .services import log

DEFAULT_GRACE_DAYS = 14


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def export_user_data(db: Session, user: User) -> dict:
    """Everything scoped to one user: their profile and everything they authored
    or were assigned. Never includes other users' data, even within the same org."""
    tasks = db.scalars(select(Task).where((Task.assignee_id == user.id) | (Task.reporter_id == user.id))).all()
    comments = db.scalars(select(Comment).where(Comment.author_id == user.id)).all()
    documents = db.scalars(select(ProjectDocument).where(ProjectDocument.uploaded_by == user.id)).all()
    messages = db.scalars(select(AgentMessage).where(AgentMessage.author_id == user.id)).all()
    audit_events = db.scalars(select(AuditEvent).where(AuditEvent.actor_id == user.id)).all()
    return {
        "profile": {"id": user.id, "name": user.name, "email": user.email, "role": user.role,
                    "title": user.title, "created_at": user.created_at.isoformat()},
        "tasks": [{"id": t.id, "project_id": t.project_id, "title": t.title, "status": t.status,
                   "assignee": t.assignee_id == user.id, "reporter": t.reporter_id == user.id,
                   "created_at": t.created_at.isoformat()} for t in tasks],
        "comments": [{"id": c.id, "task_id": c.task_id, "body": c.body,
                      "created_at": c.created_at.isoformat()} for c in comments],
        "documents": [{"id": d.id, "file_name": d.file_name, "project_id": d.project_id,
                       "created_at": d.created_at.isoformat()} for d in documents],
        "agent_messages": [{"id": m.id, "agent_id": m.agent_id, "body": m.body,
                            "created_at": m.created_at.isoformat()} for m in messages],
        "audit_events": [{"id": a.id, "action": a.action, "entity_type": a.entity_type,
                          "entity_id": a.entity_id, "created_at": a.created_at.isoformat()} for a in audit_events],
    }


def export_organization_data(db: Session, organization_id: str) -> dict:
    """Everything scoped to the organization. Every query below filters by
    organization_id (directly, or through a join for tables that only carry it
    indirectly) -- see test_privacy.py's cross-org isolation tests for what this
    must never leak."""
    org = db.get(Organization, organization_id)
    users = db.scalars(select(User).where(User.organization_id == organization_id)).all()
    teams = db.scalars(select(Team).where(Team.organization_id == organization_id)).all()
    projects = db.scalars(select(Project).where(Project.organization_id == organization_id)).all()
    project_ids = [p.id for p in projects]
    tasks = db.scalars(select(Task).where(Task.project_id.in_(project_ids))).all() if project_ids else []
    documents = db.scalars(select(ProjectDocument).where(ProjectDocument.organization_id == organization_id)).all()
    audit_events = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == organization_id)).all()
    return {
        "organization": {"id": org.id, "name": org.name, "slug": org.slug,
                         "created_at": org.created_at.isoformat()} if org else None,
        "users": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role,
                   "kind": u.kind, "active": u.active} for u in users],
        "teams": [{"id": t.id, "name": t.name} for t in teams],
        "projects": [{"id": p.id, "name": p.name, "code": p.code, "status": p.status} for p in projects],
        "tasks": [{"id": t.id, "project_id": t.project_id, "title": t.title, "status": t.status,
                   "assignee_id": t.assignee_id} for t in tasks],
        "documents": [{"id": d.id, "file_name": d.file_name, "project_id": d.project_id} for d in documents],
        "audit_events": [{"id": a.id, "action": a.action, "entity_type": a.entity_type,
                          "entity_id": a.entity_id, "created_at": a.created_at.isoformat()} for a in audit_events],
    }


# --------------------------------------------------------------------------- #
# Anonymization (user)
# --------------------------------------------------------------------------- #

def anonymize_user(db: Session, user: User, *, actor_id: str | None) -> None:
    """Scrub PII in place; the row, its id, and every FK pointing at it survive."""
    if user.anonymized_at is not None:
        return
    placeholder = f"deleted-user-{user.id}@deleted.enterai.local"
    user.name = "Deleted user"
    user.email = placeholder
    user.password_hash = hash_password(uuid4().hex)  # unusable; nobody knows this value
    user.title = None
    user.avatar = None
    user.active = False
    user.anonymized_at = datetime.now(timezone.utc)
    user.session_epoch = (user.session_epoch or 0) + 1  # kill every live session token
    log(db, user.organization_id, actor_id, "user", user.id, "anonymized")
    db.flush()


# --------------------------------------------------------------------------- #
# Organization deletion
# --------------------------------------------------------------------------- #

def delete_organization_data(db: Session, organization_id: str) -> None:
    """Hard-delete everything scoped to this organization, then the org itself.

    Order matters: break the users<->tasks and projects->users FK cycles first
    (null the pointer columns), then delete leaf tables before the tables they
    point at, mirroring 0001_initial.downgrade()'s table order for this schema.
    """
    user_ids = list(db.scalars(select(User.id).where(User.organization_id == organization_id)))
    project_ids = list(db.scalars(select(Project.id).where(Project.organization_id == organization_id)))
    task_ids = list(db.scalars(select(Task.id).where(Task.project_id.in_(project_ids)))) if project_ids else []

    # Break cycles: users<->tasks (current/last-completed pointers), tasks<->tasks
    # (subtasks), projects->users (owner).
    if user_ids:
        db.execute(update(User).where(User.id.in_(user_ids)).values(
            current_task_id=None, last_completed_task_id=None, parent_agent_id=None))
    if task_ids:
        db.execute(update(Task).where(Task.id.in_(task_ids)).values(parent_id=None))
    if project_ids:
        db.execute(update(Project).where(Project.id.in_(project_ids)).values(owner_id=None))
    db.flush()

    # Leaves first.
    if task_ids:
        db.execute(delete(Comment).where(Comment.task_id.in_(task_ids)))
        db.execute(delete(Attachment).where(Attachment.task_id.in_(task_ids)))
    db.execute(delete(DocumentChunk).where(DocumentChunk.organization_id == organization_id))
    db.execute(delete(ProjectDocument).where(ProjectDocument.organization_id == organization_id))
    if user_ids:
        db.execute(delete(AgentMessage).where(AgentMessage.agent_id.in_(user_ids)))
        db.execute(delete(AgentMessage).where(AgentMessage.author_id.in_(user_ids)))
        db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
        db.execute(delete(AuthToken).where(AuthToken.user_id.in_(user_ids)))
    db.execute(delete(Invite).where(Invite.organization_id == organization_id))
    db.execute(delete(HierarchyConfig).where(HierarchyConfig.organization_id == organization_id))

    # Then the tables those leaves pointed at.
    if task_ids:
        db.execute(delete(Task).where(Task.id.in_(task_ids)))
    if project_ids:
        db.execute(delete(Project).where(Project.id.in_(project_ids)))
    db.execute(delete(Team).where(Team.organization_id == organization_id))

    # Ledger and billing rows scoped to this org: nothing outside the org can
    # need these once the org itself is gone.
    db.execute(delete(AuditEvent).where(AuditEvent.organization_id == organization_id))
    db.execute(delete(Activity).where(Activity.organization_id == organization_id))
    db.execute(delete(ExecutionRun).where(ExecutionRun.organization_id == organization_id))
    db.execute(delete(ProviderCall).where(ProviderCall.organization_id == organization_id))
    try:
        from .billing_models import OrganizationSubscription, UsageRecord
        db.execute(delete(UsageRecord).where(UsageRecord.organization_id == organization_id))
        db.execute(delete(OrganizationSubscription).where(OrganizationSubscription.organization_id == organization_id))
    except ImportError:  # pragma: no cover - billing_models always present in this repo
        pass
    db.execute(delete(RetentionPolicy).where(RetentionPolicy.organization_id == organization_id))

    if user_ids:
        db.execute(delete(User).where(User.id.in_(user_ids)))
    db.execute(delete(Organization).where(Organization.id == organization_id))
    db.flush()


# --------------------------------------------------------------------------- #
# Deletion-request workflow
# --------------------------------------------------------------------------- #

def request_deletion(db: Session, *, organization_id: str, requested_by: str, target_type: str,
                     target_id: str, reason: str | None = None, grace_days: int = DEFAULT_GRACE_DAYS) -> DeletionRequest:
    if target_type not in ("user", "organization"):
        raise ValueError(f"Unknown deletion target_type: {target_type!r}")
    existing = db.scalars(select(DeletionRequest).where(
        DeletionRequest.target_type == target_type, DeletionRequest.target_id == target_id,
        DeletionRequest.status == "pending")).first()
    if existing:
        return existing
    request = DeletionRequest(
        organization_id=organization_id, requested_by=requested_by, target_type=target_type,
        target_id=target_id, reason=reason,
        scheduled_for=datetime.now(timezone.utc) + timedelta(days=grace_days),
    )
    db.add(request)
    log(db, organization_id, requested_by, target_type, target_id, "deletion_requested")
    db.flush()
    return request


def cancel_deletion(db: Session, request: DeletionRequest, *, actor_id: str) -> DeletionRequest:
    if request.status != "pending":
        raise ValueError(f"Cannot cancel a deletion request in status {request.status!r}")
    request.status = "canceled"
    request.canceled_at = datetime.now(timezone.utc)
    log(db, request.organization_id, actor_id, request.target_type, request.target_id, "deletion_canceled")
    db.flush()
    return request


def execute_due_deletions(db: Session, *, now: datetime | None = None) -> dict:
    """Run every pending deletion whose grace period has elapsed. Safe to call
    repeatedly (e.g. from a periodic job or an admin-triggered endpoint): rows
    already completed or canceled are simply not selected again."""
    cutoff = now or datetime.now(timezone.utc)
    due = db.scalars(select(DeletionRequest).where(
        DeletionRequest.status == "pending", DeletionRequest.scheduled_for <= cutoff)).all()
    completed_users, completed_orgs = 0, 0
    for request in due:
        if request.target_type == "user":
            user = db.get(User, request.target_id)
            if user is not None:
                anonymize_user(db, user, actor_id=request.requested_by)
            completed_users += 1
        else:
            delete_organization_data(db, request.target_id)
            completed_orgs += 1
        request.status = "completed"
        request.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"users_anonymized": completed_users, "organizations_deleted": completed_orgs}


# --------------------------------------------------------------------------- #
# Retention cleanup
# --------------------------------------------------------------------------- #

def run_retention_cleanup(db: Session, *, organization_id: str | None = None, now: datetime | None = None) -> dict:
    """Purge records older than each org's configured retention window. An org
    with no RetentionPolicy row, or a null field on one, keeps that category
    forever -- retention is opt-in, not a default that silently starts deleting
    a workspace's history. `organization_id` scopes this to one org (an admin
    running cleanup on demand); omitted, it covers every org (the periodic job)."""
    cutoff_now = now or datetime.now(timezone.utc)
    totals = {"chats": 0, "audit": 0, "activity": 0, "documents": 0}
    stmt = select(RetentionPolicy)
    if organization_id is not None:
        stmt = stmt.where(RetentionPolicy.organization_id == organization_id)
    for policy in db.scalars(stmt):
        org_id = policy.organization_id

        if policy.chat_retention_days is not None:
            cutoff = cutoff_now - timedelta(days=policy.chat_retention_days)
            agent_ids = select(User.id).where(User.organization_id == org_id)
            result = db.execute(delete(AgentMessage).where(
                AgentMessage.agent_id.in_(agent_ids), AgentMessage.created_at < cutoff))
            totals["chats"] += result.rowcount or 0

        if policy.audit_retention_days is not None:
            cutoff = cutoff_now - timedelta(days=policy.audit_retention_days)
            result = db.execute(delete(AuditEvent).where(
                AuditEvent.organization_id == org_id, AuditEvent.created_at < cutoff))
            totals["audit"] += result.rowcount or 0

        if policy.activity_retention_days is not None:
            cutoff = cutoff_now - timedelta(days=policy.activity_retention_days)
            result = db.execute(delete(Activity).where(
                Activity.organization_id == org_id, Activity.created_at < cutoff))
            totals["activity"] += result.rowcount or 0

        if policy.document_retention_days is not None:
            cutoff = cutoff_now - timedelta(days=policy.document_retention_days)
            stale_ids = list(db.scalars(select(ProjectDocument.id).where(
                ProjectDocument.organization_id == org_id, ProjectDocument.created_at < cutoff)))
            if stale_ids:
                db.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(stale_ids)))
                result = db.execute(delete(ProjectDocument).where(ProjectDocument.id.in_(stale_ids)))
                totals["documents"] += result.rowcount or 0
    db.commit()
    return totals


# --------------------------------------------------------------------------- #
# Periodic background job
# --------------------------------------------------------------------------- #

# Off by default (0). An operator opts in by setting an interval, the same
# fail-safe-by-default shape as execution.py's scheduler -- retention deletes
# data, so it should never start running just because the app booted.
CLEANUP_INTERVAL_SECONDS = float(os.getenv("RETENTION_CLEANUP_INTERVAL_SECONDS", "0"))
_LOCK_NAMESPACE = 0x454152  # distinct from execution.py's 0x454149
_background_task: asyncio.Task | None = None


def _with_cleanup_lock(engine, fn) -> None:
    """Cross-replica mutual exclusion for one cleanup pass, the same advisory-lock
    shape as execution.agent_step_lock: SQLite always grants (it cannot be serving
    the multi-replica deployment this guards)."""
    if engine.dialect.name != "postgresql":
        fn()
        return
    connection = engine.connect()
    acquired = False
    try:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:ns, 1)"), {"ns": _LOCK_NAMESPACE}).scalar())
        if acquired:
            fn()
    finally:
        if acquired:
            connection.execute(text("SELECT pg_advisory_unlock(:ns, 1)"), {"ns": _LOCK_NAMESPACE})
        connection.close()


def run_cleanup_tick() -> None:
    from .database import SessionLocal, engine
    def _tick():
        with SessionLocal() as db:
            run_retention_cleanup(db)
            execute_due_deletions(db)
    _with_cleanup_lock(engine, _tick)


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_cleanup_tick)
        except Exception:
            pass  # a missed tick is retried next interval; never crash the app over it
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


def start_background_loop() -> None:
    global _background_task
    if _background_task is None and CLEANUP_INTERVAL_SECONDS > 0:
        _background_task = asyncio.ensure_future(_loop())


def stop_background_loop() -> None:
    global _background_task
    if _background_task is not None:
        _background_task.cancel()
        _background_task = None
