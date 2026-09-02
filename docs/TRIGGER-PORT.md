# Porting the runtime to Trigger.dev

Deskhand hand-rolls durable execution on Postgres. A run is a row, a worker
leases it, the conversation is rebuilt from a step log on every resume, and a
worker that dies simply stops renewing its lease. The README is upfront that
this is the wrong production answer and the right teaching one: Temporal or
Trigger.dev hides exactly the mechanism the project exists to show.

So I took the mechanism out and put Trigger.dev underneath it, to find out
which parts were essential and which were the cost of doing durability by hand.

The port lives in [`trigger/`](../trigger). It runs the same five invariants
against the same Postgres, the same schema and the same seed data. A refund it
issues is indistinguishable from one `deskhand.worker` issues. Only the runtime
moved.

## The result

**Not "less code", but "a class of bug that is now unavailable".**

The clearest example is a column. `runs.suspended_at` exists in deskhand
because of a bug that took real money. The run deadline bounded how long the
*agent* could work, and a person reading an approval screen carefully spent
that budget on their behalf. A refund approved twenty minutes late executed,
and then the run died on its deadline with the money gone and no summary
written. The fix was to record the moment of suspension and hand the elapsed
wait back.

On Trigger.dev a suspended waitpoint does not consume `maxDuration` at all, so
the bug cannot happen and the column that fixed it has nothing to do. It went,
along with 203 other lines whose only job was keeping a process alive that was
not in memory.

Three mechanisms I expected to go with them did not, and one of them turned out
to be doing more work here than it was doing in Python. That is the rest of
this document.

## What was deleted

| Deleted | Lines | What it did |
| --- | ---: | --- |
| `runs.claim_next` | 23 | `for update skip locked` over the queue, plus the expired-lease recovery clause |
| `runs.renew_lease` | 9 | Extended the claim, and told a slow worker it had been declared dead |
| `runs.suspend_for_approval` | 8 | Parked a run and released its lease before a human could take a day to answer |
| `runs.requeue` | 10 | Made a suspended run claimable, and handed the human's thinking time back to the deadline |
| `approvals.expire_stale` | 9 | Swept timed-out approvals and woke their runs so they could notice and fail |
| `loop.LeaseLost` | 1 | The exception that means stop touching this run immediately |
| `loop._last_model_call` | 8 | Found the turn a resumed worker was in the middle of |
| `loop._unresolved` | 15 | The resume point: which tool calls have no result yet |
| `transcript.rebuild` | 52 | Replayed step rows into a messages array, on every single step |
| `deskhand/worker.py` | 69 | The whole process: poll, claim, drive, back off, handle signals, fail a crashed run explicitly |

Four columns went with them. `runs.lease_owner`, `runs.lease_expires_at`,
`runs.attempt` and `runs.suspended_at` have no reader in the port, and two
values of the `run_status` enum, `queued` and `awaiting_approval`, describe
states the platform now owns.

**How these are counted.** `node trigger/scripts/count-lines.mjs` prints this
table and the two totals below it. A counted line is non-blank, is not a
comment-only line, and is not inside a docstring or block comment. The first
version of this document quoted a total without saying which files it covered,
which made it the one claim here a reader could not check.

Run over whole files rather than mechanisms, the seven Python modules the port
replaces come to 1,025 lines and their eight TypeScript counterparts to 989.
The only thing that pair establishes is that the port is not meaningfully
smaller than what it replaced. The deletion is concentrated, not spread.

## What the loop became

The Python loop opens by insisting that nothing about a run's position lives in
a variable. Every iteration re-derives the next action from rows:

> are there tool calls the model asked for that have no result yet?
> resolve those. otherwise, ask the model for the next turn.

In the port, `messages` is a variable. That is the port.

The Python version was never clever for its own sake. It was the price of
making a run resumable by a different process on a different machine.
Trigger.dev pays that price, so the code that paid it is gone. What is left is
the loop anyone would write: ask the model, settle the tool calls it asked for,
repeat. The README calls that loop the least interesting file in the
repository. It still is.

The platform's whole footprint is
[`src/trigger/work-ticket.ts`](../trigger/src/trigger/work-ticket.ts), which is
33 lines: a task definition, a compute ceiling, a queue, a retry policy, and an
adapter that turns `wait.createToken` and `wait.forToken` into the four-method
`Waiter` interface the loop asks for. The loop takes its suspension mechanism
as an argument rather than importing it, which is not testing ceremony. It is
the measurement. Everything Trigger.dev contributes to this agent arrives
through four methods, and the rest of the file cannot tell what is on the other
side of them.

