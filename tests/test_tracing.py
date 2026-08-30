"""The tracer, and the one property it must have.

Observability that can raise turns a successful refund into a failed run, which
is strictly worse than having no observability. So most of this file is about
what `emit()` does when given things it has no business coping with.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from deskhand import tracing


def captured(caplog) -> list[dict]:
    return [json.loads(r.message) for r in caplog.records if r.name == "deskhand.events"]


def test_an_event_is_one_line_of_json(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="deskhand.events"):
        tracing.emit("thing.happened", run_id="abc", n=3)

    events = captured(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "thing.happened"
    assert events[0]["run_id"] == "abc"
    assert events[0]["n"] == 3
    # Parseable as a timestamp, not just present.
    assert datetime.fromisoformat(events[0]["ts"]).tzinfo is not None


def test_awkward_but_common_types_survive(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="deskhand.events"):
        tracing.emit(
            "thing.happened",
            run_id=uuid4(),
            cost=Decimal("1.25"),
            at=datetime.now(UTC),
        )
    assert len(captured(caplog)) == 1


def test_an_unserialisable_value_does_not_raise() -> None:
    """The important one. A tracer is called from inside a transaction that has
    already moved money; it does not get to have opinions."""

    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("no repr for you")

        def __str__(self) -> str:
            raise RuntimeError("no str for you")

    tracing.emit("thing.happened", value=Hostile())  # must not raise


def test_a_broken_logger_does_not_raise(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise OSError("log volume is full")

    monkeypatch.setattr(tracing.log, "info", explode)
    tracing.emit("thing.happened", run_id="abc")  # must not raise


def test_recursion_in_a_value_does_not_raise() -> None:
    loop: dict = {}
    loop["self"] = loop
    tracing.emit("thing.happened", value=loop)  # must not raise


def test_the_helpers_emit_the_fields_worth_alerting_on(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="deskhand.events"):
        tracing.tool_call(
            "run-1",
            4,
            tool="issue_refund",
            risk="irreversible",
            ok=True,
            replayed=True,
            duration_ms=12,
        )
        tracing.run_finished(
            "run-1",
            status="succeeded",
            stop_reason="end_turn",
            steps=14,
            cost_micros=250,
        )

    tool, finished = captured(caplog)
    assert tool["tool"] == "issue_refund"
    assert tool["risk"] == "irreversible"
    # A replayed step means a run was resumed onto work it had already done.
    # Worth knowing the rate of, so it is a first-class field.
    assert tool["replayed"] is True
    assert finished["stop_reason"] == "end_turn"
    assert finished["cost_micros"] == 250


def test_events_carry_no_content(caplog) -> None:
    """Identifiers and numbers only. Ticket bodies and tool results are
    untrusted customer text and already live in the step log; copying them into
    a log stream widens where they have to be protected."""
    with caplog.at_level(logging.INFO, logger="deskhand.events"):
        tracing.tool_call(
            "run-1",
            2,
            tool="get_ticket",
            risk="read",
            ok=True,
            replayed=False,
            duration_ms=3,
        )
    event = captured(caplog)[0]
    assert set(event) == {
        "event",
        "ts",
        "run_id",
        "seq",
        "tool",
        "risk",
        "ok",
        "replayed",
        "duration_ms",
    }


def test_starting_a_run_is_traced(caplog) -> None:
    """`run.started` is the opening line of a run's story in the log stream.

    It was defined and never called for a while, which is the quiet way an event
    stream develops a hole: nothing fails, and the runs simply appear in the log
    already in progress.
    """
    with caplog.at_level(logging.INFO, logger="deskhand.events"):
        tracing.run_started("run-1", org_id="org-1", ticket="NW-1", provider="mock", model="mock")
    event = captured(caplog)[0]
    assert event["event"] == "run.started"
    assert event["ticket"] == "NW-1"
    assert event["org_id"] == "org-1"


def test_an_approval_trace_says_which_attempt_it_belongs_to(caplog) -> None:
    """A run that crashes after acting on a decision traces it again.

    The rows do not duplicate — the transaction takes them with it — but the log
    line is already gone to stdout. The attempt number is what lets a reader
    tell one decision retraced from two decisions made.
    """
    with caplog.at_level(logging.INFO, logger="deskhand.events"):
        tracing.approval_decided(
            "run-1", tool="issue_refund", decision="approved", decided_by="u1", attempt=2
        )
    assert captured(caplog)[0]["attempt"] == 2
