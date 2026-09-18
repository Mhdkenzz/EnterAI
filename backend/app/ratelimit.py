"""Rate limiting, with one `allow(key) -> bool` interface over two backends.

`SlidingWindowRateLimiter` keeps its window in process memory. It bounds abuse
within a single worker only -- run two replicas and each one hands out the full
allowance -- so it is a development and test convenience, never a production
control. `RedisSlidingWindowRateLimiter` keeps the window in Redis, where every
replica draws down one shared allowance.

`build_rate_limiter` chooses between them, and production without REDIS_URL fails
closed at import time exactly as JWT_SECRET and SEED_ADMIN_PASSWORD do: quietly
degrading to per-process counters would advertise a limit the deployment does not
actually enforce.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from uuid import uuid4

logger = logging.getLogger("enterai.ratelimit")

_client = None


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self.max_calls:
            return False
        hits.append(now)
        return True


# One atomic round trip: trim the window, count what survives, and only then record
# the new hit. As separate commands, two replicas could both read count == limit - 1
# and both be let through.
_SLIDING_WINDOW = """
local now, window, limit = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
if redis.call('ZCARD', KEYS[1]) >= limit then
  return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
return 1
"""


class RedisSlidingWindowRateLimiter:
    def __init__(self, client, max_calls: int, window_seconds: float, namespace: str):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.namespace = namespace
        self._client = client
        self._script = client.register_script(_SLIDING_WINDOW)

    def allow(self, key: str, now: float | None = None) -> bool:
        # Wall clock, not monotonic: the window is shared across processes that do
        # not share a monotonic origin.
        now = time.time() if now is None else now
        try:
            allowed = self._script(keys=[f"{self.namespace}:{key}"],
                                   args=[now, self.window_seconds, self.max_calls, uuid4().hex])
        except Exception as error:
            # Fail closed. An unreachable shared limiter cannot tell an abusive
            # caller from a legitimate one, and these limiters gate only AI
            # endpoints -- pausing those beats letting an unmetered flood through.
            # The exception class is logged so this cannot silently swallow a bug;
            # the key never is, because it identifies a user or agent.
            logger.warning("rate_limiter_unavailable", extra={"event": "rate_limiter_unavailable",
                                                              "namespace": self.namespace,
                                                              "error": type(error).__name__})
            return False
        return bool(allowed)


def get_redis(url: str):
    """One pooled client per process, shared by every limiter. Timeouts are short
    and explicit so a hung Redis cannot hold an API worker open indefinitely."""
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2,
                                       health_check_interval=30)
    return _client


def reset_redis_client() -> None:
    """Tests swap REDIS_URL between cases; drop the memoized client with it."""
    global _client
    _client = None


def build_rate_limiter(max_calls: int, window_seconds: float, namespace: str):
    url = os.getenv("REDIS_URL", "").strip()
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    if not url:
        if environment == "production":
            raise RuntimeError(
                "REDIS_URL must be set in production. Rate limits are shared state:"
                " without Redis every replica enforces its own private allowance, so"
                " the configured limit is not the limit callers actually get."
            )
        return SlidingWindowRateLimiter(max_calls, window_seconds)
    return RedisSlidingWindowRateLimiter(get_redis(url), max_calls, window_seconds, namespace)
