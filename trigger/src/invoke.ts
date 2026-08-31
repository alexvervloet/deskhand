/**
 * Executing a tool call exactly once.
 *
 * This is the module the port was written to find out about. In deskhand it
 * makes invariant 1 true, *never re-execute a completed side effect*, and the
 * obvious guess before starting was that a durable execution platform would
 * make it redundant.
 *
 * It does not. Trigger.dev retries a failed run by re-entering `run()` **from
 * the top**, not from the point of failure. Everything the loop did before the
 * crash is replayed: the model calls, and, without this ledger, the refund. The
 * platform's own `idempotencyKey` solves the neighbouring problem, stopping a
 * retrying parent from re-*triggering* a child task, and their docs are
 * explicit that this is exactly-once task creation, not exactly-once side
 * effects. A refund is a side effect.
 *
 * So the protocol is unchanged from the Python:
 *
 *     1. Has this idempotency key already been recorded?  -> return what it did
 *     2. Otherwise run the handler
 *     3. Record the outcome under that key, in the SAME transaction
 *
 * A crash anywhere in the middle is safe in both directions. Nothing was
 * written, therefore nothing is remembered, therefore the retried run does it
 * once. And once the commit lands, the effect and the memory of it landed
 * together, so the retried run does it zero more times.
 *
 * The reason this is allowed to be so simple is that every side effect in this
 * system is a row in this same Postgres. A tool that charged a real payment
 * processor could not share a transaction with the ledger, and would need a
 * third `claimed` state plus reconciliation. That is as true on Trigger.dev as
 * it was on a hand-rolled worker: the platform moves where the retry comes
 * from, not what a retry means to an external API.
 */

import type { PoolClient } from "pg";
import { argsHash, get, validate, type ToolContext } from "./tools/index.ts";
import { ToolError } from "./tools/registry.ts";

export interface Invocation {
  toolName: string;
  risk: string;
  args: Record<string, unknown>;
  argsHash: string;
  result: string;
  ok: boolean;
  /** True when this call was already in the ledger, i.e. a retried run reached
   * a step it had already completed. The world was not touched again. */
  replayed: boolean;
  durationMs: number;
  inverse?: Record<string, unknown> | null;
}

/**
 * Make a tool result storable.
 *
 * Postgres `text` and `jsonb` cannot hold a NUL byte, and a tool that returns
 * one takes the whole run down with an error raised from the ledger write, after
 * the side effect has already happened. That is the worst possible place
 * to fail: the money moved and the record of it did not.
 *
 * Found in the Python original by the garbage fault in the evals, on its first
 * run. Real tools return NUL bytes more often than you would like: binary
 * payloads mislabelled as text, truncated UTF-8, a C library's buffer handed
 * over intact. It is carried into the port because node-postgres reports the
 * same failure the same way, at the same unrecoverable moment.
 */
export function sanitise(text: string): string {
  return text.replaceAll("\u0000", "\uFFFD");
}

/**
 * The key for the tool call at step `seq` of `runId`.
 *
 * Deterministic by construction. Nothing random, nothing clock-based: a uuid
 * here would quietly disable the whole mechanism while looking more rigorous.
 *
 * The determinism requirement is *stronger* on Trigger.dev than it was on the
 * Python worker, and this is the thing to be careful about when porting. There,
 * a resumed run replayed its persisted steps and recomputed the key from rows.
 * Here, a retried run recomputes the key from the trajectory it takes the
 * second time round. Those agree only while the trajectory is reproducible,
 * which is true of the scripted provider by construction and true of a real
 * model only in the weaker sense that the ledger degrades safely: a divergent
 * retry claims a fresh key and the run is bounded by its caps rather than by
 * this table. `docs/TRIGGER-PORT.md` says what that costs.
 */
export function idempotencyKey(runId: string, seq: number): string {
  return `${runId}:${seq}`;
}

async function recorded(db: PoolClient, key: string): Promise<Invocation | null> {
  const row = (
    await db.query(
      `select tool_name, risk, args, args_hash, result, status::text, inverse, duration_ms
         from tool_invocations where idempotency_key = $1`,
      [key],
    )
  ).rows[0];
  if (!row) return null;
  return {
    toolName: row["tool_name"],
    risk: row["risk"],
    args: row["args"],
    argsHash: row["args_hash"],
    result: row["result"],
    ok: row["status"] === "succeeded",
    replayed: true,
    durationMs: row["duration_ms"],
    inverse: row["inverse"],
  };
}

/**
 * Run one tool call, or return the record of having already run it.
 *
 * Throws only for failures that are not the model's business: a bug in a
 * handler, a database that went away. Those leave no ledger row, so the step is
 * retried intact. Failures that *are* the model's business (bad arguments, a
 * missing order, a policy violation) come back as `ok: false` with the message
 * the model should read and react to.
 */
export async function invoke(
  db: PoolClient,
  opts: {
    orgId: string;
    runId: string;
    stepId: string;
    seq: number;
    toolName: string;
    args: Record<string, unknown>;
  },
): Promise<Invocation> {
  const { orgId, runId, stepId, seq, toolName, args } = opts;
  const key = idempotencyKey(runId, seq);

  // Step 1. The run is single-writer for this key, so there is no concurrent
  // writer; the unique index on the ledger is the backstop that turns a bug
  // into an error rather than a double refund.
  const already = await recorded(db, key);
  if (already) return already;

  const tool = get(toolName);
  const fingerprint = argsHash(toolName, args);

  // The run's subject, read here rather than passed in, so no caller can hand a
  // handler a scope that disagrees with the run's own row.
  const subject = (
    await db.query(
      `select t.id as ticket_id, t.customer_id from runs r
         join tickets t on t.id = r.ticket_id where r.id = $1`,
      [runId],
    )
  ).rows[0];
  if (!subject) throw new Error(`run ${runId} has no ticket`);

  const ctx: ToolContext = {
    orgId,
    runId,
    stepId,
    ticketId: String(subject["ticket_id"]),
    customerId: String(subject["customer_id"]),
    db,
  };

  const started = process.hrtime.bigint();
  let ok: boolean;
  let result: string;
  let inverse: Record<string, unknown> | null | undefined;

  // A savepoint, so a handler that fails part-way through leaves no partial
  // write behind AND leaves the surrounding transaction usable. Without it,
  // one bad statement would poison the transaction we still need in order to
  // record that the call failed.
  await db.query("savepoint tool_call");
  try {
    validate(toolName, args);
    const outcome = await tool.handler(ctx, args);
    ok = true;
    result = sanitise(outcome.result);
    inverse = outcome.inverse;
    await db.query("release savepoint tool_call");
  } catch (error) {
    await db.query("rollback to savepoint tool_call");
    if (!(error instanceof ToolError)) throw error;
    ok = false;
    result = sanitise(error.message);
    inverse = null;
  }

  const durationMs = Number((process.hrtime.bigint() - started) / 1_000_000n);

  // Step 3. Written in the caller's transaction, alongside whatever the handler
  // just did.
  await db.query(
    `insert into tool_invocations
       (org_id, run_id, step_id, tool_name, risk, idempotency_key, args_hash,
        args, status, result, inverse, duration_ms)
     values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
    [
      orgId,
      runId,
      stepId,
      toolName,
      tool.risk,
      key,
      fingerprint,
      JSON.stringify(args),
      ok ? "succeeded" : "failed",
      result,
      inverse ? JSON.stringify(inverse) : null,
      durationMs,
    ],
  );

  return {
    toolName,
    risk: tool.risk,
    args,
    argsHash: fingerprint,
    result,
    ok,
    replayed: false,
    durationMs,
  };
}
