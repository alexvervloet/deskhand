"""Walking a finished run back.

The five invariants again, pointed backwards. Durability is that an inverse is
applied exactly once across a crash; consent is that a person authorised the
exact plan that ran; boundedness is that a compensation that keeps failing
stops; integrity is that the plan comes from the ledger and nothing else;
accountability is that what could not be taken back is on the record rather
than quietly absent.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from deskhand.db import connection, fetch_all, fetch_one
from deskhand.providers import ScriptedProvider, call, text
from deskhand.runtime import compensation
from tests.test_runtime import drive, org_id, start_run, ticket_id, user_id

pytestmark = pytest.mark.usefixtures("fresh")


# ------------------------------------------------------------------ helpers


def authorise(run_id: str, reason: str = "wrong ticket") -> str:
    """Plan, then authorise that plan. What the two endpoints do."""
    with connection() as conn, conn.cursor() as cur:
        items = compensation.plan(cur, run_id)
        compensation_id = compensation.create(
            cur,
            org_id=org_id(),
            run_id=run_id,
            requested_by=user_id("owner@northwind.test"),
            reason=reason,
            expected_plan_hash=compensation.plan_hash(items),
        )
        conn.commit()
    return compensation_id


def apply(compensation_id: str, worker: str = "test-worker") -> str:
    """Claim and advance, the way the worker would."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensations set status = 'running', lease_owner = %s,"
            "                         lease_expires_at = now() + interval '60 seconds',"
            "                         attempt = attempt + 1"
            " where id = %s",
            (worker, compensation_id),
        )
        conn.commit()
    with connection() as conn:
        return compensation.advance(conn, compensation_id, worker)


def comp_row(compensation_id: str) -> dict[str, Any]:
    row = fetch_one("select * from compensations where id = %s", (compensation_id,))
    assert row is not None
    return row


def item_rows(compensation_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        "select * from compensation_items where compensation_id = %s order by seq",
        (compensation_id,),
    )


def ticket(reference: str) -> dict[str, Any]:
    row = fetch_one("select * from tickets where reference = %s", (reference,))
    assert row is not None
    return row


# The trajectory the ordering rule exists for: normal -> high -> urgent, with
# the inverses "back to normal" and "back to high" recorded in that order.
RAISE_TWICE: list[Any] = [
    [call("set_priority", reference="NW-2", priority="high")],
    [call("set_priority", reference="NW-2", priority="urgent")],
    text("Raised it twice. Done."),
]


def two_priority_changes() -> str:
    """A finished run that moved one ticket's priority twice, then stopped."""
    run_id = start_run("NW-2")
    assert drive(run_id, ScriptedProvider(script=RAISE_TWICE)) == "succeeded"
    return run_id


# ---------------------------------------------------- the plan, and its order


def test_the_plan_walks_backwards_and_lands_on_the_original_value() -> None:
    assert ticket("NW-2")["priority"] == "normal"
    run_id = two_priority_changes()
    assert ticket("NW-2")["priority"] == "urgent"

    assert apply(authorise(run_id)) == "applied"

    # Applied forward, the two inverses would land on `high`: a value the
    # ticket genuinely held for one step and was never meant to keep. Each
    # inverse restores what its own call overwrote, so it is only correct once
    # every later call is already gone.
    assert ticket("NW-2")["priority"] == "normal"


def test_the_plan_is_ordered_newest_first() -> None:
    run_id = two_priority_changes()
    with connection() as conn, conn.cursor() as cur:
        items = compensation.plan(cur, run_id)

    assert [i["seq"] for i in items] == [1, 2]
    # Item 1 undoes the later step.
    assert items[0]["step_seq"] > items[1]["step_seq"]
    assert items[0]["inverse"]["priority"] == "high"
    assert items[1]["inverse"]["priority"] == "normal"


def test_a_read_only_run_has_nothing_to_compensate() -> None:
    run_id = start_run("NW-2")
    provider = ScriptedProvider(
        script=[
            [call("get_ticket", reference="NW-2")],
            text("Looked, did nothing."),
        ]
    )
    assert drive(run_id, provider) == "succeeded"

    with connection() as conn, conn.cursor() as cur:
        assert compensation.plan(cur, run_id) == []
        with pytest.raises(compensation.PlanError, match="changed nothing that can be walked back"):
            compensation.create(
                cur,
                org_id=org_id(),
                run_id=run_id,
                requested_by=user_id("owner@northwind.test"),
                reason="why not",
                expected_plan_hash=compensation.plan_hash([]),
            )


