"""Per-call rules that can make a call harder to run, and never easier.

The registry gives every tool a fixed floor: irreversible tools ask a person,
everything else runs. That floor is the security model, and nothing here
changes it. What this module adds is a second opinion per call, made with the
call's arguments and what the run has seen so far, and allowed exactly one
direction:

    RUN  <  ASK  <  DENY

A call's ruling is the strictest of the floor and every rule. A rule that says
RUN about `issue_refund` is outvoted by the floor, by construction, so a rule
can be wrong in the permissive direction without costing anything. That one
property is what makes it safe for a rule to read untrusted text at all.

It matters because of the shape the field has converged on. Hosted agent
platforms now offer an `auto` permission mode in which something evaluates
each call in context and runs it, denies it, or pauses for a person. Pointed at
an agent that reads customer tickets, an evaluator like that is one more thing
a ticket can talk to: if it is allowed to say "this one's fine", a persuasive
enough ticket gets it to. Here a rule can only ever ask for more oversight, so
the worst a ticket achieves by fooling one is an extra click for a person.

Rules are plain functions, registered in a tuple at import time, the same way
the registry is. No model is involved in deciding.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import psycopg
from psycopg.rows import DictRow

from deskhand.tools import RiskClass, args_hash, get, requires_approval


class Verdict(IntEnum):
    RUN = 0
    ASK = 1
    DENY = 2


@dataclass(frozen=True, slots=True)
class Ruling:
    verdict: Verdict
    # None when the ruling is the registry's own floor. Otherwise the name of
    # the rule that raised it, which the approval card and the audit log show.
    rule: str | None
    reason: str


Rule = Callable[[psycopg.Cursor[DictRow], str, str, dict[str, Any], str], Ruling | None]


def floor(name: str) -> Ruling:
    if requires_approval(name):
        return Ruling(Verdict.ASK, None, f"{name} is irreversible")
    return Ruling(Verdict.RUN, None, f"{name} is {get(name).risk}")


# ------------------------------------------------------------------- rules

# Text in a tool result that is addressed to the agent rather than written
# about the ticket: a role line, the classic opener, or a forged copy of the
# fence. Deliberately a text match. A customer can write any of these on
# purpose, or by accident, and all it buys them is a person reviewing the run's
# writes. A rule that could relax something could never be this careless.
_ADDRESSED_TO_THE_AGENT = re.compile(
    r"^\s*SYSTEM\b[^\n:]{0,20}:|ignore (?:all )?(?:previous|prior|above) instructions"
    r"|<<</?untrusted:",
    re.IGNORECASE | re.MULTILINE,
)


def after_text_addressed_to_the_agent(
    cur: psycopg.Cursor[DictRow], run_id: str, name: str, args: dict[str, Any], tool_use_id: str
) -> Ruling | None:
    """Once a run has read an instruction aimed at it, its writes need a person.

    The fence and the registry already stop an injected instruction from moving
    money. They don't stop it from closing the ticket, reassigning it, or
    tagging it away from the queue a person watches, because those are
    reversible and run freely. Reversible means a compensation can put the
    value back later, not that nobody acted on it in the meantime: a ticket
    marked resolved for six hours was in the resolved queue for six hours.

    Reads are left alone. The run has to keep reading to do its job, and an
    escalated read would stall it without protecting anything.

    Irreversible calls are included even though the floor already asks about
    them, so while the registry is intact this changes nothing for them. It
    makes the rule an independent layer rather than a decoration on the gate:
    delete the gate and an injected instruction still can't move money,
    because this rule asks about the refund on its own.
    """
    if get(name).risk is RiskClass.READ:
        return None
    cur.execute(
        "select content->>'result' as result from steps where run_id = %s and kind = 'tool_result'",
        (run_id,),
    )
    for row in cur.fetchall():
        if row["result"] and _ADDRESSED_TO_THE_AGENT.search(row["result"]):
            return Ruling(
                Verdict.ASK,
                "after-text-addressed-to-the-agent",
                "this run read text addressed to the agent, so its writes need a person",
            )
    return None


def already_declined(
    cur: psycopg.Cursor[DictRow], run_id: str, name: str, args: dict[str, Any], tool_use_id: str
) -> Ruling | None:
    """A call a person already declined in this run is refused without asking.

    The denial reaches the model with "do not retry the same action", and
    models retry anyway. Asking the same person the same question twice turns
    the gate into something people click through. The match is on the argument
    hash, so a changed proposal, a smaller refund say, still goes to a person.

    The call that was declined is excluded by its tool_use id: on resume, that
    call is still settled through the approval it already has, and its denial
    reaches the model with the person's reason.
    """
    cur.execute(
        "select 1 from approvals where run_id = %s and tool_name = %s and args_hash = %s"
        " and status = 'denied' and tool_use_id <> %s limit 1",
        (run_id, name, args_hash(name, args), tool_use_id),
    )
    if cur.fetchone() is None:
        return None
    return Ruling(
        Verdict.DENY,
        "already-declined",
        "a person already declined this exact call in this run",
    )


RULES: tuple[Rule, ...] = (after_text_addressed_to_the_agent, already_declined)


def evaluate(
    cur: psycopg.Cursor[DictRow],
    run_id: str,
    name: str,
    args: dict[str, Any],
    tool_use_id: str,
    rules: tuple[Rule, ...] = RULES,
) -> Ruling:
    """The strictest of the registry's floor and every rule's opinion.

    `max` is the whole guarantee. Ties keep the earlier ruling, so the floor
    wins a tie and the card says "irreversible" rather than naming a rule that
    changed nothing.
    """
    ruling = floor(name)
    for rule in rules:
        opinion = rule(cur, run_id, name, args, tool_use_id)
        if opinion is not None and opinion.verdict > ruling.verdict:
            ruling = opinion
    return ruling
