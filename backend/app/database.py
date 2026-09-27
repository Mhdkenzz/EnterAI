import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./enterai.db")
# Limits are per worker: provision PostgreSQL for 15 connections per API worker,
# plus migration/administration capacity. Pre-ping handles stale checkouts, not
# interrupted transactions; do not automatically retry writes after disconnects.
_pool_options = {}
# Connections a worker may hold at once. This is the hard ceiling the request
# thread budget below is derived from: AnyIO's default limiter is 40 threads, so
# without a cap the surplus threads could only wait on the pool, each still
# holding the connection it checked out.
POOL_CAPACITY = 15

# Connections the background loops take that the request-thread limiter cannot
# see, because they are dispatched with asyncio.to_thread (the default executor),
# not through anyio.to_thread. Each entry is (loop, connections held at once):
#
#   email.retry_pending    -- SessionLocal session, plus the advisory-lock
#                             connection _with_retry_lock holds across it.
#   privacy_service        -- same shape, via _with_cleanup_lock.
#   observability         -- same shape, via _with_aggregation_lock.
#   execution              -- the tick's own session, the per-agent advisory-lock
#                             connection (agent_step_lock), and the short-lived
#                             fresh session _stepped_recently opens under it.
#
# This is the worst case: all four enabled at once, which an operator can do with
# four environment variables. Reserving the worst case rather than the currently
# enabled subset is deliberate -- the invariant that matters is that request
# threads plus background work never exceed the pool, and a static reserve holds
# that whatever subset a given deployment happens to run. A background loop also
# holds its lock connection while doing its work, so it is a concurrent hold, not
# a per-tick average.
BACKGROUND_DB_USE = (
    ("email.retry_pending", 2),
    ("privacy_service.run_cleanup_tick", 2),
    ("observability.run_aggregation_tick", 2),
    ("execution.run_execution_tick", 3),
)
BACKGROUND_DB_RESERVE = sum(connections for _, connections in BACKGROUND_DB_USE)

# Connections ONE request may hold at the same time. This is not 1.
#
# `observability.provider_call` and `observability.tool_audit` open their own
# Session for the provider-call and tool-audit ledgers
# (`Session(bind=tools.db.get_bind())`, observability.py:245 and :304) while
# `tools.db` is the live request session from `get_db`. That independence is
# deliberate and load-bearing: the ledger must survive a provider failure or a
# rolled-back business transaction, so it cannot join the request transaction.
# `email._enqueue` has the same shape, opening a SessionLocal to record a
# failed delivery while the request session is still open.
#
# So a request at its worst holds the request's own connection AND an
# independent ledger connection simultaneously. Verified by instrumenting the
# pool on POST /api/project-drafts/assist: checkouts go 1 -> 2, the second at
# main.py:457 -> observability.py:312, with main.py:455's connection still held.
#
# Folding the ledger into the request transaction would be the other way to
# make this 1, and would trade a real durability property for pool headroom.
# The budget below is divided instead, which keeps both.
MAX_REQUEST_DB_CONNECTIONS = 2

# What is left for request handling. A request thread holds its connections for
# the whole request, so this is also the number of requests a worker can run at
# once; a busy worker queues the rest on its event loop instead of piling threads
# up behind an exhausted pool.
#
# Divided, not subtracted, because the request share is per thread while the
# background share is a single total: `threads * connections_per_request +
# background <= capacity`. Subtracting would treat one connection per request as
# the cost and hand out 6 threads, which is 6*2 + 9 = 21 connections against a
# pool of 15 -- a pool overrun that only shows up under the concurrency the
# limiter exists to permit. Floor division wastes at most one connection, which
# is the safe direction: the unused one is idle capacity, not a missed request.
REQUEST_THREAD_BUDGET = (
    (POOL_CAPACITY - BACKGROUND_DB_RESERVE) // MAX_REQUEST_DB_CONNECTIONS
)


def resolve_request_thread_limit(environ: dict | None = None) -> int:
    """The number of request threads to allow, from REQUEST_THREADS.

    Fail closed on anything invalid. The tempting alternative -- fall back to the
    budget when the value cannot be parsed, or clamp an over-budget one down --
    both hide a misconfiguration that was meant to be deliberate, and the second
    one silently ignores an operator who asked for more concurrency than the pool
    can serve. Refusing to start is the same fail-closed rule the rest of the
    deployment configuration follows (see deployment_config.py).
    """
    env = os.environ if environ is None else environ
    raw = env.get("REQUEST_THREADS", "").strip()
    if not raw:
        return REQUEST_THREAD_BUDGET
    try:
        limit = int(raw)
    except ValueError:
        raise RuntimeError(
            f"REQUEST_THREADS must be a whole number, got {raw!r}") from None
    if limit < 1:
        # Zero does not merely disable concurrency: startup seeds the database on
        # a worker thread, so zero tokens deadlocks the boot instead of failing.
        raise RuntimeError(
            f"REQUEST_THREADS must be at least 1, got {limit}")
    if limit > REQUEST_THREAD_BUDGET:
        # Name the full arithmetic: the over-budget number is a pool overrun, not
        # merely a style disagreement, and an operator raising REQUEST_THREADS
        # needs to see what it would cost.
        raise RuntimeError(
            f"REQUEST_THREADS={limit} exceeds the request budget of "
            f"{REQUEST_THREAD_BUDGET}: {limit} threads x "
            f"{MAX_REQUEST_DB_CONNECTIONS} connections per request + "
            f"{BACKGROUND_DB_RESERVE} reserved for background database work = "
            f"{limit * MAX_REQUEST_DB_CONNECTIONS + BACKGROUND_DB_RESERVE}, "
            f"over the pool capacity of {POOL_CAPACITY}; raise the pool size or "
            f"lower REQUEST_THREADS")
    return limit


if make_url(DATABASE_URL).get_backend_name() == "postgresql":
    _pool_options = dict(pool_size=5, max_overflow=POOL_CAPACITY - 5, pool_timeout=30,
                         pool_recycle=1800)
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, **_pool_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
