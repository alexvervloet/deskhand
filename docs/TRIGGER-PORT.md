# Porting the runtime to Trigger.dev

Deskhand hand-rolls durable execution on Postgres. A run is a row, a worker
leases it, the conversation is rebuilt from a step log on every resume, and a
worker that dies just stops renewing its lease. The README is upfront that this
is the wrong production answer and the right teaching one: Temporal or
Trigger.dev hides exactly the mechanism the project exists to show.

So I took the mechanism out and put Trigger.dev underneath it, to find out
which parts were essential and which were the cost of doing durability by hand.

The port lives in [`trigger/`](../trigger). It runs the same five invariants
against the same Postgres, the same schema and the same seed data. A refund it
issues is indistinguishable from one `deskhand.worker` issues. Only the runtime
moved.

## The result in one line

Two hundred and four lines of code deleted, all of them doing one job, and
three mechanisms that looked like they should have gone with them stayed
exactly where they were.

The total size barely moved: 1,025 code lines of Python runtime became 986
lines of TypeScript. That is not the interesting number, and quoting it either
way would be dishonest. The interesting number is which 204 lines went and what
happened to the rest.

## What was deleted

Everything in this table existed to answer one question: how does a process
survive not being in memory. Trigger.dev answers it, so the code that answered
it is gone.

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

`suspended_at` is the one I would point at in an interview. It exists in
deskhand because of a bug that took real money: `deadline_at` bounded how long
the agent could work, and a person reading an approval screen carefully spent
that budget on their behalf. A refund approved twenty minutes late executed,
and then the run died on its deadline with the money gone and no summary
written. The fix was to record the moment of suspension and give the elapsed
wait back. On Trigger.dev a suspended waitpoint does not consume `maxDuration`
at all, so the bug cannot happen and the column that fixed it has nothing to
do.

That is the honest shape of the win. Not "less code", but "a class of bug that
is now unavailable".

## What the loop became

The Python loop opens by insisting that nothing about a run's position lives in
a variable. Every iteration re-derives the next action from rows:

> are there tool calls the model asked for that have no result yet?
> resolve those. otherwise, ask the model for the next turn.

In the port, `messages` is a variable. That is the entire port, and it is worth
being clear that the Python version was never clever for its own sake. It was
the price of making a run resumable by a different process on a different
machine. Trigger.dev pays that price, so the code that paid it is gone.

What is left is the loop anyone would write: ask the model, settle the tool
calls it asked for, repeat. The README says that loop is about a hundred lines
and the least interesting file in the repository. It still is.

The platform's whole footprint is
[`src/trigger/work-ticket.ts`](../trigger/src/trigger/work-ticket.ts), which is
33 lines: a task definition, a duration ceiling, a queue, and an adapter that
turns `wait.createToken` and `wait.forToken` into the four-method `Waiter`
interface the loop asks for. The loop takes its suspension mechanism as an
argument rather than importing it, which is not testing ceremony. It is the
measurement. Everything Trigger.dev contributes to this agent arrives through
four methods, and the rest of the file cannot tell what is on the other side.

## What did not delete

### The idempotency ledger

I expected this one to go. It did not, and the reason is the single most
important thing I learned.

**Trigger.dev retries a failed run by re-entering `run()` from the top, not
from the point of failure.** Everything the loop did before the crash gets
replayed, including the refund. Their own `idempotencyKey` solves the
neighbouring problem, stopping a retrying parent from re-triggering a child
task, and the docs are explicit that this is exactly-once task *creation*. A
refund is not a task creation.

So `tool_invocations` stays, with the same three-step protocol and the same
deterministic `run_id:seq` key. The determinism requirement is actually
*stronger* here. In Python a resumed run recomputed the key from persisted
rows. Here a retried run recomputes it from the trajectory it takes the second
time, and those agree only while the trajectory is reproducible.

[`tests/replay.test.ts`](../trigger/tests/replay.test.ts) kills the process
immediately after the refund commits, retries from the top, and asserts one
refund, one ledger row under key `<run>:8`, and `replayed: true` on the four
tool calls the first attempt had completed.

### The consent binding

A waitpoint replaces almost all of `approvals.py`. The pending state, the
expiry sweep, the wake-the-run dance, the suspend and requeue pair: all of it
is plumbing for a process that has to survive not being in memory, and
`wait.createToken({ timeout })` even carries the TTL.

What stays is `args_hash`, and it stays for a reason that has nothing to do
with durability. **A token id is a capability to resume. It is not a statement
about what was consented to.** Whoever holds it can complete the waitpoint with
any payload, and the run wakes holding that payload. Nothing in the platform
knows this particular resume was supposed to mean "a human looked at a USD
19.00 refund against NW-1042 and said yes".

