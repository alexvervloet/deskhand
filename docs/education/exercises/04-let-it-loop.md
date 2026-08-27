# Exercise 04 — Let it loop

**Time:** 5 minutes. On why "it terminates eventually" is not boundedness.

## The premise

A run has a step cap. Whatever else happens, it stops after 24 steps. So loop
detection is belt-and-braces — a nicety on top of a hard limit.

Test that.

## The change

In [deskhand/runtime/loop.py](../../../deskhand/runtime/loop.py), make `_looping()`
always report nothing:

```python
def _looping(cur, run_id: str) -> str | None:
    return None
```

The step cap, the token cap, the spend cap and the deadline are all untouched.

## Predict first

Every ceiling is still in place, so every run still terminates. Do any evals
fail?

## What happens

```
python -m evals.run boundedness

  boundedness
    FAIL  identical-calls-are-caught-as-a-loop
    ok    a-run-that-will-not-stop-is-stopped
    ok    the-deadline-does-not-reset
    ok    spend-is-capped-before-the-call

3/4 passed
```

The run in the failing eval still terminates. It just terminates for the wrong
reason — `step_cap` instead of `loop_detected` — after burning every step it
had.

## Why that matters more than it looks

Compare what the operator sees.

**With loop detection:**

```
status:      exhausted
stop_reason: loop_detected
stop_detail: called search_kb with identical arguments 3 times
```

**Without:**

```
status:      exhausted
stop_reason: step_cap
stop_detail: reached the 24-step ceiling
```

The second is *true* and nearly useless. "Reached the step ceiling" is what a
genuinely hard problem looks like, and it is also what a stuck agent looks like,
and it is also what a too-tight budget looks like. The three want completely
different responses — respectively: raise the cap, fix the prompt or the tool,
lower the cap — and the message does not distinguish them.

The cost difference is real too. Loop detection fires on the third identical
call. The step cap fires on the twenty-fourth. Every one of those twenty-one
extra model calls is billed.

## The general point

A bound that stops a run is not the same as a bound that *explains* it.

This system has six ways to stop — `step_cap`, `token_cap`, `spend_cap`,
`deadline`, `loop_detected`, plus the budget ceilings — and they exist as
separate reasons precisely so that "why did it stop" has a specific answer.
Collapsing them into one catch-all limit would still guarantee termination and
would throw away every bit of information about *what went wrong*.

Notice that this is the third exercise in a row where the removed thing was
redundant for correctness and load-bearing for something else — legibility here,
disorderly-failure coverage in [03](03-make-the-key-random.md), structural
clarity in [02](02-remove-the-invisible-layer.md).

## Try it against the real thing

```bash
python -m deskhand.seed && uvicorn deskhand.main:app --reload
```

Run any ticket and watch the *steps* counter in the run header climb toward its
ceiling. That number, and the stop reason underneath it, are the two things an
operator actually reads when a run misbehaves.

## Restore

```bash
git checkout deskhand/runtime/loop.py
python -m evals.run
```
