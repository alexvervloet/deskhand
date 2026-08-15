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

import logging
import re
import time
from dataclasses import dataclass, field
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
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "tools": tools,
            "messages": messages,
        }

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


# -------------------------------------------------------------------- Scripted


@dataclass
class ScriptedProvider:
    """Replays a fixed list of turns. The workhorse of the test suite.

    Statelessness is the requirement, not a simplification. A resumed run
    rebuilds its message history from the step log and asks the provider for
    the next turn; if the provider held a private counter, resuming would
    return the wrong turn and the crash-resume tests would pass for the wrong
    reason. So the turn index is *derived* from the history it is given.
    """

    script: list[list[dict[str, Any]]]
    name: str = "mock"
    model: str = "mock"
    calls: list[int] = field(default_factory=list)

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
        self.calls.append(index)

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
        amount = (
            int(total.group(1).replace(",", "")) * 100 + int(total.group(2))
            if total
            else 1900
        )

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