def test_a_call_that_changed_nothing_is_not_in_the_plan() -> None:
    """A reversible tool records no inverse when it is a no-op, and the plan
    reads that as "nothing happened here" rather than "unknown"."""
    run_id = start_run("NW-2")
    provider = ScriptedProvider(
        script=[
            # NW-2 is already `normal`, so this handler returns early.
            [call("set_priority", reference="NW-2", priority="normal")],
            text("Nothing to do."),
        ]
    )
    assert drive(run_id, provider) == "succeeded"

    recorded = fetch_one("select inverse from tool_invocations where run_id = %s", (run_id,))
    assert recorded is not None
    assert recorded["inverse"] is None
    with connection() as conn, conn.cursor() as cur:
        assert compensation.plan(cur, run_id) == []


def test_a_plan_with_nothing_left_to_revert_is_not_offered() -> None:
    """An irreversible act never leaves a plan.

    It is never marked `reverted`, so it is in every future plan for this run
    forever. Without this refusal the screen would keep offering to walk the
    run back, with a count of zero, and every press would write a compensation
    that changed nothing and finished `partial`.
    """
    run_id = start_run("NW-1")
    script = [
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="damaged")],
        text("Refunded."),
    ]
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"
    _approve_pending(run_id)
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"

    with connection() as conn, conn.cursor() as cur:
        items = compensation.plan(cur, run_id)
        # The refund is still in the plan, because "what could not be taken
        # back" is the thing worth reading.
        assert [i["disposition"] for i in items] == ["report"]
        with pytest.raises(compensation.PlanError, match="nothing left that can be reverted"):
            compensation.create(
                cur,
                org_id=org_id(),
                run_id=run_id,
                requested_by=user_id("owner@northwind.test"),
                reason="try anyway",
                expected_plan_hash=compensation.plan_hash(items),
            )


def test_a_second_compensation_is_not_offered_once_everything_revertable_is_gone() -> None:
    run_id = two_priority_changes()
    assert apply(authorise(run_id)) == "applied"
    with (
        connection() as conn,
        conn.cursor() as cur,
        pytest.raises(compensation.PlanError, match="changed nothing that can be walked back"),
    ):
        compensation.create(
            cur,
            org_id=org_id(),
            run_id=run_id,
            requested_by=user_id("owner@northwind.test"),
            reason="again",
            expected_plan_hash=compensation.plan_hash([]),
        )


# --------------------------------------------------------------- consent


def test_a_plan_that_changed_since_it_was_shown_is_refused() -> None:
    """The `args_hash` device, one level up.

    A human authorises a list of items. If the ledger says something different
    by the time the request lands, the request is refused rather than executed
    against a plan nobody looked at.
    """
    run_id = two_priority_changes()
    with connection() as conn, conn.cursor() as cur:
        shown = compensation.plan_hash(compensation.plan(cur, run_id))

    # Someone re-queues the run and it does one more reversible thing before
    # the request arrives. The script carries the turns already taken, because
    # a resumed run rebuilds its history from the step log and the provider
    # derives which turn to serve from that history rather than from a counter.
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set status = 'queued', finished_at = null where id = %s", (run_id,)
        )
        conn.commit()
    resumed = ScriptedProvider(
        script=[
            *RAISE_TWICE,
            [call("tag_ticket", reference="NW-2", tags=["late-addition"])],
            text("And one more thing."),
        ]
    )
    assert drive(run_id, resumed) == "succeeded"

    with (
        connection() as conn,
        conn.cursor() as cur,
        pytest.raises(compensation.PlanError, match="changed since it was shown"),
    ):
        compensation.create(
            cur,
            org_id=org_id(),
            run_id=run_id,
            requested_by=user_id("owner@northwind.test"),
            reason="stale plan",
            expected_plan_hash=shown,
        )


