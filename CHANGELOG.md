# Changelog

Notable changes, newest first. This is a portfolio project rather than a
released library, so entries are grouped by the milestone that produced them
rather than by version number.

## A pre-publication audit, and the bug it found

- **Fixed: the wall-clock deadline was also bounding human deliberation.** A run
  suspended on an approval kept burning its clock while a person read the
  screen. Approve a refund twenty minutes after it was requested and the money
  moved, and *then* the run died on its deadline — customer refunded, no
  confirmation, no summary, ticket still open. Live on the demo for any approval
  answered between fifteen and thirty minutes. The run now records when it
  suspended and hands that wait back to the deadline on resume, so the bound
  covers agent work and not a person thinking. It stays absolute in the way that
  matters: only measured waiting is ever added, so a crash-looping run still
  cannot earn a fresh clock. New eval `the-deadline-does-not-run-while-a-human-thinks`,
  which fails if the extension is removed.
- **Fixed: a corrected claim that had been wrong since it was written.** Deleting
  the fence fails *two* evals, not one — `every-tool-result-is-fenced` and, in a
  different category, the last line of `garbage-does-not-derail-the-run`. The
  exercise had told readers to run only the `integrity` group, which is exactly
  the filter that hides the second one. The counts are corrected in all six
  places they appeared, and the exercise now runs the whole suite, which makes
  its own point better: two evals catch the deletion and both are assertions
  about the mechanism rather than the outcome.
- **Fixed: malformed ids returned 500.** A run or approval id that is not a uuid
  reached Postgres, which rejects it outright, so `/runs/nonsense` was an
  unhandled error rather than a 404. An id that cannot exist now gets the same
  answer as one that does not.
- **Documented:** `CLIENT_IP_HEADER` and `RUN_WORKER_INLINE` were settings with
  real deployment consequences and no mention in `.env.example`. Several
  comments described things the code does not do — an open signup, a `worker`
  process group in `fly.toml`, and a no-float rule that `format_usd` breaks.

## Teaching the system, and a bug that fell out of it

- **[Five levels of difficulty](docs/education/five-levels/)** — the whole
  system explained five times over, to an intro-CS teenager, a second-year
  undergraduate, a CS graduate learning AI, an engineering manager interviewing
  for an AI role, and a senior AI engineer. Each is a complete pass at its own
  depth rather than a summary of the one above, and each ends by naming what it
  deliberately skipped.
- **The reading path moved to [docs/education/](docs/education/)**, and every
  relative link that the move broke was repointed. Two docstrings had been
  citing `docs/04-idempotency.md`, a file that has never existed in this
  repository; they now cite the exactly-once doc they meant.
- **Fixed: the approval screen showed less than the approval bound.** The
  preview is a one-line summary, `args_hash` covers every argument, and
  `send_customer_email` put only the subject in the preview — so a person
  approved a subject line while consenting to a body they had never read. The
  screen now renders every argument the hash covers, for every tool, which
  closes the same gap for anything irreversible added later.
- **Fixed: untrusted content could close its own fence.** The strip in
  `quarantine()` was a single `str.replace`, and removing a forged delimiter
  joined the text either side of it into the delimiter just removed. Forged
  markers are now substituted rather than deleted, which keeps the two halves
  apart, makes one pass provably sufficient, and leaves the forgery visible in
  the transcript instead of quietly erasing it. Found while writing the level 5
  review; written up as LESSONS entry 10.

## Docs and deployment

- **Live demo** at [deskhand.fly.dev](https://deskhand.fly.dev) — one Fly
  machine that scales to zero, backed by Neon Postgres, running keyless against
  the scripted provider so it costs nothing and says so on every screen.
- **[docs/education/](docs/education/)** — the thesis, a concept index, the exactly-once story with
  its assumption stated plainly, and how the evals work.
- **Four exercises**, each a one-line change with a verified result. Exercise 02
  deletes the fence and watches 18 of 20 evals keep passing.
- **Demo assets** — a terminal recording of a worker dying mid-run, and
  screenshots of the approval gate and of an injected instruction being quoted
  rather than obeyed.

## Type checking

- **Pyright (what Pylance runs) added to CI**, alongside mypy. It had 30 errors
  on a tree mypy called clean.
- **mypy's scope widened** from `deskhand` to the whole tree. `tests/`,
  `evals/`, `demo/` and `check_setup.py` — about 2,400 lines — had never been
  checked by anything.
- **SQL is typed `LiteralString`** throughout, which is psycopg's own
  constraint and worth keeping: a query can no longer be assembled from a
  variable without failing the build. The one genuinely dynamic query now
  composes with `psycopg.sql.Identifier` instead of an f-string.
- `db.one()` fetches exactly one row or raises, replacing twenty
  `fetch_one(...)["id"]` sites where a missing row is a bug rather than a
  branch.

## Replay and divergence

- **`python -m deskhand.replay <run_id>`** — the trajectory as recorded, with
  approvals interleaved by the step they gated (a granted approval writes no
  step, so it would otherwise be invisible).
- **`--at N`** reconstructs the conversation exactly as it stood before step N,
  fence markers and all. Also available per step in the run viewer, and over the
  API at `GET /runs/{id}/replay?at=N`.
- **`--diverge`** replays a recorded run against a changed system prompt or
  model and reports the first decision that differs — a prompt-regression tool
  built out of production traffic.
- Divergence **never executes a tool**: recorded results are handed back
  instead. Asserted by a test that replays an agent trying to issue a refund the
  original run never made, then checks the refunds table, ticket messages, step
  log and run row are all unchanged.
- Decisions are compared on tool name plus canonical arguments, so rewording is
  not a divergence and a changed refund amount is.

## Observability

- **The step log is the trace.** Removed the unused Langfuse dependency, its
  configuration, and the preflight line that reported on it — none of it was
  wired to anything. Every model and tool call was already a row carrying
  tokens, cost, latency, arguments and result.
- **[tracing.py](deskhand/tracing.py)** emits one structured JSON line per
  event — run started, model call, tool call with its risk class, approval
  requested and decided, run finished — for whatever collects your logs.
  Identifiers and numbers only, never content.
- The tracer cannot raise, cannot block, and does not care whether its arguments
  are serialisable. Asserted rather than assumed: a tracer that throws turns a
  successful refund into a failed run.

## Trajectory evals and fault injection

- **19 trajectory evals** across the five invariants, wired into CI as a
  required step. They assert properties of the agent's *path*, not of its
  answer, against the real loop and a real database.
- **Fault injection** — tools that fail, stall, return garbage, or return
  hostile text, on purpose. Off unless a test installs them; no environment
  switch; cannot change a tool's risk class.
- Mutation-tested the gate: removing the approval check fails 11 of 19 evals,
  while removing the idempotency ledger or loop detection each fails exactly one
  and removing the fence fails two. That asymmetry is written up in LESSONS #6 and is what the
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
