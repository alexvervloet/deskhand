/**
 * The approval gate, on waitpoint tokens.
 *
 * Invariant 2: *no irreversible tool executes without a recorded human approval
 * tied to that exact run, step, and argument hash.*
 *
 * Most of `deskhand/runtime/approvals.py` is gone. The pending state, the
 * expiry sweep, the "wake the run so it can notice nobody answered" dance, the
 * `awaiting_approval` run status, the suspend-and-requeue pair and the deadline
 * arithmetic that gave a run back the time a human spent thinking — all of that
 * was plumbing for *a process that has to survive not being in memory*, and a
 * waitpoint token is exactly that plumbing, done properly, by someone else.
 * `wait.createToken({ timeout })` even carries the TTL.
 *
 * What is left is this file, and it is left for a reason that has nothing to do
 * with durability.
 *
 * **A token id is a capability to resume. It is not a statement about what was
 * consented to.** Whoever holds it can complete the waitpoint with any payload
 * they like, and the run wakes up holding that payload. Nothing in the platform
 * knows that this particular resume was supposed to mean "a human looked at a
 * USD 19.00 refund against NW-1042 and said yes". So the binding between the
 * decision and the call still has to be ours:
 *
 *   1. At request time, record the tool, the arguments, and their hash, and
 *      render the preview a human will actually read. Server-side, before
 *      anyone is asked anything.
 *   2. At resume time, ignore every argument in the completion payload. Read
 *      the decision, nothing else.
 *   3. Re-hash the call that is *about to execute* and refuse unless it matches
 *      the hash recorded in step 1.
 *
 * Step 3 is not paranoia about a hostile approver. It is load-bearing for an
 * ordinary retry. Trigger.dev re-enters `run()` from the top on failure, so the
 * trajectory is re-derived; a token created under `idempotencyKey` is handed
 * back cached, so the *second* attempt resumes on the *first* attempt's
 * approval. If the model produced a different amount that time round, the
 * consent on record is for a call nobody is making any more. Deskhand's
 * `args_hash` catches that on a platform that has no idea it happened.
 *
 * Trigger.dev's `chat.agent()` has a `needsApproval: true` flag that expresses
 * the gate itself far better than this file does — declared on the tool, in
 * backend code, unreachable from a tool result, which is the property that
 * matters. This port does not use it because it builds on `task()`. Worth
 * noting either way: its documented resume path is that the frontend sends the
 * updated assistant message back and the SDK matches it by message ID. Matching
 * an id establishes *which* call is being answered, which is a different
 * question from *what* was agreed to.
 */

import type { PoolClient } from "pg";
import { argsHash, get } from "./tools/index.ts";

export interface ApprovalRecord {
  id: string;
  toolName: string;
  args: Record<string, unknown>;
  argsHash: string;
  preview: string;
  waitpointTokenId: string | null;
}

/** What a human's answer is allowed to say. Deliberately tiny. */
export interface Decision {
  approved: boolean;
  decidedBy?: string | null;
  reason?: string | null;
}

/**
 * Record that a human decision is needed, or return the request already on
 * file.
 *
 * Idempotent on (run_id, tool_use_id), which is what makes it safe to call from
 * a retried attempt: the second attempt must find the decision that was already
 * asked for, not ask a second person a second time.
 *
 * The preview is rendered here, from these arguments, and stored. It is never
 * derived from a tool result, and it is never re-rendered at approval time from
 * whatever the run happens to be holding — the human's screen and the hash are
 * generated from one read of one set of arguments, or they are not evidence of
 * anything.
 */
