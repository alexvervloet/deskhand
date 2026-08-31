/**
 * Invariant 1: a run survives a crash and never re-executes a completed side
 * effect.
 *
 * Deskhand's version of this test kills a worker after it has already refunded
 * a customer, lets the lease expire, has a second worker claim the run, and
 * asserts exactly one refund exists. Half of that setup is gone here: there is
 * no lease to expire and no second worker to claim anything. Trigger.dev
 * notices the failure and retries.
 *
 * What it retries is the whole of `run()`, from the top. That is the fact the
 * port turns on. The loop has no memory of the first attempt — `messages`
 * starts empty, the model is asked from the beginning, and the trajectory walks
 * straight back to `issue_refund`. Everything that made deskhand's crash
 * recovery careful is still required; only the machinery that *detected* the
 * crash has been handed over.
 *
 * So this test asserts the thing the platform does not: the customer is
 * refunded exactly once, and the second pass through the refund step reports
 * itself as a replay rather than a payment.
 */

import { strict as assert } from "node:assert";
import { after, test } from "node:test";
import { advance } from "../src/loop.ts";
import {
  FixedScriptProvider,
  ScriptedProvider,
  type ContentBlock,
  type Message,
  type ModelReply,
} from "../src/provider.ts";
import { shutdown } from "../src/runs.ts";
import {
  countRefunds,
  ledger,
  LocalWaiter,
  newRun,
  refundTotal,
  resetTicket,
  ticket,
} from "./helpers.ts";

after(async () => {
  await shutdown();
});

const FULL_SCRIPT: ContentBlock[][] = [
  [{ type: "tool_use", name: "get_ticket", input: { reference: "NW-1" } }],
  [{ type: "tool_use", name: "get_order", input: { reference: "NW-1042" } }],
  [{ type: "tool_use", name: "search_kb", input: { query: "refund policy window delivered" } }],
  [
    {
      type: "tool_use",
      name: "issue_refund",
      input: {
        order_reference: "NW-1042",
        amount_cents: 4800,
        reason: "Quality complaint inside the published refund window.",
      },
    },
  ],
  [
    {
      type: "tool_use",
      name: "add_internal_note",
      input: { reference: "NW-1", body: "Refund processed against NW-1042 after human approval." },
    },
  ],
  [{ type: "tool_use", name: "set_ticket_status", input: { reference: "NW-1", status: "resolved" } }],
  [{ type: "text", text: "Refunded NW-1042 and resolved NW-1." }],
];

/**
 * A model that dies at a chosen point in the trajectory.
 *
 * `crashAtTurn` is a turn index, which is derived from the message history, so
 * the crash lands at the same place regardless of which attempt is running.
 * Setting it to 4 means: the refund at turn 3 has executed and committed, and
 * the process dies before anything else happens. That is the worst moment
 * available — the money has moved and the run has no idea.
 */
class CrashingProvider extends ScriptedProvider {
  private readonly crashAtTurn: number;

  constructor(crashAtTurn: number) {
    super();
    this.crashAtTurn = crashAtTurn;
  }

  protected override plan(): ContentBlock[][] {
    return FULL_SCRIPT;
  }

  override async complete(
    system: string,
    messages: Message[],
    tools: Array<Record<string, unknown>>,
  ): Promise<ModelReply> {
    if (ScriptedProvider.turnIndex(messages) >= this.crashAtTurn) {
      throw new Error("machine died");
    }
    return super.complete(system, messages, tools);
  }
}

test("a crash after the refund does not refund the customer twice", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  const waiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });

  // Attempt one: works the ticket, gets the approval, pays the customer, dies.
  await assert.rejects(
    advance(runId, { provider: new CrashingProvider(4), waiter }),
    /machine died/,
  );

  assert.equal(await countRefunds(runId), 1, "the refund committed before the crash");
  assert.equal(await refundTotal(runId), 4800);

  // Attempt two: Trigger.dev re-enters `run()` from the top. Nothing is carried
  // over. The loop asks the model from turn zero and walks back to the refund.
  const replayWaiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });
  // The token the first attempt opened, as the platform's idempotency key would
  // return it.
  for (const [key, id] of waiter.tokens) replayWaiter.tokens.set(key, id);

  const outcome = await advance(runId, {
    provider: new FixedScriptProvider(FULL_SCRIPT),
    waiter: replayWaiter,
  });

  assert.equal(outcome.status, "succeeded");

  // The whole point.
  assert.equal(await countRefunds(runId), 1, "exactly one refund survives the retry");
  assert.equal(await refundTotal(runId), 4800);

  const rows = await ledger(runId);
  const refunds = rows.filter((r) => r["tool_name"] === "issue_refund");
  assert.equal(refunds.length, 1, "one ledger row, claimed once, under a deterministic key");
  assert.equal(refunds[0]!["idempotency_key"], `${runId}:8`);

  // And the second pass knew it was a replay rather than a payment. Every tool
  // call the first attempt completed comes back from the ledger.
  const flags = replayWaiter.replayFlags();
  assert.equal(flags.length, 6, "six tool calls in the full trajectory");
  assert.deepEqual(
    flags.slice(0, 4),
    [true, true, true, true],
    "the four calls attempt one completed are replayed, not re-executed",
  );
  assert.deepEqual(
    flags.slice(4),
    [false, false],
    "the two it never reached run for the first time",
  );
});

test("no human is asked twice for the same decision", async () => {
  const fixture = await ticket("NW-1");
  await resetTicket(fixture);
  const runId = await newRun(fixture);

  const waiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });
  await assert.rejects(advance(runId, { provider: new CrashingProvider(4), waiter }));

  const replayWaiter = new LocalWaiter({ approved: true, decidedBy: null, reason: null });
  for (const [key, id] of waiter.tokens) replayWaiter.tokens.set(key, id);
  await advance(runId, { provider: new FixedScriptProvider(FULL_SCRIPT), waiter: replayWaiter });

  // One approval row, not two. `on conflict (run_id, tool_use_id) do nothing`
  // is what holds this, and it holds it because the tool_use id is derived from
  // the trajectory rather than generated.
  const { transaction } = await import("../src/db.ts");
  const approvals = await transaction(async (db) => {
    const { rows } = await db.query(
      "select tool_use_id, args_hash, status::text from approvals where run_id = $1",
      [runId],
    );
    return rows;
  });
  assert.equal(approvals.length, 1);
  assert.equal(approvals[0]!["status"], "approved");
});
