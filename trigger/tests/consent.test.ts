/**
 * Invariant 2: no irreversible tool executes without a recorded human approval
 * bound to that exact run, step, and argument hash.
 *
 * The second test here is the one the port exists to make. Deskhand's version
 * of it rewrites a pending call from USD 19.00 to USD 48.00 by hand, to model a
 * hostile or buggy caller. On Trigger.dev the same situation arrives *without
 * anyone being hostile*, as the ordinary consequence of the platform's retry
 * semantics:
 *
 *   - attempt one asks for USD 19.00 and opens a waitpoint
 *   - the attempt fails after the token exists, for its own reasons: an
 *     uncaught error on the resume path, an OOM, a restore that does not come
 *     back. Note this is *not* "a worker dies while the human decides". There
 *     is no worker during the wait, which is exactly what the platform is for.
 *   - the platform re-enters `run()` from the top
 *   - attempt two re-derives the trajectory and this time asks for USD 48.00
 *   - the token is idempotent, so the wait resolves on attempt one's approval
 *
 * A human said yes to nineteen dollars. Forty-eight is about to leave. Nothing
 * in the platform can see the problem, because from its point of view a token
 * was created, a person completed it, and a run resumed, all correct. The only
 * thing standing between that sequence and the money is `args_hash`.
 */

import { strict as assert } from "node:assert";
import { after, test } from "node:test";
import { advance } from "../src/loop.ts";
import { FixedScriptProvider, type ContentBlock } from "../src/provider.ts";
import { shutdown } from "../src/runs.ts";
import {
  countRefunds,
  LocalWaiter,
  newRun,
  refundTotal,
  resetTicket,
  runRow,
  ticket,
} from "./helpers.ts";

after(async () => {
  await shutdown();
});

function refundScript(amountCents: number): ContentBlock[][] {
  return [
    [{ type: "tool_use", name: "get_ticket", input: { reference: "NW-1" } }],
    [{ type: "tool_use", name: "get_order", input: { reference: "NW-1042" } }],
    [{ type: "tool_use", name: "search_kb", input: { query: "refund policy window delivered" } }],
    [
      {
        type: "tool_use",
        name: "issue_refund",
        input: {
          order_reference: "NW-1042",
          amount_cents: amountCents,
          reason: "Quality complaint inside the published refund window.",
        },
      },
    ],
    [{ type: "text", text: "Refunded NW-1042." }],
  ];
}

test("an approved refund executes, once, for the amount approved", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  const waiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });
  const outcome = await advance(runId, {
    provider: new FixedScriptProvider(refundScript(1900)),
    waiter,
  });

  assert.equal(outcome.status, "succeeded");
  assert.equal(await countRefunds(runId), 1);
  assert.equal(await refundTotal(runId), 1900);
});

test("a denied refund does not execute, and the agent is told why", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  const waiter = new LocalWaiter({ approved: false, decidedBy: null, reason: "Out of policy." });
  const outcome = await advance(runId, {
    provider: new FixedScriptProvider(refundScript(1900)),
    waiter,
  });

  assert.equal(outcome.status, "succeeded", "a denial is the process working, not a failure");
  assert.equal(await countRefunds(runId), 0);
});

test("nobody answering ends the run as expired, not as denied", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  // `null` is the waitpoint timing out.
  const outcome = await advance(runId, {
    provider: new FixedScriptProvider(refundScript(1900)),
    waiter: new LocalWaiter(null),
  });

  assert.equal(outcome.status, "failed");
  assert.equal(outcome.reason, "approval_expired");
  assert.equal(await countRefunds(runId), 0);

  const row = await runRow(runId);
  assert.equal(row["stop_reason"], "approval_expired");
});

test("a divergent retry cannot spend the first attempt's approval", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  // One waiter across both attempts, so the token created by attempt one is
  // the token attempt two resolves, which is what Trigger.dev's global-scoped
  // idempotency key guarantees.
  const waiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });
  waiter.failFirstWait = true;

  // Attempt one: asks for USD 19.00, opens the waitpoint, then fails.
  await assert.rejects(
    advance(runId, { provider: new FixedScriptProvider(refundScript(1900)), waiter }),
    /attempt failed after the approval token was created/,
  );

  // Attempt two: the platform re-enters `run()` from the top. This time the
  // trajectory diverges and the model asks for USD 48.00, the full order, and
  // resumes on the approval a human gave for USD 19.00.
  const outcome = await advance(runId, {
    provider: new FixedScriptProvider(refundScript(4800)),
    waiter,
  });

  assert.equal(outcome.status, "failed");
  assert.equal(outcome.reason, "approval_denied");
  assert.equal(await countRefunds(runId), 0, "no money may move on consent nobody gave");

  const row = await runRow(runId);
  assert.match(String(row["stop_detail"]), /changed after approval/);

  // And the token really was shared. If it were not, attempt two would have
  // opened a second waitpoint and asked a second person, and the test would be
  // proving nothing.
  assert.equal(waiter.tokens.size, 1);
});

test("the same amount across a retry is fine: it refuses divergence, not retries", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  const waiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });
  waiter.failFirstWait = true;

  await assert.rejects(
    advance(runId, { provider: new FixedScriptProvider(refundScript(1900)), waiter }),
  );

  const outcome = await advance(runId, {
    provider: new FixedScriptProvider(refundScript(1900)),
    waiter,
  });

  assert.equal(outcome.status, "succeeded");
  assert.equal(await countRefunds(runId), 1);
  assert.equal(await refundTotal(runId), 1900);
});
