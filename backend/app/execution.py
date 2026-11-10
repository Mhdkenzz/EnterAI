"""Autonomous agent execution loop (Phase 6).

An agent with a current_task makes real progress on it without a human confirming
every step, by polling on a timer rather than only responding to chat messages.
Tasks explicitly blocked by an agent leave the runnable queue until a human reopens
them; provider failures use a separate retry threshold and escalation path.
Entirely gated by CopilotService.mode: a deterministic-provider org -- the default
everywhere in dev/CI/tests -- never runs this loop, since deterministic mode has no
structured tool-calling to safely execute autonomously. Repository source edits are
available only on an agent's assigned task when organization access is enabled. File
removals and ordinary workspace changes retain their confirmation rules.

`run_execution_tick` is the unit under test: it takes an explicit `db`/`service` so
tests can inject a scripted provider and a real session, with no dependency on the
background scheduler's timing. `start_background_loop`/`stop_background_loop` are thin
wrappers wired to FastAPI's startup/shutdown events and are not unit tested directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import zlib
from datetime import datetime, timedelta, timezone

import jwt

from sqlalchemy import case, or_, select, text
from sqlalchemy.orm import Session

from .copilot import CopilotProviderError, CopilotService, WorkspaceTools
from .auth import CONFIRMATION_SECRET
from .database import SessionLocal, engine
from .models import AgentMessage, Comment, CopilotConfirmation, Notification, OrganizationAISettings, Task, User, Organization
from .observability import execution_allowed, audit_context, scheduler_failure
from .services import log
from .ratelimit import OpenRouterRateLimitExceeded, build_rate_limiter
from .task_dependencies import unmet_dependency_ids
from . import secrets_store

MAX_CONSECUTIVE_FAILURES = int(os.getenv("AGENT_EXECUTION_MAX_FAILURES", "3"))
DAILY_CALL_LIMIT = int(os.getenv("AGENT_EXECUTION_DAILY_STEP_LIMIT", "500"))
if DAILY_CALL_LIMIT <= 0:
    raise RuntimeError("AGENT_EXECUTION_DAILY_STEP_LIMIT must be greater than zero")
TICK_INTERVAL_SECONDS = float(os.getenv("AGENT_EXECUTION_INTERVAL_SECONDS", "30"))
_daily_limiter = build_rate_limiter(DAILY_CALL_LIMIT, 24 * 60 * 60, "agent-execution-daily")

# Advisory-lock namespace for agent execution; pairing it with the agent key keeps
# these locks from colliding with any other advisory lock in the same database.
LOCK_NAMESPACE = 0x454149

# Replicas tick on their own unsynchronised schedules, so a second replica starting
# its tick shortly after another finished a step would step the same agent again --
# one extra provider call per replica per interval. Skipping an agent stepped within
# most of a tick keeps the global rate at one step per interval however many replicas
# run, while still leaving a single replica's own next tick (a full interval plus the
# step's own duration) comfortably clear.
STEP_COOLDOWN_SECONDS = TICK_INTERVAL_SECONDS * 0.9

_background_task: asyncio.Task | None = None


def _lock_key(agent_id: str) -> int:
    """PostgreSQL advisory locks are keyed by integers, so agent UUIDs are hashed
    down to one. A collision only means two agents cannot be stepped in the same
    tick -- the loser is picked up by the next one -- so it costs latency, never
    correctness or work."""
    return zlib.crc32(agent_id.encode()) - 2 ** 31


@contextlib.contextmanager
def agent_step_lock(bind, agent_id: str):
    """Mutual exclusion for one agent's execution step across every replica.

    The lock is session-scoped and held on a dedicated connection rather than the
    scheduler's own session, because `_run_agent_step` commits partway through: a
    transaction-scoped lock would be released by that commit and reopen exactly the
    window this closes. Closing the connection releases the lock even if the
    explicit unlock never runs.

    Backends without advisory locks (SQLite) always grant -- they cannot be serving
    the multi-replica deployment this guards.
    """
    if bind.dialect.name != "postgresql":
        yield True
        return
    key = {"ns": LOCK_NAMESPACE, "key": _lock_key(agent_id)}
    connection = bind.connect()
    acquired = False
    try:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:ns, :key)"), key).scalar())
        yield acquired
    finally:
        if acquired:
            connection.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), key)
        connection.close()


def _stepped_recently(bind, agent_id: str, cooldown_seconds: float) -> bool:
    """Re-read the agent under the lock: another replica may have stepped it between
    this tick's eligibility query and the lock being granted."""
    if cooldown_seconds <= 0:
        return False
    with Session(bind=bind) as fresh:
        last = fresh.scalar(select(User.last_execution_at).where(User.id == agent_id))
    return last is not None and (datetime.utcnow() - last).total_seconds() < cooldown_seconds


