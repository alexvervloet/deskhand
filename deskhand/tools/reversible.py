"""Reversible tools: they change state, run without approval, and each one
records how to undo itself at the moment it acts.

The inverse is captured at execution time rather than derived later, because
the prior value is knowable now and merely guessable afterwards. A run that
fails at step 9 *can* be reverted precisely — set the priority back to `normal`,
not "back to whatever seems reasonable".

**Nothing in the product calls `apply_inverse` yet.** Every reversible tool
records its inverse, the ledger stores it, and `apply_inverse` is exercised by
the test suite — but no runtime path, endpoint, or button reverts a failed run.
What exists is the hard half: the information needed to undo, captured at the
only moment it is knowable. What does not exist is the easy half: deciding which
steps of which run to walk back, and who is allowed to ask for it. Said plainly
here rather than left for a reader to discover, because "reversible" reads like
a promise that something reverses.

Undoing is not the same as never having acted. A reverted internal note was
still readable by whoever was watching the queue. That is the honest limit of
this class, and it is why an email is not in it.
"""

from __future__ import annotations

from typing import Any

from deskhand.tools.base import RiskClass, ToolContext, ToolDef, ToolError, ToolOutcome, register
from deskhand.tools.read import schema

PRIORITIES = ["low", "normal", "high", "urgent"]
STATUSES = ["open", "pending", "resolved", "escalated"]


def _ticket(ctx: ToolContext, reference: str) -> dict[str, Any]:
    ctx.cursor.execute(
        "select id, reference, status::text, priority::text, tags, assignee_id"
        "  from tickets where org_id = %s and reference = %s",
        (ctx.org_id, reference),
    )
    row = ctx.cursor.fetchone()
    if row is None:
        raise ToolError(f"no ticket {reference!r} for this merchant")
    return dict(row)


def apply_inverse(ctx: ToolContext, inverse: dict[str, Any]) -> None:
    """Undo one recorded effect. Dispatches on the `op` captured at write time.

    Deliberately not expressed as "call the opposite tool": restoring a deleted
    row is not something any tool in the registry can do, and pretending
    otherwise would mean adding tools whose only caller is the revert path —
    tools the model could then also reach.
    """
    op = inverse["op"]
    if op == "set_tags":
        ctx.cursor.execute(
            "update tickets set tags = %s, updated_at = now() where id = %s",
            (inverse["tags"], inverse["ticket_id"]),
        )
    elif op == "set_priority":
        ctx.cursor.execute(
            "update tickets set priority = %s::ticket_priority, updated_at = now() where id = %s",
            (inverse["priority"], inverse["ticket_id"]),
        )
    elif op == "set_status":
        ctx.cursor.execute(
            "update tickets set status = %s::ticket_status, updated_at = now() where id = %s",
            (inverse["status"], inverse["ticket_id"]),
        )
    elif op == "set_assignee":
        ctx.cursor.execute(
            "update tickets set assignee_id = %s, updated_at = now() where id = %s",
            (inverse["assignee_id"], inverse["ticket_id"]),
        )
    elif op == "delete_message":
        ctx.cursor.execute("delete from ticket_messages where id = %s", (inverse["message_id"],))
    else:  # pragma: no cover - guards against a tool adding an op and forgetting this
        raise ToolError(f"no inverse handler for op {op!r}")


# --------------------------------------------------------------- tag_ticket


