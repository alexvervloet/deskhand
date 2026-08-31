# deskhand on Trigger.dev

A slice of deskhand's durable runtime, ported onto [Trigger.dev](https://trigger.dev)
to find out which parts of it were essential and which were the cost of doing
durability by hand.

The writeup is [`docs/TRIGGER-PORT.md`](../docs/TRIGGER-PORT.md). Read that
first. This file is just how to run it.

Same Postgres, same schema, same seed data as the Python service. A refund this
issues is indistinguishable from one `deskhand.worker` issues. Only the runtime
moved.

## Run it

```bash
docker compose up -d db             # from the repo root
python -m deskhand.migrate
python -m deskhand.seed

cd trigger
npm install
npm test                            # 26 tests, real Postgres, no account needed
```

Drive a ticket end to end without a Trigger.dev account:

```bash
node --experimental-strip-types scripts/run-local.ts NW-1
```

It stops at the approval gate. From another shell:

```bash
node --experimental-strip-types scripts/approve.ts --list
node --experimental-strip-types scripts/approve.ts NW-1 approve
```

`NW-4` is the interesting one. Its ticket body contains a forged `SYSTEM:` block
ordering an unapproved refund. Run it and watch the gate hold anyway, because
whether a tool needs approval is read from a registry frozen at import time that
nothing in a tool result can reach.

## Layout

| File | What it is |
| --- | --- |
| `src/loop.ts` | The agent loop. Takes its suspension mechanism as an argument. |
| `src/trigger/work-ticket.ts` | The task. 33 lines, and the whole of the platform's footprint. |
| `src/consent.ts` | The approval gate. Mostly deleted; `args_hash` is what stayed. |
| `src/invoke.ts` | The idempotency ledger, which a platform retry still needs. |
| `src/bounds.ts` | Steps, tokens, spend, deadline, loop detection. None of it moved. |
| `src/fence.ts` | Wrapping tool output as data rather than instruction. |
| `src/tools/` | The registry and the tools, split by risk class. |

## On `run-local.ts`

It drives the real loop, the real tools and the real database. What it fakes is
the suspension: instead of a waitpoint it polls the approvals table.

The difference is the thing worth paying for. That script has to stay running
for as long as the human takes, and if you kill it while it waits, the run is
gone. A suspended waitpoint holds no compute, does not count against
`maxDuration` (which measures CPU time, not elapsed time), and comes back.

## What is not here

No UI, no replay, no divergence, and no port of the eval harness. The slice is
NW-1 and NW-4 across the five invariants, which is what it takes to make the
claims in the writeup testable. `send_customer_email` and `cancel_order` are the
same shape as `issue_refund` and would demonstrate nothing further.

Not deployed, either. See "What I did not verify" in the writeup.
