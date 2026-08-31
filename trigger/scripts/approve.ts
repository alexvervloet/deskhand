/**
 * Answer the approval a run is waiting on.
 *
 *     node --experimental-strip-types scripts/approve.ts NW-1 approve
 *     node --experimental-strip-types scripts/approve.ts NW-1 deny "Out of policy."
 *     node --experimental-strip-types scripts/approve.ts --list
 *
 * Two modes, and the difference between them is the port's whole delivery
 * story. Against `run-local.ts` this writes the decision to the approvals row
 * and the polling waiter picks it up. Against a real deployment it also has to
 * *deliver* the decision, by completing the waitpoint token — which is what
 * wakes a run that is currently suspended and holding no compute at all.
 *
 * Completing the token needs a `TRIGGER_SECRET_KEY`, so it is attempted only
 * when one is set. That is not a fallback so much as a statement of the two
 * worlds: without the platform a process must be sitting there waiting, and
 * with it, nothing is.
 */

import { transaction } from "../src/db.ts";
import { shutdown } from "../src/runs.ts";

async function list(): Promise<number> {
  const rows = await transaction(async (db) => {
    const { rows } = await db.query(
      `select a.id, a.tool_name, a.preview, a.args_hash, a.waitpoint_token_id,
              t.reference, a.expires_at
         from approvals a
         join runs r on r.id = a.run_id
         join tickets t on t.id = r.ticket_id
        where a.status = 'pending' and a.expires_at > now()
        order by a.created_at`,
    );
    return rows;
  });

  if (rows.length === 0) {
    console.log("nothing is waiting on a human");
    return 0;
  }

  for (const row of rows) {
    console.log(`${row["reference"]}  ${row["tool_name"]}`);
    console.log(`  ${row["preview"]}`);
    console.log(`  args_hash ${String(row["args_hash"]).slice(0, 16)}…  token ${row["waitpoint_token_id"]}`);
  }
  return 0;
}

async function decide(reference: string, approved: boolean, reason: string | null): Promise<number> {
  const row = await transaction(async (db) => {
    const { rows } = await db.query(
      `update approvals set status = $1::approval_status, decided_at = now(), reason = $2
        where id = (
          select a.id from approvals a
            join runs r on r.id = a.run_id
            join tickets t on t.id = r.ticket_id
           where t.reference = $3 and a.status = 'pending' and a.expires_at > now()
           order by a.created_at limit 1
        )
        returning id, tool_name, preview, waitpoint_token_id`,
      [approved ? "approved" : "denied", reason, reference],
    );
    return rows[0];
  });

  if (!row) {
    console.error(`nothing pending on ${reference}`);
    return 1;
  }

  console.log(`${approved ? "approved" : "denied"}: ${row["preview"]}`);

  const token = row["waitpoint_token_id"];
  if (process.env.TRIGGER_SECRET_KEY && token && !String(token).startsWith("local_")) {
    const { wait } = await import("@trigger.dev/sdk");
    await wait.completeToken(String(token), { approved, decidedBy: null, reason });
    console.log(`completed waitpoint ${token}`);
  } else if (token && String(token).startsWith("local_")) {
    console.log("local run: the polling waiter will pick this up within a second");
  } else {
    console.log("no TRIGGER_SECRET_KEY set, so the waitpoint was not completed");
  }
  return 0;
}

async function main(): Promise<number> {
  const [first, verb, reason] = process.argv.slice(2);
  if (!first || first === "--list") return list();
  if (verb !== "approve" && verb !== "deny") {
    console.error("usage: approve.ts <TICKET_REFERENCE> <approve|deny> [reason]");
    console.error("       approve.ts --list");
    return 2;
  }
  return decide(first, verb === "approve", reason ?? null);
}

main()
  .then(async (code) => {
    await shutdown();
    process.exit(code);
  })
  .catch(async (error) => {
    console.error(error);
    await shutdown();
    process.exit(1);
  });
