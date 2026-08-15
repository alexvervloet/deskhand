"""A small fixed-window limiter for the login endpoint.

In-process and therefore per-instance, which is worth stating plainly: behind
several replicas the effective limit multiplies by the replica count. That is
an acceptable trade for a portfolio deployment and would not be for a real one,
where this belongs in Redis or at the edge. It exists because an unthrottled
login endpoint against bcrypt is both a credential-stuffing target and a
denial-of-service one — every attempt costs a deliberate ~250ms of CPU.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class FixedWindowLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._hits[key] if now - t < self.window]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True

    def reset(self) -> None:
        """Used by the test suite, which logs in far faster than a human."""
        with self._lock:
            self._hits.clear()


auth_limiter = FixedWindowLimiter(limit=10, window_seconds=60.0)
