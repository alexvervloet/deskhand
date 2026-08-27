# Level 2: you are a second-year CS student

You have done data structures. You have written SQL, probably `select` and
`join` and maybe a `group by`. You have heard ACID recited as an acronym and
could not necessarily say what the A buys you. You know a process is a running
program and that two of them can run at the same time and cause you grief.

That is the right background for this project, because Deskhand is not really an
AI project. It is a database project with a language model bolted to one side.
Every hard problem in it is a problem you would recognise from a systems course.

I am going to assume you read [level 1](01-high-school.md) or can skim it. Short
version: an AI agent is a loop that asks a model what to do and does it, some of
those things move real money, and the interesting code is all in the second half
of that sentence.

## The shape of the system

Four processes, and they share nothing but a database.

```
  browser  ──HTTP──▶  API (FastAPI)  ──▶  ┌──────────┐
                                          │ Postgres │
  worker process 1  ─────────────────▶    │          │
  worker process 2  ─────────────────▶    └──────────┘
                            │
                            └──HTTPS──▶  Anthropic's API (the model)
```

No message queue. No Redis. No shared memory. No worker registry, no leader
election, no coordinator. If you kill every worker and start three new ones on
three different machines, the system carries on. Everything they need to agree
on is a row somebody can `select`.

That constraint is self-imposed and it is the reason the project is worth
reading. Postgres is doing the job of a queue, a lock manager, an event log, and
a search engine, and the point is to see the mechanism working rather than hide
it behind a service.

## The central table, and why it is append-only

A *run* is one attempt by the agent at one support ticket. A run has *steps*.

```sql
create type step_kind as enum (
    'model_call',    -- one request to the model; content is the assistant blocks
    'tool_result',   -- one tool executed; content carries args and output
    'approval',      -- a human decision was requested and recorded
    'final',         -- the agent's closing summary
    'error'
);

create table steps (
    id            uuid primary key default gen_random_uuid(),
    run_id        uuid not null references runs (id) on delete cascade,
    seq           integer not null,
    kind          step_kind not null,
    content       jsonb not null,
    tool_name     text,
    input_tokens  integer not null default 0,
    output_tokens integer not null default 0,
    cost_micros   bigint not null default 0,
    latency_ms    integer not null default 0,
    created_at    timestamptz not null default now()
);

create unique index steps_run_seq_key on steps (run_id, seq);
```

There is no `update` and no `delete` against this table anywhere in the codebase
except one narrow case I will get to. You only insert.

The design rule that produces this: **no state about a run's progress is held in
process memory.** Not in a variable, not in an object field, not in a closure.
If it matters where the run is up to, it is a row.

Think about what a normal implementation would look like. You would write a
function `run_agent(ticket)`, and inside it you would keep `messages = []` and
append to it as you went. Clean, obvious, and it is a `list` on the heap of a
process, so the moment that process dies the run's entire history is gone. You
have a customer who has been refunded and a system with no memory of refunding
them.

The unique index on `(run_id, seq)` is not decoration. `seq` is dense and starts
at 1, and it is half of the idempotency key later on, so two steps sharing a
number would be a correctness bug and the database refuses to let it happen.

## The conversation is a fold over the log

Here is the consequence, and it is the nicest thing in the codebase.

The model's API is stateless. Every time you call it you send the entire
conversation so far. So somewhere, something has to produce that array of
messages. In Deskhand nothing stores it. It is computed, from scratch, every
single turn:

```python
def rebuild(cur, run_id, prompt, *, before_seq=None):
    cur.execute(
        "select seq, kind::text, content from steps"
        " where run_id = %s and (%s::int is null or seq < %s::int)"
        " order by seq",
        (run_id, before_seq, before_seq),
    )
    ...
```

`rebuild` is a pure function of (rows, opening prompt). No clock, no `random`,
no globals, no ambient config. Same rows in, byte-identical messages out, on any
machine, today or in a year.

You get three things from that one property, and none of them were designed for
separately:

1. **Resumption is free.** A worker that has never seen this run calls `rebuild`
   and is exactly as informed as the worker that died.
