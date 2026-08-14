"""The approval gate.

Invariant 2: *no irreversible tool executes without a recorded human approval
tied to that exact run, step, and argument hash.*

The binding to `args_hash` is the part that does the work. An approval is not
"this agent may issue refunds" or even "this run may issue a refund" — it is
"this run may issue *this* refund, of this amount, against this order". If the
arguments differ by a cent when the run resumes, the hash differs, and the
runtime refuses rather than executing something a human never saw.

Expiry is deliberately loud. An approval nobody answers ends the run with
`approval_expired`, which is a different outcome from `approval_denied` and
should be read differently: denial is the process working, expiry is the
process being absent.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import DictRow

from deskhand.config import settings
from deskhand.tools import args_hash, get


def request(
    cur: psycopg.Cursor[DictRow],
    *,
    org_id: str,
    run_id: str,
    step_seq: int,
    tool_use_id: str,
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Record that a human decision is needed, or return the existing request.

    Idempotent on (run_id, tool_use_id): a resumed run re-derives the same
    tool_use id from its persisted step log and must find the decision that was
    already made, not ask for a second one.
    """
    tool = get(tool_name)
    preview = tool.preview(args) if tool.preview else f"{tool_name}({json.dumps(args)})"

    cur.execute(
        "insert into approvals (org_id, run_id, step_seq, tool_use_id, tool_name, args,"
        "                       args_hash, preview, expires_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))"
        " on conflict (run_id, tool_use_id) do nothing",
        (
            org_id,
            run_id,
            step_seq,
            tool_use_id,
            tool_name,
            json.dumps(args),
            args_hash(tool_name, args),
            preview,
            settings.approval_ttl_seconds,
        ),
    )
    return lookup(cur, run_id, tool_use_id)  # type: ignore[return-value]


def lookup(
    cur: psycopg.Cursor[DictRow], run_id: str, tool_use_id: str
) -> dict[str, Any] | None:
    cur.execute(
        "select *, (status = 'pending' and expires_at < now()) as is_stale"
        "  from approvals where run_id = %s and tool_use_id = %s",
        (run_id, tool_use_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def pending_for_org(cur: psycopg.Cursor[DictRow], org_id: str) -> list[dict[str, Any]]:
    cur.execute(
        "select a.*, r.ticket_id, t.reference as ticket_reference, t.subject"
        "  from approvals a"
        "  join runs r on r.id = a.run_id"
        "  join tickets t on t.id = r.ticket_id"
        " where a.org_id = %s and a.status = 'pending' and a.expires_at > now()"
        " order by a.created_at",
        (org_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def decide(
    cur: psycopg.Cursor[DictRow],
    *,
    approval_id: str,
    org_id: str,
    decision: str,
    decided_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record a human decision and make the run claimable again.

    Only a pending, unexpired approval can be decided. Approving something that
    already expired would resurrect consent the process had already declared
    stale, so it is rejected rather than accepted late.
    """
    if decision not in ("approved", "denied"):
        raise ValueError(f"decision must be approved or denied, not {decision!r}")

    cur.execute(
        "update approvals set status = %s::approval_status, decided_by = %s,"
        "                     decided_at = now(), reason = %s"
        " where id = %s and org_id = %s and status = 'pending' and expires_at > now()"
        " returning *",
        (decision, decided_by, reason, approval_id, org_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("approval is not pending, has expired, or does not exist")

    cur.execute(
        "update runs set status = 'queued', updated_at = now()"
        " where id = %s and status = 'awaiting_approval'",
        (row["run_id"],),
    )
    return dict(row)


def expire_stale(cur: psycopg.Cursor[DictRow]) -> int:
    """Mark timed-out approvals expired and wake their runs so they can fail.

    Waking the run matters: a run left in `awaiting_approval` forever is
    indistinguishable from one waiting on an attentive human. It has to be
    given the chance to notice and end.
    """
    cur.execute(
        "update approvals set status = 'expired'"
        " where status = 'pending' and expires_at <= now() returning run_id"
    )
    run_ids = [r["run_id"] for r in cur.fetchall()]
    for run_id in run_ids:
        cur.execute(
            "update runs set status = 'queued', updated_at = now()"
            " where id = %s and status = 'awaiting_approval'",
            (run_id,),
        )
    return len(run_ids)
