/**
 * Run records: creating them, appending to them, ending them.
 *
 * Compare `deskhand/runtime/runs.py`, whose module docstring opens "The lease is
 * the concurrency story." There is no lease here. `claim_next`, `renew_lease`,
 * `LeaseLost`, `lease_owner`, `lease_expires_at`, the `for update skip locked`
 * queue query, the poll loop, and the worker process that ran it are all gone,
 * because deciding which process is allowed to advance which run is the problem
 * Trigger.dev exists to solve. That is the single largest deletion in the port
 * and it is not close.
 *
 * `suspend_for_approval` and `requeue` went with them, and took something less
 * obvious along: the deadline arithmetic. Deskhand had to record `suspended_at`
 * and hand the elapsed wait back to `deadline_at`, because otherwise a person
 * taking an approval seriously spent the run's wall-clock budget on their own
 * deliberation. A suspended waitpoint does not consume `maxDuration`, so the
 * bug that column existed to fix cannot occur.
 *
 * What survives is the accounting: the bounds snapshotted at creation, the
 * usage counters, the stop-reason vocabulary, and the append-only step log.
 *
 * The step log survives for a *different reason* than it had in Python, and
 * that difference is most of the writeup. There, `steps` was the resume
 * mechanism: a worker that came to a run mid-trajectory rebuilt the
 * conversation by replaying these rows, so they had to be complete, ordered and
 * strictly append-only or a resumed run would reach a different decision. Here
 * the conversation is a variable that the platform checkpoints across a wait.
 * Nothing reads these rows to decide what to do next. They are written because
 * "who did what, at what cost, and how do I replay it" is invariant 5, and no
 * amount of durable execution answers that for the merchant's auditor.
 *
 * It is no longer append-only, and `appendStep` says why. Losing that property
 * is a real cost, paid for a real reason, and it is the kind of thing that
 * should be written down rather than discovered later by whoever trusts the
 * table.
 */

import type { PoolClient } from "pg";
import { pool } from "./db.ts";

/** The vocabulary of endings. Fixed, because the UI renders these, the evals
 * assert on them, and "why did it stop" is the first question anyone asks. */
export const STOP = {
  END_TURN: "end_turn",
  STEP_CAP: "step_cap",
  TOKEN_CAP: "token_cap",
  SPEND_CAP: "spend_cap",
  DEADLINE: "deadline",
  LOOP: "loop_detected",
  APPROVAL_DENIED: "approval_denied",
  APPROVAL_EXPIRED: "approval_expired",
  ORG_BUDGET: "org_daily_budget",
  PLATFORM_BUDGET: "platform_daily_budget",
  REFUSAL: "model_refusal",
  ERROR: "error",
} as const;

const CONFIG = {
  maxStepsPerRun: Number(process.env.MAX_STEPS_PER_RUN ?? 24),
  maxTokensPerRun: Number(process.env.MAX_TOKENS_PER_RUN ?? 400_000),
  maxSpendMicrosPerRun: Number(process.env.MAX_SPEND_MICROS_PER_RUN ?? 2_000_000),
  maxRefundCentsPerRun: Number(process.env.MAX_REFUND_CENTS_PER_RUN ?? 100_000),
  maxWallclockSeconds: Number(process.env.MAX_WALLCLOCK_SECONDS_PER_RUN ?? 900),
} as const;

/**
 * Queue a run against one ticket.
 *
 * Bounds are snapshotted here rather than read at each step: a config change
 * mid-flight must not move the goalposts for a run already under way.
 *
 * **The prompt names the ticket and quotes none of it.** The reference is an
 * identifier this system minted; the subject is a line a customer typed into a
 * form. Interpolating the subject here looks harmless, since it is one short
 * line that helps the agent know what it is picking up, but the opening prompt
 * is the one message that is not fenced, so that line would be the single piece of
 * customer text reaching the model as trusted narration. The subject is not
 * lost: `get_ticket` returns it, inside the fence, with the body it belongs to.
 */
export async function create(
  db: PoolClient,
  opts: { orgId: string; ticketId: string; startedBy?: string | null },
): Promise<string> {
  const ticket = (
    await db.query("select reference from tickets where id = $1 and org_id = $2", [
      opts.ticketId,
      opts.orgId,
    ])
  ).rows[0];
  if (!ticket) throw new Error("no such ticket for this org");

  const prompt =
    `Work support ticket ${ticket["reference"]}.\n\n` +
    "Read the ticket, establish the facts from the order record and the knowledge " +
    "base, and then do what is actually due. Finish by summarising what you did and " +
    "why. If the right answer is that a human has to decide, say so and escalate " +
    "rather than guessing.";

  const row = (
    await db.query(
      `insert into runs (org_id, ticket_id, started_by, prompt, status, max_steps, max_tokens,
                         max_spend_micros, max_refund_cents, deadline_at)
       values ($1, $2, $3, $4, 'running', $5, $6, $7, $8, now() + make_interval(secs => $9))
       returning id`,
      [
        opts.orgId,
        opts.ticketId,
        opts.startedBy ?? null,
        prompt,
        CONFIG.maxStepsPerRun,
        CONFIG.maxTokensPerRun,
        CONFIG.maxSpendMicrosPerRun,
        CONFIG.maxRefundCentsPerRun,
        CONFIG.maxWallclockSeconds,
      ],
    )
  ).rows[0]!;
  return String(row["id"]);
}

