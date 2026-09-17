"""A minimal in-process sliding-window rate limiter.

This only bounds abuse within a single backend process -- fine for the current
single-instance deployment, but it will need to move to a shared store (e.g.
Redis) once the API runs behind more than one worker/replica.
"""
from __future__ import annotations

import time
from collections import defaultdict


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
