"""The conversation a worker rebuilds is the conversation the model had.

Every model call in this runtime is preceded by `transcript.rebuild`, which
reads the step log back out of Postgres. So "the history only ever grows" is
not something a variable guarantees; it is a claim about a round trip through
a column, made again on every turn and again by whichever worker resumes.

Current models check that claim. A thinking block's signature binds the exact
prefix that produced it, and on the accounts the API enforces, a request whose
earlier messages differ from that prefix is a 400. The same byte-stability is
what lets a prompt cache hit. So the property is stated strictly here: each
request is the previous request, plus the model's reply *exactly as the
provider returned it*, plus what came after. Key order included, because a
JSON object's key order is part of the text the model wrote.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deskhand.db import connection, one
from deskhand.providers import ModelReply, ScriptedProvider, call, text
from deskhand.runtime import approvals, loop, runs
from tests.test_runtime import drive, expire_lease, org_id, start_run, user_id

pytestmark = pytest.mark.usefixtures("fresh")


def exact(value: Any) -> str:
    """A comparison form that keeps key order. `sort_keys` would hide the bug."""
    return json.dumps(value, ensure_ascii=False)


class Recording(ScriptedProvider):
    """Remembers every request it answered and what it answered with."""

    def __init__(self, script, calls: list, die_on_turn: int | None = None) -> None:
        super().__init__(script=script)
        self.calls = calls
        self.die_on_turn = die_on_turn

    def complete(self, system, messages, tools) -> ModelReply:
        if self.die_on_turn is not None and self.turn_index(messages) >= self.die_on_turn:
            raise RuntimeError("worker died")
        reply = super().complete(system, messages, tools)
        self.calls.append((json.loads(exact(messages)), json.loads(exact(reply.content))))
        return reply


# The argument order a model writes, which is schema order, and not the order
# Postgres's jsonb would hand back: jsonb sorts keys by length, then bytes.
REFUND = call(
    "issue_refund",
    order_reference="NW-1042",
    amount_cents=3800,
    reason="Both bags arrived stale, inside the refund window.",
)

SCRIPT = [
    [call("get_order", reference="NW-1042")],
    [REFUND],
    [call("add_internal_note", reference="NW-1", body="Refunded after approval.")],
    text("Refunded and noted."),
]


def assert_append_only(calls: list) -> None:
    assert len(calls) >= 2, "nothing to compare"
    for k, ((sent, reply), (sent_next, _)) in enumerate(zip(calls, calls[1:], strict=False)):
        assert exact(sent_next[: len(sent)]) == exact(sent), (
            f"request {k + 1} rewrote history the model had already seen in request {k}"
        )
        echoed = sent_next[len(sent)]
        assert echoed["role"] == "assistant"
        assert exact(echoed["content"]) == exact(reply), (
            f"the model said {exact(reply)}\nand was told it said {exact(echoed['content'])}"
        )


def _approve(run_id: str) -> None:
    approval = one("select * from approvals where run_id = %s", (run_id,))
    with connection() as conn, conn.cursor() as cur:
        approvals.decide(
            cur,
            approval_id=str(approval["id"]),
            org_id=org_id(),
            decision="approved",
            decided_by=user_id("owner@northwind.test"),
        )
        conn.commit()


def test_each_request_extends_the_last_one_exactly() -> None:
    run_id = start_run("NW-1")
    calls: list = []

    assert drive(run_id, Recording(SCRIPT, calls)) == "awaiting_approval"
    _approve(run_id)
    assert drive(run_id, Recording(SCRIPT, calls)) == "succeeded"

    assert len(calls) == 4
    assert_append_only(calls)


def test_a_resumed_worker_sends_the_history_the_dead_one_would_have() -> None:
    """The same property across a crash. Worker B has never seen this run; it
    rebuilds from rows, and its first request must continue worker A's last
    one as if nothing had happened."""
    run_id = start_run("NW-1")
    calls: list = []

    assert drive(run_id, Recording(SCRIPT, calls), worker="worker-a") == "awaiting_approval"
    _approve(run_id)
    with pytest.raises(RuntimeError, match="worker died"):
        drive(run_id, Recording(SCRIPT, calls, die_on_turn=3), worker="worker-a")

    expire_lease(run_id)
    with connection() as conn, conn.cursor() as cur:
        claimed = runs.claim_next(cur, "worker-b")
        conn.commit()
    assert claimed is not None and str(claimed["id"]) == run_id
    with connection() as conn:
        assert loop.advance(conn, run_id, "worker-b", Recording(SCRIPT, calls)) == "succeeded"

    assert len(calls) == 4
    assert_append_only(calls)
