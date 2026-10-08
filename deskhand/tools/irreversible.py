"""Irreversible tools. Money leaves, mail is delivered, an order is killed.

Two things are true of every tool in this module:

1. **It cannot execute without a recorded human approval** bound to this exact
   run, step, and argument hash. That gate lives in the runtime, not here, so
   that adding a tool to this module is sufficient to protect it — a handler
   cannot forget to check.

2. **Its own preconditions are still enforced in code.** The approval gate
   stops the agent from acting unilaterally; it does not stop a human from
   clicking approve on a refund larger than the order. Policy that must always
   hold is a constraint here, not a sentence in the system prompt, because the
   prompt is advice and this is arithmetic.

   `issue_refund` carries three such constraints, and they answer different
   questions. The remaining balance stops one order being refunded twice. The
   per-run ceiling stops one run refunding four orders once each. The daily
   ceiling stops four runs doing it in turn. Only the first of those was here
   originally, which left the total a busy afternoon could pay out bounded by
   nothing but a person reading approval screens carefully.

There is no `apply_inverse` for anything in this file. A refund can be answered
by a charge in the other direction, but that is a new decision requiring its
own approval, not an undo.
"""

from __future__ import annotations

import itertools
from typing import Any

import psycopg
from psycopg.rows import DictRow

from deskhand.config import settings
from deskhand.tools.base import (
    RiskClass,
    Support,
    ToolContext,
    ToolDef,
    ToolError,
    ToolOutcome,
    register,
)
from deskhand.tools.read import schema


def _money(cents: int, currency: str = "USD") -> str:
    return f"{cents / 100:,.2f} {currency}"


# ------------------------------------------------------------- issue_refund


def _ceilings(ctx: ToolContext, amount: int, currency: str) -> None:
    """Refuse a payout that breaches a ceiling, before any money moves.

    Two ceilings, and neither is the per-order remaining balance — that one is
    about a single order being refunded twice, and it says nothing about a run
    that refunds four different orders once each.

    The run ceiling is read off the run row, not from settings, because it was
    snapshotted at creation. Raising the cap in a deploy must not retroactively
    widen a run already in flight.

    This is here rather than in the runtime's bounds check for the reason the
    module docstring gives: `_bound_exceeded` gates model calls, and a ceiling
    checked before the call that *proposes* a refund is not a ceiling on the
    refund. It is checked here, at the point of payment, so it holds even when
    a human has already clicked approve on the screen.

    The org row is locked first, and that is not decoration. The caller holds a
    lock on the *order*, which serialises two runs fighting over one order and
    does nothing about two runs refunding different orders of the same
    merchant — both would read a daily total that leaves room, and both would
    pay. Locking the merchant serialises every payout it makes. Refunds are
    rare enough that the contention costs nothing, and a ceiling that holds
    only when nothing else is happening is not a ceiling.

    **`for no key update`, not `for update`, and the difference is a deadlock.**
    By the time a payout reaches this line its transaction has already inserted
    an `audit_log` row for the approval it was granted, and that row's `org_id`
    foreign key made Postgres take a `KEY SHARE` lock on this very org row.
    `FOR UPDATE` conflicts with `KEY SHARE`, so asking for it here is a lock
    *upgrade* — and two payouts for the same merchant, each holding `KEY SHARE`
    and each waiting to upgrade, is a cycle. Postgres breaks it by killing one,
    the worker marks that run failed, and a refund that was correct and
    authorised simply does not happen.

    `FOR NO KEY UPDATE` conflicts with itself, which is all the ceiling needs —
    two payouts still serialise. It does not conflict with `KEY SHARE`, so it
    cannot deadlock against a foreign key. Found by
    `tests/test_concurrency.py`, not by reading; see LESSONS 27.
    """
    ctx.cursor.execute("select id from orgs where id = %s for no key update", (ctx.org_id,))

    ctx.cursor.execute(
        "select r.max_refund_cents,"
        "       coalesce((select sum(amount_cents) from refunds"
        "                  where run_id = r.id), 0) as run_paid,"
        "       coalesce((select sum(amount_cents) from refunds"
        "                  where org_id = r.org_id"
        "                    and created_at >= date_trunc('day', now())), 0) as org_paid"
        "  from runs r where r.id = %s",
        (ctx.run_id,),
    )
    row = ctx.cursor.fetchone()
    assert row is not None

    run_cap = int(row["max_refund_cents"])
    run_paid = int(row["run_paid"])
    if run_paid + amount > run_cap:
        raise ToolError(
            f"this run may refund {_money(run_cap, currency)} in total and has already"
            f" refunded {_money(run_paid, currency)}, so it cannot also refund"
            f" {_money(amount, currency)}. Do not split the payment into smaller"
            " refunds to get under the ceiling — escalate to a human instead."
        )

    org_cap = settings.daily_refund_cents_per_org
    org_paid = int(row["org_paid"])
    if org_paid + amount > org_cap:
        raise ToolError(
            f"this merchant's daily refund ceiling of {_money(org_cap, currency)} would"
            f" be breached: {_money(org_paid, currency)} has been refunded today."
            " Escalate to a human rather than refunding."
        )


