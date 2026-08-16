"""Setup for the trajectory evals.

Deliberately thin. The evals drive the *real* runtime against a *real*
database — same loop, same tools, same approval gate the API uses — because an
eval that runs against a simplified copy of the system is measuring the copy.
Only the model is substituted, and only because a scripted one lets a scenario
say "now the agent asks for a refund" without paying for a token or hoping.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

from deskhand import seed
from deskhand.config import settings
from deskhand.db import connection, fetch_all, fetch_one
from deskhand.runtime import approvals, loop, runs


def reset() -> None:
    """Rebuild the world. Every scenario starts from the same fixtures."""
    # No dict row factory here: the seed helpers index rows positionally, and
    # matching that is simpler than making the seed agnostic for one caller.
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            seed.seed(cur)
        conn.commit()


def org(slug: str = "northwind") -> str:
    row = fetch_one("select id from orgs where slug = %s", (slug,))
    assert row is not None
    return str(row["id"])


def user(email: str = "owner@northwind.test") -> str:
    row = fetch_one("select id from users where email = %s", (email,))
    assert row is not None
    return str(row["id"])


def start(ticket_reference: str) -> str:
    row = fetch_one("select id, org_id from tickets where reference = %s", (ticket_reference,))
    assert row is not None, f"no ticket {ticket_reference}"
    with connection() as conn, conn.cursor() as cur:
        run_id = runs.create(
            cur,
            org_id=str(row["org_id"]),
            ticket_id=str(row["id"]),
            started_by=user(),
        )
        conn.commit()
    return run_id


def drive(run_id: str, provider, worker: str = "eval") -> str:
    """Claim the run and advance it, exactly as a worker does."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set status = 'running', lease_owner = %s,"
            "                lease_expires_at = now() + interval '60 seconds',"
            "                attempt = attempt + 1"
            " where id = %s",
            (worker, run_id),
        )
        conn.commit()
    with connection() as conn:
        return loop.advance(conn, run_id, worker, provider)


def kill_worker(run_id: str) -> None:
    """Stop renewing the lease, which is all dying actually looks like."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set lease_expires_at = now() - interval '1 second' where id = %s",
            (run_id,),
        )
        conn.commit()


def claim(worker: str) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        claimed = runs.claim_next(cur, worker)
        conn.commit()
    return claimed


def decide(run_id: str, decision: str, reason: str | None = None) -> None:
    approval = fetch_one(
        "select id, org_id from approvals where run_id = %s and status = 'pending'", (run_id,)
    )
    assert approval is not None, "no pending approval to decide"
    with connection() as conn, conn.cursor() as cur:
        approvals.decide(
            cur,
            approval_id=str(approval["id"]),
            org_id=str(approval["org_id"]),
            decision=decision,
            decided_by=user(),
            reason=reason,
        )
        conn.commit()


def expire_approvals(run_id: str) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update approvals set expires_at = now() - interval '1 second' where run_id = %s",
            (run_id,),
        )
        conn.commit()


def shrink(run_id: str, **columns: object) -> None:
    """Tighten a bound on a live run, so a scenario need not burn 24 steps.

    The column names come from keyword arguments rather than a literal, so this
    is the one query in the project that has to be *composed* rather than
    written. `psycopg.sql` is how that is done safely: identifiers are quoted by
    the driver and values stay placeholders, so neither can be confused for the
    other. An f-string here would work and would also be the exact shape of an
    injection, which is why the LiteralString requirement rejects it.
    """
    statement = sql.SQL("update runs set {assignments} where id = {run_id}").format(
        assignments=sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder())
            for column in columns
        ),
        run_id=sql.Placeholder(),
    )
    with connection() as conn, conn.cursor() as cur:
        cur.execute(statement, (*columns.values(), run_id))
        conn.commit()


def refunds() -> list[dict]:
    return fetch_all("select * from refunds order by created_at")


def emails() -> list[dict]:
    return fetch_all("select * from customer_emails order by sent_at")
