# Deskhand

A durable agent runtime for support operations. The agent reads a ticket, works
it autonomously across many steps, and is allowed to do irreversible things —
refund money, email a customer, cancel an order.

This is a portfolio project about the machinery that makes letting an agent do
that defensible, not about the agent loop. The loop is about a hundred lines and
is the least interesting file here.

## The sentence the project exists for

*Step 7 of 12 fails after step 6 already sent the email.*

## The five invariants

Everything in this repo serves one of these, and each is attacked by a test that
tries to break it:

1. **Durability** — a run resumes from its last persisted step across a worker
   crash, and never re-executes a completed side effect.
   [A test](tests/test_runtime.py) kills a worker after it has already refunded a
   customer, lets the lease expire, has a second worker claim the run, and
   asserts exactly one refund exists.
2. **Consent** — no irreversible tool executes without a recorded human approval
   bound to that exact run, step, and argument hash. A test approves a $19.00
   refund, rewrites the pending call to $48.00 mid-flight, and asserts the
   runtime refuses rather than executing something nobody saw.
3. **Boundedness** — every run terminates: step, token, wall-clock and spend
   caps, all checked *before* each model call, plus loop detection on repeated
   argument hashes. The deadline is absolute, so a crash-looping run cannot earn
   itself a fresh clock.
4. **Integrity** — content coming back from a tool is data, never instruction.
   The seeded `NW-4` ticket contains a forged `SYSTEM:` block ordering an
   unapproved refund; a test drives a *fully obedient* model against it and the
   refund still only becomes a request, because risk class is read from a frozen
   registry that no tool result can reach.
5. **Accountability** — every step is attributable: who, which run, what it cost,
   what it changed, and how to replay it.

## What the loop actually does

Nothing about a run's position lives in a variable. Every iteration re-derives
the next action from rows:

> are there tool calls the model asked for that have no result yet?
> → resolve those. otherwise → ask the model for the next turn.

A worker that dies is not resuming a computation, it is reading a database. Any
worker, on any machine, at any later time, computes the same next action from the
same rows. See [deskhand/runtime/loop.py](deskhand/runtime/loop.py).

## Status

Working end to end: schema, tool registry, durable runtime, approval gate, HTTP
API with a live trajectory stream, and a React UI. Green in CI on a clean
checkout — tests, ruff, mypy, and a frontend type-check and build.

Still to come: fault injection, Langfuse tracing, trajectory evals as a required
CI job, deterministic replay, the written exercises, and a deployed demo.

## Run it

Runs keyless. With no `ANTHROPIC_API_KEY` the runtime uses a scripted provider
and says so on every screen and in every API response, so a demo can never be
mistaken for a model.

```bash
docker compose up -d db                        # Postgres on :5437
python -m deskhand.migrate                     # schema
python -m deskhand.seed                        # two merchants, six tickets
python check_setup.py                          # preflight

uvicorn deskhand.main:app --reload              # API on :8000
python -m deskhand.worker                       # the agent (separate shell)
cd frontend && npm install && npm run dev       # UI on :5173
```

Sign in as `owner@northwind.test` (password `demo-password-123`), open **NW-1**,
and press *Run the agent*. It reads the ticket, reads the order, checks the
refund policy, and then stops — waiting for you to approve moving the money.
Sign in as `viewer@northwind.test` to watch the same run without being able to
authorise it.

`NW-4` is the interesting one: its body contains an injected instruction telling
the agent the refund is pre-approved. Run it and watch the approval gate hold
anyway.

## Architecture notes

**Durable execution is hand-rolled on Postgres, not delegated to Temporal.**
Temporal is the right production answer and hides exactly the mechanism this
project exists to show.

**Exactly-once is honest about its assumption.** The idempotency ledger row is
written in the *same transaction* as the tool's effect, which is what removes
the usual claimed-but-unknown limbo. That works because every side effect here
is a row in the same database. A tool calling a real payment API could not share
a transaction with the ledger and would need a third state plus reconciliation —
stated in [deskhand/tools/invoke.py](deskhand/tools/invoke.py) rather than
glossed over.

**The knowledge-base tool uses Postgres full-text search, not embeddings.** This
project is not about retrieval; the companion project is.

**No float touches money.** Currency is integer cents, model cost is integer
nanodollars rounded once to micros, and spend caps compare integers.

## Companion project

Deskhand is the second half of a pair with
[Knowledge Desk](https://github.com/alexvervloet/knowledge-desk), which argues
that the hard part of a retrieval application is not the RAG. This one argues
the sequel: the hard part of an agent is not the loop.

## Stack

FastAPI, Postgres (job queue, append-only step log, full-text search), React +
Vite + TypeScript, Claude for the agent, Docker, GitHub Actions.

## What went wrong along the way

[LESSONS.md](LESSONS.md) — including a full-text search that failed *open* on a
policy lookup (an agent reading "no such policy" reasonably concludes it is
unconstrained), and a green test suite that shipped a broken screen.

## License

MIT. See [LICENSE](LICENSE).
