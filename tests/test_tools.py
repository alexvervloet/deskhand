"""The tool registry and the exactly-once guarantee.

These tests are about properties, not coverage. The three that matter most:
a tool's risk class cannot be moved at runtime, an approval is bound to the
exact arguments it saw, and invoking the same step twice touches the world
once.
"""

from __future__ import annotations

import dataclasses
import uuid

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from deskhand import tools
from deskhand.config import settings
from deskhand.tools import RiskClass, ToolError, args_hash, requires_approval
from deskhand.tools.invoke import idempotency_key, invoke
from deskhand.tools.reversible import apply_inverse
from tests.conftest import row

pytestmark = pytest.mark.usefixtures("fresh")


@pytest.fixture
def cur():
    """A cursor in a transaction that is rolled back afterwards, so these tests
    can issue real refunds without leaving any behind."""
    # Spelled with the type parameter so the connection really is a
    # Connection[DictRow]; `psycopg.connect(...)` alone resolves to the
    # tuple-row overload and rows come back as tuples to the checker.
    with psycopg.Connection[DictRow].connect(
        settings.database_url, row_factory=dict_row
    ) as conn:
        with conn.cursor() as c:
            yield c
        conn.rollback()


@pytest.fixture
def org(cur) -> str:
    cur.execute("select id from orgs where slug = 'northwind'")
    return str(row(cur)["id"])


def _new_run(cur, org: str) -> str:
    """A minimal run row. The ledger's foreign keys are real, so a tool
    invocation has to belong to a run and a step that actually exist —
    which is also how it works in production."""
    cur.execute("select id from tickets where org_id = %s limit 1", (org,))
    ticket = row(cur)
    cur.execute(
        "insert into runs (org_id, ticket_id, prompt, max_steps, max_tokens,"
        "                  max_spend_micros, max_refund_cents, deadline_at)"
        " values (%s, %s, 'tool test', 24, 400000, 2000000, 100000,"
        "         now() + interval '15 minutes')"
        " returning id",
        (org, ticket["id"]),
    )
    return str(row(cur)["id"])


def _step(cur, run_id: str, seq: int) -> str:
    """Get or create the step row for `seq`. Re-invoking the same seq is what a
    resumed run does, so this must not blow up on the second call."""
    cur.execute(
        "insert into steps (run_id, seq, kind, content) values (%s, %s, 'tool_result', '{}')"
        " on conflict (run_id, seq) do nothing",
        (run_id, seq),
    )
    cur.execute("select id from steps where run_id = %s and seq = %s", (run_id, seq))
    return str(row(cur)["id"])


@pytest.fixture
def run_id(cur, org) -> str:
    return _new_run(cur, org)


def run_tool(cur, org: str, name: str, args: dict, seq: int = 1, run_id: str | None = None):
    run_id = run_id or _new_run(cur, org)
    return invoke(
        cur,
        org_id=org,
        run_id=run_id,
        step_id=_step(cur, run_id, seq),
        seq=seq,
        tool_name=name,
        args=args,
    )


# ----------------------------------------------------------------- registry


def test_every_tool_declares_a_risk_class() -> None:
    assert tools.all_tools()
    for tool in tools.all_tools():
        assert isinstance(tool.risk, RiskClass)


def test_only_irreversible_tools_require_approval() -> None:
    needs = {t.name for t in tools.all_tools() if requires_approval(t.name)}
    declared = {t.name for t in tools.all_tools() if t.risk is RiskClass.IRREVERSIBLE}
    assert needs == declared
    assert needs == {"issue_refund", "send_customer_email", "cancel_order"}


def test_a_tools_risk_class_cannot_be_reassigned() -> None:
    """The frozen dataclass is the mechanism, so assert the mechanism. If this
    ever starts passing, a tool result could talk its way out of the approval
    gate by mutating the registry."""
    refund = tools.get("issue_refund")
    with pytest.raises(dataclasses.FrozenInstanceError):
        refund.risk = RiskClass.READ  # type: ignore[misc]
    assert requires_approval("issue_refund")


def test_unknown_tool_names_are_rejected_not_guessed() -> None:
    with pytest.raises(ToolError):
        tools.get("issue_refund_but_bigger")


def test_api_schemas_are_stable_and_strict() -> None:
    first = tools.api_schemas()
    second = tools.api_schemas()
    # Tools render at the front of the prompt; an unstable order would
    # invalidate the prompt cache on every single call.
    assert first == second
    assert [s["name"] for s in first] == sorted(s["name"] for s in first)
    for s in first:
        assert s["strict"] is True
        assert s["input_schema"]["additionalProperties"] is False
        assert "required" in s["input_schema"]
        assert s["description"].strip()