2. **Audit is free.** Pass `before_seq=7` and you get the conversation as it
   stood immediately before step 7. "What did the model actually have in front of
   it when it decided to refund this customer" has one reproducible answer, and
   you can get it months later. Try answering that question about a system that
   kept its messages in a variable.
3. **Prompt regression testing is free.** Replay a recorded run against a
   modified system prompt, hand back the recorded tool results instead of
   executing anything, and report the first decision that differs. You get a
   regression suite built out of real production traffic without spending a
   penny or touching the world.

Purity is not an aesthetic preference here. It is load-bearing. Put a
`datetime.now()` in that function and you lose all three.

## Two workers, one queue, no coordinator

Now the concurrency. Several workers poll the same table. You need exactly one
of them to get any given run, and you need a run whose worker died to become
available again without anybody noticing the death.

The answer is a **lease**: a claim with an expiry that has to be renewed.

```sql
update runs set
    status = 'running',
    lease_owner = %s,
    lease_expires_at = now() + make_interval(secs => %s),
    attempt = attempt + 1,
    updated_at = now()
where id = (
    select id from runs
     where status = 'queued'
        or (status = 'running' and lease_expires_at < now())
     order by created_at
     for update skip locked
     limit 1
)
returning *;
```

Take that apart, because there are three separate ideas packed into it.

**`for update`** takes a row lock. Without it, two workers both `select` the same
oldest queued run, both decide to claim it, and both `update` it. Classic
read-modify-write race, the one from your concurrency lecture, except the shared
variable is a table row.

**`skip locked`** is the part you may not have seen. Normally the second worker
blocks waiting for the first to release the lock, and then finds the row no
longer matches and gets nothing. With `skip locked` it does not wait. It steps
over any row someone else has locked and takes the next one. This is what turns
one table into a work queue that N workers can share while doing zero
coordination with each other.

**The `or` clause** is the crash recovery, and it is three lines long. A run
marked `running` whose `lease_expires_at` is in the past is a run whose worker
stopped renewing. Nothing had to detect the failure. Nothing had to reap
anything. There is no supervisor process, no heartbeat table, no health check.
The absence of a renewal *is* the signal, and a `where` clause is the entire
recovery mechanism.

Renewal happens at the top of every iteration:

```python
def renew_lease(cur, run_id, worker_id, lease_seconds=60):
    cur.execute(
        "update runs set lease_expires_at = now() + make_interval(secs => %s), ..."
        " where id = %s and lease_owner = %s and status = 'running'",
        (lease_seconds, run_id, worker_id),
    )
    return cur.rowcount == 1
```

`rowcount == 1` means we still own it. Zero means somebody else took it while we
were slow, and the correct response is to stop writing immediately and throw:

```python
class LeaseLost(Exception):
    """Another worker took this run. Stop touching it immediately."""
```

Losing a lease is not an error condition. It means this worker was slow enough
to look dead. Continuing to write would be the error.

One detail I like: when a run suspends to wait for a human approval, it drops
its lease entirely. A human might take a day to click the button. A worker
holding a 60 second lease across that would spend the whole day looking
perpetually crashed to everyone else.

## Exactly once, and why it is really about transactions

Level 1 called this a receipt book. Here is what is actually going on.

The naive protocol has a hole in it that is worth staring at until it bothers
you:

```
1. run the tool          <-- money moves
                         <-- CRASH HERE
2. record that we ran it
```

Crash between 1 and 2 and you are in the worst state available: the world
changed and your system has no idea. On resume it refunds the customer again.

Reorder it and you get a different bad state. Record first, crash, and the
system believes it refunded someone it never refunded.

The general fix in distributed systems is a three-state protocol, `claimed`,
then `done`, plus a reconciliation job that goes and asks the payment provider
what actually happened to everything stuck in `claimed`. It is genuinely
annoying and every payments team has written one.

Deskhand does not need it, and the reason is one sentence:

> Every side effect in this system is a row in the same Postgres, so the effect
> and the record of the effect are written in the same transaction.

That is what the A in ACID buys you. Atomicity is not "my query is fast" or
"my data is safe." It is: these writes land together or none of them land. There
is no in-between state for a crash to catch you in, because the in-between state
does not exist at the storage layer.

