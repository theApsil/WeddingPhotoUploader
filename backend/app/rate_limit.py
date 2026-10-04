"""In-memory sliding-window rate limiter keyed by client IP.

Bounded memory: at most ``max_keys`` distinct keys are tracked; the
least-recently-active key is evicted first, so spoofed/unbounded IPs cannot grow
the table without limit.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    """Thread-safe fixed-window counter (per minute), bounded by key count."""

    def __init__(self, limit_per_minute: int, max_keys: int = 10_000) -> None:
        self._limit = max(1, limit_per_minute)
        self._max_keys = max(1, max_keys)
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, remaining). Prunes timestamps older than 60s."""
        ts = time.time() if now is None else now
        window_start = ts - 60.0
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_keys:
                    # Evict the least-recently-used key to bound memory.
                    self._hits.popitem(last=False)
                bucket = deque()
                self._hits[key] = bucket
            else:
                self._hits.move_to_end(key)

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