Deskhand proves this invariant with a test that rewrites a pending call from
$19.00 to $48.00 by hand, modelling a hostile caller. On Trigger.dev the same
situation arrives without anyone being hostile, as an ordinary consequence of
the retry semantics:

1. Attempt one asks for USD 19.00 and opens a waitpoint.
2. The worker dies while the human is still deciding.
3. The platform re-enters `run()` from the top.
4. Attempt two re-derives the trajectory and asks for USD 48.00.
5. The token is idempotent, so the wait resolves on attempt one's approval.

A human said yes to nineteen dollars. Forty-eight is about to leave. Every step
in that sequence is correct from the platform's point of view: a token was
created, a person completed it, a run resumed. The only thing between it and
the money is a hash comparison.

That is
[`tests/consent.test.ts`](../trigger/tests/consent.test.ts), and it passes: the
run ends `approval_denied` with zero refunds. The neighbouring test confirms it
refuses divergence rather than retries, by running the same amount twice and
watching the refund go through.

### The bounds

Steps, tokens, dollars of inference and repeated identical tool calls are facts
about an agent. A job runner has no opinion about them, and that is not a gap
in Trigger.dev.

The one that nearly moved is the deadline. `maxDuration` is a wall-clock
ceiling and looks like a straight replacement, but it bounds an **attempt**.
Deskhand's `deadline_at` is absolute and stamped once at creation, specifically
so a crash-looping run cannot earn a fresh clock on every resume. Under a
platform that retries three times by default, a per-attempt ceiling is three
fresh clocks. So `maxDuration` is set as a backstop on the process and the
absolute deadline stays on the row.

### The step log, for a different reason

`steps` survives, and the rows are identical, but its job changed completely.

In Python it was the resume mechanism. A worker arriving mid-trajectory rebuilt
the conversation by replaying these rows, so they had to be complete, ordered
and append-only or a resumed run would reach a different decision. Now nothing
reads them to decide what to do next. They are written because "who did what,
at what cost, and how do I replay it" is invariant 5, and no amount of durable
execution answers that for the merchant's auditor.

One line changed as a result: the insert became an upsert on `(run_id, seq)`,
because a retried attempt walks the same trajectory and reaches the same seq
again. In Python that could only mean a bug. The idempotency ledger pointedly
did **not** get the same treatment. A step row is a description, and
overwriting one with an identical description costs nothing. A ledger row is a
claim that a side effect happened, and letting a retry overwrite that claim
would be the whole bug.

## The road not taken: `chat.agent()`

Trigger.dev has a `chat.agent()` primitive with `needsApproval: true` on a
tool, which expresses deskhand's approval gate better than my code does. It is
declared on the tool, in backend code, unreachable from a tool result, which is
precisely the property the frozen registry exists to guarantee.

I did not build on it, because deskhand is a headless backend agent with one
opening prompt and no conversation partner, and `chat.agent` is keyed on a
`chatId` with a client sending messages. Wearing that shape would have made the
comparison less honest, not more.

One thing I would want to check before using it for money. The documented
resume path is that the frontend sends the updated assistant message back and
the SDK matches it in the conversation accumulator by message ID. Matching an
ID establishes *which* call is being answered, which is a different question
from *what* was agreed to. If the arguments round-trip through the client, then
`args_hash` is load-bearing there too. I could not test this without building
the frontend half, so I am flagging it as a question rather than claiming a
finding.

## What I did not verify

Being precise about this, since the reader builds the thing.

- **Not deployed.** This ran locally against real Postgres, not on Trigger.dev
  infrastructure. The tests substitute the waiter for one that answers instead
  of suspending.
- **The checkpoint and resume itself is taken on trust.** That Trigger.dev
  suspends a waiting run, frees its compute and brings it back is their code
  and the entire reason to use them. My tests do not check it and could not.
- **"Retries re-enter `run()` from the top" comes from the documentation**, not
  from a deployment I watched. It is consistent with why `idempotencyKey`
  exists at all, and the whole idempotency argument above depends on it. If it
  is wrong, that section is wrong, and I would rather say so than imply I
  watched it happen.

What the tests *do* establish is every claim about what this repository still
has to do: a divergent retry cannot execute on a stale approval, a replay from
the top does not refund twice, an obedient model reading a forged pre-approval
still hits the gate, and a payout ceiling holds after a human clicks approve.
Those are claims about my code, and they are checked rather than asserted.

## Running it

```bash
docker compose up -d db
python -m deskhand.migrate          # includes 0007, which the port added
python -m deskhand.seed

cd trigger && npm install
npm test                            # 25 tests, real Postgres, no account needed

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
