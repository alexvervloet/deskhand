#!/usr/bin/env python
"""The durability story, told at a readable pace.

    python demo/crash_resume.py

A worker takes a ticket, gets a human to approve a refund, issues it, and then
dies. Another worker picks the run up and finishes it. The customer is refunded
exactly once.

Nothing here is staged: it drives the real loop, the real tools, and the real
lease, and every number printed is read back out of Postgres afterwards.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskhand.db import connection, fetch_all, fetch_one  # noqa: E402
from deskhand.providers import ScriptedProvider, call, text  # noqa: E402
from deskhand.runtime import approvals, loop, runs  # noqa: E402

logging.disable(logging.CRITICAL)

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
AMBER, GREEN, RED, BLUE = "\033[33m", "\033[32m", "\033[31m", "\033[34m"

PACE = 0.9


def say(text_: str = "", pause: float = PACE) -> None:
    print(text_)
    time.sleep(pause)


def step(label: str) -> None:
    say(f"{DIM}$ {label}{RESET}", 0.4)


SCRIPT = [
    [call("get_ticket", reference="NW-1")],
    [call("get_order", reference="NW-1042")],
    [call("search_kb", query="refund policy window delivered")],
    [call("issue_refund", order_reference="NW-1042", amount_cents=1900,
          reason="Stale beans inside the published window.")],
    [call("add_internal_note", reference="NW-1", body="Refund issued after approval.")],
    text("Refunded 19.00 against NW-1042 and noted it on the ticket."),
]


def provider() -> ScriptedProvider:
    return ScriptedProvider(script=[list(turn) for turn in SCRIPT])


class DiesAfterRefunding(ScriptedProvider):
    """Stops answering right after the refund lands — which is all that dying
    looks like from the outside."""

    def complete(self, system, messages, tools):
        if self.turn_index(messages) >= 4:
            raise RuntimeError("worker A died")
        return super().complete(system, messages, tools)


def drive(run_id: str, prov, worker: str) -> str:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set status = 'running', lease_owner = %s,"
            "                lease_expires_at = now() + interval '60 seconds',"
            "                attempt = attempt + 1 where id = %s",
            (worker, run_id),
        )
        conn.commit()
    with connection() as conn:
        return loop.advance(conn, run_id, worker, prov)


def refund_count() -> int:
    return len(fetch_all("select id from refunds"))


def main() -> int:
    import psycopg

    from deskhand import seed
    from deskhand.config import settings

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            seed.seed(cur)
        conn.commit()

    ticket = fetch_one("select id, org_id from tickets where reference = 'NW-1'")
    assert ticket is not None

    say()
    say(f"{BOLD}Deskhand — a worker dies mid-run. Nobody gets refunded twice.{RESET}")
    say()
    say(f"{DIM}NW-1: \"Beans arrived stale. I'd like a refund.\"{RESET}")
    say()

    step("worker A claims the ticket")
    with connection() as conn, conn.cursor() as cur:
        run_id = runs.create(cur, org_id=str(ticket["org_id"]), ticket_id=str(ticket["id"]))
        conn.commit()

    outcome = drive(run_id, provider(), "worker-a")
    for row in fetch_all(
        "select seq, kind::text, tool_name from steps where run_id = %s order by seq", (run_id,)
    ):
        if row["kind"] == "tool_result":
            say(f"  {row['seq']:>2}  {BLUE}{row['tool_name']}{RESET}", 0.28)

    say()
    approval = fetch_one("select id, preview from approvals where run_id = %s", (run_id,))
    assert approval is not None
    say(f"{AMBER}  ┌─ the run has stopped ─────────────────────────────────┐{RESET}")
    say(f"{AMBER}  │{RESET} {approval['preview']}")
    say(f"{AMBER}  └───────────────────────────────────────────────────────┘{RESET}")
    say(f"  run status: {AMBER}{outcome}{RESET}   refunds so far: {refund_count()}")
    say()

    step("a human approves it")
    with connection() as conn, conn.cursor() as cur:
        approvals.decide(
            cur,
            approval_id=str(approval["id"]),
            org_id=str(ticket["org_id"]),
            decision="approved",
            decided_by=str(fetch_one("select id from users where role = 'owner' limit 1")["id"]),
        )
        conn.commit()
    say()

    step("worker A resumes — and dies right after the money moves")
    try:
        drive(run_id, DiesAfterRefunding(script=SCRIPT), "worker-a")
    except RuntimeError as exc:
        say(f"  {RED}✗ {exc}{RESET}")

    run = fetch_one("select status::text from runs where id = %s", (run_id,))
    say(f"  refunds issued: {BOLD}{refund_count()}{RESET}   run status: {run['status']}")
    say(f"  {DIM}the run is marked running and nobody is running it{RESET}")
    say()

    step("its lease expires. nobody has to notice")
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set lease_expires_at = now() - interval '1 second' where id = %s",
            (run_id,),
        )
        conn.commit()

    step("worker B claims it")
    with connection() as conn, conn.cursor() as cur:
        claimed = runs.claim_next(cur, "worker-b")
        conn.commit()
    say(f"  {GREEN}✓ claimed{RESET} {str(claimed['id'])[:8]} — attempt {claimed['attempt']}")
    say()

    final = drive(run_id, provider(), "worker-b")
    for row in fetch_all(
        "select seq, kind::text, tool_name, content from steps"
        "  where run_id = %s and kind = 'tool_result' and seq > 8 order by seq",
        (run_id,),
    ):
        say(f"  {row['seq']:>2}  {BLUE}{row['tool_name']}{RESET}", 0.28)

    say()
    say(f"  run status: {GREEN}{final}{RESET}")
    say()
    say(f"{BOLD}  refunds issued in total: {refund_count()}{RESET}")
    say()
    say(f"{DIM}  worker B rebuilt the conversation from the step log, saw the refund{RESET}", 0.2)
    say(f"{DIM}  was already recorded, and never called the tool again.{RESET}")
    say()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
