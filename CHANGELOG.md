# Changelog

Notable changes, newest first. This is a portfolio project rather than a
released library, so entries are grouped by the milestone that produced them
rather than by version number.

## Docs and deployment

- **Live demo** at [deskhand.fly.dev](https://deskhand.fly.dev) — one Fly
  machine that scales to zero, backed by Neon Postgres, running keyless against
  the scripted provider so it costs nothing and says so on every screen.
- **[docs/](docs/)** — the thesis, a concept index, the exactly-once story with
  its assumption stated plainly, and how the evals work.
- **Four exercises**, each a one-line change with a verified result. Exercise 02
  deletes the fence and watches 18 of 19 evals keep passing.
- **Demo assets** — a terminal recording of a worker dying mid-run, and
  screenshots of the approval gate and of an injected instruction being quoted
  rather than obeyed.

## Trajectory evals and fault injection

- **19 trajectory evals** across the five invariants, wired into CI as a
  required step. They assert properties of the agent's *path*, not of its
  answer, against the real loop and a real database.
- **Fault injection** — tools that fail, stall, return garbage, or return
  hostile text, on purpose. Off unless a test installs them; no environment
  switch; cannot change a tool's risk class.
- Mutation-tested the gate: removing the approval check fails 11 of 19 evals,
  while removing the fence, the idempotency ledger, or loop detection each fails
  exactly one. That asymmetry is written up in LESSONS #6 and is what the
  exercises are built on.
- **Fixed:** a tool returning a NUL byte crashed the ledger write *after* the
  side effect had already happened — money moved with no record of it. Found by
  the garbage fault on its first run.

## API and UI

- **HTTP API** — tickets, runs, approvals, usage, the tool registry, and a
  live server-sent-events stream of a trajectory as it happens.
- **React UI** — ticket queue, approval queue, trajectory viewer with risk
  colouring, and spend against both the per-merchant and service ceilings.
  Amber means "a human must decide" and nothing else uses it.
- The UI reads the risk model from `GET /tools` rather than restating it in
  TypeScript, so the two cannot disagree.
- The SSE stream is read through `fetch` rather than `EventSource`, keeping the
  session token in a header instead of a query string.
- **Fixed:** streamed status events omitted the ticket reference, which blanked
  the run header mid-run. The test asserted events were emitted, never that they
  were complete.

## The runtime

- **Durable loop** — nothing about a run's position lives in a variable; every
  iteration re-derives the next action from rows, so any worker can resume any
  run.
- **Leases** with `for update skip locked`; a dead worker's run becomes
  claimable when its lease expires, with nobody having to notice.
- **Approval gate** — irreversible tools suspend the run until a human decides,
  with consent bound to a hash of the exact arguments.
- **Bounds** — step, token, spend and wall-clock ceilings checked *before* each
  model call, plus loop detection on repeated argument hashes. The deadline is
  absolute, so a crash-looping run cannot earn a fresh clock.
- **Integrity** — every tool result is fenced with a per-run delimiter, forged
  delimiters neutralised, and risk classes frozen at import.
- **Exactly-once execution** — the idempotency ledger row is written in the same
  transaction as the tool's effect, which removes the usual claimed-but-unknown
  limbo. Honest about depending on every side effect being a row in the same
  database.

## Foundations

- Postgres schema: identity, the support domain, the idempotency ledger and
  audit trail, runs, the append-only step log, and approvals.
- Tool registry with declared risk classes; read, reversible (each recording its
  own inverse), and irreversible tools.
- Claude provider plus a scripted one, so the whole runtime is exercised in CI
  with no API key.
- Money as integer cents, model cost as integer nanodollars. No float touches a
  currency amount anywhere.
- **Fixed:** knowledge-base search ANDed its terms, so one unmatched word turned
  a policy lookup into "no such policy" — which an agent reads as permission to
  proceed. It now ORs and ranks, so it degrades instead of failing open.
