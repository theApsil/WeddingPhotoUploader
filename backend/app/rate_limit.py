"""Simple in-memory sliding-window rate limiter keyed by client IP."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Thread-safe fixed window counter (per minute)."""

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = max(1, limit_per_minute)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, remaining). Prunes timestamps older than 60s."""
        ts = time.time() if now is None else now
        window_start = ts - 60.0
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= self._limit:
                return False, 0
            bucket.append(ts)
            remaining = self._limit - len(bucket)
            return True, remaining

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