def _eligible_agents(db: Session) -> list[User]:
    queued_task = select(Task.id).where(
        Task.assignee_id == User.id, Task.status.notin_(["done", "blocked", "review"]),
    ).exists()
    active_current_task = select(Task.id).where(
        Task.id == User.current_task_id, Task.status.notin_(["done", "blocked", "review"]),
    ).exists()
    return db.scalars(
        select(User).join(Organization, User.organization_id == Organization.id).where(
            User.execution_enabled.is_(True), Organization.execution_enabled.is_(True),
            User.kind == "agent",
            or_(active_current_task, queued_task),
            User.consecutive_task_failures < MAX_CONSECUTIVE_FAILURES,
        )
    ).all()


def _claim_next_task(db: Session, agent: User) -> Task | None:
    task = db.get(Task, agent.current_task_id) if agent.current_task_id else None
    if task and task.assignee_id == agent.id and task.status not in {"done", "blocked", "review"}:
        if not unmet_dependency_ids(db, task.id):
            return task
    agent.current_task_id = None
    priority_order = case((Task.priority == "high", 0), (Task.priority == "medium", 1), else_=2)
    candidates = db.scalars(select(Task).where(
        Task.assignee_id == agent.id, Task.status.notin_(["done", "blocked", "review"]),
    ).order_by(priority_order, Task.position, Task.created_at, Task.id)).all()
    task = next((candidate for candidate in candidates if not unmet_dependency_ids(db, candidate.id)), None)
    if task:
        agent.current_task_id = task.id
        log(db, agent.organization_id, agent.id, "task", task.id, "agent_claimed")
    db.commit()
    return task


def _block_after_repeated_failures(db: Session, agent: User, task: Task, reason: str, *, cause: str) -> None:
    task.status = "blocked"
    task.blocked_reason = reason
    db.add(Comment(task_id=task.id, author_id=agent.id, body=reason))
    recipients = db.scalars(select(User).where(
        User.organization_id == agent.organization_id, User.kind == "human",
        User.role == "admin", User.active.is_(True),
    )).all()
    if task.reporter_id:
        reporter = db.get(User, task.reporter_id)
        if reporter and reporter.kind == "human" and reporter.active and reporter.organization_id == agent.organization_id:
            recipients = list(recipients) + [reporter]
    for recipient in {person.id: person for person in recipients}.values():
        db.add(Notification(
            user_id=recipient.id, title=f"Agent blocked: {task.title}"[:220],
            body=reason, href=f"project:{task.project_id}",
        ))
    log(db, agent.organization_id, agent.id, "task", task.id, "agent_blocked",
        cause=cause, consecutive_failures=agent.consecutive_task_failures)