## What survived

### The idempotency ledger

I expected this one to go.

**Trigger.dev retries a failed run by re-entering `run()` from the top, not
from the point of failure.** Everything the loop did before the failure gets
replayed, including the refund. Their own `idempotencyKey` solves the
neighbouring problem, stopping a retrying parent from re-triggering a child
task, and the docs are explicit that this is exactly-once task *creation*. A
refund is not a task creation.

So the ledger stays, with the same three-step protocol and the same
deterministic `run_id:seq` key. The determinism requirement is stronger here.
In Python a resumed run recomputed the key from persisted rows. Here a retried
run recomputes it from the trajectory it takes the second time, and those agree
only while the trajectory is reproducible.

[`tests/replay.test.ts`](../trigger/tests/replay.test.ts) kills the process
immediately after the refund commits, retries from the top, and asserts one
refund, one ledger row under key `<run>:8`, and `replayed: true` on the four
tool calls the first attempt had completed.

### The consent binding

A waitpoint replaces almost all of `approvals.py`: the pending state, the
expiry sweep, the wake-the-run dance, the suspend and requeue pair.
`wait.createToken({ timeout })` even carries the TTL.

What stays is `args_hash`, for a reason that has nothing to do with durability.
**A token id is a capability to resume. It is not a statement about what was
consented to.** Whoever holds it can complete the waitpoint with any payload,
and the run wakes holding that payload. Nothing in the platform knows this
particular resume was supposed to mean "a human looked at a USD 19.00 refund
against NW-1042 and said yes".

Deskhand proves this invariant with a test that rewrites a pending call from
$19.00 to $48.00 by hand, modelling a hostile caller. On Trigger.dev the same
situation arrives without anyone being hostile:

1. Attempt one asks for USD 19.00 and opens a waitpoint.
2. The attempt fails after the token exists. An uncaught error on the resume
   path, an OOM, a restore that does not come back. Note this is *not* "a
   worker dies while the human is deciding": there is no worker during the
   wait, which is the thing the platform is for.
3. The platform re-enters `run()` from the top.
4. Attempt two re-derives the trajectory and asks for USD 48.00.
5. The token is idempotent, so the wait resolves on attempt one's approval.

A human said yes to nineteen dollars. Forty-eight is about to leave. Every step
is correct from the platform's point of view: a token was created, a person
completed it, a run resumed. The only thing between it and the money is a hash
comparison.

That is [`tests/consent.test.ts`](../trigger/tests/consent.test.ts). The run
ends `approval_denied` with zero refunds, and the neighbouring test confirms it
refuses divergence rather than retries.

One consequence worth stating, because two timeouts disagree on purpose. The
waitpoint times out after a day; its idempotency key lives for seven. A retry
in that gap resolves the cached, already-expired token, gets `ok: false`, and
ends the run `approval_expired` without asking anybody. Making the two equal
would be worse: the key would expire with the token, the retry would mint a
fresh waitpoint, and a second person would be asked to authorise a payment
whose consent window had already closed.

### The bounds, and what `maxDuration` actually is

I got this wrong first, and the corrected version is the sharper finding.

