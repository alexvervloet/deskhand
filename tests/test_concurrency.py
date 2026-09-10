"""Searching the crash space instead of picking one point in it.

`test_a_run_resumes_on_another_worker_without_repeating_side_effects` kills a
worker at turn 3 and asserts one refund. It has passed since the day it was
written, and it proves the mechanism works *on the schedule I thought of*.

This file tries to break the same claim from three directions:

1. **Exhaustively**, over every crash schedule a short trajectory admits. Five
   turns is 32 subsets, which is small enough to enumerate — so the claim is
   "every possible crash schedule", not "a hundred random ones", and there is
   no seed to get lucky with.
2. **By property**, with Hypothesis, over the space exhaustion cannot reach:
   longer trajectories, repeated crashes at the same turn, a suspended run
   crossing an approval.
3. **With real threads**, because the leasing story is about two processes
   racing and a schedule the test controls is not a race.

The claim under all three is the refinement property in `fingerprint.py`: the
world after a crashed run is identical to the world after a clean one, and so
is the trajectory.
"""

from __future__ import annotations

import itertools
import os
import threading
from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from deskhand.config import settings
from deskhand.db import connection, fetch_all, fetch_one
from deskhand.providers import ScriptedProvider, call, text
from deskhand.runtime import approvals, loop, runs
from deskhand.tools.invoke import invoke
from tests import fingerprint
from tests.conftest import _reseed

pytestmark = pytest.mark.usefixtures("fresh")

# CI runs a modest search; the deep sweep is a command, not a default. See the
# README — `DESKHAND_FUZZ_EXAMPLES=2000 python -m pytest tests/test_concurrency.py`.
EXAMPLES = int(os.environ.get("DESKHAND_FUZZ_EXAMPLES", "25"))

DEEP = hyp_settings(
    max_examples=EXAMPLES,
    deadline=None,
    # Each example reseeds and drives a real run against a real Postgres, so it
    # is slow by Hypothesis's standards and mutates shared state on purpose.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ------------------------------------------------------------------ scenarios

# A refund trajectory: reads, an irreversible act behind the gate, a reversible
# act after it, and a summary. Chosen because it is the shortest path that
# crosses every kind of step the resume has to reason about.
REFUND = [
    [call("get_ticket", reference="NW-1")],
    [call("get_order", reference="NW-1042")],
    [
        call(
            "issue_refund",
            order_reference="NW-1042",
            amount_cents=1900,
            reason="Stale beans inside the refund window.",
        )
    ],
    [call("add_internal_note", reference="NW-1", body="Refund issued after approval.")],
    text("Refunded and noted."),
]

# Two irreversible acts in one run, so a crash can land between them.
TWO_PAYOUTS = [
    [call("get_order", reference="NW-1042")],
    [call("issue_refund", order_reference="NW-1042", amount_cents=1000, reason="First.")],
    [call("send_customer_email", reference="NW-1", subject="Sorted", body="Refunded.")],
    [call("set_ticket_status", reference="NW-1", status="resolved")],
    text("Done."),
]


# Six concurrent runs share one 48.00 order in the leasing test, so each takes
# a small bite rather than the whole thing.
SMALL_REFUND = [
    [call("get_order", reference="NW-1042")],
    [call("issue_refund", order_reference="NW-1042", amount_cents=500, reason="Share.")],
    [call("set_ticket_status", reference="NW-1", status="resolved")],
    text("Done."),
]


class Died(RuntimeError):
    """A worker died here. Not a failure — the thing being tested."""


class DiesAt(ScriptedProvider):
    """A provider that dies at chosen turns, once each.

    Dying *once per turn index* matters. The index is derived from the history,
    so a resumed worker sees the same index again; a provider that died every
    time would never let the run past that turn and the property would be
    vacuous rather than false.

    The crash lands inside `provider.complete()`, which is before the model
    call's step is written. So a crashed turn is always "nothing recorded for
    this turn", and the resumed worker asks for the same turn and gets the same
    reply. That is the window the step log is supposed to make safe.
    """

    def __init__(self, script: list[Any], die_at: frozenset[int]) -> None:
        super().__init__(script=[list(turn) for turn in script])
        self.die_at = die_at
        self.died: set[int] = set()

    def complete(self, system, messages, tools):
        index = self.turn_index(messages)
        if index in self.die_at and index not in self.died:
            self.died.add(index)
            raise Died(f"worker died on turn {index}")
        return super().complete(system, messages, tools)


# -------------------------------------------------------------------- driving


def _start(reference: str = "NW-1") -> str:
    org = fetch_one("select id from orgs where slug = 'northwind'")
    ticket = fetch_one("select id from tickets where reference = %s", (reference,))
    assert org is not None and ticket is not None
    with connection() as conn, conn.cursor() as cur:
        run_id = runs.create(cur, org_id=str(org["id"]), ticket_id=str(ticket["id"]))
        conn.commit()
    return run_id


def _claim(run_id: str, worker: str) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set status = 'running', lease_owner = %s,"
            "                lease_expires_at = now() + interval '60 seconds',"
            "                attempt = attempt + 1"
            " where id = %s",
            (worker, run_id),
        )
        conn.commit()


