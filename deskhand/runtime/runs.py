"""Run records: creating them, leasing them, appending to them, ending them.

The lease is the concurrency story. A worker claims a run for a bounded window
and must keep renewing it; if the worker dies, the lease simply expires and the
run becomes claimable again. Nothing has to notice the death, no supervisor has
to reap anything, and a network partition that makes a live worker *look* dead
costs at most a duplicated attempt — which the idempotency ledger absorbs.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import DictRow

from deskhand.config import settings

# The vocabulary of endings. Fixed, because the UI renders these, the evals
# assert on them, and "why did it stop" is the first question anyone asks.
STOP_END_TURN = "end_turn"
STOP_STEP_CAP = "step_cap"
STOP_TOKEN_CAP = "token_cap"
STOP_SPEND_CAP = "spend_cap"
STOP_DEADLINE = "deadline"
STOP_LOOP = "loop_detected"
STOP_NO_PROGRESS = "no_progress"
STOP_APPROVAL_DENIED = "approval_denied"
STOP_APPROVAL_EXPIRED = "approval_expired"
STOP_ORG_BUDGET = "org_daily_budget"
STOP_PLATFORM_BUDGET = "platform_daily_budget"
STOP_REFUSAL = "model_refusal"
STOP_ERROR = "error"
STOP_CANCELLED = "cancelled"


def create(
    cur: psycopg.Cursor[DictRow],
    *,
    org_id: str,
    ticket_id: str,
    started_by: str | None = None,
) -> str:
    """Queue a run against one ticket.

    Bounds are snapshotted here rather than read at each step: a config change
    mid-flight must not move the goalposts for a run already under way.

    **The prompt names the ticket and quotes none of it.** The reference is an
    identifier this system minted; the subject is a line a customer typed into
    a form. Interpolating the subject here used to look harmless — it is one
    short line, and it helps the agent know what it is picking up — but the
    opening prompt is the one message `transcript.rebuild` does not fence, so
    that line was the single piece of customer text reaching the model as
    trusted narration. The subject is not lost: `get_ticket` returns it, inside
    the fence, along with the body it belongs to.
    """
    cur.execute(
        "select reference from tickets where id = %s and org_id = %s",
        (ticket_id, org_id),
    )
    ticket = cur.fetchone()
    if ticket is None:
        raise ValueError("no such ticket for this org")

    prompt = (
        f"Work support ticket {ticket['reference']}.\n\n"
        "Read the ticket, establish the facts from the order record and the knowledge "
        "base, and then do what is actually due. Finish by summarising what you did and "
        "why. If the right answer is that a human has to decide, say so and escalate "
        "rather than guessing."
    )

    cur.execute(
        "insert into runs (org_id, ticket_id, started_by, prompt, max_steps, max_tokens,"
        "                  max_spend_micros, deadline_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))"
        " returning id",
        (
            org_id,
            ticket_id,
            started_by,
            prompt,
            settings.max_steps_per_run,
            settings.max_tokens_per_run,
            int(settings.max_spend_usd_per_run * 1_000_000),
            settings.max_wallclock_seconds_per_run,
        ),
    )
    row = cur.fetchone()
    assert row is not None
    return str(row["id"])


def claim_next(
    cur: psycopg.Cursor[DictRow], worker_id: str, lease_seconds: int = 60
) -> dict[str, Any] | None:
    """Lease one runnable run, or return None.

    `for update skip locked` is what lets several workers share the queue
    without coordinating: each takes a different row instead of blocking on the
    same one. The `or` clause is the recovery path — a run still marked
    `running` whose lease has expired is a run whose worker died, and it is
    claimable again.
    """
    cur.execute(
        "update runs set"
        "   status = 'running',"
        "   lease_owner = %s,"
        "   lease_expires_at = now() + make_interval(secs => %s),"
        "   attempt = attempt + 1,"
        "   updated_at = now()"
        " where id = ("
        "   select id from runs"
        "    where status = 'queued'"
        "       or (status = 'running' and lease_expires_at < now())"
        "    order by created_at"
        "    for update skip locked"
        "    limit 1"
        " )"
        " returning *",
        (worker_id, lease_seconds),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def renew_lease(
    cur: psycopg.Cursor[DictRow], run_id: str, worker_id: str, lease_seconds: int = 60
) -> bool:
    """Extend this worker's lease. False means we lost it and must stop.

    Losing a lease is not an error — it means we were slow enough to look dead
    and somebody else has the run. Continuing to write would be the error.
    """
    cur.execute(
        "update runs set lease_expires_at = now() + make_interval(secs => %s), updated_at = now()"
        " where id = %s and lease_owner = %s and status = 'running'",
        (lease_seconds, run_id, worker_id),
    )
    return cur.rowcount == 1


def get(cur: psycopg.Cursor[DictRow], run_id: str) -> dict[str, Any]:
    cur.execute("select * from runs where id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return dict(row)


def next_seq(cur: psycopg.Cursor[DictRow], run_id: str) -> int:
    cur.execute("select coalesce(max(seq), 0) + 1 as seq from steps where run_id = %s", (run_id,))
    row = cur.fetchone()
    assert row is not None
    return int(row["seq"])


def append_step(
    cur: psycopg.Cursor[DictRow],
    *,
    run_id: str,
    seq: int,
    kind: str,
    content: dict[str, Any],
    tool_name: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int = 0,
    latency_ms: int = 0,
) -> str:
    cur.execute(
        "insert into steps (run_id, seq, kind, content, tool_name, input_tokens,"
        "                   output_tokens, cost_micros, latency_ms)"
        " values (%s, %s, %s::step_kind, %s, %s, %s, %s, %s, %s) returning id",
        (
            run_id,
            seq,
            kind,
            json.dumps(content),
            tool_name,
            input_tokens,
            output_tokens,
            cost_micros,
            latency_ms,
        ),
    )
    row = cur.fetchone()
    assert row is not None
    return str(row["id"])


def add_usage(
    cur: psycopg.Cursor[DictRow],
    run_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    provider: str,
    model: str,
) -> None:
    cur.execute(
        "update runs set input_tokens = input_tokens + %s,"
        "                output_tokens = output_tokens + %s,"
        "                cost_micros = cost_micros + %s,"
        "                provider = %s, model = %s, updated_at = now()"
        " where id = %s",
        (input_tokens, output_tokens, cost_micros, provider, model, run_id),
    )


def suspend_for_approval(cur: psycopg.Cursor[DictRow], run_id: str) -> None:
    """Park the run. Note the lease is released at the same time — a run
    waiting on a human could be waiting for a day, and holding a 60-second
    lease across that would make it look perpetually crashed.

    `suspended_at` is stamped here so `requeue` can give the wait back to the
    deadline. Without it the wall-clock budget runs while a person is reading
    the approval screen, and they are penalised for taking it seriously.
    """
    cur.execute(
        "update runs set status = 'awaiting_approval', lease_owner = null,"
        "                lease_expires_at = null, suspended_at = now(),"
        "                updated_at = now()"
        " where id = %s",
        (run_id,),
    )


def requeue(cur: psycopg.Cursor[DictRow], run_id: str) -> None:
    """Make a suspended run claimable again, once a human has decided.

    The deadline moves forward by exactly the time the run spent suspended.
    That is not the deadline resetting: only *measured* wait on a human is ever
    added, so a run that crash-loops still cannot earn itself a fresh clock,
    and a run nobody answers still ends — on the approval's own TTL, which is
    the bound that belongs to a person not answering.

    Without this the deadline bounded human deliberation as well as agent work,
    and the failure was the worst shape available: a refund approved after the
    budget ran out executed, and the run then died on the deadline with the
    money gone and no summary written.
    """
    cur.execute(
        "update runs set status = 'queued',"
        "                deadline_at = deadline_at"
        "                              + (now() - coalesce(suspended_at, now())),"
        "                suspended_at = null,"
        "                updated_at = now()"
        " where id = %s and status = 'awaiting_approval'",
        (run_id,),
    )


def finish(
    cur: psycopg.Cursor[DictRow],
    run_id: str,
    *,
    status: str,
    stop_reason: str,
    stop_detail: str | None = None,
) -> None:
    cur.execute(
        "update runs set status = %s::run_status, stop_reason = %s, stop_detail = %s,"
        "                lease_owner = null, lease_expires_at = null,"
        "                finished_at = now(), updated_at = now()"
        " where id = %s",
        (status, stop_reason, stop_detail, run_id),
    )


def audit(
    cur: psycopg.Cursor[DictRow],
    *,
    org_id: str,
    action: str,
    actor_kind: str = "system",
    actor_id: str | None = None,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    cur.execute(
        "insert into audit_log (org_id, actor_kind, actor_id, run_id, action, detail)"
        " values (%s, %s, %s, %s, %s, %s)",
        (org_id, actor_kind, actor_id, run_id, action, json.dumps(detail or {})),
    )
