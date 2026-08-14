"""The worker: claim a run, drive it, repeat.

    python -m deskhand.worker

Run as many as you like. They coordinate only through the database — there is
no leader, no assignment, and no shared memory. A worker that is killed
mid-trajectory loses nothing except the lease it was holding, which expires on
its own and lets another worker pick the run up exactly where the step log
says it was.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
import uuid
from types import FrameType

from deskhand.db import connection
from deskhand.providers import Provider, get_provider
from deskhand.runtime import approvals, loop, runs

log = logging.getLogger("deskhand")

POLL_SECONDS = 2.0
LEASE_SECONDS = 60

_stopping = False


def _stop(signum: int, frame: FrameType | None) -> None:
    global _stopping
    _stopping = True
    log.info("signal %s received; finishing the current step and stopping", signum)


def worker_id() -> str:
    """Identifies this process in the lease. Host and pid make an abandoned
    lease traceable to the machine that abandoned it."""
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:6]}"


def work_once(me: str, provider: Provider) -> bool:
    """Claim and drive at most one run. True if there was work to do."""
    with connection() as conn, conn.cursor() as cur:
        approvals.expire_stale(cur)
        run = runs.claim_next(cur, me, LEASE_SECONDS)
        conn.commit()

    if run is None:
        return False

    run_id = str(run["id"])
    log.info("claimed run %s (attempt %d)", run_id, run["attempt"])

    with connection() as conn:
        try:
            status = loop.advance(conn, run_id, me, provider, LEASE_SECONDS)
            log.info("run %s -> %s", run_id, status)
        except loop.LeaseLost:
            log.warning("lost the lease on run %s; another worker has it", run_id)
        except Exception as exc:  # noqa: BLE001
            # An unexpected failure must not leave the run marked `running`
            # with a lease nobody holds — that is a run that looks alive and
            # never moves. Fail it explicitly, with the reason on the record.
            log.exception("run %s crashed", run_id)
            with conn.cursor() as cur:
                runs.finish(
                    cur,
                    run_id,
                    status="failed",
                    stop_reason=runs.STOP_ERROR,
                    stop_detail=f"{type(exc).__name__}: {exc}",
                )
                conn.commit()
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    me = worker_id()
    provider = get_provider()
    log.info("worker %s up, provider=%s model=%s", me, provider.name, provider.model)

    while not _stopping:
        try:
            if not work_once(me, provider):
                time.sleep(POLL_SECONDS)
        except Exception:  # noqa: BLE001
            # The queue is the only thing that can be trusted to still be there
            # after an unexpected error, so back off and go round again rather
            # than exiting and losing the worker.
            log.exception("worker loop error; backing off")
            time.sleep(POLL_SECONDS * 2)

    log.info("worker %s stopped", me)
    return 0


if __name__ == "__main__":
    sys.exit(main())
