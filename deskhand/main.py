"""The HTTP API.

Every handler that touches tenant data filters on `caller.org_id`, inside the
query rather than after it. The one endpoint that commits the merchant to
something — deciding an approval — additionally requires a role that may do so.

The interesting endpoint is `GET /runs/{id}/stream`: a live view of a
trajectory as it happens, which is what makes the approval gate legible. You
watch the agent read the ticket, read the order, check the policy, and then
stop, waiting for you.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from deskhand import pricing, schemas
from deskhand.auth import new_session_token, session_expiry, verify_password
from deskhand.config import settings
from deskhand.db import connection, fetch_all, fetch_one
from deskhand.deps import ApproverDep, CallerDep
from deskhand.ratelimit import auth_limiter
from deskhand.runtime import approvals, runs

log = logging.getLogger("deskhand")

app = FastAPI(
    title="Deskhand",
    description="A durable agent runtime for support operations.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------- health


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    fetch_one("select 1 as ok")
    return {
        "ok": True,
        # Surfaced rather than hidden: a demo running against the scripted mock
        # should say so everywhere it can.
        "provider": "claude" if settings.has_model_key else "mock",
        "model": settings.model_id if settings.has_model_key else "mock",
    }


# ----------------------------------------------------------------------- auth


@app.post("/auth/login", response_model=schemas.LoginResponse)
def login(body: schemas.LoginRequest, request: Request) -> Any:
    client = request.client.host if request.client else "unknown"
    if not auth_limiter.allow(client):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts; wait a minute"
        )

    row = fetch_one(
        "select u.id, u.email, u.role::text as role, u.password_hash,"
        "       o.id as org_id, o.slug, o.name"
        "  from users u join orgs o on o.id = u.org_id"
        " where lower(u.email) = lower(%s)",
        (body.email,),
    )
    # The password is verified even when the user does not exist, against a
    # throwaway hash, so a missing account and a wrong password take the same
    # time to answer.
    stored = row["password_hash"] if row else "$2b$12$" + "x" * 53
    if not verify_password(body.password, stored) or row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")

    token, digest = new_session_token()
    expires = session_expiry()
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into sessions (token_hash, user_id, expires_at) values (%s, %s, %s)",
            (digest, row["id"], expires),
        )
        conn.commit()

    return {
        "token": token,
        "expires_at": expires,
        "user": {
            "id": str(row["id"]),
            "email": row["email"],
            "role": row["role"],
            "org_id": str(row["org_id"]),
            "org_slug": row["slug"],
            "org_name": row["name"],
            "can_approve": row["role"] in ("owner", "agent"),
        },
    }


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(caller: CallerDep) -> None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute("delete from sessions where user_id = %s", (caller.user_id,))
        conn.commit()


@app.get("/me", response_model=schemas.MeResponse)
def me(caller: CallerDep) -> Any:
    return {
        "id": caller.user_id,
        "email": caller.email,
        "role": caller.role,
        "org_id": caller.org_id,
        "org_slug": caller.org_slug,
        "org_name": caller.org_name,
        "can_approve": caller.can_approve,
    }


# -------------------------------------------------------------------- tickets

_TICKET_COLUMNS = (
    "t.id, t.reference, t.subject, t.status::text as status,"
    " t.priority::text as priority, t.tags, t.created_at,"
    " c.name as customer_name, c.email as customer_email,"
    " (select r.id from runs r where r.ticket_id = t.id"
    "   and r.status in ('queued','running','awaiting_approval')"
    "  order by r.created_at desc limit 1) as open_run_id"
)


@app.get("/tickets", response_model=list[schemas.TicketSummary])
def list_tickets(caller: CallerDep) -> Any:
    rows = fetch_all(
        f"select {_TICKET_COLUMNS} from tickets t"
        "  join customers c on c.id = t.customer_id"
        " where t.org_id = %s order by t.created_at",
        (caller.org_id,),
    )
    return [_ticket_summary(r) for r in rows]


@app.get("/tickets/{reference}", response_model=schemas.TicketDetail)
def get_ticket(reference: str, caller: CallerDep) -> Any:
    row = fetch_one(
        f"select {_TICKET_COLUMNS} from tickets t"
        "  join customers c on c.id = t.customer_id"
        " where t.org_id = %s and t.reference = %s",
        (caller.org_id, reference),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such ticket")

    messages = fetch_all(
        "select author_kind::text as author_kind, is_internal, body, created_at"
        "  from ticket_messages where ticket_id = %s order by created_at",
        (row["id"],),
    )
    return _ticket_summary(row) | {"messages": messages}


def _ticket_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "reference": row["reference"],
        "subject": row["subject"],
        "status": row["status"],
        "priority": row["priority"],
        "tags": list(row["tags"]),
        "customer_name": row["customer_name"],
        "customer_email": row["customer_email"],
        "created_at": row["created_at"],
        "open_run_id": str(row["open_run_id"]) if row["open_run_id"] else None,
    }


# ----------------------------------------------------------------------- runs


@app.post("/runs", response_model=schemas.RunSummary, status_code=status.HTTP_201_CREATED)
def start_run(body: schemas.StartRunRequest, caller: CallerDep) -> Any:
    ticket = fetch_one(
        "select id from tickets where org_id = %s and reference = %s",
        (caller.org_id, body.ticket_reference),
    )
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such ticket")

    existing = fetch_one(
        "select id from runs where ticket_id = %s"
        "  and status in ('queued','running','awaiting_approval')",
        (ticket["id"],),
    )
    if existing is not None:
        # Two agents working the same ticket would race on the same order and
        # could both propose a refund for it. One at a time.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a run is already open on this ticket ({existing['id']})",
        )

    with connection() as conn, conn.cursor() as cur:
        run_id = runs.create(
            cur,
            org_id=caller.org_id,
            ticket_id=str(ticket["id"]),
            started_by=caller.user_id,
        )
        runs.audit(
            cur,
            org_id=caller.org_id,
            run_id=run_id,
            actor_kind="human",
            actor_id=caller.user_id,
            action="run.started",
            detail={"ticket": body.ticket_reference},
        )
        conn.commit()

    return _run_summary(_require_run(run_id, caller.org_id))


@app.get("/runs", response_model=list[schemas.RunSummary])
def list_runs(caller: CallerDep, limit: int = 50) -> Any:
    rows = fetch_all(
        "select r.*, t.reference as ticket_reference from runs r"
        "  join tickets t on t.id = r.ticket_id"
        " where r.org_id = %s order by r.created_at desc limit %s",
        (caller.org_id, min(limit, 200)),
    )
    return [_run_summary(r) for r in rows]


@app.get("/runs/{run_id}", response_model=schemas.RunDetail)
def get_run(run_id: str, caller: CallerDep) -> Any:
    run = _require_run(run_id, caller.org_id)
    steps = fetch_all(
        "select * from steps where run_id = %s order by seq", (run_id,)
    )
    pending = fetch_all(
        "select * from approvals where run_id = %s order by created_at", (run_id,)
    )
    return (
        _run_summary(run)
        | {
            "prompt": run["prompt"],
            "max_steps": run["max_steps"],
            "max_tokens": run["max_tokens"],
            "max_spend_micros": run["max_spend_micros"],
            "deadline_at": run["deadline_at"],
            "steps": [_step_view(s) for s in steps],
            "approvals": [_approval_view(a) for a in pending],
        }
    )


@app.post("/runs/{run_id}/cancel", response_model=schemas.RunSummary)
def cancel_run(run_id: str, caller: CallerDep) -> Any:
    run = _require_run(run_id, caller.org_id)
    if run["status"] in ("succeeded", "failed", "exhausted", "cancelled"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"run is already {run['status']}")

    with connection() as conn, conn.cursor() as cur:
        runs.finish(
            cur,
            run_id,
            status="cancelled",
            stop_reason=runs.STOP_CANCELLED,
            stop_detail=f"cancelled by {caller.email}",
        )
        cur.execute(
            "update approvals set status = 'expired'"
            " where run_id = %s and status = 'pending'",
            (run_id,),
        )
        runs.audit(
            cur,
            org_id=caller.org_id,
            run_id=run_id,
            actor_kind="human",
            actor_id=caller.user_id,
            action="run.cancelled",
        )
        conn.commit()

    return _run_summary(_require_run(run_id, caller.org_id))


@app.get("/runs/{run_id}/stream")
def stream_run(run_id: str, caller: CallerDep) -> StreamingResponse:
    """Server-sent events: every step as it lands, then the ending.

    Implemented by polling rather than LISTEN/NOTIFY. Polling holds no database
    connection between ticks, which matters more here than latency does: a
    stream can stay open for as long as a human takes to answer an approval,
    and a notification-based version would pin a connection for that whole
    time. The cost is up to half a second of lag on a step, which nobody
    watching an agent think will notice.
    """
    _require_run(run_id, caller.org_id)

    def events() -> Iterator[str]:
        sent = 0
        last_status: str | None = None
        deadline = time.monotonic() + 900

        while time.monotonic() < deadline:
            steps = fetch_all(
                "select * from steps where run_id = %s and seq > %s order by seq",
                (run_id, sent),
            )
            for step in steps:
                sent = step["seq"]
                yield _sse("step", _step_view(step))

            run = fetch_one("select * from runs where id = %s", (run_id,))
            if run is None:
                yield _sse("error", {"message": "run disappeared"})
                return

            if run["status"] != last_status:
                last_status = run["status"]
                yield _sse("status", _run_summary(run | {"ticket_reference": None}))

            if run["status"] == "awaiting_approval":
                waiting = fetch_all(
                    "select * from approvals where run_id = %s and status = 'pending'",
                    (run_id,),
                )
                yield _sse("approval", [_approval_view(a) for a in waiting])

            if run["status"] in ("succeeded", "failed", "exhausted", "cancelled"):
                yield _sse("done", _run_summary(run | {"ticket_reference": None}))
                return

            time.sleep(0.5)

        yield _sse("done", {"note": "stream timed out; the run continues"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# ------------------------------------------------------------------ approvals


@app.get("/approvals", response_model=list[schemas.ApprovalView])
def list_approvals(caller: CallerDep) -> Any:
    with connection() as conn, conn.cursor() as cur:
        # Sweep timed-out approvals before answering, so the queue a human sees
        # never contains a decision that can no longer be made.
        approvals.expire_stale(cur)
        conn.commit()
        rows = approvals.pending_for_org(cur, caller.org_id)
    return [_approval_view(r) for r in rows]


@app.post("/approvals/{approval_id}/decide", response_model=schemas.ApprovalView)
def decide_approval(
    approval_id: str, body: schemas.DecideRequest, caller: ApproverDep
) -> Any:
    with connection() as conn, conn.cursor() as cur:
        try:
            row = approvals.decide(
                cur,
                approval_id=approval_id,
                org_id=caller.org_id,
                decision=body.decision,
                decided_by=caller.user_id,
                reason=body.reason,
            )
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        conn.commit()

    log.info(
        "approval %s %s by %s (%s)", approval_id, body.decision, caller.email, row["tool_name"]
    )
    return _approval_view(row)


# ---------------------------------------------------------------------- usage


@app.get("/usage", response_model=schemas.UsageResponse)
def usage(caller: CallerDep) -> Any:
    org = fetch_one(
        "select coalesce(sum(cost_micros), 0) as spend, count(*) as runs from runs"
        " where org_id = %s and created_at >= date_trunc('day', now())",
        (caller.org_id,),
    )
    platform = fetch_one(
        "select coalesce(sum(cost_micros), 0) as spend from runs"
        " where created_at >= date_trunc('day', now())"
    )
    refunded = fetch_one(
        "select coalesce(sum(amount_cents), 0) as cents from refunds"
        " where org_id = %s and created_at >= date_trunc('day', now())",
        (caller.org_id,),
    )
    assert org is not None and platform is not None and refunded is not None

    return {
        "org_spend_today_micros": int(org["spend"]),
        "org_spend_today_display": pricing.format_usd(int(org["spend"])),
        "org_daily_budget_micros": int(settings.daily_budget_usd_per_org * 1_000_000),
        "platform_spend_today_micros": int(platform["spend"]),
        "platform_daily_budget_micros": int(settings.platform_daily_budget_usd * 1_000_000),
        "runs_today": int(org["runs"]),
        "refunds_today_cents": int(refunded["cents"]),
        "refunds_today_display": f"${int(refunded['cents']) / 100:,.2f}",
    }


# -------------------------------------------------------------------- helpers


def _require_run(run_id: str, org_id: str) -> dict[str, Any]:
    row = fetch_one(
        "select r.*, t.reference as ticket_reference from runs r"
        "  join tickets t on t.id = r.ticket_id"
        " where r.id = %s and r.org_id = %s",
        (run_id, org_id),
    )
    if row is None:
        # 404 rather than 403 for a run belonging to another merchant: whether
        # a given id exists elsewhere is not this caller's business.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return row


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "ticket_id": str(row["ticket_id"]),
        "ticket_reference": row.get("ticket_reference"),
        "status": row["status"],
        "stop_reason": row["stop_reason"],
        "stop_detail": row["stop_detail"],
        "provider": row["provider"],
        "model": row["model"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cost_micros": row["cost_micros"],
        "cost_display": pricing.format_usd(row["cost_micros"]),
        "attempt": row["attempt"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def _step_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "tool_name": row["tool_name"],
        "content": row["content"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cost_micros": row["cost_micros"],
        "cost_display": pricing.format_usd(row["cost_micros"]),
        "latency_ms": row["latency_ms"],
        "created_at": row["created_at"],
    }


def _approval_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "ticket_reference": row.get("ticket_reference"),
        "tool_name": row["tool_name"],
        "preview": row["preview"],
        "args": row["args"],
        "status": row["status"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
    }


# ------------------------------------------------------------- static frontend

# Mounted last so it never shadows an API route. Absent in development, where
# Vite serves the UI on its own port.
_FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
