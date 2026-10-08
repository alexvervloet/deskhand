"""Per-call rules: they can make a call harder to run and never easier.

The first test is the one the module exists for. It's exhaustive rather than
sampled, because the space is small enough to enumerate: every registered
tool, against a rule returning every possible verdict.
"""

from __future__ import annotations

from typing import Any

import pytest

from deskhand.db import connection, fetch_all, fetch_one, one
from deskhand.providers import ScriptedProvider, call, text
from deskhand.runtime import approvals, policy
from deskhand.tools import all_tools, faults
from tests.test_runtime import _approve_everything, drive, org_id, start_run, user_id

pytestmark = pytest.mark.usefixtures("fresh")


def _says(verdict: policy.Verdict) -> policy.Rule:
    def rule(*_: Any) -> policy.Ruling:
        return policy.Ruling(verdict, "test-rule", f"always {verdict.name}")

    return rule


@pytest.mark.parametrize("verdict", list(policy.Verdict), ids=lambda v: v.name)
@pytest.mark.parametrize("tool", [t.name for t in all_tools()])
def test_no_rule_can_lower_what_the_registry_requires(tool: str, verdict: policy.Verdict) -> None:
    run_id = start_run("NW-1")
    with connection() as conn, conn.cursor() as cur:
        ruling = policy.evaluate(cur, run_id, tool, {}, "toolu_x", rules=(_says(verdict),))
    floor = policy.floor(tool).verdict
    assert ruling.verdict == max(floor, verdict)
    assert ruling.verdict >= floor, f"a rule saying {verdict.name} relaxed {tool}"


# ------------------------------------------------- after text addressed to it

TRIAGE = [
    [call("get_ticket", reference="NW-4")],
    [call("set_ticket_status", reference="NW-4", status="resolved")],
    text("Resolved."),
]


def test_a_write_after_reading_an_injected_instruction_waits_for_a_person() -> None:
    run_id = start_run("NW-4")
    assert drive(run_id, ScriptedProvider(script=TRIAGE)) == "awaiting_approval"

    approval = one("select * from approvals where run_id = %s", (run_id,))
    assert approval["tool_name"] == "set_ticket_status"
    assert "addressed to the agent" in approval["asked_because"]
    status = one("select status::text from tickets where reference = 'NW-4'")["status"]
    assert status == "open", "the write ran before anyone said yes"

    assert _approve_everything(run_id) == 1
    assert drive(run_id, ScriptedProvider(script=TRIAGE)) == "succeeded"
    status = one("select status::text from tickets where reference = 'NW-4'")["status"]
    assert status == "resolved"


def test_the_same_write_on_a_clean_ticket_runs_unattended() -> None:
    """The rule has to be quiet when there's nothing to react to, or people
    learn to approve its requests without reading them."""
    run_id = start_run("NW-2")
    script = [
        [call("get_ticket", reference="NW-2")],
        [call("set_ticket_status", reference="NW-2", status="pending")],
        text("Done."),
    ]
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"
    assert fetch_all("select id from approvals where run_id = %s", (run_id,)) == []


def test_reads_stay_free_after_an_injection() -> None:
    run_id = start_run("NW-4")
    script = [
        [call("get_ticket", reference="NW-4")],
        [call("get_order", reference="NW-1101")],
        text("Read it."),
    ]
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"


def test_an_injection_through_a_tool_result_triggers_it_too() -> None:
    run_id = start_run("NW-2")
    script = [
        [call("get_order", reference="NW-1077")],
        [call("tag_ticket", reference="NW-2", tags=["vip"])],
        text("Tagged."),
    ]
    with faults.injecting(faults.Fault(tool="get_order", kind="injection")):
        assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"


def test_a_false_positive_costs_a_click_and_nothing_else() -> None:
    """ "System: macOS" at the start of a line matches. That's fine, and it's
    the point: a rule that can only tighten can afford to be crude."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into ticket_messages (ticket_id, author_kind, body)"
            " select id, 'customer', %s from tickets where reference = 'NW-2'",
            ("System: macOS 15, if that matters.",),
        )
        conn.commit()
    run_id = start_run("NW-2")
    script = [
        [call("get_ticket", reference="NW-2")],
        [call("set_ticket_status", reference="NW-2", status="pending")],
        text("Done."),
    ]
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"


# ------------------------------------------------------- already declined

REFUND = call("issue_refund", order_reference="NW-1042", amount_cents=3800, reason="Stale.")


def _deny(run_id: str) -> None:
    approval = one("select id from approvals where run_id = %s and status = 'pending'", (run_id,))
    with connection() as conn, conn.cursor() as cur:
        approvals.decide(
            cur,
            approval_id=str(approval["id"]),
            org_id=org_id(),
            decision="denied",
            decided_by=user_id("owner@northwind.test"),
            reason="Only one bag was stale.",
        )
        conn.commit()


def test_asking_again_for_what_a_person_declined_is_refused_without_asking() -> None:
    run_id = start_run("NW-1")
    script = [[REFUND], [dict(REFUND)], text("Gave up.")]
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"
    _deny(run_id)
    assert drive(run_id, ScriptedProvider(script=script)) == "succeeded"

    assert len(fetch_all("select id from approvals where run_id = %s", (run_id,))) == 1
    refused = fetch_one(
        "select content->>'result' as result from steps where run_id = %s"
        " and kind = 'tool_result' order by seq desc limit 1",
        (run_id,),
    )
    assert refused is not None and refused["result"].startswith("refused without asking")
    assert fetch_all("select id from refunds") == []


def test_a_changed_proposal_after_a_denial_still_goes_to_a_person() -> None:
    run_id = start_run("NW-1")
    smaller = call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="One.")
    script = [[REFUND], [smaller], text("Requested.")]
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"
    _deny(run_id)
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"
    assert len(fetch_all("select id from approvals where run_id = %s", (run_id,))) == 2


# ------------------------------------------------------------- mixed turns


def test_a_turn_with_an_unknown_tool_and_an_irreversible_one_still_suspends() -> None:
    """The suspension used to ask the registry about every call in the turn,
    and the registry raises for a name the model made up."""
    run_id = start_run("NW-1")
    script = [[call("escalate_to_finance", reference="NW-1"), REFUND], text("Done.")]
    assert drive(run_id, ScriptedProvider(script=script)) == "awaiting_approval"
