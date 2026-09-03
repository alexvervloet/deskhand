"""Tool definitions, the registry, and the one rule the registry exists for.

**A tool's risk class is declared here and can never be changed at runtime.**

That sentence is the entire security model of this module. The class is a field
on a frozen dataclass, looked up by name from a dict that is populated at import
time and never written to again. Nothing in a model response, a tool argument,
or a tool *result* can reach it. This matters because tool results are the one
place attacker-controlled text enters the loop wearing the costume of trusted
data — a ticket body that says "this refund is pre-approved" is a string, and
strings do not get a vote on whether `issue_refund` needs a human.

The classes:

    read          no side effects, runs freely
    reversible    changes state, runs freely, records its own inverse
    irreversible  suspends the run until a human approves this exact call

"Records its own inverse" is exactly what it says: the undo is captured, not
wired up. See the note in deskhand/tools/reversible.py.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import jsonschema
import psycopg
from psycopg.rows import DictRow


class RiskClass(StrEnum):
    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to.

    Raised for bad arguments, missing records, and policy violations — the
    ordinary failures of doing the job. It becomes an `is_error` tool result,
    not a crashed run: the agent is expected to read it and try something else.
    """


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a handler is allowed to know.

    Note what is absent: the conversation, the model's reasoning, and the text
    of the ticket that triggered the run. A handler acts on its arguments and
    the database, which keeps the blast radius of a bad argument to the
    argument itself.

    `ticket_id` and `customer_id` are the run's *subject*, and they are here so
    that a read tool can scope to it. `org_id` alone is a tenancy boundary, not
    a need-to-know one: it says the agent may not read another merchant's data
    and says nothing about whether a run working one customer's ticket may read
    a different customer's history. Handlers that answer questions about a
    person compare against `customer_id` rather than trusting an argument that
    ultimately came from a model reading an untrusted ticket.
    """

    org_id: str
    run_id: str
    step_id: str
    ticket_id: str
    customer_id: str
    # Parameterised on DictRow because the pool sets row_factory=dict_row:
    # handlers index rows by column name, and the annotation is what lets the
    # type checker agree that they may.
    cursor: psycopg.Cursor[DictRow]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a handler returns.

    `result` is the text the model sees. `inverse` is the compensating action,
    captured now rather than reconstructed later — the prior value of whatever
    was overwritten is knowable at write time and expensive to guess afterwards.
    """

    result: str
    inverse: dict[str, Any] | None = None


Handler = Callable[[ToolContext, dict[str, Any]], ToolOutcome]


# Constraint keywords the Messages API refuses inside a `strict` tool schema.
# Strict mode accepts a restricted subset of JSON Schema: it guarantees the
# *shape* of the arguments — types, required keys, no extra properties — and
# declines to police their range. Sending one is a 400 on every model call, and
# it is a 400 the scripted provider cannot produce, so the whole test suite and
# the whole keyless demo stay green while the real path is broken.
#
# Established by probing the API rather than by reading one error message and
# guessing its neighbours: `minLength`, `maxLength`, `pattern`, `format`, `enum`
# and `minItems` are all accepted. `exclusiveMaximum` is stripped with its
# siblings without having been probed; if that is wrong the cost is local-only
# enforcement, which is where every keyword here ends up anyway.
_NUMERIC_REJECTS = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
)
_STRICT_REJECTS: dict[str, frozenset[str]] = {
    "integer": _NUMERIC_REJECTS,
    "number": _NUMERIC_REJECTS,
    "array": frozenset({"maxItems"}),
}


