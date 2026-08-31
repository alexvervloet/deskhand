# Changelog

Notable changes, newest first. This is a portfolio project rather than a
released library, so entries are grouped by the milestone that produced them
rather than by version number.

## A security review, and the six gaps it found

A review of the whole surface for prompt injection and the usual web
weaknesses. The defences that were designed deliberately held up — the fence
strips forged markers correctly, risk class is unreachable from a tool result,
an approval is bound to an argument hash, and an email's recipient comes from
the database rather than from the model. The gaps were all in the space
*around* those.

- **Fixed: the ticket subject reached the model unfenced.** `runs.create`
  interpolated `ticket['subject']` into the opening prompt, and that prompt is
  the one message `transcript.rebuild` cannot fence — it is built before the run
  row exists, and the fence token is derived from the run id. A subject is a
  line a customer types into a form, so the single piece of untrusted text
  arriving as trusted narration was the one attached to the ticket being worked.
  The prompt now names the reference and quotes nothing else; the subject still
  reaches the model through `get_ticket`, inside the fence. The schema comment on
  that column had claimed this property since the day it was written, which is
  [LESSONS 12](LESSONS.md). New eval `the-opening-prompt-quotes-no-customer-text`.
- **Fixed: nothing capped what a run could pay out.** Six ceilings on what a run
  costs in inference, none on what it hands back. `issue_refund` checked one
  order's remaining balance, so a run touching four orders could refund four
  times and four runs could do it in turn. Two ceilings now, per run and per
  merchant per day, enforced at the point of payment so they hold even after a
  human clicks approve — consent is for one payment, not a waiver of the limit.
  The per-run figure is snapshotted onto the row like every other bound. The
  merchant row is locked to check the daily one, because the existing lock is on
  an order and does nothing about two runs refunding two different orders at
  once. New evals `a-run-cannot-refund-past-its-ceiling` and
  `the-ceiling-counts-across-orders`.
- **Fixed: any ticket could read any customer's history.** `get_customer` took
  any email at the merchant and `list_refunds` returned the merchant's whole
  recent ledger. Org scope is a tenancy boundary, not a need-to-know one, and
  the argument deciding whose data comes back originates with a model that has
  just read a ticket written by a stranger — with `send_customer_email`
  downstream of it. `ToolContext` now carries the run's ticket and customer, read
  off the run's own row inside `invoke`, and both tools scope to it. `search_kb`,
  `get_ticket` and `get_order` stay merchant-scoped; the line is drawn at tools
  keyed by a person. New eval `a-ticket-cannot-pivot-to-another-customer`.
- **Fixed: the approval preview was rendered from unvalidated arguments.**
  `approvals.request` builds the sentence a human reads, and it built it before
  anything validated what the model had sent — so an irreversible call missing a
  required property raised a `KeyError` out of the preview lambda and failed the
  run, before any of the code that knows how to report a bad argument ran. It is
  now validated on the approval path and settled the way an unregistered tool
  already is: a failed result the agent corrects, with no approval row asking
  anyone to authorise a call that could never have executed.
- **Fixed: no security headers.** The app serves the built SPA and the API from
  one origin and keeps the session token in `localStorage`, so any script
  executing in that origin reads it and acts as the signed-in user for a week.
  React's escaping was the only thing between customer ticket bodies and that,
  with nothing behind it. Full CSP, `nosniff`, `frame-ancestors 'none'`,
  `Referrer-Policy` and `Permissions-Policy` on every response, set in middleware
  because the static mount is not a route anyone could remember to decorate.
  `script-src 'self'` is the load-bearing half; `style-src` has to allow inline
  because the UI sets style props, and a test asserts that relaxation so widening
  it further is deliberate.
- **Fixed: agent notes were filed as `system`.** `add_internal_note` wrote
  `author_kind = 'system'`, the most authoritative label in the vocabulary, for a
  body the model composed after reading a stranger's ticket. The queue was
  showing a colleague the platform's authority for model prose.
  `send_customer_email` already wrote `'agent'`; now both do.
- **Changed: the container drops root,** and run starts are throttled per
  merchant. The budget caps remain the real ceiling on spend; the throttle stops
  a signed-in user looping the endpoint and exhausting the shared platform budget
  for every other tenant.
- **Changed: the sabotage numbers, re-measured.** With 25 evals rather than 21,
  deleting the approval gate fails 14 and deleting the fence fails 3. The shape
  of the result is unchanged and is still the point: the fence is the visible
  defence and the registry is the load-bearing one.

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
- **Fixed: a hallucinated tool name killed the run.** Every question the runtime
  asks about a tool is answered from the registry, and a name the model invented
  has no answer to any of them — so the lookup raised straight past the loop and
  failed the run, including runs that had already moved money and only needed to
  write a summary. An unregistered name is now a failed tool result the agent
  reads and recovers from. New eval `a-tool-that-does-not-exist-is-not-fatal`.
- **Fixed: `--diverge` replayed a tidied-up history.** It built its own message
  list instead of using `transcript.rebuild`, and the copy had drifted: tool
  results lost their `is_error` flag and denial steps were dropped entirely, so
  a denied call was handed to the replayed model as an *empty* observation. A
  prompt tested against a run containing a failure or a human "no" was scored
  against a run that had neither. Divergence now reads each turn's history back
  through the same function the live loop uses, which is byte-identical by
  construction rather than by maintenance.
- **Fixed: `run.started` was defined and never emitted**, so runs appeared in the
  event stream already in progress. Approval traces now carry `attempt`: a run
  that crashes after acting on a decision traces it again, and while the audit
  rows roll back with the transaction, the log line has already gone to stdout.
  `tracing.py` now says plainly that its lines describe attempts and `audit_log`
  describes outcomes.
- **Said plainly: nothing calls `apply_inverse`.** Every reversible tool records
  its inverse and the ledger stores it, but no runtime path or endpoint reverts a
  failed run — the captured undo is real, the wiring is not. "Reversible" reads
  like a promise, so the module now states which half exists.
- **Said plainly: `/usage` discloses deployment-wide spend to every tenant.** It
  is there because the platform ceiling, not the per-org one, is what actually
  stops a run, and a visitor watching a demo halt should see the number that
  stopped it. Sound for two seeded merchants, unsound for a real one, and now a
  stated decision with the fix for a real deployment written next to it.
- **Removed** an unread `calls` list on the scripted provider that grew without
  bound in the inline worker, and **pinned** ruff, mypy and pyright in CI so an
  upstream release cannot redden a pull request that changed nothing.
- **Documented:** `CLIENT_IP_HEADER` and `RUN_WORKER_INLINE` were settings with
  real deployment consequences and no mention in `.env.example`. Several
  comments described things the code does not do — an open signup, a `worker`
  process group in `fly.toml`, and a no-float rule that `format_usd` breaks.

## Writing the system up, and a bug that fell out of it

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
  the transcript instead of quietly erasing it. Found while writing the docs up,
  which is where both fixes here came from; written up as LESSONS entry 10.

## Docs and deployment

- **Live demo** at [deskhand.fly.dev](https://deskhand.fly.dev) — one Fly
  machine that scales to zero, backed by Neon Postgres, running keyless against
  the scripted provider so it costs nothing and says so on every screen.
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
