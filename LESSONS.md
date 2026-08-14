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
