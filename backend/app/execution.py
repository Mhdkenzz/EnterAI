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
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .copilot import CopilotProviderError, CopilotService, WorkspaceTools
from .database import SessionLocal, engine
from .models import AgentMessage, Task, User, Organization
from .observability import execution_allowed, audit_context, scheduler_failure
from .ratelimit import SlidingWindowRateLimiter

MAX_CONSECUTIVE_FAILURES = int(os.getenv("AGENT_EXECUTION_MAX_FAILURES", "3"))
TICK_INTERVAL_SECONDS = float(os.getenv("AGENT_EXECUTION_INTERVAL_SECONDS", "30"))
DAILY_CALL_LIMIT = int(os.getenv("AGENT_EXECUTION_DAILY_LIMIT", "50"))

_daily_limiter = SlidingWindowRateLimiter(max_calls=DAILY_CALL_LIMIT, window_seconds=86400)
_background_task: asyncio.Task | None = None


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
        agent.last_execution_at = datetime.now(timezone.utc)
        db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body="I couldn't make progress just now. Try again later."))
        db.commit()
        return

    agent.consecutive_task_failures = 0
    agent.last_execution_at = datetime.now(timezone.utc)
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


def run_execution_tick(db: Session | None = None, service: CopilotService | None = None) -> int:
    """Runs one autonomous-execution pass over every eligible agent. Returns how many
    agents were actually stepped (0 if the configured provider can't tool-call)."""
    owns_db = db is None
    db = db or SessionLocal()
    service = service or CopilotService()
    stepped = 0
    try:
        if service.mode != "tool_calling":
            return 0
        for agent in _eligible_agents(db):
            if not _daily_limiter.allow(agent.id):
                continue
            org_id, agent_id = agent.organization_id, agent.id
            with audit_context("scheduler", agent_id):
                try:
                    _run_agent_step(db, agent, service)
                except Exception:
                    db.rollback()
                    scheduler_failure(db.get_bind(), org_id, agent_id)
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
