# Deskhand

A durable agent runtime for support operations. The agent reads a ticket,
works it autonomously across many steps, and is allowed to do irreversible
things — refund money, email a customer, cancel an order.

This is a portfolio project about the machinery that makes letting an agent do
that defensible, not about the agent loop. The interesting parts are durable
execution across a worker crash, a human approval gate that is a first-class
run state, bounds that guarantee termination, defense against injection
arriving through a tool result, and evals that assert properties of the
*trajectory* rather than the final answer.

> **Status: in progress.** Scaffold only so far. This README is a stub and
> gets written properly once the runtime is real.

## The sentence the project exists for

*Step 7 of 12 fails after step 6 already sent the email.*

## The five invariants

Everything in this repo serves one of these:

1. **Durability** — a run resumes from its last persisted step across a worker
   crash, and never re-executes a completed side effect.
2. **Consent** — no irreversible tool executes without a recorded human
   approval tied to that exact run, step, and argument hash.
3. **Boundedness** — every run terminates. Step cap, token cap, wall-clock cap,
   spend cap, loop detection, no-progress detection.
4. **Integrity** — content coming back from a tool is data, never instruction.
   Injection through a tool result cannot escalate privilege.
5. **Accountability** — every step is attributable: who, which run, what it
   cost, what it changed, and how to replay it.

## Companion project

Deskhand is the second half of a pair with
[Knowledge Desk](https://github.com/ewokpanda/knowledge-desk), which argues
that the hard part of a retrieval application is not the RAG. This one argues
the sequel: the hard part of an agent is not the loop.

## Stack

FastAPI, Postgres (job queue, append-only step log, full-text search for the
knowledge-base tool — no embeddings, on purpose), React + Vite + TypeScript,
Claude for the agent, Langfuse for traces, Docker, GitHub Actions.

Durable execution is hand-rolled on Postgres rather than delegated to
Temporal. Temporal is the right production answer and hides exactly the
mechanism this project exists to show.

Runs keyless against a scripted mock model, so the tests and evals are green
with no API keys.

## Run it locally

```bash
docker compose up -d db              # Postgres on :5437
python -m deskhand.migrate           # schema
python check_setup.py                # preflight
```

## License

MIT. See [LICENSE](LICENSE).
