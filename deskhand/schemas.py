"""Request and response shapes.

Kept separate from the handlers so the API's surface can be read in one sitting.
Money crosses this boundary as integer cents and never as a float; a formatted
string is provided alongside for display, so no client has to reinvent the
rounding.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # Plain str, not EmailStr: the address is looked up case-insensitively
    # against the users table, so format validation would reject nothing that
    # the lookup does not already reject, in exchange for a dependency.
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: datetime
    user: MeResponse


class MeResponse(BaseModel):
    id: str
    email: str
    role: str
    org_id: str
    org_slug: str
    org_name: str
    # Whether this person may approve an irreversible action. Sent explicitly
    # rather than inferred from the role string, so the UI never has to encode
    # the permission rule a second time and get it subtly wrong.
    can_approve: bool


class TicketSummary(BaseModel):
    id: str
    reference: str
    subject: str
    status: str
    priority: str
    tags: list[str]
    customer_name: str
    customer_email: str
    created_at: datetime
    open_run_id: str | None = None


class TicketMessage(BaseModel):
    author_kind: str
    is_internal: bool
    body: str
    created_at: datetime


class TicketDetail(TicketSummary):
    messages: list[TicketMessage]


class StartRunRequest(BaseModel):
    ticket_reference: str


class StepView(BaseModel):
    seq: int
    kind: str
    tool_name: str | None
    content: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_micros: int
    cost_display: str
    latency_ms: int
    created_at: datetime


class RunSummary(BaseModel):
    id: str
    ticket_id: str
    ticket_reference: str
    status: str
    stop_reason: str | None
    stop_detail: str | None
    provider: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_micros: int
    cost_display: str
    attempt: int
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    prompt: str
    max_steps: int
    max_tokens: int
    max_spend_micros: int
    deadline_at: datetime
    steps: list[StepView]
    approvals: list[ApprovalView]


class ApprovalView(BaseModel):
    id: str
    run_id: str
    ticket_reference: str | None = None
    tool_name: str
    # What executing this will actually do, in one line, rendered from the
    # arguments the model supplied. This is the sentence a human approves.
    preview: str
    args: dict[str, Any]
    status: str
    reason: str | None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


class DecideRequest(BaseModel):
    decision: str = Field(pattern="^(approved|denied)$")
    # Fed back to the agent as the tool's result on a denial, so it can adapt
    # rather than stalling. Worth insisting on in the UI.
    reason: str | None = Field(default=None, max_length=500)


class UsageResponse(BaseModel):
    org_spend_today_micros: int
    org_spend_today_display: str
    org_daily_budget_micros: int
    platform_spend_today_micros: int
    platform_daily_budget_micros: int
    runs_today: int
    refunds_today_cents: int
    refunds_today_display: str


LoginResponse.model_rebuild()
RunDetail.model_rebuild()
