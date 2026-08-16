"""Rebuilding a run's conversation from its step log, and fencing what the
model is allowed to trust.

Nothing holds a run's messages in memory between steps. Every time a worker
touches a run — the first time, and after another worker died mid-trajectory —
the conversation is reconstructed here from rows. That is what makes a run
portable between processes, and it is why `steps` is append-only: editing
history would edit the past.

This module is also the single place where tool output becomes model input,
which makes it the right and only place to fence it.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from psycopg.rows import DictRow


def fence_token(run_id: str) -> str:
    """A per-run marker for the untrusted region.

    Derived from the run id rather than randomly generated, because replay has
    to produce byte-identical messages and a fresh nonce each time would defeat
    that. Derived rather than fixed, because a constant delimiter published in
    an open-source repository is one a customer can type into a ticket body and
    close early.
    """
    return hashlib.sha256(f"deskhand-fence:{run_id}".encode()).hexdigest()[:12]


def quarantine(run_id: str, body: str) -> str:
    """Wrap tool output as data.

    Two things happen here, and the second is the one that matters:

    1. The output is delimited with a marker the model is told about in its
       system prompt.
    2. Any occurrence of that marker *inside* the output is removed first, so
       content cannot close its own fence and continue as if it were the
       system talking.

    This does not make the content safe. A model can still be persuaded by
    text inside the fence. What it does is remove the *structural* ambiguity —
    the model can always tell where untrusted input begins and ends — and pair
    it with the guarantee that actually holds: nothing in here can change a
    tool's risk class, so the worst a persuasive ticket achieves is a refund
    request that a human is still asked to approve.
    """
    token = fence_token(run_id)
    opener, closer = f"<<<untrusted:{token}>>>", f"<<</untrusted:{token}>>>"
    cleaned = body.replace(opener, "").replace(closer, "")
    return f"{opener}\n{cleaned}\n{closer}"


def rebuild(
    cur: psycopg.Cursor[DictRow],
    run_id: str,
    prompt: str,
    *,
    before_seq: int | None = None,
) -> list[dict[str, Any]]:
    """Replay the step log into a messages array for the next model call.

    Consecutive tool results are gathered into a single user message. That is
    an API requirement when a turn asked for several tools at once, and getting
    it wrong is subtle: splitting them across messages does not error, it just
    quietly teaches the model to stop making parallel calls.

    `before_seq` truncates the replay, returning the conversation exactly as it
    stood *before* that step ran. This function is a pure function of the rows
    and the prompt — no clock, no randomness, no ambient state — which is what
    makes "what did the model see when it decided to refund?" a question with
    one reproducible answer, months later. See deskhand/replay.py.
    """
    cur.execute(
        "select seq, kind::text, content from steps"
        " where run_id = %s and (%s::int is null or seq < %s::int)"
        " order by seq",
        (run_id, before_seq, before_seq),
    )
    steps = cur.fetchall()

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if pending:
            messages.append({"role": "user", "content": list(pending)})
            pending.clear()

    for step in steps:
        kind, content = step["kind"], step["content"]

        if kind == "model_call":
            flush()
            messages.append({"role": "assistant", "content": content["blocks"]})

        elif kind == "tool_result":
            pending.append(
                {
                    "type": "tool_result",
                    "tool_use_id": content["tool_use_id"],
                    "content": quarantine(run_id, content["result"]),
                    "is_error": not content["ok"],
                }
            )

        elif kind == "approval":
            # A decision only reaches the model once it produces a result. A
            # denial becomes the tool's result so the agent can adapt; an
            # approval produces nothing here, because the tool call that
            # follows is the visible consequence.
            if content.get("decision") == "denied":
                pending.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": content["tool_use_id"],
                        "content": quarantine(
                            run_id,
                            "A human reviewed this action and declined it."
                            + (f" Reason: {content['reason']}" if content.get("reason") else "")
                            + " Do not retry the same action. Either propose a different"
                            " course, or explain what you would need in order to proceed.",
                        ),
                        "is_error": True,
                    }
                )

        # 'final' and 'error' steps close a run; nothing follows them, so they
        # contribute no message.

    flush()
    return messages