def _run_agent_step(db: Session, agent: User, service: CopilotService) -> None:
    if not execution_allowed(db, agent):
        return
    task = _claim_next_task(db, agent)
    if not task:
        # The task was deleted or unassigned since the agent list was read.
        return
    tools = WorkspaceTools(db, agent, scope_project_id=task.project_id)
    if task.status in {"todo", "backlog"}:
        task.status = "in_progress"
        log(db, agent.organization_id, agent.id, "task", task.id, "agent_started")
        db.commit()
    try:
        result = service.run_execution_step(tools, task)
    except OpenRouterRateLimitExceeded:
        # The workspace cap is a pause, not an agent failure. The next tick can
        # resume once the rolling minute window has room again.
        return
    except CopilotProviderError:
        agent.consecutive_task_failures += 1
        # Naive UTC, matching every other timestamp in this schema. An aware value
        # written to a TIMESTAMP WITHOUT TIME ZONE column is converted to the
        # PostgreSQL server's timezone first, which silently shifts it out of step
        # with the rest of the row on any server that is not running in UTC.
        agent.last_execution_at = datetime.utcnow()
        db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body="I couldn't make progress just now. Try again later."))
        if agent.consecutive_task_failures >= MAX_CONSECUTIVE_FAILURES:
            reason = "Agent execution failed repeatedly; a workspace admin needs to review this task and retry or reassign it."
            _block_after_repeated_failures(db, agent, task, reason, cause="provider_failures")
        db.commit()
        return

    outcome = result.get("outcome")
    made_progress = bool(
        result.get("progress_made") or result.get("progress_notes") or result.get("workspace_change")
        or outcome in {"completed", "proposed", "blocked", "succeeded"}
    )
    no_progress = outcome in {"narrated", "inconclusive"} and not made_progress
    if no_progress:
        agent.consecutive_task_failures += 1
    else:
        agent.consecutive_task_failures = 0
    agent.last_execution_at = datetime.utcnow()
    narrative = _summarize(result)
    if narrative:
        db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body=narrative))
    if outcome == "proposed" and isinstance(result.get("action"), dict):
        action = result["action"]
        token = action.get("confirmation_token")
        if isinstance(token, str) and action.get("requires_confirmation"):
            claim = jwt.decode(token, CONFIRMATION_SECRET, algorithms=["HS256"], options={"require": ["exp", "kind", "sub", "org", "tool"]})
            expires_at = datetime.fromtimestamp(claim["exp"], timezone.utc).replace(tzinfo=None)
            db.add(CopilotConfirmation(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                subject_id=agent.id,
                organization_id=agent.organization_id,
                tool=claim["tool"],
                expires_at=expires_at,
                token_ciphertext=secrets_store.encrypt(token),
                label=action.get("label", "Agent action")[:240],
                details=action.get("details", []),
                task_id=task.id,
            ))
            task.status = "review"
            task.blocked_reason = None
            db.add(Comment(task_id=task.id, author_id=agent.id,
                           body=f"Proposed action for human approval: {action.get('label', 'Agent action')}"))
            admins = db.scalars(select(User).where(
                User.organization_id == agent.organization_id, User.kind == "human",
                User.role == "admin", User.active.is_(True),
            )).all()
            for recipient in admins:
                db.add(Notification(user_id=recipient.id, title=f"Agent action needs approval: {task.title}"[:220],
                                    body=action.get("label", "Review the proposed action."), href=f"project:{task.project_id}"))
            log(db, agent.organization_id, agent.id, "task", task.id, "agent_action_proposed", tool=claim["tool"])
    if no_progress and agent.consecutive_task_failures >= MAX_CONSECUTIVE_FAILURES:
        reason = "The agent repeatedly returned without recording progress. A workspace admin needs to review the task, clarify the next step, or reassign it."
        _block_after_repeated_failures(db, agent, task, reason, cause="no_progress")
    db.commit()


def _summarize(result: dict) -> str | None:
    outcome, text, notes, action = result["outcome"], result.get("text"), result["progress_notes"], result["action"]
    if outcome == "completed":
        return text or "Marked my current task complete."
    if outcome == "proposed":
        return text or f"I can {action['label'][0].lower()}{action['label'][1:]} once you confirm."
    if outcome == "narrated":
        return text
    if outcome == "blocked":
        return text or "I need help before I can continue this task."
    if notes:
        return "Progress: " + " ".join(notes)
    return None