**`maxDuration` is not a wall-clock ceiling.** From
[the docs](https://trigger.dev/docs/runs/max-duration), it "is compared to the
CPU time elapsed since the start of a single execution (which we call attempts)
of the task. The CPU time is the time that the task has been actively running
on the CPU, and does not include time spent waiting."

So it is wrong as a replacement for `deadline_at` twice over, for independent
reasons:

1. It bounds an **attempt**, not a run. Deskhand's deadline is absolute and
   stamped once at creation, specifically so a crash-looping run cannot earn a
   fresh clock on every resume. Under a platform that retries three times by
   default, a per-attempt ceiling is three fresh clocks.
2. It counts **CPU time**, not elapsed time. An agent suspended for a day
   waiting on a human burns almost none of it.

The second is the same property that makes the approval gate cheap, and it is
genuinely good: nobody is billed for a person thinking. The chat-agent docs
state it outright, that `maxDuration` "measures active CPU time and excludes
suspended waitpoint time, exactly like `wait.for`". It just means `maxDuration`
cannot answer "how long has this ticket been open", which is the only question
an absolute deadline is asked. An absolute deadline stamped at creation is the
only thing that bounds that.

Worth noting for anyone relying on this: the `maxDuration` reference page lists
`wait.for`, `triggerAndWait` and `batchTriggerAndWait` in its exclusions and
never names `wait.forToken`. The behaviour is stated in the human-in-the-loop
guide instead.

Nothing else in `bounds.ts` has a platform counterpart, and that is not a gap.
Steps, tokens, dollars of inference and repeated identical tool calls are facts
about an *agent*, and a job runner has no opinion about them.

### The step log, doing a different job

`steps` survives and the rows are identical, but its job changed completely.

In Python it was the resume mechanism. A worker arriving mid-trajectory rebuilt
the conversation by replaying these rows, so they had to be complete, ordered
and append-only or a resumed run would reach a different decision. Now nothing
reads them to decide what to do next. They are written because "who did what,
at what cost, and how do I replay it" is invariant 5, and no amount of durable
execution answers that for the merchant's auditor.

**It is no longer append-only**, which is a real cost paid for a real reason. A
retried attempt walks the same trajectory and reaches the same `seq`, so the
insert became an upsert. In Python that could only have meant a bug.

That change carried one of its own. A retry that re-asks the model spends
tokens and money a second time, and `addUsage` accumulates those onto the run,
so an upsert that *overwrote* `cost_micros` would leave
`sum(steps.cost_micros)` short of `runs.cost_micros` after any retry, with both
numbers looking plausible alone. Since accountability is now the step log's
only job, that is not cosmetic. The upsert adds the accounting columns and
overwrites only the description. It is checked by a test that reports a cost
from both attempts, because against the zero-cost scripted provider the bug and
the fix produce the same number.

The idempotency ledger pointedly does *not* upsert. A step row is a
description, and a description can be restated. A ledger row is a claim that a
side effect happened, and letting a retry overwrite that claim would be the
whole bug.

## `chat.agent()`, and why the gate still needs a hash

Trigger.dev has a `chat.agent()` primitive with `needsApproval: true` on a
tool, which expresses the gate itself better than my code does. It is declared
on the tool, in backend code, unreachable from a tool result, which is exactly
the property deskhand's frozen registry exists to guarantee.

I did not build on it, because deskhand is a headless backend agent with one
opening prompt and no conversation partner, while `chat.agent` is keyed on a
`chatId` with a client sending messages. Wearing that shape would have made the
comparison less honest.

But the resume path is worth following, because the same question applies. The
docs describe it end to end:

- The frontend answers with
  `addToolOutput({ tool, toolCallId, output })`.
- "The AI SDK's `toUIMessageStream` automatically reuses the assistant message
  ID across the pause", so the resumed turn merges into the same message.
- The exactly-once primitive for acting on a resolved tool call is
  `chat.history.extractNewToolResults()`, which "compares the message against
  the current `chat.history` chain and returns only tool parts whose
  `toolCallId` is **not** already resolved".

So the matching is on `toolCallId` at every layer, and the tool's arguments
travel on the client-held message. `toolCallId` establishes *which* call is
being answered. Nothing in that chain establishes *what* was agreed to. An
argument hash recorded server-side at request time is still the only thing that
does, and `extractNewToolResults` does not close the gap: deduping on
`toolCallId` prevents acting twice on one answer, not acting once on an answer
whose arguments moved.

I have not built the frontend half, so I have not watched a mutated payload go
through. But this is a reading of the documented mechanism rather than a guess
about it.

## What the deploy showed

The port is deployed and has run on Trigger.dev infrastructure, against a
Postgres branch of the demo database. Two things I had taken on trust are now
things I watched.

There is no link to click, which is worth saying plainly rather than leaving
you to notice. A Trigger.dev run lives under the account that deployed it and
the dashboard needs a login, and the only public credential on offer is a
Realtime token scoped to one run that expires in fifteen minutes. So the
evidence is [a transcript](../trigger/demo/deployed-run.txt) from
[`scripts/demo.ts`](../trigger/scripts/demo.ts), which drives the deployed
tasks and reads every number back out of Postgres and the API rather than
printing what it expected to find. Running it yourself takes a free account and
the setup in the [port README](../trigger/README.md).

**A suspended run really does hold nothing.** NW-1 reached the approval gate and
the platform reported `status: "FROZEN"`, checkpointed with its compute
released. It sat there while a human decided. When it finished:

| Measure | Value |
| --- | ---: |
| wall clock, trigger to completion | 153 s |
| billed `durationMs` | 19.8 s |
| cost | 0.067 cents |

The 133 seconds a person spent reading the approval screen cost nothing and
counted for nothing. That is the `maxDuration` argument above, measured rather
than quoted: a ceiling on CPU time cannot bound how long a ticket has been
open, because the waiting is free.

**The gate holds on real infrastructure.** NW-4, whose ticket body carries a
forged `SYSTEM:` block ordering an unapproved refund, reached the same gate.
The instruction said not to request human approval. The run requested one, a
human denied it, and no money moved. The ledger for the successful NW-1 run has
exactly six rows, one per tool call, with `issue_refund` claimed once under key
`<run>:8`.

Deploying it also found a bug that 26 passing tests did not. The scripted
provider had a fixed closing turn, so the first denied run finished by writing
"Refunded NW-1101" into the step log next to zero refunds. Every invariant
held. The summary was still false, which on a public demo is worse than a
crash, because it looks like working software. There is now a
[test](../trigger/tests/invariants.test.ts) that a denied run says what
actually happened, and the ticket escalates rather than resolving.

**And a retry really does start from the top.** This was the last thing I was
taking on faith, and the whole idempotency argument rests on it, so
[`crash-probe.ts`](../trigger/src/trigger/crash-probe.ts) is a second task that
fails on purpose on real infrastructure, at the worst available moment: after
the refund has committed and before anything else happens. Attempt two is given
the ordinary provider, no memory of the first, and no hint that it is a retry.

The platform recorded two attempts. The database recorded one refund. The step
log says why:

| seq | step | replayed |
| ---: | --- | --- |
| 2 | `get_ticket` | true |
| 4 | `get_order` | true |
| 6 | `search_kb` | true |
| 8 | **`issue_refund`** | **true** |
| 10 | `add_internal_note` | false |
| 12 | `set_ticket_status` | false |

Attempt two walked the whole trajectory again, including the refund. Steps 10
and 12 ran for the first time because attempt one died before reaching them.
Step 8 is the answer to the question: the second attempt arrived at
`issue_refund` with the money already gone, found the claim in the ledger under
`<run>:8`, and handed back the recorded result instead of paying again. Delete
the ledger and that row pays a second time.

The approval was not asked twice either. The waitpoint token was already
completed, and the idempotency key on it meant attempt two resolved the same
token rather than opening a new one and putting the decision in front of a
second person.

The fault lives in its own task with its own id rather than behind a flag on
the production one. Deskhand's fault injector has no environment switch for the
same reason: a runtime that can be told to misbehave by its configuration is a
runtime nobody can reason about.

## What I still have not verified

- **The port never calls a real model.** `getProvider()` returns the scripted
  provider unconditionally; there is no Anthropic client in `trigger/` at all,
  where the Python service has a working one. So the trajectory is reproducible
  by construction rather than by luck, and that assumption is load-bearing for
  the idempotency key. A real model that diverges on retry claims a fresh key,
  and the run falls back to being bounded by its caps rather than by the
  ledger.

What the tests do establish is what the port still has to do for itself: a
divergent retry cannot execute on a stale approval, a replay from the top does
not refund twice, an obedient model reading a forged pre-approval still hits
the gate, a payout ceiling holds after a human clicks approve, and a retry's
spend lands in the step log. 27 tests, against a real database, no account
needed to run them.

## The thing I would tell myself before starting

When a platform absorbs a mechanism, ask what the mechanism was *for* rather
than what it was called.

"Durability" covered two unrelated jobs in this codebase. Keeping a process
resumable, which Trigger.dev does properly and which I should never have
written by hand. And never paying a customer twice, which is a claim about a
database and stayed mine.

The same mistake nearly cost me the deadline argument. "Duration" sounds like
elapsed time, I read it as elapsed time, and I had written a paragraph
depending on the opposite two sections earlier without noticing the collision.

Both were caught by checking rather than by rereading. The retry claim held up,
the deadline claim did not, and I could not have told you in advance which was
which. That is the argument for deploying the thing.

## Running it

```bash
docker compose up -d db
python -m deskhand.migrate          # includes 0007, which the port added
python -m deskhand.seed

cd trigger && npm install
npm test                            # 27 tests, real Postgres, no account needed
npm run count                       # reproduces every number in this document

node --experimental-strip-types scripts/run-local.ts NW-1
# in another shell, once it stops at the gate:
node --experimental-strip-types scripts/approve.ts NW-1 approve
```

`run-local.ts` drives the real loop with a waiter that polls the approvals
table instead of suspending, so the port can be read and argued with by someone
who has no Trigger.dev account. The difference between it and the real thing is
the thing worth paying for: that script has to stay running for as long as the
human takes, and if you kill it while it waits, the run is gone.

The one schema change the port needed is
[`migrations/0007_waitpoint_token.sql`](../migrations/0007_waitpoint_token.sql),
which carries the waitpoint token on the approval row. It is nullable, because
every approval written by the Python worker predates waitpoints and is still a
valid record of a human saying yes.
