/**
 * Drive one ticket through the loop without Trigger.dev.
 *
 *     node --experimental-strip-types scripts/run-local.ts NW-1
 *
 * This exists so the port can be read, run and argued with by someone who has
 * no Trigger.dev account, and it is honest about being a stand-in. The loop,
 * the tools, the fence, the bounds, the consent binding and the ledger are all
 * the real ones, running against the real seeded Postgres. What is faked is the
 * suspension: instead of a waitpoint, `PollingWaiter` sits on the approvals
 * table until somebody answers it.
 *
 * The difference between the two is exactly the thing worth paying for. This
 * script has to stay running for as long as the human takes, and if it is
 * killed while waiting, the run is simply gone. A suspended waitpoint holds no
 * compute, does not count against `maxDuration`, and comes back.
 *
 * Answer an approval from another shell:
 *
 *     node --experimental-strip-types scripts/approve.ts NW-1 approve
 */

import { transaction } from "../src/db.ts";
import { advance, type Waiter } from "../src/loop.ts";
import { getProvider } from "../src/provider.ts";
import * as runs from "../src/runs.ts";

const POLL_MS = 1_000;

class PollingWaiter implements Waiter {
  private readonly tokens = new Map<string, string>();

  async createToken(opts: { key: string; timeoutSeconds: number; tags: string[] }) {
    const existing = this.tokens.get(opts.key);
    if (existing) return { id: existing };
    const id = `local_${opts.key}`;
    this.tokens.set(opts.key, id);
    return { id };
  }

  async forToken<T>(tokenId: string): Promise<{ ok: boolean; output?: T }> {
    for (;;) {
      const row = await transaction(async (db) => {
        const { rows } = await db.query(
          `select status::text, decided_by, reason, expires_at < now() as stale
             from approvals where waitpoint_token_id = $1`,
          [tokenId],
        );
        return rows[0];
      });

      if (!row) throw new Error(`no approval carries token ${tokenId}`);
      if (row["stale"] && row["status"] === "pending") return { ok: false };

      if (row["status"] === "approved" || row["status"] === "denied") {
        return {
          ok: true,
          output: {
            approved: row["status"] === "approved",
            decidedBy: row["decided_by"] ? String(row["decided_by"]) : null,
            reason: row["reason"],
          } as T,
        };
      }

      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
  }

  log(message: string, fields?: Record<string, unknown>) {
    const extra = fields ? ` ${JSON.stringify(fields)}` : "";
    console.log(`${new Date().toISOString()} ${message}${extra}`);
  }
}

async function main(): Promise<number> {
  const reference = process.argv[2];
  if (!reference) {
    console.error("usage: run-local.ts <TICKET_REFERENCE>   e.g. NW-1");
    return 2;
  }

  const fixture = await transaction(async (db) => {
    const { rows } = await db.query("select id, org_id from tickets where reference = $1", [
      reference,
    ]);
    return rows[0];
  });
  if (!fixture) {
    console.error(`no ticket ${reference}. Run \`python -m deskhand.seed\` first.`);
    return 1;
  }

  const runId = await transaction((db) =>
    runs.create(db, { orgId: String(fixture["org_id"]), ticketId: String(fixture["id"]) }),
  );
  console.log(`run ${runId} on ${reference}`);
  console.log(`approve with: node --experimental-strip-types scripts/approve.ts ${reference} approve`);

  const outcome = await advance(runId, {
    provider: getProvider(),
    waiter: new PollingWaiter(),
  });

  console.log(`\n${outcome.status} (${outcome.reason})`);
  if (outcome.summary) console.log(outcome.summary);
  return outcome.status === "succeeded" ? 0 : 1;
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
