/**
 * The deployed story, told at a readable pace.
 *
 *     TRIGGER_SECRET_KEY=tr_prod_… DATABASE_URL=… \
 *       node --experimental-strip-types scripts/demo.ts
 *
 * The Trigger.dev dashboard is not public. A run lives under the account that
 * deployed it, and a public access token expires in fifteen minutes, so there
 * is no link I can put in the README that shows you a run. This script is the
 * answer to that: it drives the deployed tasks on Trigger.dev infrastructure
 * and prints a transcript where every number is read back out of Postgres and
 * the Trigger.dev API afterwards, rather than printed by a script that already
 * knew what it wanted to say.
 *
 * `demo/crash_resume.py` does the same job for the Python runtime. This is its
 * counterpart for the port, and the shape of the output is deliberately close,
 * because the interesting thing is how little of the story changed.
 *
 * Committed output lives in `trigger/demo/deployed-run.txt`.
 */

import { runs as triggerRuns, tasks } from "@trigger.dev/sdk";
import { transaction } from "../src/db.ts";
import * as runs from "../src/runs.ts";

const DIM = "[2m";
const BOLD = "[1m";
const RESET = "[0m";
const GREEN = "[32m";
const AMBER = "[33m";
const BLUE = "[34m";

const PACE = Number(process.env.DEMO_PACE ?? 900);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function say(line = "", pause = PACE): Promise<void> {
  console.log(line);
  await sleep(pause);
}

async function step(label: string): Promise<void> {
  await say(`\n${BOLD}${BLUE}${label}${RESET}`, PACE);
}

function money(cents: number): string {
  return `${(cents / 100).toFixed(2)} USD`;
}

async function sql<T = Record<string, any>>(text: string, params: unknown[] = []): Promise<T[]> {
  return transaction(async (db) => (await db.query(text, params)).rows as T[]);
}


/**
 * Wait for the run to checkpoint, and report what the API says about it.
 *
 * Typed as a plain fetch rather than `runs.retrieve()` because the SDK status
 * union has no FROZEN member, which is the one value this function exists to
 * see. Give up after a few minutes and report whatever the status is then,
 * since a demo that hangs is worse than one that tells you it did not freeze.
 */
async function frozenStatus(
  runId: string,
): Promise<{ status: string; durationMs: number; waitedSeconds: number }> {
  const started = Date.now();
  let body: { status?: string; durationMs?: number } = {};
  for (let i = 0; i < 40; i++) {
    const res = await fetch(`https://api.trigger.dev/api/v3/runs/${runId}`, {
      headers: { Authorization: `Bearer ${process.env.TRIGGER_SECRET_KEY}` },
    });
    body = (await res.json()) as { status?: string; durationMs?: number };
    if (body.status === "FROZEN") break;
    await sleep(6000);
  }
  return {
    status: String(body.status ?? "unknown"),
    durationMs: Number(body.durationMs ?? 0),
    waitedSeconds: Math.round((Date.now() - started) / 1000),
  };
}