export async function requestApproval(
  db: PoolClient,
  opts: {
    orgId: string;
    runId: string;
    stepSeq: number;
    toolUseId: string;
    toolName: string;
    args: Record<string, unknown>;
    waitpointTokenId: string;
    ttlSeconds: number;
  },
): Promise<ApprovalRecord> {
  const tool = get(opts.toolName);
  const preview = tool.preview
    ? tool.preview(opts.args)
    : `${opts.toolName}(${JSON.stringify(opts.args)})`;

  await db.query(
    `insert into approvals (org_id, run_id, step_seq, tool_use_id, tool_name, args,
                            args_hash, preview, waitpoint_token_id, expires_at)
     values ($1, $2, $3, $4, $5, $6, $7, $8, $9, now() + make_interval(secs => $10))
     on conflict (run_id, tool_use_id) do nothing`,
    [
      opts.orgId,
      opts.runId,
      opts.stepSeq,
      opts.toolUseId,
      opts.toolName,
      JSON.stringify(opts.args),
      argsHash(opts.toolName, opts.args),
      preview,
      opts.waitpointTokenId,
      opts.ttlSeconds,
    ],
  );

  const row = await lookup(db, opts.runId, opts.toolUseId);
  if (!row) throw new Error(`approval for ${opts.toolUseId} vanished immediately after insert`);
  return row;
}

export async function lookup(
  db: PoolClient,
  runId: string,
  toolUseId: string,
): Promise<ApprovalRecord | null> {
  const row = (
    await db.query(
      `select id, tool_name, args, args_hash, preview, waitpoint_token_id
         from approvals where run_id = $1 and tool_use_id = $2`,
      [runId, toolUseId],
    )
  ).rows[0];
  if (!row) return null;
  return {
    id: String(row["id"]),
    toolName: row["tool_name"],
    args: row["args"],
    argsHash: row["args_hash"],
    preview: row["preview"],
    waitpointTokenId: row["waitpoint_token_id"],
  };
}

/** Raised when consent on record does not cover the call about to execute. */
export class ConsentMismatch extends Error {
  readonly toolName: string;

  constructor(toolName: string) {
    super(
      `the arguments to ${toolName} changed after approval;` +
        " refusing to execute something a human did not see",
    );
    this.name = "ConsentMismatch";
    this.toolName = toolName;
  }
}

/**
 * Check that the call about to run is the call that was approved.
 *
 * `args` is what the runtime is holding *now*, and `record` is what the human
 * was shown *then*. Both are hashed with the same canonical serialisation, and
 * a mismatch is refused rather than executed. A human who approved a USD 19.00
 * refund has not approved a USD 1,900.00 one, and the difference between those
 * two sentences is this function.
 *
 * Note what is not an input: the completion payload. The decision arrives from
 * outside the run and is trusted for exactly one bit — yes or no — plus who
 * said it, for the audit log. Any arguments it carries are ignored, because a
 * payload that could restate the call would be a payload that could change it.
 */
export function assertConsentCovers(
  record: ApprovalRecord,
  toolName: string,
  args: Record<string, unknown>,
): void {
  if (record.argsHash !== argsHash(toolName, args)) throw new ConsentMismatch(toolName);
}

/**
 * Record the human's answer. The waitpoint already woke the run; this is the
 * durable trace of who said what, which the platform's own run history does not
 * carry in a form the merchant's audit log can join against.
 */
export async function recordDecision(
  db: PoolClient,
  approvalId: string,
  decision: Decision,
): Promise<void> {
  await db.query(
    `update approvals set status = $1::approval_status, decided_by = $2,
                          decided_at = now(), reason = $3
      where id = $4 and status = 'pending'`,
    [
      decision.approved ? "approved" : "denied",
      decision.decidedBy ?? null,
      decision.reason ?? null,
      approvalId,
    ],
  );
}

/** Mark an approval nobody answered. A timed-out waitpoint means the process
 * around the agent was absent, which reads differently from a denial and is
 * recorded differently. */
export async function recordExpiry(db: PoolClient, approvalId: string): Promise<void> {
  await db.query(
    "update approvals set status = 'expired' where id = $1 and status = 'pending'",
    [approvalId],
  );
}
