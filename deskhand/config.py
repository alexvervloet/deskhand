"""Configuration, loaded from the environment with working defaults.

Every setting has a default that runs, so a fresh clone with no .env starts
and its tests pass. The one thing that changes behaviour by its absence is
ANTHROPIC_API_KEY: without it the runtime uses the scripted mock provider,
and says so loudly rather than pretending to be a model.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://deskhand:deskhand@localhost:5437/deskhand"

    # --- Model ---
    anthropic_api_key: str | None = None
    # Sonnet 5 rather than an Opus tier. This is a demo on a public URL doing
    # short tool-calling turns over six seeded tickets, which is not work that
    # repays the most capable model; Opus is one environment variable away if a
    # route ever turns out to need it. The request shape below is unchanged by
    # the choice — both take adaptive thinking and `effort`.
    model_id: str = "claude-sonnet-5"
    # low | medium | high | xhigh | max. `high` is the API default; agentic work
    # is the case where xhigh earns its cost, so it is worth sweeping per route.
    model_effort: str = "high"
    # A hard ceiling on tokens per model call. Thinking is adaptive on this
    # model family and counts against this, so it is sized for thinking +
    # answer rather than for the answer alone.
    max_tokens_per_call: int = 8192

    # --- Per-run bounds ---
    max_steps_per_run: int = 24
    max_tokens_per_run: int = 400_000
    max_wallclock_seconds_per_run: int = 900
    max_spend_usd_per_run: Decimal = Decimal("2.00")
    loop_detection_threshold: int = 3

    # --- Payout ceilings ---
    # What the agent may hand back to customers, as opposed to what it costs to
    # run. These are the only bounds in this file denominated in the merchant's
    # money rather than ours, and they are the ones that matter: a runaway run
    # was always capped at a couple of dollars of inference, while the amount it
    # could refund was capped only by a human reading approval screens.
    # Cents, because money here is integer cents everywhere and a Decimal of
    # dollars in this one place would invite a float on the way to a comparison.
    max_refund_cents_per_run: int = 100_000
    daily_refund_cents_per_org: int = 500_000

    # --- Spend ceilings ---
    daily_budget_usd_per_org: Decimal = Decimal("10.00")
    # Per-org caps bound one tenant, so they only bound the bill if the number
    # of tenants is bounded too. This is the number that actually caps what the
    # deployment can spend in a day, whatever the tenant count turns out to be.
    platform_daily_budget_usd: Decimal = Decimal("50.00")

    # --- Approvals ---
    approval_ttl_seconds: int = 86_400

    # --- Deployment ---
    # Behind a proxy the socket peer is the proxy, so the login throttle would
    # see every visitor as one caller. Name the header the proxy sets *and
    # overwrites* (Fly-Client-IP, X-Real-IP) — never one a client can forge.
    client_ip_header: str | None = None
    # Run the agent inside the API process instead of as its own service. Wrong
    # for production, where they scale and fail independently; right for a demo
    # machine that should be allowed to sleep when nobody is looking at it.
    run_worker_inline: bool = False

    # No observability settings, deliberately. The step log is the trace —
    # every model and tool call is a row with tokens, cost, latency, arguments
    # and result — and deskhand/tracing.py emits a structured line per event for
    # whatever collects your logs. Neither needs configuring.

    @property
    def has_model_key(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
