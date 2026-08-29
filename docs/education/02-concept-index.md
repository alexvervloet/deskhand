# Concept index

Where each idea lives in the code. Ordered by how likely you are to want it.

## The loop and the state machine

| Concept | Where |
|---|---|
| The loop itself | [runtime/loop.py](../../deskhand/runtime/loop.py) `advance()` |
| "What should happen next?" — the resume point | `_unresolved()` in the same file |
| Rebuilding the conversation from rows | [runtime/transcript.py](../../deskhand/runtime/transcript.py) `rebuild()` |
| Run states and what each means | [migrations/0004_runs.sql](../../migrations/0004_runs.sql), `run_status` |
| Why it stopped — the stop-reason vocabulary | [runtime/runs.py](../../deskhand/runtime/runs.py), the `STOP_*` constants |
| The system prompt | `SYSTEM_PROMPT` in [runtime/loop.py](../../deskhand/runtime/loop.py) |

## Durability

| Concept | Where |
|---|---|
| The append-only step log | `steps` table in [0004_runs.sql](../../migrations/0004_runs.sql) |
| Leasing a run; reclaiming a dead worker's | [runtime/runs.py](../../deskhand/runtime/runs.py) `claim_next()` |
| Losing a lease mid-run | `renew_lease()`, and `LeaseLost` in loop.py |
| Exactly-once execution | [tools/invoke.py](../../deskhand/tools/invoke.py) — and [03-exactly-once.md](03-exactly-once.md) |
| The idempotency key | `idempotency_key()`, deliberately `run_id:seq` and not a uuid |
| Why a savepoint wraps every handler | the `try` block in `invoke()` |

## Consent

| Concept | Where |
|---|---|
| Risk classes, declared once and frozen | [tools/base.py](../../deskhand/tools/base.py) `RiskClass`, `ToolDef` |
| The only question the runtime asks | `requires_approval()` — reads the registry, by name |
| Binding consent to exact arguments | `args_hash()`, and the mismatch check in `loop._settle()` |
| Requesting, deciding, expiring | [runtime/approvals.py](../../deskhand/runtime/approvals.py) |
| What a human is shown before approving | the `preview` on each irreversible tool |
| Who may approve at all | [deps.py](../../deskhand/deps.py) `require_approver` |
| Feeding a denial back to the agent | the `approval` branch of `transcript.rebuild()` |

## Boundedness

| Concept | Where |
|---|---|
| Every ceiling, checked before the model call | `_bound_exceeded()` in loop.py |
| Loop detection on repeated argument hashes | `_looping()` in loop.py |
| Bounds frozen at run creation | `runs.create()` |
| The absolute deadline | `deadline_at` — a timestamp, not a duration |
| Per-org and platform daily spend | the last two queries in `_bound_exceeded()` |
| Ceilings on money paid out | `_ceilings()` in [tools/irreversible.py](../../deskhand/tools/irreversible.py) |
| Why the payout cap is not in `_bound_exceeded` | it gates model calls; a cap before the *proposal* is not a cap on the payment |
| Why the org row is locked to check it | the caller's lock is on one order, and two runs can refund two orders at once |
| Fail-closed by default | `max_refund_cents default 0` in [0006_refund_ceiling.sql](../../migrations/0006_refund_ceiling.sql) |
| Throttling how fast runs can be started | `run_limiter` in [ratelimit.py](../../deskhand/ratelimit.py) |

## Integrity

| Concept | Where |
|---|---|
| The fence, and why it is per-run | [runtime/transcript.py](../../deskhand/runtime/transcript.py) `quarantine()` |
| Neutralising a forged delimiter | the `.replace()` calls in `quarantine()` |
| The attack, in the fixtures | ticket `NW-4` in [seed.py](../../deskhand/seed.py) |
| The defence that actually holds | `requires_approval()` — see [01-thesis.md](01-thesis.md) |
| Rendering untrusted content as untrusted | [Trajectory.tsx](../../frontend/src/components/Trajectory.tsx) |
| Why the opening prompt holds no customer text | `runs.create()` — it is the one message `rebuild` cannot fence |
| Scoping a read to the ticket's own customer | `ctx.customer_id` checks in [tools/read.py](../../deskhand/tools/read.py) |
| Where that scope comes from | `invoke()` reads it off the run's row, so no caller can supply it |
| Filing model prose as the agent, not the system | `author_kind` in `_add_internal_note` |
| Validating an irreversible call before a human sees it | the `validate` branch in `_settle()` |
| Stopping a stolen token being one XSS away | `_SECURITY_HEADERS` in [main.py](../../deskhand/main.py) |

## Accountability

| Concept | Where |
|---|---|
| Cost, in integers | [pricing.py](../../deskhand/pricing.py) — nanodollars per token |
| Per-step token and cost accounting | `_record_reply()` in loop.py |
| Which run paid for what | `run_id` on `refunds` and `customer_emails` |
| Who approved it | `approvals.decided_by`, plus `audit_log` |
| The audit trail | `runs.audit()` |
| Structured events for a log collector | [tracing.py](../../deskhand/tracing.py) |
| Why the tracer cannot raise | [tests/test_tracing.py](../../tests/test_tracing.py) |

## Tools

| Concept | Where |
|---|---|
| The registry | [tools/base.py](../../deskhand/tools/base.py) |
| Read tools | [tools/read.py](../../deskhand/tools/read.py) |
| Reversible tools, and their inverses | [tools/reversible.py](../../deskhand/tools/reversible.py) |
| Irreversible tools | [tools/irreversible.py](../../deskhand/tools/irreversible.py) |
| Policy enforced in code, not the prompt | the `remaining` check in `_issue_refund` |
| Knowledge-base search, and why it ORs | `_or_query()` in read.py — plus [LESSONS](../../LESSONS.md) #2 |

## Replay

| Concept | Where |
|---|---|
| Reading a run back | [replay.py](../../deskhand/replay.py) — and [05-replay.md](05-replay.md) |
| The conversation before any step | `transcript.rebuild(..., before_seq=N)` |
| Divergence against a changed prompt | `replay.diverge()` |
| Why divergence cannot execute a tool | `test_divergence_never_executes_a_tool` |
| Time travel in the UI | [StepPrompt.tsx](../../frontend/src/components/StepPrompt.tsx) |

## Testing and evals

| Concept | Where |
|---|---|
| Trajectory evals | [evals/run.py](../../evals/run.py) |
| Claims about a path, made readable | [evals/trajectory.py](../../evals/trajectory.py) |
| Deliberate tool failures | [tools/faults.py](../../deskhand/tools/faults.py) |
| What the fault seam must not be able to do | [tests/test_faults.py](../../tests/test_faults.py) |
| Trying to break each invariant | [tests/test_runtime.py](../../tests/test_runtime.py) |

## The model

| Concept | Where |
|---|---|
| The real provider, and the current request shape | [providers.py](../../deskhand/providers.py) `ClaudeProvider` |
| The scripted provider, and why it is stateless | `ScriptedProvider` |
| The keyless demo trajectories | `DefaultMockProvider` — plus [LESSONS](../../LESSONS.md) #7 |

## Serving it

| Concept | Where |
|---|---|
| The API | [main.py](../../deskhand/main.py) |
| Live trajectory stream | `stream_run()` — polling, and why |
| Keeping the token out of the URL | [frontend/src/api.ts](../../frontend/src/api.ts) `streamRun` |
| The worker | [worker.py](../../deskhand/worker.py) |
| Running the agent in-process for the demo | the `lifespan` in main.py |
