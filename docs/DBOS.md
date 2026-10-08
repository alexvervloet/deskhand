# The same design, as a library: a note on DBOS

This is a note, not a port. Nothing here has been run. [TRIGGER-PORT.md](TRIGGER-PORT.md)
is the version with code and measurements behind it, and anything below that
reads like a result is a prediction.

## Why it's worth a note

Trigger.dev moved deskhand's state onto somebody else's platform. DBOS keeps it
where deskhand already keeps it: in your own Postgres. It's a library that
checkpoints each workflow step to the application's database, and since July
2026 a "transactional datasource" lets a step's checkpoint commit in the same
transaction as the rows the step wrote
([DBOS, July 2026](https://dbos.dev/blog/new-in-dbos-july-2026)).

That last part is the idempotency ledger. `invoke()` records a tool's effect
and its ledger row in one transaction, so a resumed worker finds the row and
doesn't act twice. Deskhand built that by hand for the same reason DBOS built
it into the library: a checkpoint written after the effect, in its own
transaction, leaves a window where the effect happened and the record of it
didn't.

## What would map onto what

| Deskhand | DBOS |
|---|---|
| a run, leased by a worker | a workflow, recovered on restart from its last completed step |
| `steps`, rebuilt into a conversation every turn | step outputs, replayed into the workflow function on recovery |
| a model call as a `model_call` row | a `@DBOS.step()` whose output is checkpointed |
| `invoke()` plus the ledger, in one transaction | a transactional step on a datasource |
| `awaiting_approval`, a polling worker | `DBOS.recv(topic, timeout_seconds=...)` in the workflow, `DBOS.send(...)` from the approve endpoint |
| the run id as the idempotency key for starting a run | `SetWorkflowID`, which returns the existing workflow instead of starting another |

## What I'd expect to delete, and what I'd expect to stay

The lease machinery, the worker process, `_unresolved` and `transcript.rebuild`
would go, the same parts the Trigger.dev port deleted, for the same reason:
they keep a process alive that isn't in memory, and the library does that.

What stayed on Trigger.dev would stay here too, because none of it is about
durability. The frozen registry and `requires_approval`. The `args_hash` check
at the moment of execution, which a `recv` that returns "approved" doesn't
replace: a message saying yes is not a message saying yes *to these bytes*.
The refund ceilings. The basis checks on the approval card, the per-call rules,
and the compensation planner, which reads the ledger and nothing else.

## The two things I'd check first

**Determinism.** DBOS recovers a workflow by running its function again and
substituting recorded step outputs. That makes the workflow code itself part
of the correctness argument: it has to make the same calls in the same order.
Deskhand's loop has no such requirement, because its position is a database
query and not a point in a function. Moving to DBOS trades "every resume
re-derives the next action from rows" for "the loop must be replayable", and
the crash-space search in `tests/test_concurrency.py` is what would show
whether the trade held.

**What the checkpoint does to the model's words.** `steps.content` was `jsonb`
for months, and jsonb sorts object keys, so every rebuilt turn told the model
it had written its tool arguments in a different order
([LESSONS 29](../LESSONS.md)). A step output that goes through a serializer on
the way into a checkpoint and back is the same question in a new place. The
conversation fingerprint from that entry is the test to point at it first.
