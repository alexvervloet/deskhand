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
    """

    org_id: str
    run_id: str
    step_id: str
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
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
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