def _tag_ticket(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ticket = _ticket(ctx, args["reference"])
    before = list(ticket["tags"])
    added = [t for t in args["tags"] if t not in before]
    if not added:
        return ToolOutcome(f"{ticket['reference']} already carries all of those tags.")

    ctx.cursor.execute(
        "update tickets set tags = %s, updated_at = now() where id = %s",
        (before + added, ticket["id"]),
    )
    return ToolOutcome(
        f"Tagged {ticket['reference']} with {', '.join(added)}."
        f" Tags are now: {', '.join(before + added)}.",
        inverse={"op": "set_tags", "ticket_id": str(ticket["id"]), "tags": before},
    )


register(
    ToolDef(
        name="tag_ticket",
        risk=RiskClass.REVERSIBLE,
        description=(
            "Add one or more tags to a ticket. Tags are how the human queue is "
            "filtered, so use the vocabulary already in use on other tickets rather "
            "than inventing synonyms. Existing tags are kept."
        ),
        parameters=schema(
            {
                "reference": {"type": "string", "description": "Ticket reference."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                    "description": "Tags to add, lower-case, hyphenated.",
                },
            }
        ),
        handler=_tag_ticket,
        preview=lambda a: f"Tag {a['reference']} with {', '.join(a['tags'])}",
    )
)


# ------------------------------------------------------------- set_priority


def _set_priority(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ticket = _ticket(ctx, args["reference"])
    before = ticket["priority"]
    after = args["priority"]
    if before == after:
        return ToolOutcome(f"{ticket['reference']} is already {after}.")

    ctx.cursor.execute(
        "update tickets set priority = %s::ticket_priority, updated_at = now() where id = %s",
        (after, ticket["id"]),
    )
    return ToolOutcome(
        f"Priority of {ticket['reference']} changed from {before} to {after}.",
        inverse={"op": "set_priority", "ticket_id": str(ticket["id"]), "priority": before},
    )


register(
    ToolDef(
        name="set_priority",
        risk=RiskClass.REVERSIBLE,
        description=(
            "Set a ticket's priority. Raise it when the customer is blocked, out of "
            "pocket, or has been waiting past the published turnaround; lower it when "
            "a ticket turns out to be a question rather than a problem."
        ),
        parameters=schema(
            {
                "reference": {"type": "string", "description": "Ticket reference."},
                "priority": {"type": "string", "enum": PRIORITIES},
            }
        ),
        handler=_set_priority,
        preview=lambda a: f"Set {a['reference']} priority to {a['priority']}",
    )
)


# --------------------------------------------------------- set_ticket_status


def _set_ticket_status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ticket = _ticket(ctx, args["reference"])
    before = ticket["status"]
    after = args["status"]
    if before == after:
        return ToolOutcome(f"{ticket['reference']} is already {after}.")

    ctx.cursor.execute(
        "update tickets set status = %s::ticket_status, updated_at = now() where id = %s",
        (after, ticket["id"]),
    )
    return ToolOutcome(
        f"Status of {ticket['reference']} changed from {before} to {after}.",
        inverse={"op": "set_status", "ticket_id": str(ticket["id"]), "status": before},
    )


register(
    ToolDef(
        name="set_ticket_status",
        risk=RiskClass.REVERSIBLE,
        description=(
            "Set a ticket's status. Use 'resolved' only when the customer's problem is "
            "actually settled, 'pending' when waiting on the customer, and 'escalated' "
            "when the request needs a human decision you are not authorised to make."
        ),
        parameters=schema(
            {
                "reference": {"type": "string", "description": "Ticket reference."},
                "status": {"type": "string", "enum": STATUSES},
            }
        ),
        handler=_set_ticket_status,
        preview=lambda a: f"Set {a['reference']} status to {a['status']}",
    )
)


# ------------------------------------------------------------ assign_ticket


def _assign_ticket(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ticket = _ticket(ctx, args["reference"])
    email = args["assignee_email"]
    ctx.cursor.execute(
        "select id, email from users where org_id = %s and lower(email) = lower(%s)",
        (ctx.org_id, email),
    )
    user = ctx.cursor.fetchone()
    if user is None:
        raise ToolError(f"no colleague {email!r} at this merchant")

    before = ticket["assignee_id"]
    ctx.cursor.execute(
        "update tickets set assignee_id = %s, updated_at = now() where id = %s",
        (user["id"], ticket["id"]),
    )
    return ToolOutcome(
        f"Assigned {ticket['reference']} to {user['email']}.",
        inverse={
            "op": "set_assignee",
            "ticket_id": str(ticket["id"]),
            "assignee_id": str(before) if before else None,
        },
    )


register(
    ToolDef(
        name="assign_ticket",
        risk=RiskClass.REVERSIBLE,
        description=(
            "Assign a ticket to a named colleague by email. Use it when handing off "
            "work you cannot finish, together with an internal note saying why."
        ),
        parameters=schema(
            {
                "reference": {"type": "string", "description": "Ticket reference."},
                "assignee_email": {
                    "type": "string",
                    "description": "Email of a colleague at this merchant.",
                },
            }
        ),
        handler=_assign_ticket,
        preview=lambda a: f"Assign {a['reference']} to {a['assignee_email']}",
    )
)


# --------------------------------------------------------- add_internal_note


def _add_internal_note(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ticket = _ticket(ctx, args["reference"])
    # Authored as 'agent', not 'system', and the difference is not cosmetic.
    # This body is model output, and the model wrote it after reading a ticket
    # somebody outside the company typed. 'system' is the most authoritative
    # label in the vocabulary — it reads as the platform stating a fact — so
    # filing model prose under it launders text that a customer influenced into
    # text a colleague trusts. `send_customer_email` already writes 'agent'.
    ctx.cursor.execute(
        "insert into ticket_messages (ticket_id, author_kind, is_internal, body)"
        " values (%s, 'agent', true, %s) returning id",
        (ticket["id"], args["body"]),
    )
    row = ctx.cursor.fetchone()
    assert row is not None
    return ToolOutcome(
        f"Added an internal note to {ticket['reference']}. The customer cannot see it.",
        inverse={"op": "delete_message", "message_id": str(row["id"])},
    )


register(
    ToolDef(
        name="add_internal_note",
        risk=RiskClass.REVERSIBLE,
        description=(
            "Write an internal note on a ticket. Staff and future runs can read it; "
            "the customer never can. Use it to record what you checked and why you "
            "reached a conclusion, especially before escalating — the next person to "
            "open the ticket should not have to redo your work. This does not reply "
            "to the customer; use send_customer_email for that."
        ),
        parameters=schema(
            {
                "reference": {"type": "string", "description": "Ticket reference."},
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                    "description": "The note, in plain prose.",
                },
            }
        ),
        handler=_add_internal_note,
        preview=lambda a: f"Add an internal note to {a['reference']}",
    )
)