def test_a_run_that_can_still_act_is_refused() -> None:
    """A compensation against a live run races its worker for the same rows."""
    run_id = two_priority_changes()
    with connection() as conn, conn.cursor() as cur:
        items = compensation.plan(cur, run_id)
        cur.execute("update runs set status = 'running' where id = %s", (run_id,))
        with pytest.raises(compensation.PlanError, match="cancel it before compensating"):
            compensation.create(
                cur,
                org_id=org_id(),
                run_id=run_id,
                requested_by=user_id("owner@northwind.test"),
                reason="too soon",
                expected_plan_hash=compensation.plan_hash(items),
            )


def test_another_merchants_run_is_not_compensable() -> None:
    run_id = two_priority_changes()
    with connection() as conn, conn.cursor() as cur:
        items = compensation.plan(cur, run_id)
        with pytest.raises(compensation.PlanError, match="no such run"):
            compensation.create(
                cur,
                org_id=org_id("lumen"),
                run_id=run_id,
                requested_by=user_id("owner@northwind.test"),
                reason="not mine",
                expected_plan_hash=compensation.plan_hash(items),
            )


def test_only_one_compensation_per_run_is_in_flight() -> None:
    run_id = two_priority_changes()
    authorise(run_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        authorise(run_id, reason="again")


# -------------------------------------------------------------- durability


def test_a_crash_mid_compensation_does_not_revert_twice() -> None:
    """The crash-resume story, pointed backwards.

    A worker dies after applying one inverse. Another claims the compensation,
    reads the item statuses, and continues from the first that is still
    pending — never re-applying the one that already landed.
    """
    run_id = two_priority_changes()
    compensation_id = authorise(run_id)

    # Worker A applies exactly one item, then stops renewing its lease.
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensations set status = 'running', lease_owner = 'worker-a',"
            "                         lease_expires_at = now() + interval '60 seconds',"
            "                         attempt = 1"
            " where id = %s",
            (compensation_id,),
        )
        conn.commit()

    with connection() as conn:
        with conn.cursor() as cur:
            item = fetch_one(
                "select * from compensation_items where compensation_id = %s and seq = 1",
                (compensation_id,),
            )
            assert item is not None
            comp = compensation.get(cur, compensation_id)
            assert compensation._claim_item(cur, str(item["id"]))
            ctx = compensation._context(cur, comp, item)
            compensation.apply_inverse(ctx, item["inverse"])
        conn.commit()

    assert ticket("NW-2")["priority"] == "high"

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensations set lease_expires_at = now() - interval '1 second' where id = %s",
            (compensation_id,),
        )
        conn.commit()
        claimed = compensation.claim_next(cur, "worker-b")
        conn.commit()
    assert claimed is not None and str(claimed["id"]) == compensation_id

    with connection() as conn:
        assert compensation.advance(conn, compensation_id, "worker-b") == "applied"

    # If item 1 had been applied a second time the ticket would read `high`,
    # because the inverse restores an absolute value rather than stepping down.
    assert ticket("NW-2")["priority"] == "normal"
    assert [i["status"] for i in item_rows(compensation_id)] == ["reverted", "reverted"]


def test_the_database_refuses_a_second_revert_of_the_same_invocation() -> None:
    """Belt and braces under the runtime's own guarantee.

    The conditional update already makes a second attempt a no-op. The partial
    unique index is what turns a leasing bug, or a compensation built from a
    stale plan, into a constraint violation rather than a ticket that quietly
    gets un-tagged twice.
    """
    run_id = two_priority_changes()
    compensation_id = authorise(run_id)
    assert apply(compensation_id) == "applied"

    invocation = fetch_one(
        "select invocation_id from compensation_items where compensation_id = %s and seq = 1",
        (compensation_id,),
    )
    assert invocation is not None
    with (
        connection() as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.UniqueViolation),
    ):
        cur.execute(
            "insert into compensation_items"
            "  (compensation_id, seq, invocation_id, step_seq, tool_name, risk,"
            "   disposition, status)"
            " values (%s, 99, %s, 1, 'set_priority', 'reversible', 'revert', 'reverted')",
            (compensation_id, invocation["invocation_id"]),
        )


