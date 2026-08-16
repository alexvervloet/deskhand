"""Reading a run back, and replaying it against a change.

The property that matters most here is a negative one: divergence must never
touch the world. It exists to be pointed at runs that already moved real money,
and a tool that re-ran them would be worse than useless.
"""

from __future__ import annotations

import pytest

from deskhand import replay
from deskhand.db import connection, fetch_all, fetch_one
from deskhand.providers import ScriptedProvider, call, text
from deskhand.runtime import approvals, loop, runs, transcript
from deskhand.runtime.loop import SYSTEM_PROMPT

pytestmark = pytest.mark.usefixtures("fresh")

SCRIPT = [
    [call("get_ticket", reference="NW-1")],
    [call("get_order", reference="NW-1042")],
    [call("issue_refund", order_reference="NW-1042", amount_cents=1900,
          reason="Stale beans inside the window.")],
    [call("add_internal_note", reference="NW-1", body="Refunded after approval.")],
    text("Refunded 19.00 against NW-1042."),
]


def provider() -> ScriptedProvider:
    return ScriptedProvider(script=[list(turn) for turn in SCRIPT])


@pytest.fixture
def finished_run() -> str:
    """A completed run that issued a refund with a human's approval."""
    ticket = fetch_one("select id, org_id from tickets where reference = 'NW-1'")
    assert ticket is not None
    with connection() as conn, conn.cursor() as cur:
        run_id = runs.create(cur, org_id=str(ticket["org_id"]), ticket_id=str(ticket["id"]))
        conn.commit()

    def drive() -> None:
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                "update runs set status = 'running', lease_owner = 'test',"
                "  lease_expires_at = now() + interval '60 seconds' where id = %s",
                (run_id,),
            )
            conn.commit()
        with connection() as conn:
            loop.advance(conn, run_id, "test", provider())

    drive()
    approval = fetch_one("select id, org_id from approvals where run_id = %s", (run_id,))
    owner = fetch_one("select id from users where role = 'owner' limit 1")
    with connection() as conn, conn.cursor() as cur:
        approvals.decide(
            cur, approval_id=str(approval["id"]), org_id=str(approval["org_id"]),
            decision="approved", decided_by=str(owner["id"]),
        )
        conn.commit()
    drive()
    return run_id


# ---------------------------------------------------------- reconstruction


def test_the_conversation_before_a_step_is_reconstructible(finished_run) -> None:
    run = fetch_one("select prompt from runs where id = %s", (finished_run,))
    with connection() as conn, conn.cursor() as cur:
        before_first = transcript.rebuild(cur, finished_run, run["prompt"], before_seq=1)
        before_refund = transcript.rebuild(cur, finished_run, run["prompt"], before_seq=5)
        whole = transcript.rebuild(cur, finished_run, run["prompt"])

    # Before anything ran, the model had only the opening prompt.
    assert len(before_first) == 1
    assert before_first[0]["role"] == "user"

    assert 1 < len(before_refund) < len(whole)
    # It had read the ticket and the order by then, and nothing further.
    flat = str(before_refund)
    assert "Beans arrived stale" in flat
    assert "Refunded 19.00" not in flat


def test_reconstruction_is_deterministic(finished_run) -> None:
    """Same rows in, same bytes out — months later, on another machine. That is
    the whole reason a trajectory is auditable rather than merely logged."""
    run = fetch_one("select prompt from runs where id = %s", (finished_run,))
    with connection() as conn, conn.cursor() as cur:
        first = transcript.rebuild(cur, finished_run, run["prompt"])
        second = transcript.rebuild(cur, finished_run, run["prompt"])
    assert first == second


def test_a_run_loads_as_its_decisions(finished_run) -> None:
    _, turns = replay.load(finished_run)
    assert [t.calls[0][1] for t in turns if t.calls] == [
        "get_ticket", "get_order", "issue_refund", "add_internal_note"
    ]
    # The result that followed each call is attached to it, which is what makes
    # replaying observations without re-running tools possible.
    refund_turn = next(t for t in turns if t.calls and t.calls[0][1] == "issue_refund")
    assert "Refunded 19.00 USD" in refund_turn.results[refund_turn.calls[0][0]]


# ---------------------------------------------------------------- divergence