# --------------------------------------------------------------- args_hash


def test_args_hash_ignores_key_order_but_not_values() -> None:
    a = args_hash("issue_refund", {"order_reference": "NW-1042", "amount_cents": 1900})
    b = args_hash("issue_refund", {"amount_cents": 1900, "order_reference": "NW-1042"})
    assert a == b

    bigger = args_hash("issue_refund", {"order_reference": "NW-1042", "amount_cents": 190000})
    assert bigger != a, "approving $19 must not also approve $1,900"


def test_args_hash_distinguishes_tools() -> None:
    assert args_hash("cancel_order", {"x": 1}) != args_hash("issue_refund", {"x": 1})


# ------------------------------------------------------------- validation


def test_unexpected_arguments_are_an_error(cur, org) -> None:
    out = run_tool(cur, org, "search_kb", {"query": "refund", "limit": 99})
    assert not out.ok
    assert "invalid arguments" in out.result


def test_wrong_types_are_an_error(cur, org) -> None:
    out = run_tool(cur, org, "issue_refund", {
        "order_reference": "NW-1042", "amount_cents": "nineteen", "reason": "test"
    })
    assert not out.ok


# ------------------------------------------------------------- read tools


def test_search_kb_finds_the_policy_a_ticket_needs(cur, org) -> None:
    out = run_tool(cur, org, "search_kb", {"query": "stale coffee refund window"})
    assert out.ok
    assert "Refund policy" in out.result


def test_search_survives_words_the_policy_does_not_use(cur, org) -> None:
    """Regression. Postgres' websearch/plainto tsquery helpers AND every term,
    so one unmatched word turned a policy lookup into "no such policy" — which
    an agent reads as permission to proceed without one. Policy lookups must
    degrade, never fail open."""
    out = run_tool(
        cur, org, "search_kb", {"query": "refund window for stale beans zzzqqq"}
    )
    assert out.ok
    assert "Refund policy" in out.result


def test_read_tools_cannot_reach_another_merchant(cur, org) -> None:
    out = run_tool(cur, org, "get_order", {"reference": "LU-2201"})
    assert not out.ok
    assert "no order" in out.result


def test_get_order_reports_what_is_left_to_refund(cur, org) -> None:
    out = run_tool(cur, org, "get_order", {"reference": "NW-1042"})
    assert out.ok
    assert "48.00 USD" in out.result
    assert "No refunds have been issued" in out.result


# -------------------------------------------------------- reversible tools


def test_reversible_tools_record_a_usable_inverse(cur, org) -> None:
    before = run_tool(cur, org, "get_ticket", {"reference": "NW-2"})
    assert "priority=normal" in before.result

    changed = run_tool(cur, org, "set_priority", {"reference": "NW-2", "priority": "urgent"})
    assert changed.ok
    assert changed.inverse is not None, "a reversible tool must record its inverse"
    assert changed.inverse == {
        "op": "set_priority",
        "ticket_id": changed.inverse["ticket_id"],
        "priority": "normal",
    }

    after = run_tool(cur, org, "get_ticket", {"reference": "NW-2"}, seq=2)
    assert "priority=urgent" in after.result

    ctx = tools.ToolContext(org_id=org, run_id="r", step_id="s", cursor=cur)
    apply_inverse(ctx, changed.inverse)

    reverted = run_tool(cur, org, "get_ticket", {"reference": "NW-2"}, seq=3)
    assert "priority=normal" in reverted.result


def test_tagging_keeps_existing_tags(cur, org) -> None:
    first = run_tool(cur, org, "tag_ticket", {"reference": "NW-2", "tags": ["shipping"]})
    assert first.ok
    second = run_tool(cur, org, "tag_ticket", {"reference": "NW-2", "tags": ["late"]}, seq=2)
    assert "shipping" in second.result and "late" in second.result


def test_internal_notes_are_not_customer_visible(cur, org) -> None:
    out = run_tool(cur, org, "add_internal_note", {
        "reference": "NW-2", "body": "Checked the carrier; parcel is in transit."
    })
    assert out.ok
    cur.execute(
        "select is_internal from ticket_messages where body like 'Checked the carrier%'"
    )
    assert row(cur)["is_internal"] is True


# ------------------------------------------------------ irreversible tools


def test_a_refund_cannot_exceed_what_remains(cur, org) -> None:
    """The approval gate stops the agent acting alone. It does not stop a human
    approving arithmetic that does not work, so the constraint lives here too."""
    out = run_tool(cur, org, "issue_refund", {
        "order_reference": "NW-1042", "amount_cents": 999_999, "reason": "test"
    })
    assert not out.ok
    assert "already refunded" in out.result or "leaving" in out.result


