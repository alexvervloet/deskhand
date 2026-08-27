"""What a model call costs, in integers.

Rates are nanodollars per token. That unit is not arbitrary: published pricing
is dollars per million tokens, and dollars-per-million maps to *micro*dollars
per token exactly — USD 5.00/MTok is 5 micros a token. Cache reads are a tenth
of that and cache writes are 1.25x, which is where whole micros stop being
enough, so everything is held one thousand times finer and rounded once, at the
end, when a step's cost is recorded.

No float touches a price in arithmetic. A run's spend cap is compared against
integers, so "did this run exceed its budget" has exactly one answer. The only
float in this module is in `format_usd`, which produces a string for a human to
read and is never fed back into a comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

NANOS_PER_MICRO = 1_000


@dataclass(frozen=True, slots=True)
class Rate:
    """Nanodollars per token."""

    input: int
    output: int

    @property
    def cache_read(self) -> int:
        """Cached input is billed at roughly a tenth of the input rate."""
        return self.input // 10

    @property
    def cache_write(self) -> int:
        """Writing to the cache carries a 1.25x premium over plain input."""
        return self.input * 125 // 100


# Dollars per million tokens x 1000 == nanodollars per token.
# Source: the pricing table in the claude-api skill, cached 2026-06-24.
RATES: dict[str, Rate] = {
    "claude-opus-5": Rate(input=5_000, output=25_000),
    "claude-opus-4-8": Rate(input=5_000, output=25_000),
    "claude-fable-5": Rate(input=10_000, output=50_000),
    "claude-sonnet-5": Rate(input=3_000, output=15_000),
    "claude-sonnet-4-6": Rate(input=3_000, output=15_000),
    "claude-haiku-4-5": Rate(input=1_000, output=5_000),
    # The scripted provider spends nothing. Naming it here rather than
    # special-casing at the call site keeps the accounting path identical
    # whether or not a key is set.
    "mock": Rate(input=0, output=0),
}


class UnknownModel(KeyError):
    """Raised rather than guessing. A model whose price is unknown cannot be
    charged against a spend cap, and silently costing zero would turn the cap
    into decoration."""


def rate_for(model: str) -> Rate:
    try:
        return RATES[model]
    except KeyError:
        raise UnknownModel(
            f"no published rate for {model!r}; add it to deskhand/pricing.py before"
            " running against it, or the per-run and per-org spend caps cannot be"
            " enforced"
        ) from None


def cost_micros(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Cost of one model call in microdollars, rounded half-up once.

    `input_tokens` from the API is already the *uncached* remainder, so the
    three input figures are added rather than netted.
    """
    rate = rate_for(model)
    nanos = (
        input_tokens * rate.input
        + output_tokens * rate.output
        + cache_read_tokens * rate.cache_read
        + cache_write_tokens * rate.cache_write
    )
    return (nanos + NANOS_PER_MICRO // 2) // NANOS_PER_MICRO


def format_usd(micros: int) -> str:
    return f"${micros / 1_000_000:,.4f}".rstrip("0").rstrip(".")
