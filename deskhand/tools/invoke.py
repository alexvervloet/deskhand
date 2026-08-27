"""Executing a tool call exactly once.

This is the module that makes invariant 1 — *never re-execute a completed side
effect* — true, and it is short because the guarantee comes from where the
write happens rather than from cleverness.

The protocol:

    1. Has this idempotency key already been recorded?  -> return what it did
    2. Otherwise run the handler
    3. Record the outcome under that key, in the SAME transaction

The caller commits. So a crash anywhere in the middle is safe in both
directions: nothing was written, therefore nothing is remembered, therefore the
resumed run does it once. And once the commit lands, the effect and the memory
of it landed together, so the resumed run does it zero more times.

The reason this is allowed to be so simple is that every side effect in this
system is a row in this same Postgres. See the note in the module docstring of
`deskhand/tools/base.py` and the honest version in docs/education/03-exactly-once.md: a
tool that charged a real payment processor could not share a transaction with
the ledger, and would need a third `claimed` state plus reconciliation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import DictRow

from deskhand.tools import faults
from deskhand.tools.base import ToolContext, ToolError, args_hash, get

log = logging.getLogger("deskhand")


@dataclass(frozen=True, slots=True)
class Invocation:
    tool_name: str
    risk: str
    args: dict[str, Any]
    args_hash: str
    result: str
    ok: bool
    # True when this call was already in the ledger, i.e. a resumed run reached
    # a step it had already completed. The world was not touched again.
    replayed: bool
    duration_ms: int
    inverse: dict[str, Any] | None = None


def sanitise(text: str) -> str:
    """Make a tool result storable.

    Postgres `text` and `jsonb` cannot hold a NUL byte, and a tool that returns
    one takes the whole run down with a `DataError` raised from the ledger
    write — after the side effect has already happened. That is the worst
    possible place to fail: the money moved and the record of it did not.

    Found by the garbage fault in the evals on its first run, which is
    precisely what that fault is for. Real tools return NUL bytes more often
    than you would like: binary payloads mislabelled as text, truncated UTF-8,
    a C library's buffer handed over intact.
    """
    return text.replace("\x00", "�")


def idempotency_key(run_id: str, seq: int) -> str:
    """The key for the tool call at step `seq` of `run_id`.

    Deterministic by construction: a resumed run replays its persisted steps in
    order, recomputes the identical key, and the ledger recognises it. Nothing
    random, nothing clock-based — a uuid here would quietly disable the whole
    mechanism while looking more rigorous.
    """
    return f"{run_id}:{seq}"


def _recorded(cur: psycopg.Cursor[DictRow], key: str) -> Invocation | None:
    cur.execute(
        "select tool_name, risk, args, args_hash, result, status::text, inverse, duration_ms"
        "  from tool_invocations where idempotency_key = %s",
        (key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return Invocation(
        tool_name=row["tool_name"],
        risk=row["risk"],
        args=row["args"],
        args_hash=row["args_hash"],
        result=row["result"],
        ok=row["status"] == "succeeded",
        replayed=True,
        duration_ms=row["duration_ms"],
        inverse=row["inverse"],
    )


def invoke(
    cur: psycopg.Cursor[DictRow],
    *,
    org_id: str,
    run_id: str,
    step_id: str,
    seq: int,
    tool_name: str,
    args: dict[str, Any],
) -> Invocation:
    """Run one tool call, or return the record of having already run it.

    Raises only for failures that are not the model's business — a bug in a
    handler, a database that went away. Those leave no ledger row, so the step
    is retried intact. Failures that *are* the model's business (bad arguments,
    a missing order, a policy violation) come back as `ok=False` with the
    message the model should read and react to.
    """
    key = idempotency_key(run_id, seq)

    # Step 1. The run holds an exclusive lease while this executes, so there is
    # no concurrent writer for this key; the unique index on the ledger is the
    # backstop that turns a leasing bug into an error rather than a double
    # refund.
    already = _recorded(cur, key)
    if already is not None:
        log.info(
            "tool %s at %s:%d already recorded — not re-executing", tool_name, run_id, seq
        )
        return already

    tool = get(tool_name)
    fingerprint = args_hash(tool_name, args)
    ctx = ToolContext(org_id=org_id, run_id=run_id, step_id=step_id, cursor=cur)

    started = time.monotonic()
    try:
        # A savepoint, so a handler that fails part-way through leaves no
        # partial write behind AND leaves the surrounding transaction usable —
        # without it, one bad SQL statement would poison the transaction we
        # still need in order to record that the call failed.
        with cur.connection.transaction():
            tool.validate(args)
            # Fault injection sits inside the savepoint so an injected crash
            # rolls back exactly what a real one would. It is a no-op unless a
            # test installed something.
            faults.before(tool_name)
            outcome = faults.after(tool_name, tool.handler(ctx, args))
        ok, result, inverse = True, sanitise(outcome.result), outcome.inverse
    except ToolError as exc:
        ok, result, inverse = False, sanitise(str(exc)), None

    duration_ms = int((time.monotonic() - started) * 1000)

    # Step 3. Written in the caller's transaction, alongside whatever the
    # handler just did.
    cur.execute(
        "insert into tool_invocations"
        "   (org_id, run_id, step_id, tool_name, risk, idempotency_key, args_hash,"
        "    args, status, result, inverse, duration_ms)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            org_id,
            run_id,
            step_id,
            tool_name,
            str(tool.risk),
            key,
            fingerprint,
            json.dumps(args),
            "succeeded" if ok else "failed",
            result,
            json.dumps(inverse) if inverse else None,
            duration_ms,
        ),
    )

    return Invocation(
        tool_name=tool_name,
        risk=str(tool.risk),
        args=args,
        args_hash=fingerprint,
        result=result,
        ok=ok,
        replayed=False,
        duration_ms=duration_ms,
        inverse=inverse,
    )