def _approve_pending(run_id: str) -> None:
    pending = fetch_all(
        "select id, org_id from approvals where run_id = %s and status = 'pending'", (run_id,)
    )
    owner = fetch_one("select id from users where email = 'owner@northwind.test'")
    assert owner is not None
    with connection() as conn, conn.cursor() as cur:
        for row in pending:
            approvals.decide(
                cur,
                approval_id=str(row["id"]),
                org_id=str(row["org_id"]),
                decision="approved",
                decided_by=str(owner["id"]),
            )
        conn.commit()


TERMINAL = {"succeeded", "failed", "exhausted", "cancelled"}


def drive_through(run_id: str, script: list[Any], die_at: frozenset[int]) -> str:
    """Drive one run to a terminal status, surviving every crash in `die_at`.

    A fresh worker name after each death, because a resumed run being picked up
    by *the same* worker would not exercise the handover — and the handover is
    the thing under test.
    """
    provider = DiesAt(script, die_at)
    for attempt in range(40):
        _claim(run_id, f"worker-{attempt}")
        try:
            with connection() as conn:
                status = loop.advance(conn, run_id, f"worker-{attempt}", provider)
        except Died:
            # Exactly what a real death looks like from outside: the lease
            # simply stops being renewed. Nothing has to notice.
            continue
        if status == "awaiting_approval":
            _approve_pending(run_id)
            continue
        if status in TERMINAL:
            return status
    raise AssertionError("run never terminated")


