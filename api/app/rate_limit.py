"""In-memory, per-process rate limiting -- deliberately simple: a single API
process today, no Redis. Known limitation: if the deployment ever scales to
more than one API replica, this limit stops being GLOBAL -- each replica
counts on its own, so the effective limit becomes (N replicas x configured
limit). Revisit if the deployment shape changes."""
from __future__ import annotations

import time


class FixedWindowRateLimiter:
    """Fixed window (not a true token bucket): easy to reason about, enough
    to stop a runaway client or a brute-force attempt against the API key
    space -- not meant to be perfectly fair at window boundaries."""

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= self._window_seconds:
            window_start, count = now, 0
        count += 1
        self._windows[key] = (window_start, count)
        return count <= self._max_requests
