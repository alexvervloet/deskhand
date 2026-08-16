# Lessons

Things that did not go the way the plan assumed, written down while the detail
was still fresh. Entries that are just "the plan worked" are omitted.

---

## 1. A module-level connection pool needs an explicit exit hook

**Expected.** A lazily-created `ConnectionPool` at module scope would behave
like any other global: build on first use, get collected at exit, no ceremony.

**What happened.** Every short-lived script — `python -m deskhand.seed`, a
one-off query — ended in a wall of warnings:

```
couldn't stop thread 'pool-1-worker-0' within 5.0 seconds
hint: you can try to call 'close()' explicitly or to use the pool as context manager
```

`psycopg_pool` runs its own worker and scheduler threads. They are not daemon
threads, so interpreter shutdown waits on them, times out, and complains. The
queries all succeeded — the noise arrives *after* the useful output, which is
exactly where it is most likely to be read as a failure.

**Fix.** `atexit.register(close_pool)` at the point the pool is created, in
[deskhand/db.py](deskhand/db.py).

**Next time.** Any pooled resource created at module scope gets its teardown
registered in the same breath as its construction. The tell is a library that
spawns threads you did not ask for; assume they need to be told to stop.

---

## 2. Postgres full-text search fails *open* on a policy lookup

**Expected.** `websearch_to_tsquery` is the helper built for natural-language
queries, so it looked like the obvious choice for a knowledge-base tool an
agent drives in its own words.

**What happened.** It ANDs every term. The seeded query "stale coffee refund
window" matched nothing — not because the refund policy was missing, but
because that article never uses the word *window*. The tool returned:

```
No knowledge-base article matches 'stale coffee refund window'.
```

`plainto_tsquery` does the same thing.

**Why it is worse than a bad search result.** The tool whose entire job is
answering "am I allowed to do this" returned *there is no policy* when the
policy existed and was one word away. An agent reading that reasonably
concludes it is unconstrained and proceeds. A retrieval bug turned into a
permissions bug, and it would have shown up in a demo as the agent confidently
refunding something outside the window.

**Fix.** Tokenise to word characters, OR the terms, and let `ts_rank` do the
work: a document matching four of five terms outranks one matching two, so the
result degrades in quality instead of vanishing. In
[deskhand/tools/read.py](deskhand/tools/read.py), with a regression test named
after the failure mode rather than the function.

**Next time.** For any tool whose empty result would be read as permission,
ask what happens when it returns nothing, and make sure the answer is
"degrades" rather than "fails open". The general version: retrieval quality
bugs stop being quality bugs the moment retrieval is what gates an action.

---

## 3. The runtime tests silently broke the tool tests, and the obvious fix was
slow enough to be useless

**Expected.** Each test module reads the seeded world, so one seed per session
would do.

**What happened.** The runtime tests issue real refunds and resolve real
tickets. They run before `test_tools.py` alphabetically, so by the time the
tool tests asked "how much of NW-1042 is refundable" the answer had changed.
Fourteen failures, none of them in the code under test, and every one of them
would have looked like a real regression to someone reading CI output.

The obvious fix — reseed before every test that writes — was correct and took
the suite from 2 seconds to 30. The cost was bcrypt: seeding hashes five demo
accounts, bcrypt is deliberately slow, and that is per test rather than per
session.

**Fix.** Memoise the hash for the shared demo password once per process
(`_demo_hash()` in [deskhand/seed.py](deskhand/seed.py)). All five demo
accounts share one published password, so they can share one hash; real signup
still hashes per user. Suite back to 2.6 seconds, and now order-independent.

**Next time.** Two things. Order-dependence between test modules is invisible
until the second module that writes shows up, so establish per-test isolation
when the *first* one does. And when a correctness fix makes the suite slow
enough that people will start skipping it, that is a bug in the fix — find the
one expensive thing inside it rather than accepting the tradeoff.

---

## 4. A green test suite and a broken screen

**Expected.** The stream endpoint had a passing test. It asserted that steps
arrive, that a `done` event closes the stream, and that the count was right.

**What happened.** Running the real thing found a bug the test could not: the
`status` and `done` events were built with `_run_summary(run | {"ticket_reference": None})`,
because the query behind them did not join `tickets` and the field had to be
filled with *something*. The client merges each status event into the run it is
displaying, so the run header would show `NW-1` until the first status arrived
and then go blank — for the rest of the run.

