"""Model providers: the real one, and a scripted one that needs no key.

Both return the same `ModelReply`, and the runtime cannot tell them apart. That
matters more than it sounds: the durable loop, the approval gate, the bounds
and the evals are all exercised identically whether or not an API key is set,
so the machinery this project is actually about is testable in CI for free.

The mock is **not** a small language model and makes no claim to be. It is a
handful of fixed trajectories chosen by keyword, whose job is to drive the
runtime through its interesting states — including the approval gate and a
crash resume. Every run it produces is tagged `provider=mock` in the API, the
step log, and the run viewer, so a demo can never be mistaken for a model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from deskhand import pricing
from deskhand.config import settings

log = logging.getLogger("deskhand")


@dataclass(frozen=True, slots=True)
class ModelReply:
    # Raw content blocks, stored and replayed verbatim. Thinking blocks in
    # particular must go back to the model unmodified, so nothing here is
    # normalised, summarised, or pruned on the way through.
    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_micros: int
    provider: str
    model: str
    latency_ms: int

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


class Provider(Protocol):
    name: str
    model: str

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...


# --------------------------------------------------------------------- Claude


# Models that reject adaptive thinking and `output_config.effort`.
#
# Keyed on the exact model id rather than matched on a prefix. A prefix rule
# would be shorter and would give the wrong answer for `claude-haiku-5` on the
# day it ships — and the way you find out is a 400 on every call, which is the
# most expensive kind of wrong this file can be. An unlisted model gets the
# current-generation request shape, and adding one here is a one-line change
# with the API's own error message pointing at it.
#
# Established by a real call, not by reading: `python -m evals.live --smoke`.
NO_ADAPTIVE_THINKING = frozenset({"claude-haiku-4-5"})


class ClaudeProvider:
    """The real thing.

    Notes on the request shape, because several of these changed recently and
    the wrong one is a 400 rather than a warning:

    * `thinking` is adaptive. Fixed `budget_tokens` is removed on this model
      family; depth is controlled by `effort` instead.
    * No `temperature`/`top_p`/`top_k` — they are rejected outright.
    * `max_tokens` bounds thinking *plus* the answer, and thinking is on by
      default here, so it is sized with that in mind.
    * The system prompt carries a cache breakpoint. Tools render ahead of it
      and are emitted in a stable order, so the cached prefix survives between
      steps of a run and between runs of the same shape.
    """

    name = "claude"

    def __init__(self, model: str | None = None, effort: str | None = None) -> None:
        import anthropic

        self.model = model or settings.model_id
        self.effort = effort or settings.model_effort
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        # Built as a dict because the SDK's `create` is heavily overloaded and
        # the parameter types are Literal-heavy; assembling here keeps one
        # readable request shape instead of a wall of casts at the call site.
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": settings.max_tokens_per_call,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
        }
        # Adaptive thinking and `output_config.effort` arrived together with the
        # 4.6 family. On a model that predates them each is a 400 on every
        # call — not a warning, not a degraded response — so a model that does
        # not take them gets neither, rather than one and a crash.
        if self.model not in NO_ADAPTIVE_THINKING:
            request["thinking"] = {"type": "adaptive"}
            request["output_config"] = {"effort": self.effort}

        started = time.monotonic()
        response = self._client.messages.create(**request)
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        # `stop_reason` is checked by the caller before it reads content. A
        # safety refusal returns HTTP 200 with an empty or partial content
        # list, so anything that indexes content[0] unconditionally breaks
        # here rather than at the API boundary.
        return ModelReply(
            content=[b.model_dump(exclude_none=True) for b in response.content],
            stop_reason=response.stop_reason or "end_turn",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_micros=pricing.cost_micros(
                self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
        )


# --------------------------------------------------------------------- OpenAI


class OpenAIProvider:
    """A second model behind the same `Provider` protocol, for comparison.

    **The seam was never neutral.** `transcript.rebuild` emits Anthropic content
    blocks, `steps.content` stores them, `loop._unresolved` reads `type ==
    "tool_use"` out of them, and `ModelReply.content` is replayed verbatim. That
    is a reasonable thing for a project that runs on one provider, and it means
    "swap the provider" is really "write an adapter". This class is the adapter,
    and everything it does is translation:

        Anthropic messages  ->  Chat Completions messages   (`_to_openai`)
        Chat Completions reply -> Anthropic content blocks  (`_to_blocks`)

    Nothing downstream can tell. The step log, the approval gate, the ledger and
    the replay view all see the shape they have always seen.

    Two differences from `ClaudeProvider` that are deliberate, and are stated in
    the writeup rather than smoothed over:

    * **No `strict`.** Anthropic and OpenAI accept different subsets of JSON
      Schema in strict mode, and `_api_safe` in tools/base.py strips for
      Anthropic's. Sending that to OpenAI is a coin flip on a 400. The
      constraints are not lost — `ToolDef.validate()` runs the full schema
      locally before anything executes, which is the path a bad argument was
      always meant to take. It does mean invalid-argument counts are not
      comparable between the two providers.
    * **No cached-token accounting.** OpenAI reports cached input, but `Rate`
      models Anthropic's cache economics (a tenth to read, 1.25x to write) and
      OpenAI does not charge to write. Rather than report a number computed with
      the wrong ratio, this reports zero and the comparison quotes billed input.
    """

    name = "openai"

    def __init__(self, model: str | None = None, effort: str | None = None) -> None:
        import openai

        self.model = model or settings.openai_model_id
        # `none`. Not a cost decision — a hard constraint, and one that only a
        # real call surfaces: gpt-5.4-mini refuses function tools together with
        # any other reasoning effort on /v1/chat/completions and tells you to
        # use /v1/responses instead. It happens to make the comparison cleaner,
        # because the Claude side of it runs a model with no thinking either.
        self.effort = effort or settings.openai_reasoning_effort
        self._client = openai.OpenAI(api_key=settings.openai_api_key)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *_to_openai(messages)],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ],
            # Reasoning models bill thinking as output and cap it under this,
            # not under the retired `max_tokens`.
            "max_completion_tokens": settings.max_tokens_per_call,
            "reasoning_effort": self.effort,
        }

        started = time.monotonic()
        response = self._client.chat.completions.create(**request)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0]
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0

        return ModelReply(
            content=_to_blocks(choice.message),
            stop_reason=_stop_reason(choice.finish_reason),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_micros=pricing.cost_micros(
                self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
        )


# OpenAI's finish reasons, mapped onto the vocabulary the loop already reads.
# `content_filter` becomes `refusal` so it takes the path a safety decline takes
# — the run ends `model_refusal` rather than being read as an empty answer.
_FINISH_REASONS = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _stop_reason(finish_reason: str | None) -> str:
    return _FINISH_REASONS.get(finish_reason or "stop", "end_turn")


def _to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped messages to Chat Completions messages.

    The shapes disagree in one structural way rather than many cosmetic ones.
    Anthropic puts tool results in a *user* message as `tool_result` blocks, and
    a turn that resolved three calls is one message with three blocks. Chat
    Completions wants one `role: "tool"` message per result. So a single message
    can fan out into several, and the order has to survive it: a tool message
    must follow the assistant message carrying the call it answers, or the API
    rejects the conversation.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue

        if message["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            # An assistant turn with neither text nor calls is not a legal
            # message. It also cannot happen: the loop ends a run whose turn had
            # no tool calls, so a turn that is still in the history had one.
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue

        # A user turn: tool results, or the opening prompt.
        text_parts = []
        for block in content:
            if block.get("type") == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = "".join(b.get("text", "") for b in body if b.get("type") == "text")
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        # `is_error` has no home in this shape. The text already
                        # reads as a failure — it is the message a ToolError
                        # carried — so the model still sees what went wrong; it
                        # just is not flagged as structurally an error the way
                        # Anthropic flags it. Noted because it is a real
                        # difference in what the two models are shown.
                        "content": str(body),
                    }
                )
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        if text_parts:
            out.append({"role": "user", "content": "\n".join(text_parts)})
    return out


def _to_blocks(message: Any) -> list[dict[str, Any]]:
    """A Chat Completions reply back into Anthropic content blocks.

    This is the half that has to be right, because whatever it returns is
    written into `steps.content` and every later read of that run — the resume,
    the replay, the compensation plan's step numbers — is a read of these
    blocks.

    `arguments` arrives as a JSON *string* and the model is not obliged to make
    it parse. A tool call whose arguments are not JSON is handed on with an
    empty input, which the schema then rejects, which the agent reads as a
    ToolError and can correct. That is the same route a well-formed but invalid
    argument takes, and it is a great deal better than an exception out of the
    provider taking down a run that may already have moved money.
    """
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            log.warning("model returned unparseable arguments for %s", call.function.name)
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.function.name,
                "input": arguments,
            }
        )
    return blocks


# -------------------------------------------------------------------- Scripted


@dataclass
class ScriptedProvider:
    """Replays a fixed list of turns. The workhorse of the test suite.

    Statelessness is the requirement, not a simplification. A resumed run
    rebuilds its message history from the step log and asks the provider for
    the next turn; if the provider held a private counter, resuming would
    return the wrong turn and the crash-resume tests would pass for the wrong
    reason. So the turn index is *derived* from the history it is given.

    Which makes a script *positional*, and that catches people out. Driving one
    run twice with two different scripts does not start the second script at
    its own first entry: the run already has assistant turns on it, and the
    index lands wherever that history says. A second drive has to carry the
    turns already taken —

        provider(script=[*FIRST_SCRIPT, [call("...")], text("...")])

    — or it silently serves the wrong turn and the test fails somewhere else.
    """

    script: list[list[dict[str, Any]]]
    name: str = "mock"
    model: str = "mock"

    @staticmethod
    def turn_index(messages: list[dict[str, Any]]) -> int:
        return sum(1 for m in messages if m.get("role") == "assistant")

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        index = self.turn_index(messages)

        if index < len(self.script):
            blocks = [dict(b) for b in self.script[index]]
        else:
            blocks = [{"type": "text", "text": "Nothing further to do."}]

        # Deterministic ids. A uuid here would break replay: the tool_use id is
        # what an approval is tied to, and a resumed run must produce the same
        # one or the human's decision would no longer match anything.
        for position, block in enumerate(blocks):
            if block.get("type") == "tool_use":
                block.setdefault("id", f"toolu_mock_{index}_{position}")

        has_tools = any(b.get("type") == "tool_use" for b in blocks)
        return ModelReply(
            content=blocks,
            stop_reason="tool_use" if has_tools else "end_turn",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_micros=0,
            provider=self.name,
            model=self.model,
            latency_ms=0,
        )


def text(body: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": body}]


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "name": name, "input": args}


# ------------------------------------------------------- the keyless default

# Order references are four digits (NW-1042); ticket references are one or two
# (NW-1). Crude, and adequate for a fixture-driven demo — the mock's job is to
# reach the interesting states, not to parse English.
_ORDER_REF = re.compile(r"\b([A-Z]{2}-\d{3,})\b")
_TICKET_REF = re.compile(r"\b([A-Z]{2}-\d{1,2})\b")
_TOTAL = re.compile(r"total: ([\d,]+)\.(\d{2}) ")


def _brief(messages: list[dict[str, Any]]) -> str:
    """The opening prompt plus the first tool result, and nothing after it.

    Deliberately *not* the whole conversation. The plan below is recomputed
    from scratch on every turn — it has to be, because the provider is
    stateless so that a resumed run reaches the same decision — and reading the
    growing transcript made that recomputation unstable: the agent would set
    off down the "where is my order" path, a knowledge-base search would return
    an article that happens to contain the word *refund*, and the next turn
    would decide it had been working a refund all along.

    That is not a hypothetical. It happened, and produced a demo in which the
    agent asked to refund a customer who only wanted a tracking number. The
    ticket is what the plan is about, so the plan reads the ticket and stops.
    """
    parts: list[str] = []
    seen_result = False
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in content or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_result" and not seen_result:
                inner = block.get("content")
                parts.append(inner if isinstance(inner, str) else str(inner))
                seen_result = True
        if seen_result:
            break
    return "\n".join(parts)


class DefaultMockProvider(ScriptedProvider):
    """The trajectory used when there is no API key and no explicit script.

    It picks one of three shapes from the ticket text and fills in references
    and amounts by reading them back out of earlier tool results. That is
    enough to walk the runtime through a full run — including suspending on an
    irreversible call and resuming after a human decides — with no key and no
    network.
    """

    def __init__(self) -> None:
        super().__init__(script=[])

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        self.script = self._plan(messages)
        return super().complete(system, messages, tools)

    def _plan(self, messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        seen = _brief(messages)
        ticket = _TICKET_REF.search(seen)
        ticket_ref = ticket.group(1) if ticket else "NW-1"

        wants_refund = any(
            word in seen.lower() for word in ("refund", "charged twice", "money back")
        )

        plan: list[list[dict[str, Any]]] = [[call("get_ticket", reference=ticket_ref)]]

        if not wants_refund:
            plan += [
                [call("search_kb", query="shipping times tracking delay")],
                [
                    call(
                        "add_internal_note",
                        reference=ticket_ref,
                        body=(
                            "Checked the knowledge base: this is inside the published "
                            "turnaround, so no action is due yet."
                        ),
                    )
                ],
                [call("set_ticket_status", reference=ticket_ref, status="pending")],
                text(
                    f"{ticket_ref} is within the published turnaround. I left an internal "
                    "note and moved it to pending."
                ),
            ]
            return plan

        order = _ORDER_REF.search(seen)
        order_ref = order.group(1) if order else None
        if order_ref is None:
            plan += [
                [call("search_kb", query="refund policy window")],
                text("I could not find an order reference on this ticket."),
            ]
            return plan

        total = _TOTAL.search(seen)
        amount = int(total.group(1).replace(",", "")) * 100 + int(total.group(2)) if total else 1900

        plan += [
            [call("get_order", reference=order_ref)],
            [call("search_kb", query="refund policy window delivered")],
            [
                call(
                    "issue_refund",
                    order_reference=order_ref,
                    amount_cents=amount,
                    reason="Quality complaint inside the published refund window.",
                )
            ],
            [
                call(
                    "add_internal_note",
                    reference=ticket_ref,
                    body=f"Refund processed against {order_ref} after human approval.",
                )
            ],
            [call("set_ticket_status", reference=ticket_ref, status="resolved")],
            text(f"Refunded {order_ref} and resolved {ticket_ref}."),
        ]
        return plan


def get_provider() -> Provider:
    """The provider this process will use.

    Falls back to the mock rather than failing, because running keyless is a
    supported mode — but the choice is logged and surfaced on every run, so it
    is never a silent substitution.
    """
    if settings.has_model_key:
        return ClaudeProvider()
    log.warning("no ANTHROPIC_API_KEY — using the scripted mock provider")
    return DefaultMockProvider()
