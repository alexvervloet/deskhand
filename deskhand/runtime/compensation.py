"""Walking a finished run back.

Every reversible tool has recorded its inverse since the day the tool layer was
written. This is the module that finally applies them, and the interesting part
is not the applying — it is everything the plan refuses to do.

**The plan is a pure function of ledger rows.** Not of the model, not of the
conversation, not of anything a tool returned. A recovery path that asks a
language model which effects to undo has put an untrusted decision at exactly
the moment the system is already known to have got something wrong. `plan()`
reads `tool_invocations`, sorts, and stops.

**Nothing is undone.** The word throughout is *compensation*, because undo
promises something that is false for half the ledger. An email that was read
cannot be unread and money that left is gone. The plan applies the inverses
that exist and *reports* the acts that have none, and a compensation whose plan
contains one of those finishes `partial` rather than `applied` no matter how
well the rest of it went. `partial` is the expected outcome, not a degraded
one.

**Order is load-bearing.** Inverses are applied newest first. Two priority
changes on one ticket -- normal to high at step 3, high to urgent at step 7 --
record the inverses "set it to normal" and "set it to high". Apply them in the
order they were captured and the ticket lands on `high`, which is a value it
held for four steps and was never supposed to keep. Apply them backwards and it
lands on `normal`, which is where it started. Each inverse restores the state
its own call overwrote, so it is only correct while every later call has
already been walked back.

**A failure stops everything.** An inverse that raises leaves the compensation
`blocked` with the remaining items `skipped`. Continuing would mean applying an
inverse whose precondition -- that every later effect is already gone -- is no
longer true. Nothing here knows which items are independent of each other, and
guessing wrong writes a state that neither the run nor the compensation
intended.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import psycopg
from psycopg.rows import DictRow

from deskhand.config import settings
from deskhand.runtime import runs
from deskhand.tools.base import ToolContext, ToolError, is_registered
from deskhand.tools.base import get as tool_def
from deskhand.tools.reversible import apply_inverse

log = logging.getLogger("deskhand")

# A compensation races the worker if the run can still act. Cancel it first.
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "exhausted", "cancelled")

# Why a compensation stopped, in the same fixed vocabulary `runs` uses.
STOP_COMPLETE = "complete"
STOP_INVERSE_FAILED = "inverse_failed"
STOP_ATTEMPTS = "attempts_exhausted"
STOP_CANCELLED = "cancelled"

REVERT = "revert"
REPORT = "report"


class LeaseLost(Exception):
    """Another worker took this compensation. Stop touching it immediately."""


class PlanError(Exception):
    """The compensation cannot be created as asked."""


# ----------------------------------------------------------------- planning


def plan(cur: psycopg.Cursor[DictRow], run_id: str) -> list[dict[str, Any]]:
    """The ordered list of items a compensation for this run would contain.

    Read-only and side-effect free, so the same call renders the preview a
    human reads and validates the request they submit.

    What is excluded, and why each exclusion is a decision rather than a
    filter:

    * **Failed invocations.** A handler that raised did so inside a savepoint,
      so its writes were rolled back before the ledger row was written. There
      is no effect to walk back.
    * **Read tools.** Nothing happened.
    * **Reversible calls with a null inverse.** Every reversible handler
      returns early without an inverse exactly when it changed nothing --
      tagging a ticket that already carries the tags, setting a priority to the
      value it already has. `tests/test_tools.py` asserts that correspondence,
      because if a handler ever changes state and forgets its inverse, this
      filter is where the effect quietly leaves the plan.
    * **Anything already reverted**, by an earlier compensation on the same
      run. A blocked compensation that a human fixes and re-requests must not
      walk the same items back twice.

    Irreversible calls are *not* excluded. They cannot be reverted and they are
    the most important line in the preview.
    """
    cur.execute(
        "select ti.id as invocation_id, ti.tool_name, ti.risk, ti.inverse, ti.args,"
        "       s.seq as step_seq"
        "  from tool_invocations ti"
        "  join steps s on s.id = ti.step_id"
        " where ti.run_id = %s"
        "   and ti.status = 'succeeded'"
        "   and ti.risk <> 'read'"
        "   and (ti.risk = 'irreversible' or ti.inverse is not null)"
        "   and not exists ("
        "         select 1 from compensation_items ci"
        "          where ci.invocation_id = ti.id and ci.status = 'reverted')"
        " order by s.seq desc",
        (run_id,),
    )
    items = []
    for seq, row in enumerate(cur.fetchall(), start=1):
        revertable = row["inverse"] is not None
        items.append(
            {
                "seq": seq,
                "invocation_id": str(row["invocation_id"]),
                "step_seq": int(row["step_seq"]),
                "tool_name": row["tool_name"],
                "risk": row["risk"],
                "inverse": row["inverse"],
                "disposition": REVERT if revertable else REPORT,
                "describe": describe(row["tool_name"], row["risk"], row["inverse"], row["args"]),
            }
        )
    return items


def plan_hash(items: list[dict[str, Any]]) -> str:
    """A fingerprint of "this exact plan".

    The consent story, one level up from `approvals.args_hash`. That hash stops
    a human who approved a USD 19.00 refund from having approved a USD 1,900.00
    one; this one stops a human who authorised walking back four specific acts
    from having authorised whatever the ledger says a moment later.

    Covers the identity and treatment of every item, in order. It does not
    cover the inverse payload, which is immutable in the ledger -- a row there
    is written once and never updated.
    """
    payload = json.dumps(
        [[i["seq"], i["invocation_id"], i["disposition"]] for i in items],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def describe(tool_name: str, risk: str, inverse: dict[str, Any] | None, args: Any) -> str:
    """One line a human reads before authorising. Deliberately plain.

    Rendered from the tool name, the *inverse*, and the registry -- all three
    of which this system wrote. Never from a tool's result text, which is where
    a customer's words live.

    For an act with no inverse the sentence comes from `irreversible_note` on
    the ToolDef, so the most important line on the screen is the tool's own
    declaration rather than a string this module guessed about it.
    """
    if inverse is None:
        note = tool_def(tool_name).irreversible_note if is_registered(tool_name) else None
        return note or f"{tool_name} did something this system cannot take back"
    op = inverse.get("op")
    if op == "set_tags":
        tags = inverse.get("tags") or []
        return f"restore tags to {', '.join(tags) if tags else '(none)'}"
    if op == "set_priority":
        return f"restore priority to {inverse['priority']}"
    if op == "set_status":
        return f"restore status to {inverse['status']}"
    if op == "set_assignee":
        who = inverse.get("assignee_id")
        return "restore assignee to nobody" if who is None else "restore the previous assignee"
    if op == "delete_message":
        return "delete the note it added"
    return f"apply the recorded inverse for {tool_name}"


# ----------------------------------------------------------------- creating


def create(
    cur: psycopg.Cursor[DictRow],
    *,
    org_id: str,
    run_id: str,
    requested_by: str,
    reason: str,
    expected_plan_hash: str,
) -> str:
    """Authorise a compensation, or refuse.

    Refuses for four reasons, and each of them is the point rather than
    validation noise:

    * the run belongs to somebody else
    * the run can still act, so a compensation would race its worker
    * there is nothing in the plan
    * the plan is not the plan the caller was shown
    """
    run = _run_for_org(cur, run_id, org_id)

    if run["status"] not in TERMINAL_RUN_STATUSES:
        raise PlanError(
            f"run is {run['status']}; cancel it before compensating, "
            "or a compensation and its worker will race for the same rows"
        )

    items = plan(cur, run_id)
    if not items:
        raise PlanError("this run has nothing to compensate")

    actual = plan_hash(items)
    if actual != expected_plan_hash:
        raise PlanError(
            "the plan changed since it was shown; refusing to walk back something nobody looked at"
        )

    cur.execute(
        "insert into compensations (org_id, run_id, requested_by, reason, plan_hash, max_attempts)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        (org_id, run_id, requested_by, reason, actual, settings.max_compensation_attempts),
    )
    row = cur.fetchone()
    assert row is not None
    compensation_id = str(row["id"])

    for item in items:
        cur.execute(
            "insert into compensation_items"
            "   (compensation_id, seq, invocation_id, step_seq, tool_name, risk,"
            "    inverse, disposition)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                compensation_id,
                item["seq"],
                item["invocation_id"],
                item["step_seq"],
                item["tool_name"],
                item["risk"],
                json.dumps(item["inverse"]) if item["inverse"] else None,
                item["disposition"],
            ),
        )

    runs.audit(
        cur,
        org_id=org_id,
        run_id=run_id,
        actor_kind="human",
        actor_id=requested_by,
        action="compensation.requested",
        detail={
            "compensation_id": compensation_id,
            "reason": reason,
            "items": len(items),
            "unrevertable": sum(1 for i in items if i["disposition"] == REPORT),
            "plan_hash": actual,
        },
    )
    return compensation_id


# ------------------------------------------------------------------ reading


def get(cur: psycopg.Cursor[DictRow], compensation_id: str) -> dict[str, Any]:
    cur.execute("select * from compensations where id = %s", (compensation_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no compensation {compensation_id}")
    return dict(row)


def items(cur: psycopg.Cursor[DictRow], compensation_id: str) -> list[dict[str, Any]]:
    cur.execute(
        "select * from compensation_items where compensation_id = %s order by seq",
        (compensation_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def for_run(cur: psycopg.Cursor[DictRow], run_id: str) -> list[dict[str, Any]]:
    cur.execute(
        "select * from compensations where run_id = %s order by created_at desc",
        (run_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _run_for_org(cur: psycopg.Cursor[DictRow], run_id: str, org_id: str) -> dict[str, Any]:
    cur.execute("select * from runs where id = %s and org_id = %s", (run_id, org_id))
    row = cur.fetchone()
    if row is None:
        raise PlanError("no such run for this merchant")
    return dict(row)


# ------------------------------------------------------------------ leasing


def claim_next(
    cur: psycopg.Cursor[DictRow], worker_id: str, lease_seconds: int = 60
) -> dict[str, Any] | None:
    """Lease one runnable compensation, or return None.

    The same shape as `runs.claim_next`, for the same reason: `for update skip
    locked` lets several workers share the queue without coordinating, and a
    `running` row whose lease expired is a worker that died.
    """
    cur.execute(
        "update compensations set"
        "   status = 'running',"
        "   lease_owner = %s,"
        "   lease_expires_at = now() + make_interval(secs => %s),"
        "   attempt = attempt + 1,"
        "   updated_at = now()"
        " where id = ("
        "   select id from compensations"
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
    cur: psycopg.Cursor[DictRow], compensation_id: str, worker_id: str, lease_seconds: int = 60
) -> bool:
    cur.execute(
        "update compensations set lease_expires_at = now() + make_interval(secs => %s),"
        "                         updated_at = now()"
        " where id = %s and lease_owner = %s and status = 'running'",
        (lease_seconds, compensation_id, worker_id),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------- executing


def advance(
    conn: psycopg.Connection[DictRow],
    compensation_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> str:
    """Drive one leased compensation to a terminal status.

    One item per iteration, one commit per item. A worker that dies between two
    items loses nothing: the next one reads the item statuses and continues
    from the first that is still `pending`. Nothing about the position lives in
    a variable here either.
    """
    while True:
        with conn.cursor() as cur:
            if not renew_lease(cur, compensation_id, worker_id, lease_seconds):
                conn.commit()
                raise LeaseLost(compensation_id)

            comp = get(cur, compensation_id)

            # Boundedness. A run is bounded by steps, tokens, spend and a
            # deadline. A compensation makes no model calls and has a plan that
            # cannot grow, so the only way it can fail to terminate is by
            # crashing and being re-claimed forever. This is that bound, and it
            # is checked before doing any work rather than after.
            if int(comp["attempt"]) > int(comp["max_attempts"]):
                _finish(
                    cur,
                    comp,
                    status="blocked",
                    reason=STOP_ATTEMPTS,
                    detail=(
                        f"gave up after {comp['attempt']} attempts;"
                        " a person needs to look at why it keeps failing"
                    ),
                )
                conn.commit()
                return "blocked"

            item = _next_pending(cur, compensation_id)
            if item is None:
                status, reason, detail = _outcome(cur, compensation_id)
                _finish(cur, comp, status=status, reason=reason, detail=detail)
                conn.commit()
                return status

            if item["disposition"] == REPORT:
                # Nothing to execute. The row exists so that "what could this
                # not take back" has an answer that outlives the incident.
                _mark(
                    cur,
                    item["id"],
                    "unrevertable",
                    detail=f"{item['tool_name']} is irreversible; no inverse exists",
                )
                conn.commit()
                continue

        # Claim the item and apply its inverse in one transaction. Not a
        # savepoint, unlike `tools.invoke`: there, the failure has to be
        # recorded alongside the rest of a step that is still in flight, so the
        # surrounding transaction must survive. Here nothing else is in flight,
        # so a failure rolls the whole thing back and the record of it is
        # written fresh. Either the ticket moved and the row says `reverted`,
        # or neither happened.
        with conn.cursor() as cur:
            claimed = _claim_item(cur, item["id"])
            if not claimed:
                # Another worker took it between the read and here. Its own
                # transaction owns the outcome.
                conn.commit()
                continue

            ctx = _context(cur, comp, item)
            try:
                apply_inverse(ctx, item["inverse"])
            except (ToolError, psycopg.Error) as exc:
                conn.rollback()
                _fail(conn, comp, item, exc)
                return "blocked"
            conn.commit()

        log.info(
            "compensation %s reverted %s from step %s",
            compensation_id,
            item["tool_name"],
            item["step_seq"],
        )


def _context(
    cur: psycopg.Cursor[DictRow], comp: dict[str, Any], item: dict[str, Any]
) -> ToolContext:
    """The scope an inverse is applied under.

    `org_id` is the compensation's, not the inverse's. The ids inside an
    inverse were captured by handlers that already filtered on the org, so they
    are in-tenant by construction -- but `apply_inverse` scopes every statement
    to `ctx.org_id` anyway, so the guarantee does not depend on that argument
    still being true two releases from now.
    """
    cur.execute(
        "select t.id as ticket_id, t.customer_id from runs r"
        "  join tickets t on t.id = r.ticket_id where r.id = %s",
        (comp["run_id"],),
    )
    subject = cur.fetchone()
    assert subject is not None
    return ToolContext(
        org_id=str(comp["org_id"]),
        run_id=str(comp["run_id"]),
        # The step whose effect is being walked back. Carried so a handler that
        # wants to know has a true answer rather than a placeholder.
        step_id=str(item["invocation_id"]),
        ticket_id=str(subject["ticket_id"]),
        customer_id=str(subject["customer_id"]),
        cursor=cur,
    )


def _next_pending(cur: psycopg.Cursor[DictRow], compensation_id: str) -> dict[str, Any] | None:
    cur.execute(
        "select * from compensation_items"
        " where compensation_id = %s and status = 'pending'"
        " order by seq limit 1",
        (compensation_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _claim_item(cur: psycopg.Cursor[DictRow], item_id: str) -> bool:
    """Flip one item to `reverted`, losing the race if somebody already did.

    This is `tools.invoke`'s guarantee expressed in a different table. The
    write that says "this was undone" and the undoing itself are in one
    transaction, so a crash before the commit leaves neither and a crash after
    leaves both. The conditional `status = 'pending'` is what makes a second
    attempt a no-op instead of a second revert.
    """
    cur.execute(
        "update compensation_items set status = 'reverted', applied_at = now()"
        " where id = %s and status = 'pending' returning id",
        (item_id,),
    )
    return cur.fetchone() is not None


def _mark(
    cur: psycopg.Cursor[DictRow], item_id: str, status: str, detail: str | None = None
) -> None:
    cur.execute(
        "update compensation_items set status = %s::compensation_item_status, detail = %s"
        " where id = %s",
        (status, detail, item_id),
    )


def _fail(
    conn: psycopg.Connection[DictRow],
    comp: dict[str, Any],
    item: dict[str, Any],
    exc: Exception,
) -> None:
    """Record a failed inverse and stop, leaving everything after it untouched.

    The remaining items are marked `skipped` rather than left `pending`,
    because `pending` would mean "a worker will get to this" and no worker
    will. A person decides whether to fix the obstruction and request a fresh
    compensation, which will re-plan around whatever this one managed.
    """
    detail = f"{type(exc).__name__}: {exc}"
    with conn.cursor() as cur:
        _mark(cur, item["id"], "failed", detail=detail)
        cur.execute(
            "update compensation_items set status = 'skipped',"
            "       detail = 'not attempted: an earlier inverse failed'"
            " where compensation_id = %s and status = 'pending'",
            (comp["id"],),
        )
        _finish(
            cur,
            comp,
            status="blocked",
            reason=STOP_INVERSE_FAILED,
            detail=(f"could not revert {item['tool_name']} from step {item['step_seq']}: {detail}"),
        )
        conn.commit()
    log.warning("compensation %s blocked on step %s: %s", comp["id"], item["step_seq"], detail)


def _outcome(cur: psycopg.Cursor[DictRow], compensation_id: str) -> tuple[str, str, str]:
    cur.execute(
        "select status::text as status, count(*) as n from compensation_items"
        " where compensation_id = %s group by status",
        (compensation_id,),
    )
    counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
    reverted = counts.get("reverted", 0)
    unrevertable = counts.get("unrevertable", 0)

    if unrevertable:
        return (
            "partial",
            STOP_COMPLETE,
            f"reverted {reverted}; {unrevertable} irreversible "
            f"{'act' if unrevertable == 1 else 'acts'} could not be taken back",
        )
    return "applied", STOP_COMPLETE, f"reverted {reverted}"


def _finish(
    cur: psycopg.Cursor[DictRow],
    comp: dict[str, Any],
    *,
    status: str,
    reason: str,
    detail: str | None = None,
) -> None:
    cur.execute(
        "update compensations set status = %s::compensation_status, stop_reason = %s,"
        "                         stop_detail = %s, lease_owner = null,"
        "                         lease_expires_at = null, finished_at = now(),"
        "                         updated_at = now()"
        " where id = %s",
        (status, reason, detail, comp["id"]),
    )
    runs.audit(
        cur,
        org_id=str(comp["org_id"]),
        run_id=str(comp["run_id"]),
        actor_kind="system",
        action=f"compensation.{status}",
        detail={
            "compensation_id": str(comp["id"]),
            "stop_reason": reason,
            "stop_detail": detail,
        },
    )
    log.info("compensation %s finished: %s (%s)", comp["id"], status, reason)
