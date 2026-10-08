"""What the approval card says about each argument, checked against the record.

The approval gate stops the model from moving money on its own. It doesn't stop
a person approving a refund whose amount came from a claim only the customer
made, or whose order belongs to somebody else. These tests pin what the card
says in both cases, and that it says nothing comforting about what it didn't
check.
"""

from __future__ import annotations

from typing import Any

import pytest

from deskhand.db import connection, fetch_one
from deskhand.providers import ScriptedProvider, call, text
from deskhand.runtime import approvals
from deskhand.tools import irreversible
from tests.test_runtime import drive, start_run

pytestmark = pytest.mark.usefixtures("fresh")


def refund_basis(amount: int, order: str = "NW-1042", ticket: str = "NW-1") -> dict[str, Any]:
    """The checks for a refund proposed on a run working `ticket`, by argument."""
    run_id = start_run(ticket)
    with connection() as conn, conn.cursor() as cur:
        org = fetch_one("select org_id from runs where id = %s", (run_id,))
        assert org is not None
        checks = approvals.basis(
            cur,
            run_id,
            str(org["org_id"]),
            "issue_refund",
            {"order_reference": order, "amount_cents": amount, "reason": "Stale."},
        )
    return {c["arg"]: c for c in checks}


def test_an_amount_that_is_whole_items_on_the_order_is_supported() -> None:
    checks = refund_basis(3800)
    assert checks["amount_cents"]["status"] == "supported"
    assert checks["amount_cents"]["note"].startswith("2 × ")
    assert "BEAN-ETH-12" in checks["amount_cents"]["note"]


def test_the_fewest_units_are_named_when_several_combinations_fit() -> None:
    assert refund_basis(1900)["amount_cents"]["note"].startswith("1 × ")


def test_an_amount_spanning_two_lines_names_both() -> None:
    note = refund_basis(2900)["amount_cents"]["note"]
    assert "BEAN-ETH-12" in note and "SHIP-STD" in note


def test_an_amount_nothing_on_the_order_adds_up_to_is_flagged() -> None:
    """The data-injection case. "The bags were $22 each" is a false fact, not
    an instruction, so nothing about the fence catches it. A model that
    believes it proposes $44.00, which is inside the remaining balance and
    every ceiling. The handler would pay it."""
    check = refund_basis(4400)["amount_cents"]
    assert check["status"] == "unsupported"
    assert "44.00" in check["note"]
    assert "BEAN-ETH-12" in check["note"], "the card should show what the order actually is"


def test_the_ticket_customers_own_order_is_supported() -> None:
    check = refund_basis(1900)["order_reference"]
    assert check["status"] == "supported"
    assert "Dana Whitfield" in check["note"]


def test_an_order_belonging_to_someone_else_is_flagged() -> None:
    """Read tools refuse to answer about a customer other than the ticket's.
    A refund only has a person between it and the money, and "against order
    NW-1101" doesn't say whose order that is."""
    check = refund_basis(2400, order="NW-1101")["order_reference"]
    assert check["status"] == "unsupported"
    assert "Ben Iyer" in check["note"]


def test_an_order_that_does_not_exist_is_flagged_and_the_amount_left_unchecked() -> None:
    checks = refund_basis(1900, order="NW-9999")
    assert checks["order_reference"]["status"] == "unsupported"
    assert checks["amount_cents"]["status"] == "unchecked"


def test_an_argument_nothing_checks_says_so() -> None:
    """Leaving the reason off the list would read as "checked, nothing to
    say", which is the opposite of true."""
    check = refund_basis(1900)["reason"]
    assert check["status"] == "unchecked"


def test_an_order_too_large_to_reconcile_is_unchecked_not_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(irreversible, "_RECONCILE_LIMIT", 2)
    assert refund_basis(1900)["amount_cents"]["status"] == "unchecked"


def test_the_checks_are_recorded_with_the_approval_request() -> None:
    run_id = start_run("NW-1")
    provider = ScriptedProvider(
        script=[
            [call("get_order", reference="NW-1042")],
            [
                call(
                    "issue_refund", order_reference="NW-1042", amount_cents=4400, reason="$22 each."
                )
            ],
            text("Requested."),
        ]
    )
    assert drive(run_id, provider) == "awaiting_approval"

    row = fetch_one("select basis from approvals where run_id = %s", (run_id,))
    assert row is not None
    assert [c["arg"] for c in row["basis"]] == ["order_reference", "amount_cents", "reason"]
    assert [c["status"] for c in row["basis"]] == ["supported", "unsupported", "unchecked"]