def test_refunds_accumulate_against_the_order(cur, org) -> None:
    first = run_tool(cur, org, "issue_refund", {
        "order_reference": "NW-1042", "amount_cents": 1900, "reason": "one stale bag"
    })
    assert first.ok

    view = run_tool(cur, org, "get_order", {"reference": "NW-1042"}, seq=2)
    assert "Already refunded: 19.00 USD" in view.result
    assert "Refundable remaining: 29.00 USD" in view.result

    too_much = run_tool(cur, org, "issue_refund", {
        "order_reference": "NW-1042", "amount_cents": 3000, "reason": "the rest"
    }, seq=3)
    assert not too_much.ok


def test_a_run_with_no_ceiling_on_its_row_refunds_nothing(cur, org) -> None:
    """`max_refund_cents` defaults to 0, and 0 is no payout authority.

    A run row inserted by a code path that has never heard of the column gets a
    ceiling of zero rather than an assumed one. This is the direction a
    forgotten field should fail in when the field is a limit on money.
    """
    run_id = _new_run(cur, org)
    cur.execute("update runs set max_refund_cents = 0 where id = %s", (run_id,))

    out = run_tool(cur, org, "issue_refund", {
        "order_reference": "NW-1042", "amount_cents": 100, "reason": "a token amount"
    }, run_id=run_id)

    assert not out.ok
    assert "may refund" in out.result
    cur.execute("select count(*) as n from refunds")
    assert row(cur)["n"] == 0


def test_a_shipped_order_cannot_be_cancelled(cur, org) -> None:
    out = run_tool(cur, org, "cancel_order", {
        "order_reference": "NW-1042", "reason": "customer changed their mind"
    })
    assert not out.ok
    assert "delivered" in out.result


def test_email_lands_on_the_thread_as_well_as_the_outbox(cur, org) -> None:
    out = run_tool(cur, org, "send_customer_email", {
        "reference": "NW-2",
        "subject": "Your order is on its way",
        "body": "Thanks for waiting — NW-1077 shipped and should arrive shortly.",
    })
    assert out.ok
    cur.execute("select count(*) as n from customer_emails")
    assert row(cur)["n"] == 1
    cur.execute(
        "select count(*) as n from ticket_messages"
        " where author_kind = 'agent' and is_internal = false"
    )
    assert row(cur)["n"] == 1


# ------------------------------------------------------------ exactly once


def test_the_same_step_executes_once_however_often_it_is_replayed(cur, org, run_id) -> None:
    """This is invariant 1. A worker that dies after refunding but before
    recording progress resumes onto the same step number, and must not pay
    the customer twice."""
    args = {"order_reference": "NW-1042", "amount_cents": 1900, "reason": "stale beans"}

    first = run_tool(cur, org, "issue_refund", args, seq=4, run_id=run_id)
    assert first.ok and not first.replayed

    for _ in range(3):
        again = run_tool(cur, org, "issue_refund", args, seq=4, run_id=run_id)
        assert again.replayed, "a replayed step must not re-execute"
        assert again.result == first.result

    cur.execute("select count(*) as n from refunds")
    assert row(cur)["n"] == 1, "the customer was refunded more than once"


def test_a_different_step_of_the_same_run_is_a_different_key() -> None:
    run_id = str(uuid.uuid4())
    assert idempotency_key(run_id, 4) != idempotency_key(run_id, 5)


def test_a_failed_call_is_remembered_as_failed(cur, org, run_id) -> None:
    args = {"order_reference": "NOPE-1", "amount_cents": 100, "reason": "test"}

    first = run_tool(cur, org, "issue_refund", args, seq=1, run_id=run_id)
    assert not first.ok and not first.replayed

    again = run_tool(cur, org, "issue_refund", args, seq=1, run_id=run_id)
    assert again.replayed and not again.ok
    assert again.result == first.result


def test_a_failing_tool_leaves_the_transaction_usable(cur, org) -> None:
    """Without a savepoint around the handler, one bad SQL statement would
    poison the transaction we still need in order to record the failure — and
    the run would die instead of the model getting a chance to recover."""
    failed = run_tool(cur, org, "issue_refund", {
        "order_reference": "NW-1042", "amount_cents": 999_999, "reason": "too much"
    })
    assert not failed.ok

    recovered = run_tool(cur, org, "get_order", {"reference": "NW-1042"}, seq=2)
    assert recovered.ok, "the transaction should still be usable after a tool failure"