export async function get(db: PoolClient, runId: string): Promise<Record<string, any>> {
  const row = (await db.query("select * from runs where id = $1", [runId])).rows[0];
  if (!row) throw new Error(`no run ${runId}`);
  return row;
}

/**
 * Record one step. Not an append: see below.
 *
 * `on conflict do update` is the port's addition, and it is there because a
 * retried attempt walks the same trajectory and reaches `seq` again. In Python
 * that could not happen: a resumed run read the existing rows and continued
 * past them, so an insert at an occupied seq meant a bug and the unique index
 * was right to say so. Here it is the ordinary consequence of re-entering
 * `run()` from the top.
 *
 * The content is overwritten because the second write describes the same step.
 * **The accounting is added, not overwritten, and that distinction is the whole
 * correctness of this function.** A retry that re-asks the model spends real
 * tokens and real money a second time. `addUsage` accumulates those onto the
 * run, so overwriting `cost_micros` here would leave
 * `sum(steps.cost_micros) != runs.cost_micros` after any retry, and the step
 * log would quietly under-report the bill. Since invariant 5 is now the step
 * log's only job, an accounting hole in it is not a cosmetic problem.
 *
 * The bug this avoids is invisible against the scripted provider, which reports
 * zero cost for everything. It would have appeared the first time this ran
 * against a real model and retried.
 *
 * Note the asymmetry with the idempotency ledger, which does *not* upsert at
 * all. A step row is a description, and a description can be restated. A ledger
 * row is a claim that a side effect happened, and letting a retry overwrite
 * that claim would be the whole bug.
 */
export async function appendStep(
  db: PoolClient,
  opts: {
    runId: string;
    seq: number;
    kind: "model_call" | "tool_result" | "approval" | "final" | "error";
    content: Record<string, unknown>;
    toolName?: string | null;
    inputTokens?: number;
    outputTokens?: number;
    costMicros?: number;
    latencyMs?: number;
  },
): Promise<string> {
  const row = (
    await db.query(
      `insert into steps (run_id, seq, kind, content, tool_name, input_tokens,
                          output_tokens, cost_micros, latency_ms)
       values ($1, $2, $3::step_kind, $4, $5, $6, $7, $8, $9)
       on conflict (run_id, seq) do update
         set content = excluded.content,
             kind = excluded.kind,
             tool_name = excluded.tool_name,
             input_tokens = steps.input_tokens + excluded.input_tokens,
             output_tokens = steps.output_tokens + excluded.output_tokens,
             cost_micros = steps.cost_micros + excluded.cost_micros,
             latency_ms = excluded.latency_ms
       returning id`,
      [
        opts.runId,
        opts.seq,
        opts.kind,
        JSON.stringify(opts.content),
        opts.toolName ?? null,
        opts.inputTokens ?? 0,
        opts.outputTokens ?? 0,
        opts.costMicros ?? 0,
        opts.latencyMs ?? 0,
      ],
    )
  ).rows[0]!;
  return String(row["id"]);
}

export async function addUsage(
  db: PoolClient,
  runId: string,
  usage: {
    inputTokens: number;
    outputTokens: number;
    costMicros: number;
    provider: string;
    model: string;
  },
): Promise<void> {
  await db.query(
    `update runs set input_tokens = input_tokens + $1,
                     output_tokens = output_tokens + $2,
                     cost_micros = cost_micros + $3,
                     provider = $4, model = $5, updated_at = now()
      where id = $6`,
    [usage.inputTokens, usage.outputTokens, usage.costMicros, usage.provider, usage.model, runId],
  );
}

/**
 * End a run.
 *
 * The Python version cleared `lease_owner` and `lease_expires_at` here. This
 * one does not write them at all, which is the point: a file whose docstring
 * says there is no lease should not have a statement that nulls one out four
 * screens further down. Nothing in the port ever sets those columns, so they
 * are already null on every run it creates.
 */
export async function finish(
  db: PoolClient,
  runId: string,
  opts: { status: string; stopReason: string; stopDetail?: string | null },
): Promise<void> {
  await db.query(
    `update runs set status = $1::run_status, stop_reason = $2, stop_detail = $3,
                     finished_at = now(), updated_at = now()
      where id = $4`,
    [opts.status, opts.stopReason, opts.stopDetail ?? null, runId],
  );
}

export async function audit(
  db: PoolClient,
  opts: {
    orgId: string;
    action: string;
    actorKind?: "human" | "agent" | "system";
    actorId?: string | null;
    runId?: string | null;
    detail?: Record<string, unknown>;
  },
): Promise<void> {
  await db.query(
    `insert into audit_log (org_id, actor_kind, actor_id, run_id, action, detail)
     values ($1, $2, $3, $4, $5, $6)`,
    [
      opts.orgId,
      opts.actorKind ?? "system",
      opts.actorId ?? null,
      opts.runId ?? null,
      opts.action,
      JSON.stringify(opts.detail ?? {}),
    ],
  );
}

/** Close the pool. Only the scripts need this; the task runs inside a process
 * the platform owns and recycles. */
export async function shutdown(): Promise<void> {
  await pool.end();
}