The test asserted that events were *emitted*. It said nothing about whether
they were *complete*. Both bugs on this screen were of that shape: the pending
approvals in the stream had the same missing join.

**Fix.** Join the ticket in both queries, and add a test that walks every
summary the stream emits and asserts the reference is present on each one —
which is the assertion the original test should have made.

**Next time.** For an endpoint whose output a client merges into existing
state, "did it emit" is the weak version of the question. The useful one is
"is every field on every message correct", because a partial message does not
fail — it overwrites. And running the product end to end catches a class of
thing that no unit test was ever going to; the first real click found two.

---

## 5. The fault injector found a real crash on its first run

**Expected.** The `garbage` fault — a tool returning binary noise — was written
to check that the agent survives nonsense. A box to tick.

**What happened.** It never got as far as the agent:

```
psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes
```

Postgres `text` and `jsonb` cannot hold a NUL byte. The exception came from the
*ledger write* in `invoke()` — which happens **after** the handler has already
run. So the failure mode was: the refund is issued, the write recording that it
was issued blows up, the transaction rolls back, and the run dies. On a real
external side effect that would be money moved with no record of moving it. It
is difficult to design a worse place for a crash.

Real tools return NUL bytes more often than is comfortable: binary payloads
mislabelled as text, truncated UTF-8, a C library handing over its buffer
intact.

**Fix.** `sanitise()` in [deskhand/tools/invoke.py](deskhand/tools/invoke.py),
replacing NUL with U+FFFD rather than dropping it — a result that had a NUL in
it should look like it had a NUL in it.

**Next time.** Nothing here is subtle in hindsight, and I would not have found
it by thinking harder about the code. The lesson is about the *order of work*:
the fault injector was on the plan as scaffolding for the evals, so it felt
like tooling rather than testing. It paid for itself before the first eval it
was built to support even ran. Build the thing that makes failures happen
early, not once everything else is finished.

---

## 6. Defence in depth means most of your evals keep passing when you break something

**Expected.** Nineteen trajectory evals across five invariants. Deliberately
break a safety mechanism and watch the gate light up.

**What happened.** It depends entirely on *which* mechanism, and the pattern is
worth staring at. Removing the approval gate (`requires_approval` → `False`):

```
8/19 passed   — 11 failures across durability, consent, integrity, accountability
```

Good. But deleting the fence around untrusted tool output:

```
3/4 integrity evals still passed
```

Only the eval written specifically to assert *the fence exists* caught it. Both
prompt-injection evals passed with the fence gone — because the fence is not
what actually stops the attack. The risk class does. The fence removes
structural ambiguity; the registry removes authority. Kill the fence and a
fully obedient model still cannot escalate, so the outcome-shaped evals see
nothing wrong.

Same shape for the idempotency ledger. Disable it and `crash-resume-pays-once`
still passes, because an orderly resume is caught by the step log; the ledger
only covers the disorderly case (a leasing bug, an approval firing twice). One
eval failed — the one written for that layer specifically.

**What this means.** Redundancy is the point, and redundancy makes each
individual layer *invisible to outcome testing*. If every eval asks "did the
right thing happen", a system with three defences will keep answering yes after
you delete two of them — and you will find out which one was load-bearing
during an incident.

**Next time.** Write one eval per layer that asserts *the layer is present*,
separately from the evals that assert the outcome is right. `every-tool-result-is-fenced`
and `the-ledger-catches-a-double-execution` exist for exactly this reason and
would otherwise look redundant next to the injection and crash-resume evals.
They are not redundant; they are the only thing standing between a silent
removal and production. (The companion project reaches the same conclusion from
the other direction, in its exercise on removing an invisible layer.)

---

## 7. Two correct decisions that combined into an incoherent demo

**Expected.** The keyless mock provider picks one of a few fixed trajectories
by keyword. Boring, deterministic, nothing to think about.

**What happened.** A smoke test of the built container drove the "where is my
order" ticket and the run stopped at the approval gate asking to refund the
customer 19.00 USD. Nobody had asked for a refund.

Two earlier decisions, each right on its own, produced it:

1. **Lesson 2** made knowledge-base search OR its terms and rank, so a policy
   lookup degrades instead of failing open. Correct — and it means a search for
   *"shipping times tracking delay"* now also returns the **Refund policy**
   article, because that article contains "delivery" and "days".
