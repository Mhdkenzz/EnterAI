"""Rate limits have to be shared once more than one replica serves traffic.

These run against fakeredis by default so the suite needs no infrastructure; CI
also runs them against a real Redis (REDIS_URL set) to prove the Lua window works
on the real server, not just the emulator.
"""
import os

import pytest

from app import ratelimit
from app.ratelimit import (RedisSlidingWindowRateLimiter, SlidingWindowRateLimiter,
                           build_rate_limiter, reset_redis_client)


@pytest.fixture
def client():
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        import redis
        real = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        real.flushdb()
        yield real
        real.flushdb()
    else:
        import fakeredis
        yield fakeredis.FakeRedis()


@pytest.fixture(autouse=True)
def _clean_environment():
    before = {key: os.environ.get(key) for key in ("REDIS_URL", "ENVIRONMENT")}
    yield
    for key, value in before.items():
        os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
    reset_redis_client()


def test_redis_window_blocks_bursts_and_recovers(client):
    limiter = RedisSlidingWindowRateLimiter(client, max_calls=3, window_seconds=60, namespace="t1")
    assert [limiter.allow("user-1", now=n) for n in (1000, 1001, 1002)] == [True, True, True]
    assert limiter.allow("user-1", now=1003) is False
    # A different caller has its own allowance...
    assert limiter.allow("user-2", now=1003) is True
    # ...and the first one recovers once the window has rolled past.
    assert limiter.allow("user-1", now=1061) is True


def test_two_limiter_instances_share_one_allowance(client):
    """The multi-replica property: separate limiter objects, as separate API
    workers would hold, must draw down the same budget."""
    replica_a = RedisSlidingWindowRateLimiter(client, max_calls=2, window_seconds=60, namespace="t2")
    replica_b = RedisSlidingWindowRateLimiter(client, max_calls=2, window_seconds=60, namespace="t2")
    assert replica_a.allow("shared", now=2000) is True
    assert replica_b.allow("shared", now=2001) is True
    assert replica_a.allow("shared", now=2002) is False
    assert replica_b.allow("shared", now=2002) is False


def test_namespaces_do_not_share_an_allowance(client):
    plan = RedisSlidingWindowRateLimiter(client, max_calls=1, window_seconds=60, namespace="ai-plan")
    daily = RedisSlidingWindowRateLimiter(client, max_calls=1, window_seconds=60, namespace="agent-daily")
    assert plan.allow("same-id", now=3000) is True
    assert plan.allow("same-id", now=3000) is False
    assert daily.allow("same-id", now=3000) is True


def test_an_unreachable_redis_fails_closed():
    class Broken:
        def register_script(self, _script):
            def call(*args, **kwargs):
                raise ConnectionError("redis is down")
            return call

    limiter = RedisSlidingWindowRateLimiter(Broken(), max_calls=10, window_seconds=60, namespace="t3")
    assert limiter.allow("user-1") is False


def test_production_without_redis_refuses_to_build_a_limiter():
    os.environ["ENVIRONMENT"] = "production"
    os.environ.pop("REDIS_URL", None)
    with pytest.raises(RuntimeError) as error:
        build_rate_limiter(max_calls=5, window_seconds=60, namespace="ai-plan")
    assert "REDIS_URL" in str(error.value)


def test_non_production_without_redis_falls_back_in_process():
    os.environ["ENVIRONMENT"] = "development"
    os.environ.pop("REDIS_URL", None)
    limiter = build_rate_limiter(max_calls=5, window_seconds=60, namespace="ai-plan")
    assert isinstance(limiter, SlidingWindowRateLimiter)


def test_configured_redis_is_used_in_every_environment(monkeypatch):
    import fakeredis
    monkeypatch.setattr(ratelimit, "get_redis", lambda url: fakeredis.FakeRedis())
    os.environ["ENVIRONMENT"] = "production"
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/0"
    limiter = build_rate_limiter(max_calls=5, window_seconds=60, namespace="ai-plan")
    assert isinstance(limiter, RedisSlidingWindowRateLimiter)
    assert limiter.allow("user-1") is True
