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
    model_id: str = "claude-opus-5"
    # low | medium | high | xhigh | max. `high` is the API default; agentic work
    # is the case where xhigh earns its cost, so it is worth sweeping per route.
    model_effort: str = "high"
    # A hard ceiling on tokens per model call. On Opus 5 thinking is on by
    # default and counts against this, so it is sized for thinking + answer.
    max_tokens_per_call: int = 8192

    # --- Per-run bounds ---
    max_steps_per_run: int = 24
    max_tokens_per_run: int = 400_000
    max_wallclock_seconds_per_run: int = 900
    max_spend_usd_per_run: Decimal = Decimal("2.00")
    loop_detection_threshold: int = 3

    # --- Spend ceilings ---
    daily_budget_usd_per_org: Decimal = Decimal("10.00")
    # Per-org caps bound one tenant. They bound the bill only if tenants are
    # scarce, and signup is open, so this is the number that actually caps
    # what the deployment can spend in a day.
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

    # --- Observability ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def has_model_key(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_tracing(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


settings = Settings()