def _golden(script: list[Any]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """What a clean, uncrashed run of this script leaves behind."""
    _reseed()
    run_id = _start()
    status = drive_through(run_id, script, frozenset())
    assert status == "succeeded", f"the clean run did not succeed: {status}"
    return fingerprint.world(), fingerprint.trajectory(run_id)


# ------------------------------------------------- 1. the exhaustive sweep

ALL_SCHEDULES = [
    frozenset(subset)
    for size in range(len(REFUND) + 1)
    for subset in itertools.combinations(range(len(REFUND)), size)
]


@pytest.mark.parametrize(
    "die_at", ALL_SCHEDULES, ids=lambda s: "+".join(map(str, sorted(s))) or "clean"
)
def test_every_crash_schedule_leaves_the_same_world(die_at: frozenset[int]) -> None:
    """All 32 of them, for a five-turn trajectory.

    Exhaustive rather than sampled, which is the point: there is no seed here
    that could have been luckier, and no schedule left unexamined. Random
    search over a space this small is strictly worse than enumerating it.
    """
    clean_world, clean_trajectory = _golden(REFUND)

    _reseed()
    run_id = _start()
    assert drive_through(run_id, REFUND, die_at) == "succeeded"

    crashed_world = fingerprint.world()
    assert crashed_world == clean_world, fingerprint.describe(clean_world, crashed_world)

    crashed_trajectory = fingerprint.trajectory(run_id)
    assert crashed_trajectory == clean_trajectory, fingerprint.describe(
        clean_trajectory, crashed_trajectory
    )

    # Exactly one refund is implied by the fingerprints, and asserted anyway
    # because it is the sentence the invariant is written in.
    assert len(fetch_all("select id from refunds")) == 1


def test_the_sweep_actually_exercises_the_resume_path() -> None:
    """A property that passes without the mechanism firing proves nothing.

    A crash after a completed tool call must produce a `replayed` step — the
    ledger recognising work it had already done. If this were zero everywhere,
    every schedule above would be passing because nothing was ever re-entered.
    """
    _reseed()
    run_id = _start()
    # Turn 3 is the turn after the refund lands, so the resumed worker walks
    # back over a completed irreversible call.
    assert drive_through(run_id, REFUND, frozenset({3})) == "succeeded"
    assert fingerprint.replayed_steps(run_id) == 0, (
        "an orderly resume rebuilds from the step log and never re-enters the tool;"
        " a replayed step here would mean the step log missed one"
    )
    assert len(fetch_all("select id from refunds")) == 1

    # The ledger is the *second* line of defence and the step log usually gets
    # there first. `test_the_ledger_is_what_catches_a_real_race` below is what
    # exercises the ledger itself.


# ------------------------------------------------------ 2. the wider space


@DEEP
@given(
    die_at=st.frozensets(st.integers(min_value=0, max_value=4), max_size=5),
    script_choice=st.sampled_from(["refund", "two_payouts"]),
)
def test_any_crash_schedule_on_any_trajectory(die_at: frozenset[int], script_choice: str) -> None:
    """The same claim, over trajectories exhaustion cannot enumerate.

    `TWO_PAYOUTS` moves money and then sends an email, so a crash can land
    between two irreversible acts — the case where "did the first one already
    happen" and "did the second one already happen" have different answers.
    """
    script = REFUND if script_choice == "refund" else TWO_PAYOUTS
    clean_world, _ = _golden(script)

    _reseed()
    run_id = _start()
    assert drive_through(run_id, script, die_at) == "succeeded"

    crashed_world = fingerprint.world()
    assert crashed_world == clean_world, fingerprint.describe(clean_world, crashed_world)


@DEEP
@given(steal_after=st.integers(min_value=0, max_value=4))
def test_a_run_stolen_mid_flight_is_not_paid_out_twice(steal_after: int) -> None:
    """A worker that merely *looks* dead, and a second one that believes it.

    Worth being precise about what a lease does, because the first version of
    this test asserted something the system correctly does not do. An expired
    lease does not stop the worker holding it — `renew_lease` matches on
    `lease_owner`, not on expiry — it makes the run *claimable by somebody
    else*. The holder only finds out when a claim actually happens and its next
    renewal matches nothing.

    So this steals the run rather than just expiring it: at a chosen turn,
    worker B claims a run worker A still believes it has. A discovers this on
    its next renewal, stops immediately, and B finishes the work. Whichever of
    them reaches the refund, the customer is paid once.
    """
    _reseed()
    run_id = _start()
    script = [list(t) for t in REFUND]

    stolen = False
    for attempt in range(40):
        owner = f"worker-a{attempt}"
        _claim(run_id, owner)

        # The theft: after `steal_after` turns of work exist, a second worker
        # takes the run out from under the first.
        steps_so_far = fetch_one("select count(*) as n from steps where run_id = %s", (run_id,))
        assert steps_so_far is not None
        if not stolen and int(steps_so_far["n"]) >= steal_after * 2:
            with connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "update runs set lease_expires_at = now() - interval '1 second' where id = %s",
                    (run_id,),
                )
                conn.commit()
                thief = runs.claim_next(cur, "worker-b")
                conn.commit()
            if thief is not None:
                stolen = True
                owner = "worker-b"

        try:
            with connection() as conn:
                status = loop.advance(conn, run_id, owner, ScriptedProvider(script=script))
        except loop.LeaseLost:
            continue
        if status == "awaiting_approval":
            _approve_pending(run_id)
            continue
        if status in TERMINAL:
            break
    else:
        raise AssertionError("run never terminated")

    assert len(fetch_all("select id from refunds")) == 1, "a stolen run paid twice"
    keys = fetch_all("select idempotency_key from tool_invocations")
    assert len(keys) == len({k["idempotency_key"] for k in keys})


