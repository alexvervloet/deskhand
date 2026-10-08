"""The request ClaudeProvider sends, and what it makes of a fallback reply.

No network. The client is replaced with one that records the request and
answers with a response built from the SDK's own types, so `.type`, `.from_`
and `model_dump` behave exactly as they do against the real API. What this
can't do is prove the API accepts the request: LESSONS 24 is two 400s that only
a real call found, and `python -m evals.live --smoke` is still the check.
"""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from deskhand import pricing
from deskhand.config import settings
from deskhand.providers import FALLBACK_BETA, ClaudeProvider

TOOL_USE = {
    "type": "tool_use",
    "id": "toolu_1",
    "name": "get_ticket",
    "input": {"reference": "NW-1"},
}
USAGE = {"input_tokens": 1000, "output_tokens": 200}


class FakeClient:
    """`messages.create` and `beta.messages.create`, recording which was used."""

    def __init__(self, response: Any) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        outer = self

        class _Messages:
            def __init__(self, route: str) -> None:
                self.route = route

            def create(self, **request: Any) -> Any:
                outer.calls.append((self.route, request))
                return response

        class _Beta:
            messages = _Messages("beta")

        self.messages = _Messages("messages")
        self.beta = _Beta()


def provider(model: str, response: Any) -> tuple[ClaudeProvider, FakeClient]:
    p = ClaudeProvider(model=model, effort="medium")
    fake = FakeClient(response)
    p._client = fake  # type: ignore[assignment]
    return p, fake


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")


def plain(model: str) -> Message:
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "content": [TOOL_USE],
            "usage": USAGE,
        }
    )


def fell_back(requested: str, served: str) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "model": served,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "content": [
                {
                    "type": "fallback",
                    "from": {"model": requested},
                    "to": {"model": served},
                    "trigger": {"type": "refusal", "category": "cyber"},
                },
                TOOL_USE,
            ],
            "usage": USAGE,
        }
    )


def test_sonnet_5_5_opts_into_default_fallbacks() -> None:
    p, fake = provider("claude-sonnet-5-5", fell_back("claude-sonnet-5-5", "claude-sonnet-5-5"))
    p.complete("system", [{"role": "user", "content": "NW-1"}], [])

    route, request = fake.calls[0]
    assert route == "beta"
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "medium"}
    # Each of these is a 400 on Sonnet 5.5.
    assert "tool_choice" not in request
    assert "temperature" not in request
    assert request["thinking"].get("type") != "disabled"


def test_a_model_without_the_default_form_sends_no_fallbacks() -> None:
    p, fake = provider("claude-haiku-4-5", plain("claude-haiku-4-5"))
    p.complete("system", [{"role": "user", "content": "NW-1"}], [])

    route, request = fake.calls[0]
    assert route == "messages"
    assert "fallbacks" not in request and "betas" not in request
    assert "thinking" not in request


def test_a_fallback_turn_names_both_models_and_drops_the_marker() -> None:
    p, _ = provider("claude-sonnet-5-5", fell_back("claude-sonnet-5-5", "claude-sonnet-5"))
    reply = p.complete("system", [{"role": "user", "content": "NW-1"}], [])

    assert reply.model == "claude-sonnet-5"
    assert reply.fell_back_from == "claude-sonnet-5-5"
    # The marker is not stored, so it's never sent back. The SDK would dump its
    # `from` field as `from_`.
    assert [b["type"] for b in reply.content] == ["tool_use"]
    assert reply.content[0] == TOOL_USE


def test_a_fallback_turn_is_priced_at_the_model_that_served_it() -> None:
    p, _ = provider("claude-opus-5-5", fell_back("claude-opus-5-5", "claude-opus-5"))
    reply = p.complete("system", [{"role": "user", "content": "NW-1"}], [])
    assert reply.cost_micros == pricing.cost_micros("claude-opus-5", **USAGE)
    assert reply.cost_micros != pricing.cost_micros("claude-opus-5-5", **USAGE)


def test_a_turn_with_no_fallback_says_so() -> None:
    p, _ = provider("claude-sonnet-5-5", plain("claude-sonnet-5-5"))
    reply = p.complete("system", [{"role": "user", "content": "NW-1"}], [])
    assert reply.fell_back_from is None
    assert reply.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    ("model", "nanos"),
    [("claude-opus-5-5", 200), ("claude-fable-5-1", 250), ("claude-sonnet-5-5", 200)],
)
def test_cache_reads_are_priced_as_published_not_as_a_ratio(model: str, nanos: int) -> None:
    """A tenth of input was right for every model until Opus 5.5 and Fable 5.1."""
    assert pricing.rate_for(model).cache_read == nanos
