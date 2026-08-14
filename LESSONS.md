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