2. **The provider is stateless by design**, so a resumed run reaches the same
   decision as the worker that died. Correct — and it means the mock recomputes
   its plan from the transcript on *every* turn.

Compose them: turn 1 reads the ticket, sees no refund language, sets off down
the shipping path. Turn 2's knowledge-base result now contains the word
*refund*. Turn 3 recomputes the plan, concludes it has been working a refund
all along, and asks to move money.

**Fix.** The plan is derived from the opening prompt and the *first* tool
result only, and stops there — `_brief()` in
[deskhand/providers.py](deskhand/providers.py). The ticket is what the plan is
about, so the plan reads the ticket and stops reading.

**Next time.** Neither decision was wrong and neither review would have caught
this, because the interaction lives in the space between two files that never
mention each other. The thing that found it was running the actual product on
data I had not hand-picked — the same move that found the bugs in lesson 4.
Worth generalising: for anything that recomputes a decision from accumulating
context, ask what happens when the context grows to contain a word that changes
the decision. "Stateless" and "reads everything" are a bad pair.

---

## 8. The tracer worked perfectly everywhere except production

**Expected.** `deskhand/tracing.py` emits a structured JSON line per event. I
watched a full run print fourteen of them locally, checked the fields, wrote
seven tests covering the awkward cases, and moved on.

**What happened.** After deploying, I grepped Fly's log stream for the events
and got nothing. Not malformed, not truncated — absent.

The local run printed them because I had called `logging.basicConfig()` in the
throwaway script I used to watch them. Under uvicorn nobody calls it: the root
logger has no handler and sits at WARNING, so every `log.info()` from a logger
with no level of its own is discarded. The tests passed throughout, because
pytest's `caplog` attaches its own handler and sets the level for you.

So the feature was dead in the only environment that mattered, and all three
places I had looked — the local run, the test suite, the code itself — agreed it
was fine.

**Fix.** `_configure()` in [deskhand/tracing.py](deskhand/tracing.py): the event
stream gets its own stdout handler and its own level at import, with
`propagate = False` so an application that *does* configure the root logger gets
one copy of each line rather than two. A library has no business doing this. An
application's dedicated event stream does, because the alternative is a stream
that only works when somebody remembered to configure it.

**Next time.** Two things I will actually change. Logging is configuration, not
code, so "it printed on my machine" is evidence about my machine — verify
observability *in the deployed environment*, which took one `flyctl logs` and
would have taken one at any point. And be suspicious of test helpers that make
a thing work: `caplog` attaching a handler is convenient and it silently removed
the exact failure mode from the suite. A test that passes because the harness
configured something the product does not configure itself is testing the
harness.

---

## 9. The type checker I was not running had thirty things to say

**Expected.** `mypy` clean on every commit, so the code is type-checked.

**What happened.** Opening the project in an editor showed errors everywhere.
Pylance runs Pyright, not mypy, and Pyright had **30 errors** on a tree mypy
called clean.

Two causes, and the first is embarrassing:

**`files = ["deskhand"]`.** mypy had never looked at `tests/`, `evals/`,
`demo/`, or `check_setup.py` — about 2,400 lines, a third of the project. I had
written that line on day one to get a clean baseline and never revisited it.
Twenty-two of the thirty errors were in files no checker had ever read.

**Pyright is stricter, and was right.** The remaining eight were real. The best
of them: psycopg 3.3 types its `query` parameter as `LiteralString`, not `str`
— deliberately, so a query cannot be assembled from a variable that might hold
request data. My `db.py` helpers took `str` and passed it straight through,
which type-checked under mypy and quietly discarded the guarantee. Adopting
`LiteralString` made one place fail: a test helper building an `UPDATE` from
keyword arguments with an f-string. It was safe in context and it was also
exactly the shape of an injection, so it now composes properly with
`psycopg.sql.Identifier`.

**Fix.** Both checkers over the whole tree, both in CI, and a `db.one()` helper
for the twenty "row could be None" errors that came from `fetch_one(...)["id"]`
— a missing row there is a bug, not a branch, and saying so once beats an
`assert` at every call site.

**Next time.** Two things. A type checker's scope is part of its configuration
and deserves the same suspicion as its strictness — "mypy passes" meant much
less than I thought it did, and nothing in the output said so. And if the
editor and CI run different tools, the one CI does not run will drift until
someone opens the project and finds it full of red. Run in CI what the editor
runs.