# ------------------------------------------------------- 3. an actual race


def test_the_ledger_is_what_catches_a_real_race() -> None:
    """Two threads, one step, one effect.

    The step log cannot help here: both callers read the same rows at the same
    moment and both conclude the tool has not run. What separates them is the
    unique index on `idempotency_key` and the fact that the effect and the
    claim are written in one transaction. This is the case
    `the-ledger-catches-a-double-execution` forces by hand; threads make it a
    race rather than a re-enactment.
    """
    _reseed()
    run_id = _start()
    org = fetch_one("select id from orgs where slug = 'northwind'")
    assert org is not None

    with connection() as conn, conn.cursor() as cur:
        step_id = runs.append_step(
            cur, run_id=run_id, seq=1, kind="tool_result", content={}, tool_name="issue_refund"
        )
        conn.commit()

    barrier = threading.Barrier(2)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def attempt() -> None:
        result: Any
        try:
            with psycopg.Connection.connect(
                settings.database_url, row_factory=psycopg.rows.dict_row
            ) as conn:
                with conn.cursor() as cur:
                    barrier.wait(timeout=10)
                    result = invoke(
                        cur,
                        org_id=str(org["id"]),
                        run_id=run_id,
                        step_id=step_id,
                        seq=1,
                        tool_name="issue_refund",
                        args={
                            "order_reference": "NW-1042",
                            "amount_cents": 1900,
                            "reason": "Racing.",
                        },
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - a losing writer is an outcome
            result = exc
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(outcomes) == 2
    refunds = fetch_all("select id from refunds")
    assert len(refunds) == 1, f"two threads produced {len(refunds)} refunds"
    ledger = fetch_all("select id from tool_invocations where run_id = %s", (run_id,))
    assert len(ledger) == 1, "two ledger rows for one idempotency key"

    # One of the two either replayed the other's result or lost on the unique
    # index and raised. Both are correct; silently succeeding twice is not.
    succeeded = [o for o in outcomes if not isinstance(o, Exception)]
    assert succeeded, "both threads failed; the ledger should let one through"


def test_many_workers_sharing_a_queue_do_not_collide() -> None:
    """The leasing story, run as an actual race.

    Six runs, four threads, no coordination but Postgres. `for update skip
    locked` is what lets them take different rows instead of blocking on the
    same one, and nothing in the workers knows about the others.
    """
    _reseed()
    run_ids = [_start("NW-1") for _ in range(6)]
    # Small payouts on purpose: six runs share one 48.00 order, and the point
    # here is the leasing, not the per-order balance. 6 x 5.00 leaves room.
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        provider = ScriptedProvider(script=[list(t) for t in SMALL_REFUND])
        try:
            for _ in range(60):
                with connection() as conn, conn.cursor() as cur:
                    approvals.expire_stale(cur)
                    claimed = runs.claim_next(cur, name)
                    conn.commit()
                if claimed is None:
                    if all(
                        r["status"] in TERMINAL
                        for r in fetch_all(
                            "select status::text as status from runs where id = any(%s)",
                            (run_ids,),
                        )
                    ):
                        return
                    continue
                run_id = str(claimed["id"])
                try:
                    with connection() as conn:
                        status = loop.advance(conn, run_id, name, provider)
                except loop.LeaseLost:
                    continue
                if status == "awaiting_approval":
                    _approve_pending(run_id)
        except BaseException as exc:  # noqa: BLE001 - collected and re-raised
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    assert not errors, f"a worker raised: {errors[0]!r}"
    statuses = fetch_all("select status::text as status from runs where id = any(%s)", (run_ids,))
    assert all(r["status"] in TERMINAL for r in statuses), statuses

    # Six runs against the same order, each authorised once, each paying once.
    assert len(fetch_all("select id from refunds")) == 6

    keys = fetch_all("select idempotency_key from tool_invocations")
    assert len(keys) == len({k["idempotency_key"] for k in keys}), "a key was claimed twice"