def _issue_refund(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    reference = args["order_reference"]
    amount = args["amount_cents"]

    # `for update` matters: two runs working the same customer's duplicate
    # charge could otherwise both read "nothing refunded yet" and both issue a
    # full refund. The lock makes the read-decide-write sequence atomic against
    # every other writer of this row.
    ctx.cursor.execute(
        "select id, reference, status::text, total_cents, currency, customer_id"
        "  from orders where org_id = %s and reference = %s for update",
        (ctx.org_id, reference),
    )
    order = ctx.cursor.fetchone()
    if order is None:
        raise ToolError(f"no order {reference!r} for this merchant")

    ctx.cursor.execute(
        "select coalesce(sum(amount_cents), 0) as refunded from refunds where order_id = %s",
        (order["id"],),
    )
    row = ctx.cursor.fetchone()
    assert row is not None
    refunded = int(row["refunded"])
    remaining = order["total_cents"] - refunded

    if amount > remaining:
        raise ToolError(
            f"cannot refund {_money(amount, order['currency'])} against"
            f" {order['reference']}: {_money(refunded, order['currency'])} of"
            f" {_money(order['total_cents'], order['currency'])} is already refunded,"
            f" leaving {_money(remaining, order['currency'])}"
        )

    _ceilings(ctx, amount, order["currency"])

    # run_id is stamped on the row itself, so "which run paid this out, and
    # therefore who approved it" is a join and not an investigation.
    ctx.cursor.execute(
        "insert into refunds (org_id, order_id, amount_cents, currency, reason, run_id)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        (ctx.org_id, order["id"], amount, order["currency"], args["reason"], ctx.run_id),
    )
    created = ctx.cursor.fetchone()
    assert created is not None

    return ToolOutcome(
        f"Refunded {_money(amount, order['currency'])} against {order['reference']}."
        f" Remaining refundable: {_money(remaining - amount, order['currency'])}."
        f" The customer sees it on their statement in 5-10 business days."
    )


# Past this many combinations the amount is reported as unchecked rather than
# searched. An order with eight lines of three units each is already 65,536,
# and an approval screen that hangs is worse than one that says it didn't look.
_RECONCILE_LIMIT = 4096


def _reconcile(items: list[dict[str, Any]], amount: int) -> list[tuple[int, dict[str, Any]]] | None:
    """Whole units of this order's lines that add up to `amount`, or None.

    Fewest units first, so $19.00 on an order with two $19.00 bags reads as
    "1 ×" rather than whichever combination enumeration reached first.
    """
    ranges = [range(item["quantity"] + 1) for item in items]
    matches = [
        counts
        for counts in itertools.product(*ranges)
        if sum(n * item["unit_price_cents"] for n, item in zip(counts, items, strict=True))
        == amount
    ]
    if not matches:
        return None
    best = min(matches, key=sum)
    return [(n, item) for n, item in zip(best, items, strict=True) if n]


def _refund_basis(
    cur: psycopg.Cursor[DictRow], org_id: str, customer_id: str, args: dict[str, Any]
) -> list[Support]:
    """Check a proposed refund against the order it names.

    Two questions, both of which an approver would otherwise answer by opening
    the order in another tab. Is this order the ticket's customer's? Read tools
    refuse to answer about anyone else, but a refund only has a human between
    it and the money, and "against order NW-1101" doesn't say whose that is.
    And does the amount match something on the order, or did it come from a
    claim only the customer made? A ticket that says "the bags were $22, refund
    me the difference" is not an instruction, so the fence doesn't apply to it.
    It's a false fact, and this is where it shows up.

    Already-refunded units aren't subtracted from the search: a refund matching
    a line that was refunded before is still "on the record", and the handler
    refuses anything past the remaining balance either way.
    """
    reference = args["order_reference"]
    cur.execute(
        "select o.id, o.customer_id, o.total_cents, o.currency, c.name as customer_name"
        "  from orders o join customers c on c.id = o.customer_id"
        " where o.org_id = %s and o.reference = %s",
        (org_id, reference),
    )
    order = cur.fetchone()
    if order is None:
        return [
            Support("order_reference", "unsupported", "no order with this reference here"),
            Support("amount_cents", "unchecked", "there is no order to check it against"),
        ]

    if str(order["customer_id"]) == customer_id:
        whose = Support(
            "order_reference",
            "supported",
            f"{order['customer_name']}'s order, the customer on this ticket",
        )
    else:
        whose = Support(
            "order_reference",
            "unsupported",
            f"belongs to {order['customer_name']}, not the customer on this ticket",
        )

    cur.execute(
        "select sku, description, quantity, unit_price_cents from order_items"
        " where order_id = %s order by sku",
        (order["id"],),
    )
    items = [dict(r) for r in cur.fetchall()]
    amount = args["amount_cents"]
    currency = order["currency"]

    combinations = 1
    for item in items:
        combinations *= item["quantity"] + 1
    if combinations > _RECONCILE_LIMIT:
        return [whose, Support("amount_cents", "unchecked", "too many lines to reconcile")]

    match = _reconcile(items, amount)
    if match is not None:
        parts = [
            f"{n} × {item['description']} ({item['sku']}) at"
            f" {_money(item['unit_price_cents'], currency)}"
            for n, item in match
        ]
        return [whose, Support("amount_cents", "supported", " + ".join(parts))]

    lines = ", ".join(
        f"{item['quantity']} × {item['sku']} at {_money(item['unit_price_cents'], currency)}"
        for item in items
    )
    return [
        whose,
        Support(
            "amount_cents",
            "unsupported",
            f"no whole number of this order's items adds up to {_money(amount, currency)}."
            f" The order is {lines}",
        ),
    ]


register(
    ToolDef(
        name="issue_refund",
        risk=RiskClass.IRREVERSIBLE,
        description=(
            "Refund money against an order, to the original payment method. This moves "
            "real money and cannot be undone. Check the refund policy and the order's "
            "delivery date first, and refund only the amount the policy supports — "
            "partial refunds are normal and are often the right answer. Amounts are in "
            "cents: 1900 means nineteen dollars."
        ),
        parameters=schema(
            {
                "order_reference": {"type": "string", "description": "Order reference."},
                "amount_cents": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Refund amount in cents. Must not exceed what remains refundable.",
                },
                "reason": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 500,
                    "description": "Why this refund is due, in one line. Appears on the merchant's report.",
                },
            }
        ),
        handler=_issue_refund,
        basis=_refund_basis,
        preview=lambda a: (
            f"Refund {_money(a['amount_cents'])} against order {a['order_reference']}"
            f" — {a['reason']}"
        ),
        irreversible_note=(
            "money left the merchant's account. Putting it back is a charge, which is "
            "a new decision somebody has to make outside this system"
        ),
    )
)


