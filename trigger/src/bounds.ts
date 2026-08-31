/**
 * Every run terminates, and every run is capped on what it costs.
 *
 * Going in I expected a durable execution platform to absorb the bounds along
 * with the durability, and it absorbs none of them. The one that looked like it
 * would move is the deadline, and the reason it cannot is worth getting exactly
 * right, because I got it wrong first: **`maxDuration` is not a wall-clock
 * ceiling.**
 *
 * From `runs/max-duration.mdx`, it "is compared to the CPU time elapsed since
 * the start of a single execution (which we call attempts) of the task. The CPU
 * time is the time that the task has been actively running on the CPU, and does
 * not include time spent waiting."
 *
 * So it is wrong for `deadline_at` twice over, for independent reasons:
 *
 *   1. It bounds an **attempt**, not a run. Deskhand's deadline is absolute and
 *      stamped once at creation, specifically so a crash-looping run cannot
 *      earn itself a fresh clock on every resume. Under a platform that retries
 *      three times by default, a per-attempt ceiling is three fresh clocks.
 *   2. It counts **CPU time**, not elapsed time. An agent that suspends for a
 *      day waiting on a human burns almost none of it.
 *
 * The second is the same property that makes the approval gate cheap here, and
 * it is genuinely good: nobody is billed for a person thinking. It just means
 * `maxDuration` cannot answer "how long has this ticket been open", which is
 * the only question an absolute deadline is asked. So `maxDuration` is set as a
 * backstop on runaway compute, and the wall-clock deadline stays here, on the
 * row, checked against `now()` in the database that stamped it.
 *
 * Nothing else in this file has a platform counterpart, and that is not a gap
 * in Trigger.dev. Steps, tokens, dollars of inference and repeated identical
 * tool calls are facts about an *agent*, and a job runner has no opinion about
 * them. The caps that matter for an agent that spends money are the ones you
 * were always going to write yourself.
 */

import type { PoolClient } from "pg";

const CONFIG = {
  dailyBudgetMicrosPerOrg: Number(process.env.DAILY_BUDGET_MICROS_PER_ORG ?? 10_000_000),
  platformDailyBudgetMicros: Number(process.env.PLATFORM_DAILY_BUDGET_MICROS ?? 50_000_000),
  loopDetectionThreshold: Number(process.env.LOOP_DETECTION_THRESHOLD ?? 3),
} as const;

export interface Breach {
  reason: string;
  detail: string;
}

function formatUsd(micros: number): string {
  return `USD ${(micros / 1_000_000).toFixed(2)}`;
}

/**
 * Would taking another model call break one of this run's ceilings?
 *
 * Checked *before* the call, never after. A cap you verify afterwards is not a
 * cap, it is an invoice.
 */
export async function exceeded(
  db: PoolClient,
  run: Record<string, any>,
  seq: number,
): Promise<Breach | null> {
  if (seq >= Number(run["max_steps"])) {
    return { reason: "step_cap", detail: `reached the ${run["max_steps"]}-step ceiling` };
  }

  const usedTokens = Number(run["input_tokens"]) + Number(run["output_tokens"]);
  if (usedTokens >= Number(run["max_tokens"])) {
    return { reason: "token_cap", detail: `used ${usedTokens} tokens of ${run["max_tokens"]}` };
  }

  if (Number(run["cost_micros"]) >= Number(run["max_spend_micros"])) {
    return {
      reason: "spend_cap",
      detail: `spent ${formatUsd(Number(run["cost_micros"]))} of ${formatUsd(Number(run["max_spend_micros"]))}`,
    };
  }

  // Asked of the database rather than of `Date.now()`, because the deadline was
  // stamped by the database. Comparing a clock in one process against a
  // timestamp from another is how a run ends early on a machine whose NTP has
  // drifted.
  const past = (await db.query("select now() > $1 as past", [run["deadline_at"]])).rows[0]!["past"];
  if (past) {
    return { reason: "deadline", detail: "ran past its wall-clock deadline" };
  }

  // Per-org daily spend, then the ceiling that actually bounds the bill. The
  // per-org cap bounds one tenant; it only bounds the deployment if the number
  // of tenants is bounded too.
  const orgSpent = Number(
    (
      await db.query(
        `select coalesce(sum(cost_micros), 0) as spent from runs
          where org_id = $1 and created_at >= date_trunc('day', now())`,
        [run["org_id"]],
      )
    ).rows[0]!["spent"],
  );
  if (orgSpent >= CONFIG.dailyBudgetMicrosPerOrg) {
    return { reason: "org_daily_budget", detail: "this merchant's daily budget is exhausted" };
  }

  const platformSpent = Number(
    (
      await db.query(
        `select coalesce(sum(cost_micros), 0) as spent from runs
          where created_at >= date_trunc('day', now())`,
      )
    ).rows[0]!["spent"],
  );
  if (platformSpent >= CONFIG.platformDailyBudgetMicros) {
    return { reason: "platform_daily_budget", detail: "the service daily budget is exhausted" };
  }

  return null;
}

/**
 * Has the agent made the identical call too many times?
 *
 * A step cap alone would eventually stop a loop, but only after paying for
 * every iteration of it. Matching on the argument hash catches the specific
 * failure early, and names it: same tool, same arguments, no new information.
 * The run ends with `loop_detected` rather than an ambiguous `step_cap`.
 */
export async function looping(db: PoolClient, runId: string): Promise<string | null> {
  const row = (
    await db.query(
      `select tool_name, args_hash, count(*) as n from tool_invocations
        where run_id = $1 group by tool_name, args_hash
       having count(*) >= $2 order by n desc limit 1`,
      [runId, CONFIG.loopDetectionThreshold],
    )
  ).rows[0];
  if (!row) return null;
  return `called ${row["tool_name"]} with identical arguments ${row["n"]} times`;
}