```python
def invoke(cur, *, org_id, run_id, step_id, seq, tool_name, args):
    key = idempotency_key(run_id, seq)          # f"{run_id}:{seq}"

    already = _recorded(cur, key)                # 1. seen this key before?
    if already is not None:
        return already                           #    yes: return what it did

    ...
    outcome = tool.handler(ctx, args)            # 2. the refund row is inserted

    cur.execute(                                 # 3. same transaction
        "insert into tool_invocations (..., idempotency_key, ...) values (...)",
        (...)
    )
```

The caller commits. So:

- Crash before the commit: neither the refund row nor the ledger row exists.
  Nothing happened, nothing is remembered, the resumed run does it once.
- Crash after the commit: both exist. The resumed run finds the key, returns the
  recorded result, and does not touch the world again.

There is no third case. That is the whole guarantee.

And the key is derived, not generated:

```python
def idempotency_key(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"
```

A resumed run replays its persisted steps in order, arrives at step 6, computes
`8fc3...:6`, and the ledger recognises it. Reach for `uuid4()` here because it
feels more rigorous and you have silently deleted the entire mechanism: the
second attempt computes a key nobody has ever seen, finds no record, and refunds
the customer twice. The unique index on `idempotency_key` is the backstop that
turns a leasing bug into a constraint violation instead of a double payout.

## Savepoints, or: why one bad statement does not poison everything

A subtlety you will hit the first time you try to record a failure inside a
transaction.

In Postgres, once a statement in a transaction errors, the whole transaction is
poisoned. Every subsequent statement returns "current transaction is aborted."
So if a tool handler blows up on bad SQL, and you then try to write "this tool
call failed" into the ledger, that write fails too, and you lose the record of
the failure.

The fix is a savepoint, which is a nested transaction you can roll back to
without killing the outer one:

```python
try:
    with cur.connection.transaction():   # a SAVEPOINT
        tool.validate(args)
        faults.before(tool_name)
        outcome = faults.after(tool_name, tool.handler(ctx, args))
    ok, result, inverse = True, sanitise(outcome.result), outcome.inverse
except ToolError as exc:
    ok, result, inverse = False, sanitise(str(exc)), None
```

A handler that fails halfway leaves no partial write behind, and leaves the
surrounding transaction usable, which is what lets the very next statement
record that it failed.

Also notice `sanitise`. Postgres `text` and `jsonb` cannot hold a NUL byte, and a
tool returning one takes the whole run down with a `DataError` raised from the
ledger write, after the side effect has already committed inside the handler. The
worst possible ordering. This was found by a fault injector deliberately
returning garbage, on its first run, before the eval it was written for had even
executed.

## The registry: a dict you write once

The three risk classes from level 1 live in a frozen dataclass in a module-level
dict:

```python
class RiskClass(StrEnum):
    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"

@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    risk: RiskClass
    description: str
    parameters: dict[str, Any]
    handler: Handler
    preview: Callable[[dict[str, Any]], str] | None = None

_REGISTRY: dict[str, ToolDef] = {}
```

`_REGISTRY` is populated by `register()` calls at import time and never written
again. `register()` refuses a duplicate name. `frozen=True` means the dataclass
raises on attribute assignment.

And then the only question the runtime ever asks about a tool:

```python
def requires_approval(name: str) -> bool:
    return get(name).risk is RiskClass.IRREVERSIBLE
```

Answered by name, from the dict. Not from the model's request. Not from a tool
argument. Never from a previous tool's output. Trace every path by which
attacker-controlled text could reach that expression and you will find there
isn't one, which is a much stronger statement than "we told the model to be
careful."

This is a general design move worth stealing: when you have data that must never
be influenced by input, put it somewhere input cannot syntactically reach, and
then the argument is about program structure rather than about behaviour.

## Binding consent to arguments with a hash

A human approves "refund 19.00 USD against NW-1042." What exactly did they
approve?

Not "this agent may issue refunds." Not "this run may issue a refund." They
approved *that* refund, that amount, that order. So the approval stores a
fingerprint of the exact call:

```python
def args_hash(name: str, args: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool": name, "args": args}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()
```

`sort_keys=True` is doing real work. `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}`
are the same dict and must produce the same hash, but `json.dumps` preserves
insertion order by default and would give you two different strings. Canonical
form first, then hash. This is the same discipline as any other content
addressing scheme you will meet later.

