"""Structured events for whatever collects your logs.

**The step log is the trace.** Every model call and every tool call is already a
row in `steps` carrying tokens, cost, latency, arguments, and result, joined to
a run that knows who started it and what it was allowed to spend. That is a
complete, queryable, permanently retained trace, and it is the same data any
external tracing product would hold — except it is in the database the product
already depends on, and it is subject to the same backups and the same access
control.

What the database is bad at is being *watched*. So this module emits one
structured line per interesting event, for a log collector to pick up and alert
on. It is a companion to the step log, not a second copy of it: the line carries
identifiers and numbers, never the content.

One consequence of being a log rather than a table, worth stating because it
looks like a bug the first time you see it: these lines are emitted inside the
transaction that is doing the work, and they are not rolled back with it. A step
that crashes after tracing leaves its line behind and its rows do not survive, so
a retried attempt traces twice while the audit log records once. That is the
right trade — a tracer that waited for a commit would miss exactly the events
worth alerting on, which are the ones where the commit never came — but it means
these lines describe *attempts*, and `audit_log` is what describes outcomes.
Events carry `attempt` where a repeat is expected, so the two can be told apart.

The only real engineering requirement here is negative.

**Observability must never take the product down.** A tracer that raises turns a
successful refund into a failed run, which is a strictly worse outcome than
having no tracing at all. So `emit()` cannot raise, cannot block, and cannot
care whether its arguments are serialisable. That property is asserted in
tests/test_tracing.py rather than assumed.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# A separate logger so a collector can select these lines by name, and so the
# application's own logging configuration can silence them without silencing
# anything that matters.
log = logging.getLogger("deskhand.events")


def _configure() -> None:
    """Give the event stream its own handler, once.

    Normally a library has no business configuring logging. This is not a
    library, and the alternative was worse: under uvicorn the root logger has no
    handler and sits at WARNING, so every event emitted here was silently
    dropped in the deployed app while appearing perfectly in local scripts that
    happened to call `basicConfig`. An event stream that only works when
    somebody remembered to configure it is not an event stream.

    `propagate = False` so that an application which *does* configure the root
    logger gets one copy of each line rather than two.
    """
    if log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_configure()


def emit(event: str, **fields: Any) -> None:
    """Emit one structured event. Never raises, under any circumstances."""
    try:
        payload: dict[str, Any] = {
            "event": event,
            "ts": datetime.now(UTC).isoformat(),
            **fields,
        }
        # `default=str` handles uuids, Decimals and datetimes without the caller
        # having to think about it. If a value defeats even that, the except
        # below catches it.
        log.info(json.dumps(payload, default=str, separators=(",", ":")))
    except Exception:  # noqa: BLE001, S110 - deliberately total, and re-logging is the loop
        # Swallowed on purpose, and not re-logged: a logging failure inside a
        # logging call is exactly where an infinite loop comes from.
        pass


def run_started(run_id: str, *, org_id: str, ticket: str, provider: str, model: str) -> None:
    emit(
        "run.started",
        run_id=run_id,
        org_id=org_id,
        ticket=ticket,
        provider=provider,
        model=model,
    )


def model_call(
    run_id: str,
    seq: int,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    latency_ms: int,
    stop_reason: str,
    tool_calls: int,
) -> None:
    emit(
        "model.call",
        run_id=run_id,
        seq=seq,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_micros=cost_micros,
        latency_ms=latency_ms,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
    )


def tool_call(
    run_id: str,
    seq: int,
    *,
    tool: str,
    risk: str,
    ok: bool,
    replayed: bool,
    duration_ms: int,
) -> None:
    emit(
        "tool.call",
        run_id=run_id,
        seq=seq,
        tool=tool,
        risk=risk,
        ok=ok,
        # The field worth alerting on. A replayed step means a run was resumed
        # onto work it had already done — healthy, and worth knowing the rate of.
        replayed=replayed,
        duration_ms=duration_ms,
    )


def approval_requested(run_id: str, *, tool: str, args_hash: str) -> None:
    emit("approval.requested", run_id=run_id, tool=tool, args_hash=args_hash)


def approval_decided(
    run_id: str, *, tool: str, decision: str, decided_by: str | None, attempt: int = 1
) -> None:
    emit(
        "approval.decided",
        run_id=run_id,
        tool=tool,
        decision=decision,
        decided_by=decided_by,
        # A run that crashed after acting on a decision retries and traces this
        # again. One human said yes once; the attempt number is what stops a
        # reader — or a counter — from believing they said it twice.
        attempt=attempt,
    )


def run_finished(
    run_id: str, *, status: str, stop_reason: str, steps: int, cost_micros: int
) -> None:
    emit(
        "run.finished",
        run_id=run_id,
        status=status,
        stop_reason=stop_reason,
        steps=steps,
        cost_micros=cost_micros,
    )
