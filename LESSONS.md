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
