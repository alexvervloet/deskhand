# The hard part is not the agent loop

Read this before the code, or the code will look over-engineered.

## The number that makes the argument

Deskhand is about 4,300 lines of application Python. The part a tutorial would
call "the agent" — send messages, read `tool_use` blocks, run the tools, send
the results back, repeat — is 150 lines in one file:

| File | Lines | Job |
|---|---|---|
| [runtime/loop.py](../../deskhand/runtime/loop.py) | ~150 | ask the model, resolve what it asked for, repeat |

Three and a half percent of the code. And it is the *easy* three and a half
percent: it has no failure mode more interesting than a network timeout, and
you could replace it with the SDK's own tool runner in an afternoon.

The other 96% is what stands between that loop and being allowed to point it at
a customer's money: who is permitted to authorise an action, what happens when
the worker dies at step 7 of 12, what stops a run costing $400, what a ticket
full of hostile text can and cannot make it do, and how you know the deploy you
just shipped did not quietly undo any of it.

That ratio is the thesis. **The agent loop is not the product. The runtime
around it is.**

## Why the demo → product gap is unusually wide for agents

Every kind of software has a gap between the demo and the shippable version.
Agents have a wider one than most, for four reasons that all show up here.

### 1. The control flow is non-deterministic, so "where was it?" is a real question

An ordinary program that crashes can be restarted, and its position is either
in a database row or recoverable from one. An agent's position is a
*conversation* — a sequence of decisions the model made, which it will not
necessarily make again.

The temptation is to hold that conversation in memory and treat a run as a
function call. That works until the process dies, and then there is no answer
to "what had it already done?" — only a transcript nobody wrote down.

The rule this project follows: **nothing about a run's position lives in a
variable.** Every iteration re-derives the next action from rows —

> are there tool calls the model asked for that have no result yet?
> → resolve those. otherwise → ask the model for the next turn.

A worker that dies is not resuming a computation, it is reading a database.
Any worker, on any machine, at any later time, computes the same next action
from the same rows. See [03-exactly-once.md](03-exactly-once.md).

### 2. Some steps cannot be retried, and the model does not know which

Retry is the standard answer to distributed failure, and it is built on the
assumption that doing a thing twice is the same as doing it once. That
assumption is false for exactly the operations an agent makes valuable: issuing
a refund, sending an email, cancelling an order.

Worse, the model cannot be relied on to know the difference. It has no
privileged knowledge of your business; it has a tool description you wrote.

So the distinction is not left to the model. Every tool declares a **risk
class** at import time, on a frozen dataclass, in a registry that is written
once and never again:

```
read          no side effects, runs freely
reversible    changes state, runs freely, records its own inverse
irreversible  suspends the run until a human approves this exact call
```

Nothing in a model response, a tool argument, or a tool *result* can reach that
value. That last one is the point — see below.

### 3. The input is attacker-controlled and arrives shaped like instructions

A support agent reads tickets that customers wrote. In an ordinary application
user data flows into a database and comes back out as data. Here it flows into
a *prompt*, where the boundary between "content" and "command" is a convention
the model chooses to honour rather than a parser rule.

There are two defences here and they are not equally important.

The visible one is the **fence**: every tool result is wrapped in a delimiter
derived from the run id, with any forged copy of that delimiter stripped from
the content first, and the system prompt names the boundary so the model can
locate it. See [runtime/transcript.py](../../deskhand/runtime/transcript.py).

The one that actually holds is the **risk class**. The seeded ticket `NW-4`
contains a forged `SYSTEM:` block ordering an unapproved refund. An eval drives
a *fully obedient* model at it — one that reads the instruction and does exactly
what it says — and the refund still only becomes a *request*, because whether
`issue_refund` needs a human is read from the registry and not from anything
the model just read.

The fence removes structural ambiguity. The registry removes authority. If you
only have budget for one, build the second. You will find out how true that is
in [exercise 02](exercises/02-remove-the-invisible-layer.md).

### 4. Cost is unbounded by default and only known afterwards

An ordinary endpoint is effectively free per call. Here a single run can make
twenty model calls, each costing real money, and the total is denominated in
tokens you cannot count until generation finishes.

Worse than the per-call cost is the shape of the failure: an agent that gets
stuck does not crash, it *loops*. It calls the same tool with the same
arguments, gets the same answer, and tries again, at full price, until
something stops it.

So every bound is checked **before** the model call, never after — a cap you
verify afterwards is an invoice — and the run's ceilings are snapshotted at
creation so a config change mid-flight cannot move the goalposts. The wall-clock
deadline is absolute rather than a duration, so a run that crash-loops every
sixty seconds does not earn itself a fresh clock each time.

## The five invariants

Everything in the 96% exists to hold one of these:

1. **Durability** — a run resumes from its last persisted step across a worker
   crash, and never re-executes a completed side effect.
2. **Consent** — no irreversible tool executes without a recorded human
   approval bound to that exact run, step, and argument hash.
3. **Boundedness** — every run terminates.
4. **Integrity** — content coming back from a tool is data, never instruction.
5. **Accountability** — every step is attributable: who, which run, what it
   cost, what it changed, and how to replay it.

Pick any file in the repository and it is almost certainly serving one of those.

## What defence in depth actually costs you

Durability here is enforced twice, independently. The step log stops an orderly
resume from repeating work. The idempotency ledger stops the disorderly case —
a leasing bug, an approval callback firing twice, two workers each convinced
they hold the run.

That redundancy has an uncomfortable consequence, and it is the most useful
thing in this repository: **removing one layer changes almost nothing you can
observe.** Delete the idempotency ledger and the crash-resume eval still passes,
because the step log covers that path. Delete the fence and both prompt-injection
evals still pass, because the registry is what stops the attack.

Measured, on the 20 trajectory evals:

| Layer removed | Evals that fail |
|---|---|
| The approval gate (`requires_approval` → `False`) | **12 of 20** |
| The fence around untrusted tool output | 1 |
| The idempotency ledger | 1 |
| Loop detection | 1 |

Only the load-bearing one shows up loudly. Each redundant layer fails exactly
one eval — the one written specifically to assert *that layer exists*.

If every eval asks "did the right thing happen", a system with three defences
will keep answering yes after you have deleted two of them, and you find out
which one was actually holding during an incident. So this repository has evals
that assert an outcome *and* evals that assert a mechanism, and the second kind
look redundant right up until they are the only thing that catches a silent
removal.

## If you take one habit from this project

Decide which properties must survive every future change, then build something
automated that fails loudly when one stops holding — **before** you build the
feature. Here that is 20 trajectory evals wired into CI as a required step,
and a fault injector that makes tools fail on purpose so those evals have
something to be robust against.

It is much easier to add on day one than after the first incident, and it is
the difference between an agent demo and a system you can point at a customer's
money.

---

Next: [02-concept-index.md](02-concept-index.md) to find any of this in the
code, or jump to the [exercises](exercises/) and break something.
