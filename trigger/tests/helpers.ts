/**
 * Test fixtures, and the local stand-in for the platform.
 *
 * These tests drive the *real* loop against the *real* seeded Postgres. Only
 * two things are substituted: the model, which is scripted so a scenario can
 * say "now it asks for a refund" deterministically, and the waiter, which
 * answers instead of suspending.
 *
 * Substituting the waiter is the honest limit of what can be checked without a
 * Trigger.dev login, and it is worth being precise about what it does and does
 * not establish. It does not test that Trigger.dev checkpoints and resumes a
 * suspended run. That is their code, it is the reason to use them, and taking
 * their word for it is the entire premise of the port. What it does test is
 * every claim in docs/TRIGGER-PORT.md about what the *port* still has to do:
 * that a divergent retry cannot execute on a stale approval, and that a replay
 * from the top does not refund twice. Those are claims about this repository,
 * and they are checked here rather than asserted.
 */

import { transaction } from "../src/db.ts";
import type { Waiter } from "../src/loop.ts";
import type { Decision } from "../src/consent.ts";
import * as runs from "../src/runs.ts";

export interface TicketFixture {
  orgId: string;
  ticketId: string;
  reference: string;
}

export async function ticket(reference: string): Promise<TicketFixture> {
  return transaction(async (db) => {
    const row = (
      await db.query("select id, org_id from tickets where reference = $1", [reference])
    ).rows[0];
    if (!row) throw new Error(`no seeded ticket ${reference}; run \`python -m deskhand.seed\``);
    return { orgId: String(row["org_id"]), ticketId: String(row["id"]), reference };
  });
}

/**
 * Put one ticket back the way the seed left it.
 *
 * These tests move real money in the real seeded database, which is the point
 * of them, so a second run would otherwise find NW-1042 already refunded to its
 * total and fail for a reason that has nothing to do with what is being tested. Refunds go first: `refunds.run_id` is `on delete set null`, so
 * dropping the runs would orphan them rather than remove them.
 */
export async function resetTicket(fixture: TicketFixture): Promise<void> {
  await transaction(async (db) => {
    await db.query(
      "delete from refunds where run_id in (select id from runs where ticket_id = $1)",
      [fixture.ticketId],
    );
    // Cascades to steps, approvals and tool_invocations.
    await db.query("delete from runs where ticket_id = $1", [fixture.ticketId]);
    await db.query(
      "delete from ticket_messages where ticket_id = $1 and author_kind = 'agent'",
      [fixture.ticketId],
    );
    await db.query("update tickets set status = 'open' where id = $1", [fixture.ticketId]);
  });
}

export async function newRun(fixture: TicketFixture): Promise<string> {
  return transaction((db) =>
    runs.create(db, { orgId: fixture.orgId, ticketId: fixture.ticketId }),
  );
}

export async function countRefunds(runId: string): Promise<number> {
  return transaction(async (db) => {
    const row = (
      await db.query("select count(*)::int as n from refunds where run_id = $1", [runId])
    ).rows[0]!;
    return Number(row["n"]);
  });
}

export async function refundTotal(runId: string): Promise<number> {
  return transaction(async (db) => {
    const row = (
      await db.query(
        "select coalesce(sum(amount_cents), 0)::int as total from refunds where run_id = $1",
        [runId],
      )
    ).rows[0]!;
    return Number(row["total"]);
  });
}

export async function runRow(runId: string): Promise<Record<string, any>> {
  return transaction((db) => runs.get(db, runId));
}

export async function ledger(runId: string): Promise<Array<Record<string, any>>> {
  return transaction(async (db) => {
    const { rows } = await db.query(
      `select idempotency_key, tool_name, args_hash, status::text, result
         from tool_invocations where run_id = $1 order by idempotency_key`,
      [runId],
    );
    return rows;
  });
}

/**
 * A waiter that answers instead of suspending.
 *
 * `tokens` is keyed by the same string the real adapter passes to
 * `idempotencyKeys.create`, so a second attempt at the same approval resolves
 * to the token the first attempt opened, which is what Trigger.dev's global-
 * scoped idempotency key does, and what makes the divergent-retry test in
 * `consent.test.ts` a faithful reproduction rather than a contrivance.
 */
export class LocalWaiter implements Waiter {
  readonly tokens = new Map<string, string>();
  readonly logs: Array<{ message: string; fields?: Record<string, unknown> }> = [];
  private readonly answer: Decision | null;
  /** Throw out of the first wait, modelling an attempt that fails after its
   * token exists: an uncaught error on the resume path, an OOM, a restore that
   * does not come back. Not a worker dying while a human decides, because no
   * worker is running then. The next call answers normally, which is what the
   * retried attempt sees. */
  failFirstWait = false;
  private waits = 0;

  constructor(answer: Decision | null) {
    this.answer = answer;
  }

  async createToken(opts: { key: string; timeoutSeconds: number; tags: string[] }) {
    const existing = this.tokens.get(opts.key);
    if (existing) return { id: existing };
    const id = `waitpoint_local_${this.tokens.size + 1}`;
    this.tokens.set(opts.key, id);
    return { id };
  }

  async forToken<T>(_tokenId: string): Promise<{ ok: boolean; output?: T }> {
    this.waits += 1;
    if (this.failFirstWait && this.waits === 1) {
      throw new Error("attempt failed after the approval token was created");
    }
    // `null` models the timeout branch: nobody answered.
    if (this.answer === null) return { ok: false };
    return { ok: true, output: this.answer as T };
  }

  log(message: string, fields?: Record<string, unknown>) {
    this.logs.push({ message, fields });
  }

  /** The `replayed` flags the loop logged, in order. `true` means a tool call
   * reached a step already in the ledger and did not touch the world again. */
  replayFlags(): boolean[] {
    return this.logs
      .filter((l) => l.message === "tool call")
      .map((l) => Boolean(l.fields?.["replayed"]));
  }
}