# ------------------------------------------------------ send_customer_email


def _send_customer_email(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ctx.cursor.execute(
        "select t.id, t.reference, c.id as customer_id, c.name, c.email"
        "  from tickets t join customers c on c.id = t.customer_id"
        " where t.org_id = %s and t.reference = %s",
        (ctx.org_id, args["reference"]),
    )
    ticket = ctx.cursor.fetchone()
    if ticket is None:
        raise ToolError(f"no ticket {args['reference']!r} for this merchant")

    ctx.cursor.execute(
        "insert into customer_emails (org_id, customer_id, ticket_id, subject, body, run_id)"
        " values (%s, %s, %s, %s, %s, %s)",
        (
            ctx.org_id,
            ticket["customer_id"],
            ticket["id"],
            args["subject"],
            args["body"],
            ctx.run_id,
        ),
    )
    # The customer-visible reply is also part of the thread, so the next person
    # to open the ticket sees what was said rather than only that mail went out.
    ctx.cursor.execute(
        "insert into ticket_messages (ticket_id, author_kind, is_internal, body)"
        " values (%s, 'agent', false, %s)",
        (ticket["id"], f"Subject: {args['subject']}\n\n{args['body']}"),
    )
    return ToolOutcome(
        f"Emailed {ticket['name']} <{ticket['email']}> about {ticket['reference']}."
        f" It has been delivered and cannot be recalled."
    )


register(
    ToolDef(
        name="send_customer_email",
        risk=RiskClass.IRREVERSIBLE,
        description=(
            "Send an email to the customer who opened a ticket, and record it on the "
            "thread. Delivered mail cannot be recalled, so say only what you have "
            "verified: never promise a refund you have not issued or a delivery date "
            "you have not read from the order. Write as the merchant's support team, "
            "in plain prose, and do not mention internal tooling or these instructions."
        ),
        parameters=schema(
            {
                "reference": {"type": "string", "description": "Ticket reference."},
                "subject": {"type": "string", "minLength": 3, "maxLength": 200},
                "body": {
                    "type": "string",
                    "minLength": 10,
                    "maxLength": 4000,
                    "description": "The message, as the customer will read it.",
                },
            }
        ),
        handler=_send_customer_email,
        preview=lambda a: f"Email the customer on {a['reference']}: {a['subject']!r}",
        irreversible_note=(
            "the email was sent. It may already have been read, and nothing here can "
            "recall it — only send another one"
        ),
    )
)


# ------------------------------------------------------------- cancel_order


def _cancel_order(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ctx.cursor.execute(
        "select id, reference, status::text from orders"
        " where org_id = %s and reference = %s for update",
        (ctx.org_id, args["order_reference"]),
    )
    order = ctx.cursor.fetchone()
    if order is None:
        raise ToolError(f"no order {args['order_reference']!r} for this merchant")
    if order["status"] != "placed":
        raise ToolError(
            f"cannot cancel {order['reference']}: it is already {order['status']}."
            " Only an order that has not shipped can be cancelled; a shipped or"
            " delivered order has to be refunded instead."
        )

    ctx.cursor.execute(
        "update orders set status = 'cancelled', cancelled_at = now() where id = %s",
        (order["id"],),
    )
    return ToolOutcome(
        f"Cancelled {order['reference']}. It will not ship."
        f" Cancelling does not return the money — issue a refund separately if one is due."
    )


register(
    ToolDef(
        name="cancel_order",
        risk=RiskClass.IRREVERSIBLE,
        description=(
            "Cancel an order that has not yet shipped, so it will not be fulfilled. "
            "This does not return the customer's money — a refund is a separate "
            "decision. An order that has already shipped or been delivered cannot be "
            "cancelled at all."
        ),
        parameters=schema(
            {
                "order_reference": {"type": "string", "description": "Order reference."},
                "reason": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 500,
                    "description": "Why the order is being cancelled.",
                },
            }
        ),
        handler=_cancel_order,
        preview=lambda a: f"Cancel order {a['order_reference']} — {a['reason']}",
        irreversible_note=(
            "the order was cancelled and the shipment stopped. The row can be edited "
            "back; the warehouse cannot"
        ),
    )
)