def test_a_second_compensation_plans_around_what_the_first_reverted() -> None:
    run_id = two_priority_changes()
    assert apply(authorise(run_id)) == "applied"
    with connection() as conn, conn.cursor() as cur:
        assert compensation.plan(cur, run_id) == []


# ------------------------------------------------------------ accountability


def test_an_irreversible_act_is_reported_and_never_touched() -> None:
    """The honest half.

    Money that left is gone. The compensation says so, on the record, and
    finishes `partial` rather than claiming a clean revert.
    """
    run_id = start_run("NW-1")
    script = [
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="damaged")],
        [call("set_ticket_status", reference="NW-1", status="resolved")],
        text("Refunded and closed."),
    ]
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"
    _approve_pending(run_id)
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"

    refunds_before = fetch_all("select * from refunds where run_id = %s", (run_id,))
    assert len(refunds_before) == 1

    compensation_id = authorise(run_id, reason="agent misread the ticket")
    assert apply(compensation_id) == "partial"

    comp = comp_row(compensation_id)
    assert comp["status"] == "partial"
    assert "could not be taken back" in comp["stop_detail"]

    items = item_rows(compensation_id)
    by_tool = {i["tool_name"]: i for i in items}
    assert by_tool["set_ticket_status"]["status"] == "reverted"
    assert by_tool["issue_refund"]["status"] == "unrevertable"
    assert by_tool["issue_refund"]["disposition"] == "report"

    # The status went back. The money did not move in either direction.
    assert ticket("NW-1")["status"] == "open"
    assert fetch_all("select * from refunds where run_id = %s", (run_id,)) == refunds_before


def test_who_asked_and_why_is_on_the_record() -> None:
    run_id = two_priority_changes()
    compensation_id = authorise(run_id, reason="ticket was triaged wrong")
    assert apply(compensation_id) == "applied"

    actions = [
        r["action"]
        for r in fetch_all(
            "select action from audit_log where run_id = %s order by created_at", (run_id,)
        )
    ]
    assert "compensation.requested" in actions
    assert "compensation.applied" in actions

    requested = fetch_one(
        "select actor_kind, actor_id, detail from audit_log"
        " where run_id = %s and action = 'compensation.requested'",
        (run_id,),
    )
    assert requested is not None
    assert requested["actor_kind"] == "human"
    assert str(requested["actor_id"]) == user_id("owner@northwind.test")
    assert requested["detail"]["reason"] == "ticket was triaged wrong"


# --------------------------------------------------------------- integrity


def test_the_plan_ignores_everything_the_ticket_says() -> None:
    """The plan is a pure function of ledger rows.

    NW-4's body carries a forged instruction. A run over it produces exactly
    the plan a run over a clean ticket does, because nothing in a tool result
    or a ticket body is read on this path at all.
    """
    hostile = start_run("NW-4")
    clean = start_run("NW-2")
    for run_id, reference in ((hostile, "NW-4"), (clean, "NW-2")):
        assert (
            drive(
                run_id,
                ScriptedProvider(
                    script=[
                        [call("get_ticket", reference=reference)],
                        [call("set_priority", reference=reference, priority="high")],
                        text("Done."),
                    ]
                ),
            )
            == "succeeded"
        )

    with connection() as conn, conn.cursor() as cur:
        hostile_plan = compensation.plan(cur, hostile)
        clean_plan = compensation.plan(cur, clean)

    shape = lambda plan: [(i["seq"], i["tool_name"], i["disposition"]) for i in plan]  # noqa: E731
    assert shape(hostile_plan) == shape(clean_plan)
    # The `get_ticket` call that read the attack is not in either plan.
    assert all(i["tool_name"] == "set_priority" for i in hostile_plan)


def test_no_tool_can_ask_for_a_compensation() -> None:
    """Undoing is not a tool, so the model cannot request one.

    Stated as a test rather than a comment because the registry is the thing
    that decides what the model may reach, and a future tool named
    `revert_run` would be a very reasonable-looking mistake.
    """
    from deskhand.tools import all_tools

    names = {t.name for t in all_tools()}
    assert not any("revert" in n or "undo" in n or "compensat" in n for n in names)


# ------------------------------------------------------------ failure, bounds


