"""The scheduler must step each agent once per interval, however many replicas run.

Two independent guards, tested separately:
  * `agent_step_lock` stops two replicas stepping the same agent at the same time.
  * the cooldown stops a replica that ticks slightly later from repeating a step
    another replica just finished.

The locking tests only mean something on PostgreSQL (SQLite has no advisory locks
and cannot be serving a multi-replica deployment), so they skip elsewhere.
"""
import threading
import time
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app import execution
from app.database import SessionLocal, engine
from app.execution import _lock_key, _stepped_recently, agent_step_lock, run_execution_tick
from app.models import Organization, User

postgres_only = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="advisory locks are a PostgreSQL feature"
)


def test_lock_keys_are_stable_and_distinct_per_agent():
    assert _lock_key("agent-a") == _lock_key("agent-a")
    assert _lock_key("agent-a") != _lock_key("agent-b")
    # PostgreSQL advisory lock keys are signed 32-bit integers.
    assert -2 ** 31 <= _lock_key("agent-a") < 2 ** 31


def test_backends_without_advisory_locks_always_grant():
    class Bind:
        dialect = type("D", (), {"name": "sqlite"})()

    with agent_step_lock(Bind(), "agent-a") as acquired:
        assert acquired is True


@postgres_only
def test_a_second_replica_cannot_hold_the_same_agent_lock():
    with agent_step_lock(engine, "agent-contended") as first:
        assert first is True
        with agent_step_lock(engine, "agent-contended") as second:
            assert second is False, "a second replica acquired a lock already held"
    # Released on exit, so the next tick can take it.
    with agent_step_lock(engine, "agent-contended") as third:
        assert third is True


@postgres_only
def test_different_agents_do_not_block_each_other():
    with agent_step_lock(engine, "agent-one") as first:
        with agent_step_lock(engine, "agent-two") as second:
            assert first is True and second is True


@postgres_only
def test_the_lock_survives_a_commit_on_the_scheduler_session():
    """Why the lock lives on its own connection: a transaction-scoped lock would be
    released by the commit `_run_agent_step` makes partway through a step."""
    with agent_step_lock(engine, "agent-commit") as held:
        assert held is True
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db.commit()
        with agent_step_lock(engine, "agent-commit") as competitor:
            assert competitor is False


@postgres_only
def test_only_one_of_many_concurrent_replicas_wins_the_lock():
    """Eight replicas reach for the same agent at once; exactly one may proceed."""
    replicas = 8
    results, lock = [], threading.Lock()
    start, release = threading.Barrier(replicas), threading.Event()

    def replica():
        start.wait(timeout=30)
        with agent_step_lock(engine, "agent-race") as acquired:
            with lock:
                results.append(acquired)
            if acquired:
                # Keep holding until every other replica has had its turn to try,
                # so a loser's False means "someone held it", not "it was free".
                release.wait(timeout=30)

    threads = [threading.Thread(target=replica) for _ in range(replicas)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with lock:
            if len(results) == replicas:
                break
        time.sleep(0.02)
    release.set()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == replicas, f"only {len(results)} replicas reported"
    assert results.count(True) == 1, f"expected exactly one winner, got {results}"


@pytest.fixture
def throwaway_agent():
    with SessionLocal() as db:
        org = db.scalar(select(Organization))
        agent = User(organization_id=org.id, name="Cooldown Probe", email=f"{uuid4().hex}@agents.internal",
                     password_hash="unused", kind="agent", hierarchy_level="worker")
        db.add(agent)
        db.commit()
        agent_id = agent.id
    yield agent_id
    with SessionLocal() as db:
        db.delete(db.get(User, agent_id))
        db.commit()


def test_cooldown_skips_an_agent_another_replica_just_stepped(throwaway_agent):
    with SessionLocal() as db:
        bind = db.get_bind()

        def set_last_execution(value):
            db.get(User, throwaway_agent).last_execution_at = value
            db.commit()

        set_last_execution(datetime.utcnow())
        assert _stepped_recently(bind, throwaway_agent, cooldown_seconds=27) is True
        # A step a full interval ago is fair game again.
        set_last_execution(datetime.utcnow() - timedelta(seconds=120))
        assert _stepped_recently(bind, throwaway_agent, cooldown_seconds=27) is False
        # An explicit zero disables the window entirely.
        set_last_execution(datetime.utcnow())
        assert _stepped_recently(bind, throwaway_agent, cooldown_seconds=0) is False
        # An agent that has never run is never "recent".
        set_last_execution(None)
        assert _stepped_recently(bind, throwaway_agent, cooldown_seconds=27) is False


def test_the_scheduler_records_last_execution_in_naive_utc():
    """`last_execution_at` used to be written timezone-aware while every other
    column in the schema is naive UTC. On a PostgreSQL server outside UTC that
    stored it shifted by the server's offset, which would put the cooldown
    comparison hours out and either strand an agent or never skip one."""
    from fastapi.testclient import TestClient

    from app.main import app
    from test_agent_execution import (_FailingProvider, _admin_headers_and_org,
                                      _agent_with_task, _reset_hierarchy, _service_with)

    headers, _org, db = _admin_headers_and_org()
    try:
        with TestClient(app) as client:
            agent, _task = _agent_with_task(client, headers, db)
        run_execution_tick(db, _service_with(_FailingProvider()), cooldown_seconds=0)
        stored = db.scalar(select(User.last_execution_at).where(User.id == agent.id))
        assert stored is not None, "the scheduler did not record an execution"
        assert stored.tzinfo is None, "aware timestamps shift on non-UTC PostgreSQL servers"
        assert abs((datetime.utcnow() - stored).total_seconds()) < 300
    finally:
        with TestClient(app) as client:
            _reset_hierarchy(client, headers)
        db.close()


def test_cooldown_default_leaves_a_single_replicas_own_next_tick_clear():
    """A single replica sleeps a full interval between ticks, so its own next tick
    must never be mistaken for another replica's duplicate."""
    assert execution.STEP_COOLDOWN_SECONDS < execution.TICK_INTERVAL_SECONDS
