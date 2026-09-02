/**
 * Create a run and hand it to the deployed task.
 *
 *     TRIGGER_SECRET_KEY=tr_prod_… DATABASE_URL=… \
 *       node --experimental-strip-types scripts/start.ts NW-1
 *
 * The counterpart to `run-local.ts`, which drives the loop in this process.
 * This one does not drive anything: it writes the run row and triggers
 * `work-ticket`, and the work happens on Trigger.dev's infrastructure. If you
 * close this terminal a second later, the run carries on, suspends on the
 * approval gate, and waits there holding no compute. That difference is the
 * entire reason the port exists.
 *
 * The run row is created here rather than inside the task on purpose. A run's
 * bounds are snapshotted at creation, and the payload the task receives is
 * therefore a pointer to an already-bounded run rather than an instruction to
 * invent one. A task that created its own run could be triggered twice for the
 * same ticket and bound neither.
 */

import { tasks } from "@trigger.dev/sdk";
import { transaction } from "../src/db.ts";
import * as runs from "../src/runs.ts";

async function main(): Promise<number> {
  const reference = process.argv[2];
  if (!reference) {
    console.error("usage: start.ts <TICKET_REFERENCE>   e.g. NW-1");
    return 2;
  }
  if (!process.env.TRIGGER_SECRET_KEY) {
    console.error(
      "TRIGGER_SECRET_KEY is not set, so there is nothing to trigger. Use an environment " +
        "API key from the Trigger.dev dashboard, or run scripts/run-local.ts instead.",
    );
    return 2;
  }

  const ticket = await transaction(async (db) => {
    const { rows } = await db.query("select id, org_id from tickets where reference = $1", [
      reference,
    ]);
    return rows[0];
  });
  if (!ticket) {
    console.error(`no ticket ${reference}. Run \`python -m deskhand.seed\` against this database.`);
    return 1;
  }

  const runId = await transaction((db) =>
    runs.create(db, { orgId: String(ticket["org_id"]), ticketId: String(ticket["id"]) }),
  );

  // Tagged with the deskhand run id so the platform's run list and this
  // database can be joined by eye, which is most of what makes a deployed
  // demo legible to somebody who did not write it.
  const handle = await tasks.trigger(
    "work-ticket",
    { runId },
    { tags: [`deskhand:${runId}`, `ticket:${reference}`] },
  );

  console.log(`deskhand run  ${runId}`);
  console.log(`trigger run   ${handle.id}`);
  console.log(`\nwatch it:     https://cloud.trigger.dev/runs/${handle.id}`);
  console.log(
    `approve it:   TRIGGER_SECRET_KEY=… node --experimental-strip-types scripts/approve.ts ${reference} approve`,
  );
  return 0;
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