When the run resumes and is about to execute the approved call, it recomputes
the hash from the arguments it is actually holding and compares:

```python
if decision["args_hash"] != args_hash(name, args):
    _end(cur, run, status="failed", reason=runs.STOP_APPROVAL_DENIED,
         detail=(f"the arguments to {name} changed after approval;"
                 " refusing to execute something a human did not see"))
    return "failed"
```

Note the behaviour on mismatch. It does not re-ask, and it does not execute the
approved version. It fails the run. If the arguments moved between consent and
execution, something is wrong at a level where guessing is the wrong response.

There is an eval that approves a 19.00 refund, rewrites the pending call to
48.00 mid-flight, and asserts the runtime refuses.

## Money is integers, all the way down

You have been told not to use floats for money. Here is the version with the
reason attached.

`0.1 + 0.2 != 0.3` in IEEE 754 because binary fractions cannot represent tenths
exactly, the same way decimal cannot represent a third. Every arithmetic
operation carries a small error, and errors accumulate. For a physics simulation
that is fine. For a spend cap, "did this run exceed its budget" acquires a fuzzy
answer, and fuzzy answers about money become tickets.

So:

- Order totals and refunds are **integer cents**. `1900` is 19.00 USD.
- Model spend is **integer micro-dollars**. `1_000_000` is 1.00 USD.
- Published model pricing is dollars per million tokens, which maps to
  micro-dollars per token exactly. 5.00 USD per MTok is 5 micros per token.
- Except cache reads are a tenth of the input rate and cache writes are 1.25x,
  and a tenth of 5 micros is not a whole number. So rates are held in
  **nanodollars**, a thousand times finer, and rounded to micros exactly once,
  when a step's cost is written.

Rounding once at a defined boundary rather than at every step is the whole
technique. It is the same reason you accumulate in a wider type and narrow at
the end.

## Bounds, checked before

Every run snapshots its limits at creation:

```sql
max_steps        integer not null,
max_tokens       bigint not null,
max_spend_micros bigint not null,
deadline_at      timestamptz not null,
```

Copied onto the row rather than read from config each step, for two reasons. A
deploy that raises the step cap must not silently extend a run already in
flight. And a run examined months later should be bounded the way it originally
was, not the way the config file reads today.

`deadline_at` is an absolute timestamp, not a duration. This matters more than it
looks. Store a duration and every resume restarts the clock, so a run that
crash-loops every 60 seconds runs forever while satisfying a 15 minute timeout
on every individual attempt. Absolute deadline, set once, inherited by every
resume.

And the loop detector, which is the failure mode specific to agents:

```sql
select tool_name, args_hash, count(*) as n from tool_invocations
 where run_id = %s group by tool_name, args_hash
having count(*) >= %s order by n desc limit 1
```

Same tool, same argument hash, three times. The step cap would eventually stop
this anyway, but only after paying for every lap, and it would end the run with
`step_cap`, which tells you nothing. This ends it with `loop_detected` plus
"called get_order with identical arguments 3 times." Diagnosing from a stop
reason at 3am is a real activity and precise stop reasons are worth the twenty
lines.

## What to take from this level

Four things, and none of them are about AI:

1. **Derive state, do not store it.** Progress in rows, position recomputed from
   those rows every time. You get crash recovery, audit, and replay from one
   decision.
2. **Atomicity is a design tool.** "The effect and the record of the effect share
   a transaction" removes an entire class of distributed systems problem. Notice
   when your architecture lets you use that, and notice when it does not.
3. **Deterministic identifiers where you need to recognise a repeat.** Random
   ones look safer and break idempotency silently.
4. **Put must-not-be-influenced data where input cannot reach it**, so your
   safety argument is structural rather than behavioural.

## Where the boundary is

- I said "the model's API is stateless, you send everything each turn." What that
  array actually contains, why it grows the way it does, and what it costs is
  level 3.
- I said tool results get wrapped in a delimiter. Why that defence is weak, and
  why prompt injection has no clean solution, is level 3.
- I have not explained how you test something non-deterministic at all. Level 3.

---

Next: [level 3, for a CS graduate learning AI](03-cs-grad-learning-ai.md).
