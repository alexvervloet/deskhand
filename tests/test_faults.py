"""The fault-injection layer itself.

A testing seam that can be reached from production, or that quietly widens the
trust boundary, is worse than not having one. These tests are mostly about
what faults *cannot* do.
"""

from __future__ import annotations

import psycopg
import pytest

from deskhand.config import settings
from deskhand.tools import RiskClass, faults, get, requires_approval
from deskhand.tools.invoke import invoke, sanitise

pytestmark = pytest.mark.usefixtures("fresh")


@pytest.fixture
def cur():
    with psycopg.connect(settings.database_url) as conn:
        conn.row_factory = psycopg.rows.dict_row
        with conn.cursor() as c:
            yield c
        conn.rollback()


@pytest.fixture
def run(cur) -> tuple[str, str, str]:
    cur.execute("select id from orgs where slug = 'northwind'")
    org = str(cur.fetchone()["id"])
    cur.execute("select id from tickets where reference = 'NW-1'")
    ticket = str(cur.fetchone()["id"])
    cur.execute(
        "insert into runs (org_id, ticket_id, prompt, max_steps, max_tokens,"
        "                  max_spend_micros, deadline_at)"
        " values (%s, %s, 'fault test', 24, 400000, 2000000, now() + interval '15 min')"
        " returning id",
        (org, ticket),
    )
    run_id = str(cur.fetchone()["id"])
    cur.execute(
        "insert into steps (run_id, seq, kind, content) values (%s, 1, 'tool_result', '{}')"
        " returning id",
        (run_id,),
    )
    return org, run_id, str(cur.fetchone()["id"])


def call_tool(cur, run, name: str, args: dict, seq: int = 1):
    org_id, run_id, step_id = run
    return invoke(
        cur, org_id=org_id, run_id=run_id, step_id=step_id, seq=seq,
        tool_name=name, args=args,
    )


# ------------------------------------------------------------------- safety


def test_faults_are_off_unless_a_test_turns_them_on() -> None:
    assert not faults.active()


def test_faults_are_torn_down_when_the_block_exits() -> None:
    with faults.injecting(faults.Fault(tool="get_ticket", kind="error")):
        assert faults.active()
    assert not faults.active()


def test_a_fault_cannot_change_a_risk_class() -> None:
    """The seam must not be a way around the approval gate."""
    with faults.injecting(
        faults.Fault(tool="issue_refund", kind="injection"),
        faults.Fault(tool="issue_refund", kind="garbage"),
        faults.Fault(tool="issue_refund", kind="error"),
    ):
        assert get("issue_refund").risk is RiskClass.IRREVERSIBLE
        assert requires_approval("issue_refund")


def test_the_environment_cannot_switch_faults_on(monkeypatch) -> None:
    """A deployment that can be told to corrupt its own tool results by setting
    a variable is a worse deployment than one that cannot. Asserted by trying
    every name someone might plausibly have reached for."""
    import importlib

    for name in (
        "DESKHAND_FAULTS",
        "DESKHAND_FAULT",
        "FAULTS",
        "FAULT_INJECTION",
        "DESKHAND_CHAOS",
        "DEBUG",
    ):
        monkeypatch.setenv(name, "issue_refund:injection")

    importlib.reload(faults)
    assert not faults.active(), "an environment variable installed a fault"


# ------------------------------------------------------------------- kinds


def test_an_error_fault_is_a_failure_the_model_can_read(cur, run) -> None:
    with faults.injecting(
        faults.Fault(tool="get_ticket", kind="error", detail="ticket store down")
    ):
        result = call_tool(cur, run, "get_ticket", {"reference": "NW-1"})
    assert not result.ok
    assert "ticket store down" in result.result


def test_a_crash_fault_is_not_the_models_business(cur, run) -> None:
    """An unexpected exception propagates, leaving no ledger row, so the step
    is retried intact rather than being recorded as a deterministic failure."""
    with (
        faults.injecting(faults.Fault(tool="get_ticket", kind="crash")),
        pytest.raises(RuntimeError),
    ):
        call_tool(cur, run, "get_ticket", {"reference": "NW-1"})

    cur.execute("select count(*) as n from tool_invocations")
    assert cur.fetchone()["n"] == 0


def test_a_crash_rolls_back_what_the_handler_had_written(cur, run) -> None:
    with (
        faults.injecting(faults.Fault(tool="issue_refund", kind="crash")),
        pytest.raises(RuntimeError),
    ):
        call_tool(cur, run, "issue_refund", {
            "order_reference": "NW-1042", "amount_cents": 1900, "reason": "test"
        })
    cur.execute("select count(*) as n from refunds")
    assert cur.fetchone()["n"] == 0, "a crashed handler left a refund behind"


def test_times_bounds_how_often_a_fault_fires(cur, run) -> None:
    with faults.injecting(faults.Fault(tool="get_ticket", kind="error", times=1)):
        first = call_tool(cur, run, "get_ticket", {"reference": "NW-1"}, seq=1)
        second = call_tool(cur, run, "get_ticket", {"reference": "NW-1"}, seq=2)
    assert not first.ok
    assert second.ok, "the fault fired more often than it was told to"


def test_an_injection_fault_appends_a_hostile_payload(cur, run) -> None:
    with faults.injecting(faults.Fault(tool="get_ticket", kind="injection")):
        result = call_tool(cur, run, "get_ticket", {"reference": "NW-1"})
    assert result.ok
    assert "SYSTEM OVERRIDE" in result.result
    # The real content survives: the attack is appended to a genuine result,
    # which is what makes it worth defending against.
    assert "Beans arrived stale" in result.result


def test_a_garbage_fault_does_not_take_the_run_down(cur, run) -> None:
    """Regression. Postgres text cannot hold a NUL byte, so a tool returning
    one raised a DataError from the ledger write — after the side effect had
    already happened."""
    with faults.injecting(faults.Fault(tool="get_ticket", kind="garbage")):
        result = call_tool(cur, run, "get_ticket", {"reference": "NW-1"})
    assert result.ok
    assert "\x00" not in result.result


# ---------------------------------------------------------------- sanitise


def test_nul_bytes_are_replaced_not_dropped() -> None:
    assert sanitise("a\x00b") == "a�b"
    assert "\x00" not in sanitise("\x00" * 10)


def test_sanitise_leaves_ordinary_text_alone() -> None:
    text = "Refunded 19.00 USD — see policy §4, naïve façade, 日本語"
    assert sanitise(text) == text
