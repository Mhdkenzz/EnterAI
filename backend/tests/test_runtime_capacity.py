"""The request-thread limiter must be a real global bound on the connection pool.

AnyIO's default limiter is 40 threads, but one PostgreSQL worker only has
`POOL_CAPACITY` (15) connections. Threads beyond that can do nothing but wait on
the pool -- and each waiting thread may still be holding the connection it
checked out, so the surplus converts a slow pool into a stalled worker. The
startup hook therefore caps the limiter to a budget derived from the pool after
reserving what the database-using background loops need.

These tests drive the real `startup()` in a subprocess (the house pattern for
startup configuration -- see test_auth.py and test_schema_startup.py) and read
back the limiter AnyIO actually installed, rather than mocking the limiter.
"""
import os
import subprocess
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Starts the app and prints the limiter the startup hook installed. Reads the
# real AnyIO limiter rather than a stub, so a wiring mistake cannot pass.
_STARTUP_PROBE = '''
import asyncio
import anyio.to_thread
from app import main

async def run():
    try:
        await main.startup()
        print("LIMITER_TOKENS", anyio.to_thread.current_default_thread_limiter().total_tokens)
    finally:
        main.shutdown()

asyncio.run(run())
'''


def _start_up(requested_threads):
    """Boot the app with REQUEST_THREADS set (or unset) and report the limiter.

    Returns (returncode, stdout, stderr). A startup that hangs is a failure, not
    a slow test: REQUEST_THREADS=0 used to deadlock here forever, because
    startup itself awaits a worker thread to seed the database.
    """
    env = {**os.environ, "ENABLE_DEMO_SEED": "false"}
    env.pop("REQUEST_THREADS", None)
    if requested_threads is not None:
        env["REQUEST_THREADS"] = requested_threads
    try:
        finished = subprocess.run([sys.executable, "-c", _STARTUP_PROBE], env=env,
                                  cwd=BACKEND, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None, "", "startup never completed (deadlocked)"
    return finished.returncode, finished.stdout, finished.stderr


def _installed_limiter(stdout):
    for line in stdout.splitlines():
        if line.startswith("LIMITER_TOKENS "):
            return int(line.split(" ", 1)[1])
    raise AssertionError(f"startup did not report a limiter: {stdout!r}")


def test_a_request_holds_more_than_one_connection_at_once():
    """Why the budget is divided rather than subtracted.

    A request is not one connection. `observability.provider_call` and
    `observability.tool_audit` deliberately open their own Session for the
    provider-call and tool-audit ledgers -- independent of the request
    transaction, so a provider failure or a business rollback cannot erase the
    record -- while the request's own session is still open. Traced on
    POST /api/project-drafts/assist with pool checkout instrumentation, the
    second connection is checked out at main.py:457 -> observability.py:312
    with the request's connection (main.py:455) still held.

    That independence is a durability property worth keeping, so the request
    budget is sized for the worst request rather than the average one.
    """
    from app.database import MAX_REQUEST_DB_CONNECTIONS

    assert MAX_REQUEST_DB_CONNECTIONS >= 2, (
        "a request holds an independent ledger session alongside its own; one "
        "connection per request would let a second checkout exhaust the pool")


def test_request_budget_leaves_room_for_concurrent_per_request_connections():
    """The invariant, stated on the constants a reader can check by hand.

    The pool must satisfy requests-plus-background in the worst case, with
    every request thread at its maximum concurrent connection count at the same
    moment as every background loop at its maximum.
    """
    from app.database import (BACKGROUND_DB_RESERVE, MAX_REQUEST_DB_CONNECTIONS,
                              POOL_CAPACITY, REQUEST_THREAD_BUDGET)

    used = (REQUEST_THREAD_BUDGET * MAX_REQUEST_DB_CONNECTIONS
            + BACKGROUND_DB_RESERVE)
    assert used <= POOL_CAPACITY, (
        f"{REQUEST_THREAD_BUDGET} request threads x {MAX_REQUEST_DB_CONNECTIONS} "
        f"connections + {BACKGROUND_DB_RESERVE} background = {used}, over the "
        f"pool's {POOL_CAPACITY}")


def test_request_budget_is_derived_from_the_maximum_request_connections():
    """The division is derived, not hand-typed, so the two numbers cannot drift."""
    from app.database import (BACKGROUND_DB_RESERVE, MAX_REQUEST_DB_CONNECTIONS,
                              POOL_CAPACITY, REQUEST_THREAD_BUDGET)

    assert REQUEST_THREAD_BUDGET == (
        (POOL_CAPACITY - BACKGROUND_DB_RESERVE) // MAX_REQUEST_DB_CONNECTIONS)
    # Floor division may waste a connection; it must never hand back more than
    # the safe division, and the budget must still serve at least one request.
    assert REQUEST_THREAD_BUDGET >= 1
    assert REQUEST_THREAD_BUDGET * MAX_REQUEST_DB_CONNECTIONS <= POOL_CAPACITY - BACKGROUND_DB_RESERVE


def test_pool_budget_reserves_every_known_background_connection():
    """The reserve side is still derived: the per-loop costs must sum to the
    reserve the request budget is computed from."""
    from app.database import BACKGROUND_DB_RESERVE, BACKGROUND_DB_USE

    assert sum(connections for _, connections in BACKGROUND_DB_USE) == BACKGROUND_DB_RESERVE


def test_default_request_thread_limit_is_the_pool_budget():
    """With nothing configured, the limiter is the budget -- not a hand-picked
    number, and not AnyIO's 40."""
    from app.database import REQUEST_THREAD_BUDGET

    code, stdout, stderr = _start_up(None)
    assert code == 0, stderr
    assert _installed_limiter(stdout) == REQUEST_THREAD_BUDGET


def test_the_default_limiter_is_not_the_pre_correction_budget():
    """Six request threads was the old, unsafe answer: 6 threads x 2 connections
    + 9 background = 21 connections against a 15-connection pool. If the budget
    is ever wired back to 6 -- or the multiplier is dropped -- this fails."""
    from app.database import (BACKGROUND_DB_RESERVE, MAX_REQUEST_DB_CONNECTIONS,
                              POOL_CAPACITY, REQUEST_THREAD_BUDGET)

    assert REQUEST_THREAD_BUDGET == 3
    assert REQUEST_THREAD_BUDGET == (POOL_CAPACITY - BACKGROUND_DB_RESERVE) // MAX_REQUEST_DB_CONNECTIONS
    assert MAX_REQUEST_DB_CONNECTIONS == 2

    code, stdout, stderr = _start_up(None)
    assert code == 0, stderr
    installed = _installed_limiter(stdout)
    assert installed != 6, "the pre-correction budget is back"
    assert installed * MAX_REQUEST_DB_CONNECTIONS + BACKGROUND_DB_RESERVE <= POOL_CAPACITY


def test_six_request_threads_is_refused_because_it_overruns_the_pool():
    """The observable startup limiter must reject the old value, not honour it."""
    from app.database import MAX_REQUEST_DB_CONNECTIONS

    code, _stdout, stderr = _start_up("6")
    assert code != 0, "REQUEST_THREADS=6 must not boot"
    assert "REQUEST_THREADS" in stderr

    # And the same refusal at the unit level, naming the arithmetic.
    from app.database import (BACKGROUND_DB_RESERVE, MAX_REQUEST_DB_CONNECTIONS,
                              POOL_CAPACITY, resolve_request_thread_limit)

    with pytest.raises(RuntimeError, match="REQUEST_THREADS"):
        resolve_request_thread_limit({"REQUEST_THREADS": "6"})
    # 6 threads alone is 12 connections, which fits; it is the background
    # reserve on top that overruns. The whole point of the correction.
    assert (6 * MAX_REQUEST_DB_CONNECTIONS + BACKGROUND_DB_RESERVE) > POOL_CAPACITY


def test_a_requested_limit_within_budget_is_honoured():
    code, stdout, stderr = _start_up("3")
    assert code == 0, stderr
    assert _installed_limiter(stdout) == 3


def test_a_limit_that_would_exceed_the_pool_budget_fails_startup():
    """Raising REQUEST_THREADS above the budget is how the pool gets overrun.
    The worker must refuse to start rather than honour it."""
    code, _stdout, stderr = _start_up("12")
    assert code != 0, "an over-budget REQUEST_THREADS must not boot"
    assert "REQUEST_THREADS" in stderr


def test_a_limit_of_zero_fails_startup_instead_of_deadlocking():
    """Zero tokens deadlocks startup, which awaits a worker thread to seed the
    database: the process would hang rather than serve or fail."""
    code, _stdout, stderr = _start_up("0")
    assert code not in (0, None), "REQUEST_THREADS=0 must not start"
    assert "REQUEST_THREADS" in stderr


def test_a_malformed_limit_fails_startup_with_a_clear_error():
    """A typo must name the variable, not surface as a bare int() failure."""
    code, _stdout, stderr = _start_up("abc")
    assert code != 0
    assert "REQUEST_THREADS" in stderr


@pytest.mark.parametrize("requested,accepted", [
    (None, True), ("1", True), ("3", True), (" 2 ", True),
    # An empty value is "not configured" (an unset compose variable, an empty
    # shell export), not a bad one: it must fall back to the budget rather than
    # stop the boot or, worse, be read as "no limit".
    ("", True),
    ("0", False), ("-1", False), ("abc", False), ("2.5", False),
    # 4 threads x 2 connections + 9 background = 17 > 15: over budget, and the
    # boundary case that makes the division rather than the subtraction visible.
    ("4", False), ("6", False), ("7", False), ("40", False),
])
def test_every_limit_is_either_applied_or_refused(requested, accepted):
    """The full accept/reject matrix, checked against the same observable startup
    path the process actually runs."""
    from app.database import REQUEST_THREAD_BUDGET, resolve_request_thread_limit

    environ = {} if requested is None else {"REQUEST_THREADS": requested}
    if accepted:
        limit = resolve_request_thread_limit(environ)
        assert limit > 0
        # An unset or empty value gets the full budget, which is the safe
        # direction: it is the most request threads the pool can serve.
        if requested is None or not requested.strip():
            assert limit == REQUEST_THREAD_BUDGET
        else:
            assert limit == int(requested)
    else:
        with pytest.raises(RuntimeError, match="REQUEST_THREADS"):
            resolve_request_thread_limit(environ)