def run_execution_tick(db: Session | None = None, service: CopilotService | None = None,
                       cooldown_seconds: float | None = None) -> int:
    """Runs one autonomous-execution pass over every eligible agent. Returns how many
    agents were actually stepped (0 if the configured provider can't tool-call).

    `cooldown_seconds` is the anti-double-step window described on
    STEP_COOLDOWN_SECONDS; tests that deliberately tick back to back pass 0.
    """
    owns_db = db is None
    db = db or SessionLocal()
    injected_service = service
    service = service or CopilotService()
    cooldown = STEP_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
    stepped = 0
    try:
        if injected_service is not None and service.mode != "tool_calling":
            return 0
        for agent in _eligible_agents(db):
            agent_service = injected_service or CopilotService(db, agent.organization_id)
            if agent_service.mode != "tool_calling":
                continue
            org_id, agent_id = agent.organization_id, agent.id
            bind = db.get_bind()
            with agent_step_lock(bind, agent_id) as acquired:
                # Another replica is mid-step on this agent, or has just finished
                # one. Either way it is not this replica's turn. The daily
                # allowance is only drawn down after both checks pass, so a replica
                # that loses the race never spends budget it will not use.
                if not acquired or _stepped_recently(bind, agent_id, cooldown):
                    continue
                db.refresh(agent)
                if not _daily_limiter.allow(agent_id):
                    if agent.daily_limit_reached_at is None:
                        agent.daily_limit_reached_at = datetime.utcnow()
                        reason = f"This agent reached its {DAILY_CALL_LIMIT}-step rolling 24-hour execution limit. It will resume when capacity renews."
                        for admin in db.scalars(select(User).where(
                            User.organization_id == org_id, User.kind == "human",
                            User.role == "admin", User.active.is_(True),
                        )).all():
                            current_task = db.get(Task, agent.current_task_id) if agent.current_task_id else None
                            db.add(Notification(
                                user_id=admin.id, title=f"Agent paused at daily limit: {agent.name}"[:220],
                                body=reason,
                                href=f"project:{current_task.project_id}" if current_task else None,
                            ))
                        log(db, org_id, agent_id, "agent", agent_id, "daily_execution_limit_reached",
                            limit=DAILY_CALL_LIMIT)
                        db.commit()
                    continue
                if agent.daily_limit_reached_at is not None:
                    agent.daily_limit_reached_at = None
                with audit_context("scheduler", agent_id):
                    try:
                        _run_agent_step(db, agent, agent_service)
                    except Exception as error:
                        db.rollback()
                        scheduler_failure(bind, org_id, agent_id, error)
                stepped += 1
        return stepped
    finally:
        if owns_db:
            db.close()


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_execution_tick)
        except Exception:
            scheduler_failure(engine)  # No tenant/agent is known for a global tick failure.
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_background_loop() -> None:
    """No-op under the deterministic default (dev/CI/tests) so the very common case of
    booting the app -- including every TestClient startup across the whole suite --
    never spawns a thread or an extra DB session for a tick that would do nothing
    anyway. Only spins up the scheduler once an operator actually configures a
    tool-calling AI_PROVIDER."""
    global _background_task
    configured = CopilotService().mode == "tool_calling"
    if not configured:
        try:
            with SessionLocal() as db:
                configured = db.scalar(select(OrganizationAISettings.id).where(
                    OrganizationAISettings.api_key_encrypted.is_not(None),
                ).limit(1)) is not None
        except Exception:
            configured = False
    if _background_task is None and configured:
        _background_task = asyncio.ensure_future(_loop())


def stop_background_loop() -> None:
    global _background_task
    if _background_task is not None:
        _background_task.cancel()
        _background_task = None