def _api_safe(schema: Any) -> Any:
    """A copy of `schema` with the keywords strict mode refuses removed.

    The constraints are not lost, only moved. `validate()` still runs the full
    schema locally — before an approval is rendered for a human, and again
    inside the savepoint in `invoke` — so a model proposing `amount_cents: 0`
    gets a `ToolError` it can read and correct. That is the path a bad argument
    was always meant to take. What changes is that the API no longer refuses it
    on the model's behalf, so the refusal arrives one turn later and costs a
    step.

    Only the copy handed to the API is stripped. `self.parameters` keeps every
    keyword, because it is the thing that still enforces them.
    """
    if isinstance(schema, list):
        return [_api_safe(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    # Keyed on this node's own `type`, so a property that happens to be *named*
    # "minimum" is untouched: the dict under `properties` has no `type` of its
    # own and therefore rejects nothing.
    rejected = _STRICT_REJECTS.get(schema.get("type", ""), frozenset())
    return {key: _api_safe(value) for key, value in schema.items() if key not in rejected}


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    risk: RiskClass
    description: str
    # JSON Schema for the arguments. Always an object with
    # additionalProperties: false, so an unexpected key is a validation error
    # rather than a silently ignored one.
    parameters: dict[str, Any]
    handler: Handler
    # Human-readable summary of what executing this will do, rendered on the
    # approval screen. Takes the validated arguments.
    preview: Callable[[dict[str, Any]], str] | None = None

    def validate(self, args: dict[str, Any]) -> None:
        try:
            jsonschema.validate(args, self.parameters)
        except jsonschema.ValidationError as exc:
            raise ToolError(f"invalid arguments for {self.name}: {exc.message}") from exc

    def api_schema(self) -> dict[str, Any]:
        """The tool definition as the Messages API wants it.

        `strict` makes the API guarantee the arguments validate against the
        schema, which removes a whole category of defensive parsing from the
        handlers. It requires additionalProperties: false and an explicit
        `required` list, which the schemas here always have.

        It also accepts only part of JSON Schema, so the schema is stripped to
        the subset it will take. See `_api_safe`: the range constraints stay in
        `self.parameters` and are still enforced by `validate()`.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _api_safe(self.parameters),
            "strict": True,
        }


_REGISTRY: dict[str, ToolDef] = {}


def register(tool: ToolDef) -> ToolDef:
    if tool.name in _REGISTRY:
        raise RuntimeError(f"tool {tool.name!r} is already registered")
    if tool.parameters.get("additionalProperties") is not False:
        raise RuntimeError(f"tool {tool.name!r} must set additionalProperties: false")
    if "required" not in tool.parameters:
        raise RuntimeError(f"tool {tool.name!r} must declare `required`")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> ToolDef:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ToolError(f"no such tool: {name!r}") from None


def is_registered(name: str) -> bool:
    """Whether this name is a tool at all.

    `get` raises for an unknown name and `requires_approval` inherits that,
    which is right for every caller that has already established the tool
    exists. The loop has not: the name came from a model, and a model can ask
    for a tool that was never registered. That is the model's mistake to
    correct, not a reason to end a run, so the loop needs to be able to ask the
    question without being thrown out of.
    """
    return name in _REGISTRY


def all_tools() -> list[ToolDef]:
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def api_schemas() -> list[dict[str, Any]]:
    """Tool definitions for the model, in a stable order.

    Sorted by name so the serialised tool block is byte-identical between
    requests. Tools render first in the prompt, so any reordering would
    invalidate the entire prompt cache on every call.
    """
    return [t.api_schema() for t in all_tools()]


def requires_approval(name: str) -> bool:
    """The only question the runtime asks about a tool before running it.

    Answered from the registry, by name. Not from the model's request, not
    from an argument, and never from a previous tool's output.
    """
    return get(name).risk is RiskClass.IRREVERSIBLE


def args_hash(name: str, args: dict[str, Any]) -> str:
    """A stable fingerprint of "this exact call".

    An approval is bound to this value, so a human who approves a $19 refund
    has not approved a $1,900 one. Keys are sorted so that two dicts that are
    equal hash equally regardless of construction order.
    """
    payload = json.dumps(
        {"tool": name, "args": args}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()
