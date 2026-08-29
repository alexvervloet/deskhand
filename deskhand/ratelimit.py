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

# Starting runs. Keyed by org, because the cost lands on the merchant's budget
# and not on the individual who clicked.
#
# The budget caps are the real ceiling and they are checked before every model
# call, so this is not what stops a bill running away. It stops the cheaper
# nuisance the budget caps handle badly: a signed-in user looping the endpoint
# fills the queue with runs that each burn a little of a shared daily budget
# before stopping, and every other tenant on the deployment finds the platform
# ceiling exhausted by someone else's afternoon.
run_limiter = FixedWindowLimiter(limit=30, window_seconds=60.0)
