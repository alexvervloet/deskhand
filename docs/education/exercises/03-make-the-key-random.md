# Exercise 03 — Make the idempotency key random

**Time:** 5 minutes. A mistake that looks like an improvement.

## The premise

Here is the key that makes exactly-once execution work:

```python
def idempotency_key(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"
```

A reasonable reviewer might object to this. It is not globally unique. It is
guessable. It collides if you ever reuse a step number. Surely a uuid is the
professional choice?

## The change

In [deskhand/tools/invoke.py](../../../deskhand/tools/invoke.py):

```python
def idempotency_key(run_id: str, seq: int) -> str:
    import uuid
    return f"{run_id}:{seq}:{uuid.uuid4()}"
```

Now it is globally unique and unguessable.

## Predict first

Which evals fail? Note that the crash-resume eval — the one that kills a worker
after it has issued a refund — is right there in the `durability` group.

## What happens

```
python -m evals.run durability

  durability
    ok    crash-resume-pays-once
    FAIL  the-ledger-catches-a-double-execution
    ok    live-lease-is-not-stealable

2/3 passed
```

One failure. And, again, it is *not* the dramatic one — `crash-resume-pays-once`
still passes, cheerfully, with the idempotency mechanism completely disabled.

## Why the crash-resume eval doesn't notice

Because durability is enforced twice, and that eval exercises the other layer.

When worker B picks up the run, it rebuilds the conversation from the step log
and asks: are there tool calls with no result yet? The refund's `tool_result`
row is already there, so the answer is no. **`invoke()` is never called.** The
key it would have computed is irrelevant, because nothing computes it.

The ledger covers the case the step log cannot: something calls `invoke()` twice
for the same step. A leasing bug. An approval callback that fires twice. Two
workers each convinced they hold the run. In that case the key *is* computed
twice, and with a uuid it is different both times, so the tool executes twice
and the customer is refunded twice.

`the-ledger-catches-a-double-execution` exists to force that path directly — it
calls `invoke()` twice at the same step number, deliberately — which is why it
is the only thing that notices.

## What to take from it

Two things.

**Determinism was the feature.** The key looks like an identifier and is
actually a *derivation*: its job is that two independent attempts at the same
logical step arrive at the same string. Uniqueness would defeat that. Any time
you find yourself making an idempotency key "more unique", check what is
supposed to recognise it.

**The failure would have been invisible in production for a long time.** The
common path — orderly crash, orderly resume — is covered by the step log. You
would only discover the ledger was inert during the exact incident it existed
to survive: the disorderly one, at 3am, in the form of a customer refunded
twice.

This is the same shape as [exercise 02](02-remove-the-invisible-layer.md). A
redundant layer, silently removed, with one mechanism-shaped eval standing
between you and finding out later.

## Restore

```bash
git checkout deskhand/tools/invoke.py
python -m evals.run
```
