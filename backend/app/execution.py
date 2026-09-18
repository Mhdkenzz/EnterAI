"""Autonomous agent execution loop (Phase 6).

An agent with a current_task makes real progress on it without a human confirming
every step, by polling on a timer rather than only responding to chat messages.
Entirely gated by CopilotService.mode: a deterministic-provider org -- the default
everywhere in dev/CI/tests -- never runs this loop, since deterministic mode has no
structured tool-calling to safely execute autonomously. Only two actions ever execute
without human confirmation (log_progress, mark_task_complete -- see copilot.py); every
other write an agent proposes while executing still mints a confirmation token exactly
like the interactive chat flow.

`run_execution_tick` is the unit under test: it takes an explicit `db`/`service` so
tests can inject a scripted provider and a real session, with no dependency on the
background scheduler's timing. `start_background_loop`/`stop_background_loop` are thin
wrappers wired to FastAPI's startup/shutdown events and are not unit tested directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import zlib
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .copilot import CopilotProviderError, CopilotService, WorkspaceTools
from .database import SessionLocal, engine
from .models import AgentMessage, Task, User, Organization
from .observability import execution_allowed, audit_context, scheduler_failure
from .ratelimit import build_rate_limiter

MAX_CONSECUTIVE_FAILURES = int(os.getenv("AGENT_EXECUTION_MAX_FAILURES", "3"))
TICK_INTERVAL_SECONDS = float(os.getenv("AGENT_EXECUTION_INTERVAL_SECONDS", "30"))
DAILY_CALL_LIMIT = int(os.getenv("AGENT_EXECUTION_DAILY_LIMIT", "50"))

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

_daily_limiter = build_rate_limiter(max_calls=DAILY_CALL_LIMIT, window_seconds=86400, namespace="agent-daily")
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
    return db.scalars(
        select(User).join(Organization, User.organization_id == Organization.id).where(
            User.execution_enabled.is_(True), Organization.execution_enabled.is_(True),
            User.kind == "agent",
            User.current_task_id.isnot(None),
            User.consecutive_task_failures < MAX_CONSECUTIVE_FAILURES,
        )
    ).all()


def _run_agent_step(db: Session, agent: User, service: CopilotService) -> None:
    if not execution_allowed(db, agent):
        return
    task = db.get(Task, agent.current_task_id)
    if not task:
        # The task was deleted or unassigned since the agent list was read; clear the
        # stale pointer so it stops being picked up every tick.
        agent.current_task_id = None
        db.commit()
        return
    tools = WorkspaceTools(db, agent, scope_project_id=task.project_id)
    try:
        result = service.run_execution_step(tools, task)
    except CopilotProviderError as error:
        agent.consecutive_task_failures += 1
        # Naive UTC, matching every other timestamp in this schema. An aware value
        # written to a TIMESTAMP WITHOUT TIME ZONE column is converted to the
        # PostgreSQL server's timezone first, which silently shifts it out of step
        # with the rest of the row on any server that is not running in UTC.
        agent.last_execution_at = datetime.utcnow()
        db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body="I couldn't make progress just now. Try again later."))
        db.commit()
        return

    agent.consecutive_task_failures = 0
    agent.last_execution_at = datetime.utcnow()
    narrative = _summarize(result)
    if narrative:
        db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body=narrative))
    db.commit()


def _summarize(result: dict) -> str | None:
    outcome, text, notes, action = result["outcome"], result.get("text"), result["progress_notes"], result["action"]
    if outcome == "completed":
        return text or "Marked my current task complete."
    if outcome == "proposed":
        return text or f"I can {action['label'][0].lower()}{action['label'][1:]} once you confirm."
    if outcome == "narrated":
        return text
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
    service = service or CopilotService()
    cooldown = STEP_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
    stepped = 0
    try:
        if service.mode != "tool_calling":
            return 0
        for agent in _eligible_agents(db):
            org_id, agent_id = agent.organization_id, agent.id
            bind = db.get_bind()
            with agent_step_lock(bind, agent_id) as acquired:
                # Another replica is mid-step on this agent, or has just finished
                # one. Either way it is not this replica's turn. The daily
                # allowance is only drawn down after both checks pass, so a replica
                # that loses the race never spends budget it will not use.
                if not acquired or _stepped_recently(bind, agent_id, cooldown):
                    continue
                if not _daily_limiter.allow(agent_id):
                    continue
                with audit_context("scheduler", agent_id):
                    try:
                        _run_agent_step(db, agent, service)
                    except Exception:
                        db.rollback()
                        scheduler_failure(bind, org_id, agent_id)
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
    if _background_task is None and CopilotService().mode == "tool_calling":
        _background_task = asyncio.ensure_future(_loop())


def stop_background_loop() -> None:
    global _background_task
    if _background_task is not None:
        _background_task.cancel()
        _background_task = None
