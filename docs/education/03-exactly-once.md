# Exactly once, and the assumption it rests on

The claim in the README is that a run "never re-executes a completed side
effect". This is how that is true, and — more usefully — the condition under
which it would stop being true.

## The problem

An agent issues a refund at step 8 of 12. The worker dies at step 9. Another
worker picks the run up.

The naive retry story says: re-run from the last checkpoint. But the last
checkpoint is *before* the refund, and re-running it pays the customer twice.
The careful retry story says: record that you did it. But then when do you
record it — before the refund, or after?

* **Before**: you crash between recording and doing. The record says the money
  moved and it did not. The customer is out of pocket and the ledger disagrees.
* **After**: you crash between doing and recording. The money moved, nothing
  remembers, and the resumed run does it again.

This is the classic dual-write problem, and it does not have a general
solution. It has a *conditional* one.

## What this system does

The ledger row is written in the **same transaction** as the tool's effect:

```python
# tools/invoke.py, in outline
already = _recorded(cur, key)          # 1. has this key run before?
if already is not None:
    return already                     #    -> return what it did, touch nothing

with cur.connection.transaction():     # 2. run the handler (savepoint)
    outcome = tool.handler(ctx, args)

cur.execute("insert into tool_invocations ...")   # 3. record it
# the caller commits
```

Because step 2 and step 3 land in one transaction, there is no window between
them:

* Crash before the commit → neither the refund row nor the ledger row exists.
  Nothing happened and nothing remembers it happening. The resumed run does it
  once.
* Crash after the commit → both exist. The resumed run finds the key, returns
  the recorded result, and does not touch the world.

There is no third state. That is the whole mechanism.

## The assumption

**Every side effect in this system is a row in the same Postgres.**

A refund is `insert into refunds`. An email is `insert into customer_emails`.
Cancelling an order is `update orders`. All of them can join the ledger's
transaction, so the two-phase problem collapses into one phase.

Take that away and the mechanism goes with it. A tool that charged Stripe could
not enrol Stripe in a Postgres transaction. You would be back to choosing
between recording before and recording after, and you would need:

1. A third state — `claimed` — written before the call, so a resumed run can
   tell "this may have happened" from "this definitely did not".
2. A **reconciliation** path that asks the provider what actually happened,
   keyed by an idempotency key you passed *them*. Every serious payments API
   has one; that is what it is for.
3. An answer for "the provider is down and we cannot reconcile", which is
   usually: park the run, alert a human, do not guess.

That is a materially bigger design, and this project does not implement it,
because implementing it would mean either taking a dependency on a real payment
provider or building a fake one — and the fake would prove nothing.

What it does instead is state the boundary plainly, in the module that relies
on it. The comment at the top of
[tools/invoke.py](../../deskhand/tools/invoke.py) says the same thing this page
does.

## The key

```python
def idempotency_key(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"
```

Deterministic on purpose. A resumed run replays its persisted steps in order,
arrives at the same step number, and computes the same key — which is the only
reason the ledger recognises anything.

A `uuid4()` here would look more rigorous and would silently disable the entire
mechanism: every attempt would mint a fresh key, find no prior record, and
execute. It would pass every test that does not involve a crash. That is
[exercise 03](exercises/03-make-the-key-random.md).

## Two layers, not one

The ledger is not the only thing standing between a crash and a double refund,
and it is worth being precise about which one fires when.

**The step log** catches the orderly case. Worker B rebuilds the conversation
from `steps`, sees that the refund's `tool_result` row already exists, and
therefore never calls the tool at all. In the crash-resume eval this is what
actually saves you — `invoke()` is not even reached.

**The ledger** catches the disorderly case: a leasing bug, an approval callback
firing twice, two workers each convinced they hold the run. Here `invoke()`
*is* reached, with the same key, and returns the recorded result.

You can see the split by breaking each one. Disable the ledger and
`crash-resume-pays-once` still passes; only the eval written for the ledger
fails. That is not a redundant eval — it is the only thing that would notice.

## What "replayed" means in the UI

A step badged `replayed` in the run viewer is one where `invoke()` found an
existing ledger row and returned it. It means: this step was reached a second
time, and the world was not touched again.

On a healthy run you will never see it. On a run that survived something, it is
the receipt.