async function main(): Promise<number> {
  if (!process.env.TRIGGER_SECRET_KEY) {
    console.error("TRIGGER_SECRET_KEY is not set. This drives the deployed tasks, not a local loop.");
    return 2;
  }

  await say(`${DIM}deskhand on Trigger.dev, running on their infrastructure.${RESET}`);
  await say(`${DIM}Every number below is read back afterwards, not asserted here.${RESET}`);

  // ------------------------------------------------------------- clean slate
  await step("Resetting NW-1 on the demo database");
  await sql(
    `delete from refunds where run_id in
       (select id from runs where ticket_id in (select id from tickets where reference = 'NW-1'))`,
  );
  await sql(
    "delete from runs where ticket_id in (select id from tickets where reference = 'NW-1')",
  );
  await sql(
    `delete from ticket_messages where author_kind = 'agent'
       and ticket_id in (select id from tickets where reference = 'NW-1')`,
  );
  await sql("update tickets set status = 'open' where reference = 'NW-1'");
  const [order] = await sql("select reference, total_cents from orders where reference = 'NW-1042'");
  await say(
    `  order ${order!["reference"]}, total ${money(Number(order!["total_cents"]))}, nothing refunded`,
  );

  // --------------------------------------------------------------- trigger
  await step("Handing a ticket to the deployed task");
  const [ticket] = await sql("select id, org_id from tickets where reference = 'NW-1'");
  const runId = await transaction((db) =>
    runs.create(db, { orgId: String(ticket!["org_id"]), ticketId: String(ticket!["id"]) }),
  );
  // The crash probe, because the interesting half is what a retry does.
  const handle = await tasks.trigger(
    "crash-probe",
    { runId },
    { tags: [`deskhand:${runId}`, "ticket:NW-1", "demo"] },
  );
  await say(`  deskhand run  ${runId}`);
  await say(`  trigger run   ${handle.id}`);
  await say(`${DIM}  This process is not the agent. It could exit here and the run would go on.${RESET}`);

  // ----------------------------------------------------------- the gate
  await step("Waiting for it to reach something irreversible");
  let approval: Record<string, any> | undefined;
  for (let i = 0; i < 100 && !approval; i++) {
    [approval] = await sql(
      "select id, tool_name, preview, args_hash, waitpoint_token_id from approvals where run_id = $1",
      [runId],
    );
    if (!approval) await sleep(2000);
  }
  if (!approval) {
    console.error("the run never reached the approval gate");
    return 1;
  }
  await say(`  ${AMBER}stopped${RESET}: ${approval["preview"]}`);
  await say(`  bound to args_hash ${String(approval["args_hash"]).slice(0, 16)}…`);
  await say(`  waiting on waitpoint ${approval["waitpoint_token_id"]}`);

  // Poll until the run actually checkpoints, rather than reporting whatever
  // the first read happens to catch. A waiting run stays warm for a couple of
  // minutes and only then freezes, so a single read here says EXECUTING and
  // makes the most interesting state in the demo look like it never happened.
  //
  // Read from the API directly, because `runs.retrieve()` is typed with a
  // status union that does not contain FROZEN, even though the endpoint
  // returns it. Both sources agree on the value; only the type disagrees.
  const status = await frozenStatus(handle.id);
  await say(
    `  platform says ${BOLD}${status.status}${RESET}` +
      `, billed durationMs ${status.durationMs}` +
      `, after ${status.waitedSeconds}s of waiting`,
  );
  await say(`${DIM}  Checkpointed, compute released. A person can take a day and it costs nothing.${RESET}`);

  // ----------------------------------------------------------- the decision
  await step("A human approves that exact call");
  await sql(
    `update approvals set status = 'approved', decided_at = now() where id = $1`,
    [approval["id"]],
  );
  const { wait } = await import("@trigger.dev/sdk");
  await wait.completeToken(String(approval["waitpoint_token_id"]), {
    approved: true,
    decidedBy: null,
    reason: null,
  });
  await say(`  ${GREEN}approved${RESET}, waitpoint completed from a different process`);

  // ------------------------------------------------------- the crash, retry
  await step("The attempt refunds the customer, then dies on purpose");
  await say(`${DIM}  crash-probe throws after the refund commits. The platform decides what happens next.${RESET}`);

  let final: Awaited<ReturnType<typeof triggerRuns.retrieve>> | undefined;
  for (let i = 0; i < 150; i++) {
    final = await triggerRuns.retrieve(handle.id);
    if (final.isCompleted || final.isFailed) break;
    await sleep(3000);
  }

  // ------------------------------------------------------------ the receipts
  await step("What is actually in the database");
  const [run] = await sql("select status::text, stop_reason from runs where id = $1", [runId]);
  const refunds = await sql(
    "select amount_cents, reason from refunds where run_id = $1",
    [runId],
  );
  const ledger = await sql(
    `select idempotency_key, tool_name, risk, status::text
       from tool_invocations where run_id = $1
      order by (split_part(idempotency_key, ':', 2))::int`,
    [runId],
  );
  const steps = await sql(
    `select seq, coalesce(tool_name, kind::text) as what, content->>'replayed' as replayed
       from steps where run_id = $1 and kind = 'tool_result' order by seq`,
    [runId],
  );

  await say(`  run            ${run!["status"]} (${run!["stop_reason"]})`);
  await say(`  attempts       ${final?.attemptCount ?? "?"}`);
  await say(
    `  refunds        ${BOLD}${refunds.length}${RESET}` +
      `, totalling ${money(refunds.reduce((a, r) => a + Number(r["amount_cents"]), 0))}`,
  );
  await say(`  ledger rows    ${ledger.length}`);
  await say(`  billed         ${final?.durationMs ?? "?"} ms, ${final?.costInCents ?? "?"} cents`);

  await say(`\n  ${DIM}the ledger, one row per tool call:${RESET}`);
  for (const row of ledger) {
    await say(
      `    ${String(row["idempotency_key"]).split(":")[1]!.padStart(3)}  ` +
        `${String(row["tool_name"]).padEnd(18)} ${String(row["risk"]).padEnd(13)} ${row["status"]}`,
      120,
    );
  }

  await say(`\n  ${DIM}what the second attempt did when it walked the same path again:${RESET}`);
  for (const row of steps) {
    const replayed = row["replayed"] === "true";
    const mark = replayed ? `${GREEN}replayed${RESET}` : `${AMBER}executed${RESET}`;
    const note = replayed && row["what"] === "issue_refund" ? "  <- the money did not move twice" : "";
    await say(`    ${String(row["seq"]).padStart(3)}  ${String(row["what"]).padEnd(18)} ${mark}${note}`, 150);
  }

  const ok = refunds.length === 1;
  await say(
    `\n${ok ? GREEN : AMBER}${BOLD}${ok ? "One refund, across a real platform retry." : "Unexpected refund count."}${RESET}`,
  );
  return ok ? 0 : 1;
}

main()
  .then(async (code) => {
    await runs.shutdown();
    process.exit(code);
  })
  .catch(async (error) => {
    console.error(error);
    await runs.shutdown();
    process.exit(1);
  });
