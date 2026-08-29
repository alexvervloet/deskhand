"""The HTTP surface.

The properties worth asserting here are the boundaries: a session token that
does not work, a merchant that cannot see another's tickets, and a role that
can watch a run spend money but cannot authorise it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from deskhand.db import connection, fetch_all, fetch_one
from deskhand.main import app
from deskhand.providers import ScriptedProvider, call, text
from deskhand.ratelimit import auth_limiter
from deskhand.runtime import loop
from deskhand.tools import args_hash

pytestmark = pytest.mark.usefixtures("fresh")

client = TestClient(app)

OWNER = "owner@northwind.test"
VIEWER = "viewer@northwind.test"
OTHER_ORG = "owner@lumen.test"
PASSWORD = "demo-password-123"


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    # The suite logs in far faster than any human, and a shared limiter would
    # make later tests fail for reasons unrelated to what they assert.
    auth_limiter.reset()


def login(email: str = OWNER) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def drive_run(run_id: str, provider) -> str:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "update runs set status = 'running', lease_owner = 'test',"
            "                lease_expires_at = now() + interval '60 seconds'"
            " where id = %s",
            (run_id,),
        )
        conn.commit()
    with connection() as conn:
        return loop.advance(conn, run_id, "test", provider)


# ----------------------------------------------------------------------- auth


def test_healthz_says_which_provider_is_in_use() -> None:
    body = client.get("/healthz").json()
    assert body["ok"] is True
    # A demo running on the scripted mock must never look like a model run.
    assert body["provider"] in ("claude", "mock")


def test_login_returns_a_token_and_the_callers_permissions() -> None:
    response = client.post("/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["can_approve"] is True
    assert body["user"]["org_slug"] == "northwind"
    assert len(body["token"]) > 20


def test_the_token_is_not_what_is_stored() -> None:
    token = client.post(
        "/auth/login", json={"email": OWNER, "password": PASSWORD}
    ).json()["token"]
    stored = fetch_all("select token_hash from sessions")
    assert all(row["token_hash"] != token for row in stored)


def test_a_wrong_password_is_rejected() -> None:
    response = client.post("/auth/login", json={"email": OWNER, "password": "nope"})
    assert response.status_code == 401


def test_an_unknown_account_and_a_wrong_password_look_identical() -> None:
    missing = client.post("/auth/login", json={"email": "ghost@nowhere.test", "password": "x"})
    wrong = client.post("/auth/login", json={"email": OWNER, "password": "x"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


def test_login_is_rate_limited() -> None:
    for _ in range(10):
        client.post("/auth/login", json={"email": OWNER, "password": "wrong"})
    blocked = client.post("/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert blocked.status_code == 429


def test_endpoints_require_a_session() -> None:
    assert client.get("/tickets").status_code == 401
    assert client.get("/tickets", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_logout_invalidates_the_token() -> None:
    headers = login()
    assert client.get("/me", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/me", headers=headers).status_code == 401


# -------------------------------------------------------------------- tenancy


def test_a_merchant_sees_only_its_own_tickets() -> None:
    northwind = client.get("/tickets", headers=login()).json()
    lumen = client.get("/tickets", headers=login(OTHER_ORG)).json()

    assert {t["reference"] for t in northwind} == {"NW-1", "NW-2", "NW-3", "NW-4"}
    assert {t["reference"] for t in lumen} == {"LU-1", "LU-2"}


def test_another_merchants_ticket_is_not_found_rather_than_forbidden() -> None:
    response = client.get("/tickets/LU-1", headers=login())
    # 404, not 403: whether that reference exists somewhere else is not this
    # caller's business.
    assert response.status_code == 404


def test_another_merchants_run_is_not_reachable() -> None:
    lumen_headers = login(OTHER_ORG)
    run_id = client.post(
        "/runs", json={"ticket_reference": "LU-1"}, headers=lumen_headers
    ).json()["id"]

    assert client.get(f"/runs/{run_id}", headers=login()).status_code == 404
    assert client.post(f"/runs/{run_id}/cancel", headers=login()).status_code == 404


# ----------------------------------------------------------------------- runs


def test_starting_a_run_queues_it_against_the_ticket() -> None:
    headers = login()
    response = client.post("/runs", json={"ticket_reference": "NW-2"}, headers=headers)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["ticket_reference"] == "NW-2"

    listing = client.get("/tickets", headers=headers).json()
    nw2 = next(t for t in listing if t["reference"] == "NW-2")
    assert nw2["open_run_id"] == body["id"]


def test_only_one_run_at_a_time_per_ticket() -> None:
    """Two agents on the same ticket would race on the same order and could
    both propose a refund for it."""
    headers = login()
    assert client.post("/runs", json={"ticket_reference": "NW-2"}, headers=headers).status_code == 201
    second = client.post("/runs", json={"ticket_reference": "NW-2"}, headers=headers)
    assert second.status_code == 409


def test_a_run_detail_carries_its_trajectory_and_its_bounds() -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-2"}, headers=headers
    ).json()["id"]

    drive_run(run_id, ScriptedProvider(script=[
        [call("get_ticket", reference="NW-2")],
        text("Nothing due yet."),
    ]))

    body = client.get(f"/runs/{run_id}", headers=headers).json()
    assert body["status"] == "succeeded"
    assert [s["kind"] for s in body["steps"]] == [
        "model_call", "tool_result", "model_call", "final"
    ]
    assert body["max_steps"] > 0
    assert body["cost_display"].startswith("$")


def test_cancelling_a_run_closes_its_pending_approvals() -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-1"}, headers=headers
    ).json()["id"]

    drive_run(run_id, ScriptedProvider(script=[
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="stale")],
        text("done"),
    ]))
    assert client.get("/approvals", headers=headers).json()

    response = client.post(f"/runs/{run_id}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    # A cancelled run must not leave a live decision in somebody's queue.
    assert client.get("/approvals", headers=headers).json() == []


# ------------------------------------------------------------------ approvals


def test_the_approval_queue_shows_what_will_actually_happen() -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-1"}, headers=headers
    ).json()["id"]
    drive_run(run_id, ScriptedProvider(script=[
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900,
              reason="Stale beans inside the window.")],
        text("done"),
    ]))

    queue = client.get("/approvals", headers=headers).json()
    assert len(queue) == 1
    assert queue[0]["tool_name"] == "issue_refund"
    # The preview is the sentence a human approves, not a blob of JSON.
    assert "Refund 19.00 USD against order NW-1042" in queue[0]["preview"]
    assert queue[0]["args"]["amount_cents"] == 1900


def test_an_approval_carries_every_argument_it_is_bound_to() -> None:
    """The preview is a summary; the hash is not.

    `args_hash` covers every argument, so anything the approval screen cannot
    show is something a human would be consenting to unseen. An email is the
    case that exposes it: the preview names the subject, and the body is the
    part that actually reaches the customer.
    """
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-1"}, headers=headers
    ).json()["id"]
    body = "We are sorry about the beans. A refund of 19.00 USD is on its way."
    drive_run(run_id, ScriptedProvider(script=[
        [call("send_customer_email", reference="NW-1",
              subject="About your order", body=body)],
        text("done"),
    ]))

    approval = client.get("/approvals", headers=headers).json()[0]
    assert approval["tool_name"] == "send_customer_email"

    # Every argument the runtime will hash is on the payload the screen renders.
    with connection() as conn, conn.cursor() as cur:
        cur.execute("select args, args_hash from approvals where run_id = %s", (run_id,))
        row = cur.fetchone()
    assert row is not None
    assert approval["args"] == row["args"]
    assert args_hash("send_customer_email", approval["args"]) == row["args_hash"]

    # And the body in particular, which the one-line preview does not carry.
    assert approval["args"]["body"] == body
    assert body not in approval["preview"]


def test_a_viewer_can_watch_a_run_but_not_approve_one() -> None:
    owner_headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-1"}, headers=owner_headers
    ).json()["id"]
    drive_run(run_id, ScriptedProvider(script=[
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="stale")],
        text("done"),
    ]))

    viewer_headers = login(VIEWER)
    assert client.get(f"/runs/{run_id}", headers=viewer_headers).status_code == 200
    queue = client.get("/approvals", headers=viewer_headers).json()
    assert len(queue) == 1

    blocked = client.post(
        f"/approvals/{queue[0]['id']}/decide",
        json={"decision": "approved"},
        headers=viewer_headers,
    )
    assert blocked.status_code == 403
    assert fetch_all("select id from refunds") == []


def test_approving_through_the_api_records_who_decided() -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-1"}, headers=headers
    ).json()["id"]
    script = [
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="stale")],
        text("Refunded."),
    ]
    drive_run(run_id, ScriptedProvider(script=script))

    approval_id = client.get("/approvals", headers=headers).json()[0]["id"]
    decided = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=headers
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    drive_run(run_id, ScriptedProvider(script=script))
    refunds = fetch_all("select amount_cents from refunds")
    assert len(refunds) == 1

    who = fetch_one(
        "select u.email from audit_log a join users u on u.id = a.actor_id"
        " where a.run_id = %s and a.action = 'approval.granted'",
        (run_id,),
    )
    assert who is not None and who["email"] == OWNER


def test_the_same_approval_cannot_be_decided_twice() -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-1"}, headers=headers
    ).json()["id"]
    drive_run(run_id, ScriptedProvider(script=[
        [call("issue_refund", order_reference="NW-1042", amount_cents=1900, reason="stale")],
        text("done"),
    ]))

    approval_id = client.get("/approvals", headers=headers).json()[0]["id"]
    first = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "denied", "reason": "no"},
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        f"/approvals/{approval_id}/decide", json={"decision": "approved"}, headers=headers
    )
    assert second.status_code == 409


def test_a_decision_must_be_approved_or_denied() -> None:
    headers = login()
    response = client.post(
        "/approvals/00000000-0000-0000-0000-000000000000/decide",
        json={"decision": "maybe"},
        headers=headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------- usage


def test_usage_reports_both_ceilings() -> None:
    body = client.get("/usage", headers=login()).json()
    assert body["org_daily_budget_micros"] > 0
    # The per-org cap bounds one tenant; the platform cap is the one that
    # bounds the bill. Both are shown, because seeing only the first is how a
    # deployment gets a surprise.
    assert body["platform_daily_budget_micros"] > 0
    assert body["org_spend_today_display"].startswith("$")


# --------------------------------------------------------------------- stream


def test_the_stream_replays_the_trajectory_and_closes() -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-2"}, headers=headers
    ).json()["id"]
    drive_run(run_id, ScriptedProvider(script=[
        [call("get_ticket", reference="NW-2")],
        text("Nothing due."),
    ]))

    with client.stream("GET", f"/runs/{run_id}/stream", headers=headers) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: step" in body
    assert "event: done" in body
    assert body.count("event: step") == 4


def test_every_streamed_summary_carries_the_ticket_reference() -> None:
    """Regression. The client merges each status event into the run it is
    displaying, so a summary with a null reference blanked the run header the
    moment the run changed state."""
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-2"}, headers=headers
    ).json()["id"]
    drive_run(run_id, ScriptedProvider(script=[text("Nothing due.")]))

    with client.stream("GET", f"/runs/{run_id}/stream", headers=headers) as response:
        body = "".join(response.iter_text())

    payloads = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    summaries = [p for p in payloads if isinstance(p, dict) and "status" in p]
    assert summaries, "the stream emitted no run summaries"
    for summary in summaries:
        assert summary["ticket_reference"] == "NW-2"


# ----------------------------------------------------------------------- tools


def test_the_registry_is_exposed_so_the_ui_need_not_restate_it() -> None:
    """A second copy of "which tools are irreversible" would eventually
    disagree with the first, silently, in the unsafe direction."""
    tools = client.get("/tools", headers=login()).json()
    by_risk: dict[str, set[str]] = {}
    for tool in tools:
        by_risk.setdefault(tool["risk"], set()).add(tool["name"])

    assert by_risk["irreversible"] == {
        "issue_refund",
        "send_customer_email",
        "cancel_order",
    }
    assert "search_kb" in by_risk["read"]
    assert all(t["description"].strip() for t in tools)


# ---------------------------------------------------------------------- replay


def test_the_conversation_before_a_step_is_readable_back(fresh) -> None:
    headers = login()
    run_id = client.post(
        "/runs", json={"ticket_reference": "NW-2"}, headers=headers
    ).json()["id"]
    drive_run(run_id, ScriptedProvider(script=[
        [call("get_ticket", reference="NW-2")],
        text("Nothing due."),
    ]))

    before_first = client.get(f"/runs/{run_id}/replay?at=1", headers=headers).json()
    assert [m["role"] for m in before_first["messages"]] == ["user"]
    assert before_first["system"], "the system prompt is part of what the model saw"

    before_third = client.get(f"/runs/{run_id}/replay?at=3", headers=headers).json()
    assert len(before_third["messages"]) > len(before_first["messages"])
    # The ticket the agent had read by then, fenced exactly as the model got it.
    assert "Where is my order" in str(before_third["messages"])
    assert "<<<untrusted:" in str(before_third["messages"])


def test_another_merchants_run_cannot_be_replayed() -> None:
    lumen = login(OTHER_ORG)
    run_id = client.post(
        "/runs", json={"ticket_reference": "LU-1"}, headers=lumen
    ).json()["id"]
    assert client.get(f"/runs/{run_id}/replay", headers=login()).status_code == 404


# ------------------------------------------------------------- malformed input


def test_an_id_that_cannot_be_an_id_is_a_404_not_a_500() -> None:
    """A run id Postgres would reject outright.

    `runs.id` is a uuid, so handing the database a string that is not one raises
    rather than returning no rows — which surfaced as a 500 for what is only
    ever a bad request. It gets the same answer as an id that simply does not
    exist, and for the same reason: whether an id could exist is not the
    caller's business.
    """
    headers = login()
    for path in ("/runs/nonsense", "/runs/nonsense/replay", "/runs/nonsense/stream"):
        assert client.get(path, headers=headers).status_code == 404, path

    assert (
        client.post("/runs/nonsense/cancel", headers=headers).status_code == 404
    )
    # And the same for an approval id, answered the way an unknown one is.
    decided = client.post(
        "/approvals/nonsense/decide", json={"decision": "approved"}, headers=headers
    )
    assert decided.status_code == 409


def test_a_negative_limit_is_rejected_rather_than_reaching_postgres() -> None:
    """`limit -1` is a Postgres error, not an empty page.

    An over-large limit stays a clamp: asking for more than there is is a
    reasonable request, where asking for a negative number is not.
    """
    headers = login()
    assert client.get("/runs?limit=-1", headers=headers).status_code == 422
    assert client.get("/runs?limit=0", headers=headers).status_code == 422
    assert client.get("/runs?limit=100000", headers=headers).status_code == 200


# ------------------------------------------------------------------- headers


def test_every_response_carries_the_security_headers() -> None:
    """Including the ones nobody remembers to decorate.

    The token lives in localStorage, so a script executing in this origin can
    read it and act as the signed-in user until it expires. `script-src 'self'`
    is what keeps that from being one XSS away, and it has to be on the error
    responses and the static SPA too, not only on the routes that succeeded.
    """
    responses = [
        client.get("/healthz"),                       # public, 200
        client.get("/tickets"),                       # unauthenticated, 401
        client.get("/runs/nonsense", headers=login()),  # authenticated, 404
    ]
    for response in responses:
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"


def test_the_policy_permits_no_inline_or_third_party_script() -> None:
    """The two ways the SPA could be made to run somebody else's code.

    `style-src` deliberately allows inline, because the UI sets style props and
    the browser reads those as inline styles. Script does not, and that is the
    half that matters: reading the token requires script execution.
    """
    csp = client.get("/healthz").headers["Content-Security-Policy"]
    directives = dict(
        (part.split(" ", 1) + [""])[:2]
        for part in (p.strip() for p in csp.split(";"))
        if part
    )
    assert directives["script-src"] == "'self'"
    assert "unsafe-inline" not in directives["script-src"]
    assert "unsafe-eval" not in directives["script-src"]
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    # The one relaxation, asserted so that widening it is a deliberate edit.
    assert directives["style-src"] == "'self' 'unsafe-inline'"