def test_the_same_agent_does_not_diverge_from_itself(finished_run) -> None:
    result = replay.diverge(finished_run, provider(), SYSTEM_PROMPT)
    assert not result.diverged
    assert result.matched_turns == 5


def test_a_different_decision_is_located_exactly(finished_run) -> None:
    cautious = ScriptedProvider(script=[
        [call("get_ticket", reference="NW-1")],
        [call("get_order", reference="NW-1042")],
        [call("set_ticket_status", reference="NW-1", status="escalated")],
        text("Outside my authority."),
    ])
    result = replay.diverge(finished_run, cautious, SYSTEM_PROMPT)

    assert result.diverged
    assert result.matched_turns == 2, "the first two decisions were identical"
    assert result.original[0][0] == "issue_refund"
    assert result.replayed[0][0] == "set_ticket_status"


def test_a_changed_argument_is_a_divergence(finished_run) -> None:
    """Same tool, different amount. This is the case a diff of tool *names*
    would miss, and it is the one that costs money."""
    greedier = ScriptedProvider(script=[
        [call("get_ticket", reference="NW-1")],
        [call("get_order", reference="NW-1042")],
        [call("issue_refund", order_reference="NW-1042", amount_cents=4800,
              reason="Stale beans inside the window.")],
    ])
    result = replay.diverge(finished_run, greedier, SYSTEM_PROMPT)
    assert result.diverged
    assert "4800" in result.replayed[0][1]
    assert "1900" in result.original[0][1]


def test_rewording_is_not_a_divergence(finished_run) -> None:
    """Two runs that make the same calls have made the same decisions, however
    differently they narrate them. A report that fired on prose would be noise."""
    chattier = ScriptedProvider(script=[
        [{"type": "text", "text": "Let me start by reading the ticket."},
         call("get_ticket", reference="NW-1")],
        [{"type": "text", "text": "Now the order."},
         call("get_order", reference="NW-1042")],
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900,
              reason="Stale beans inside the window.")],
        [call("add_internal_note", reference="NW-1", body="A differently worded note.")],
        text("Done, phrased entirely differently."),
    ])
    result = replay.diverge(finished_run, chattier, SYSTEM_PROMPT)
    # The internal note's *argument* differs, so it diverges there — at step 4,
    # not at either of the reworded turns before it.
    assert result.diverged
    assert result.matched_turns == 3
    assert result.original[0][0] == "add_internal_note"


# ------------------------------------------------------------- the safety bit


def test_divergence_never_executes_a_tool(finished_run) -> None:
    """The property that makes this safe to point at production runs."""
    before_refunds = fetch_all("select id from refunds")
    before_notes = fetch_all("select id from ticket_messages")
    before_steps = fetch_all("select id from steps where run_id = %s", (finished_run,))

    replay.diverge(finished_run, provider(), SYSTEM_PROMPT)
    replay.diverge(
        finished_run,
        ScriptedProvider(script=[
            [call("issue_refund", order_reference="NW-1042", amount_cents=2900,
                  reason="a refund the original run never made")],
        ]),
        SYSTEM_PROMPT,
    )

    assert fetch_all("select id from refunds") == before_refunds, "a replay moved money"
    assert fetch_all("select id from ticket_messages") == before_notes
    assert fetch_all("select id from steps where run_id = %s", (finished_run,)) == before_steps


def test_divergence_writes_nothing_to_the_run(finished_run) -> None:
    before = fetch_one("select * from runs where id = %s", (finished_run,))
    replay.diverge(finished_run, provider(), SYSTEM_PROMPT)
    after = fetch_one("select * from runs where id = %s", (finished_run,))
    assert before == after


def test_replayed_observations_are_still_fenced(finished_run) -> None:
    """A replay hands the model recorded tool output. It is the same untrusted
    text it was the first time, so it arrives inside the same fence."""
    seen: list[dict] = []

    class Recording(ScriptedProvider):
        def complete(self, system, messages, tools):
            seen.append({"messages": messages})
            return super().complete(system, messages, tools)

    replay.diverge(finished_run, Recording(script=[list(t) for t in SCRIPT]), SYSTEM_PROMPT)

    token = transcript.fence_token(finished_run)
    results = [
        block
        for call_ in seen
        for message in call_["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert results
    assert all(str(b["content"]).startswith(f"<<<untrusted:{token}>>>") for b in results)