def test_a_failed_inverse_blocks_and_leaves_the_rest_untouched() -> None:
    """A compensation does not walk past a failure.

    The plan is ordered because items can depend on each other, and nothing
    here knows which are independent. Continuing would apply an inverse whose
    precondition — that every later effect is already gone — is no longer true.
    """
    run_id = two_priority_changes()
    compensation_id = authorise(run_id)

    # Make the first inverse impossible: it names a ticket id, and the id it
    # names is about to stop matching anything in this org.
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensation_items"
            "   set inverse = jsonb_set(inverse, '{ticket_id}',"
            "       to_jsonb('00000000-0000-0000-0000-000000000000'::text))"
            " where compensation_id = %s and seq = 1",
            (compensation_id,),
        )
        conn.commit()

    assert apply(compensation_id) == "blocked"

    comp = comp_row(compensation_id)
    assert comp["stop_reason"] == compensation.STOP_INVERSE_FAILED
    assert "could not revert set_priority" in comp["stop_detail"]

    statuses = [i["status"] for i in item_rows(compensation_id)]
    assert statuses == ["failed", "skipped"]

    # Nothing moved. The second inverse would have set `normal`, and applying
    # it without the first would have skipped a value the ticket really held.
    assert ticket("NW-2")["priority"] == "urgent"


def test_an_inverse_whose_row_is_gone_raises_rather_than_lying() -> None:
    """A statement that changes no rows is not a successful undo.

    Letting it pass would write `reverted` against something that was not
    reverted, and an audit trail that overstates what it walked back is worse
    than one that stops and says so.
    """
    from deskhand.tools.base import ToolContext, ToolError
    from deskhand.tools.reversible import apply_inverse

    with connection() as conn, conn.cursor() as cur:
        ctx = ToolContext(
            org_id=org_id(),
            run_id="00000000-0000-0000-0000-000000000000",
            step_id="00000000-0000-0000-0000-000000000000",
            ticket_id=ticket_id("NW-2"),
            customer_id="00000000-0000-0000-0000-000000000000",
            cursor=cur,
        )
        with pytest.raises(ToolError, match="nothing to undo"):
            apply_inverse(
                ctx,
                {
                    "op": "set_priority",
                    "ticket_id": "00000000-0000-0000-0000-000000000000",
                    "priority": "low",
                },
            )


def test_an_inverse_cannot_reach_into_another_merchant() -> None:
    run_id = two_priority_changes()
    compensation_id = authorise(run_id)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensations set org_id = %s where id = %s", (org_id("lumen"), compensation_id)
        )
        conn.commit()

    assert apply(compensation_id) == "blocked"
    assert ticket("NW-2")["priority"] == "urgent"


def test_a_compensation_that_keeps_failing_gives_up() -> None:
    """Boundedness. No model calls to cap, so the bound is on attempts."""
    run_id = two_priority_changes()
    compensation_id = authorise(run_id)

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensations set attempt = max_attempts + 1 where id = %s",
            (compensation_id,),
        )
        conn.commit()

    assert apply(compensation_id) == "blocked"
    comp = comp_row(compensation_id)
    assert comp["stop_reason"] == compensation.STOP_ATTEMPTS
    assert [i["status"] for i in item_rows(compensation_id)] == ["pending", "pending"]


def test_a_live_worker_cannot_be_stolen_from() -> None:
    run_id = two_priority_changes()
    compensation_id = authorise(run_id)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update compensations set status = 'running', lease_owner = 'worker-a',"
            "                         lease_expires_at = now() + interval '60 seconds'"
            " where id = %s",
            (compensation_id,),
        )
        conn.commit()

    with connection() as conn, pytest.raises(compensation.LeaseLost):
        compensation.advance(conn, compensation_id, "worker-b")


# ------------------------------------------------------------------- helper


def _approve_pending(run_id: str) -> None:
    from deskhand.runtime import approvals

    pending = fetch_all(
        "select id, org_id from approvals where run_id = %s and status = 'pending'", (run_id,)
    )
    with connection() as conn, conn.cursor() as cur:
        for row in pending:
            approvals.decide(
                cur,
                approval_id=str(row["id"]),
                org_id=str(row["org_id"]),
                decision="approved",
                decided_by=user_id("owner@northwind.test"),
            )
        conn.commit()
