/**
 * The one claim the writeup could not check: does a retry really re-enter
 * `run()` from the top?
 *
 * Everything the idempotency argument rests on depends on the answer. If a
 * failed attempt resumed near the point of failure, the ledger in `invoke.ts`
 * would be redundant and the port would be carrying a table for nothing. The
 * docs say the run function is re-entered, and their `idempotencyKey` only
 * makes sense if that is true, but reading it in a document is not the same as
 * watching a customer get refunded once across a genuine platform retry.
 *
 * So this task fails on purpose, on real infrastructure, at the worst possible
 * moment: after the refund has committed and before anything else happens.
 *
 * **The fault lives here rather than in `work-ticket.ts`, and that is the
 * point.** Deskhand's own fault injector is off unless a test turns it on and
 * has no environment switch, because a runtime that can be told to misbehave by
 * its configuration is a runtime nobody can reason about. Adding a
 * `failAfterRefund` flag to the production task would have been the quickest
 * way to answer the question and the worst thing to leave behind. This is a
 * separate task with a separate id. The production path cannot reach it, and it
 * cannot be switched on from outside.
 *
 * Everything else is shared and real: the same `advance`, the same tools, the
 * same ledger, the same waitpoint adapter, the same database.
 */

import { logger, task } from "@trigger.dev/sdk";
import { advance } from "../loop.ts";
import {
  DefaultMockProvider,
  ScriptedProvider,
  type Message,
  type ModelReply,
} from "../provider.ts";
import { triggerWaiter } from "./waiter.ts";

/**
 * The default trajectory, until the refund has landed. Then it dies.
 *
 * Turn 3 of the refund plan is `issue_refund`. By the time the loop asks for
 * turn 4 the money has moved and committed, so throwing here is the worst
 * moment available: the customer has been paid and the run has no idea.
 */
class DieAfterRefund extends DefaultMockProvider {
  override async complete(
    system: string,
    messages: Message[],
    tools: Array<Record<string, unknown>>,
  ): Promise<ModelReply> {
    if (ScriptedProvider.turnIndex(messages) >= 4) {
      throw new Error("crash-probe: dying deliberately, after the refund committed");
    }
    return super.complete(system, messages, tools);
  }
}

export interface CrashProbePayload {
  runId: string;
}

export const crashProbe = task({
  id: "crash-probe",
  maxDuration: 900,
  // Exactly two attempts, back to back. One to refund and die, one to prove
  // the customer is not paid twice. No jitter, because a demonstration that
  // takes an unpredictable amount of time to make its point is a worse
  // demonstration.
  // `retry` on a task, `retries` in the config. They are not interchangeable
  // and the type error is the only thing that says so.
  retry: {
    maxAttempts: 2,
    minTimeoutInMs: 2_000,
    maxTimeoutInMs: 2_000,
    factor: 1,
    randomize: false,
  },
  run: async (payload: CrashProbePayload, { ctx }) => {
    const attempt = ctx.attempt.number;
    logger.info("crash-probe attempt", { attempt, runId: payload.runId });

    // Attempt one dies after the refund. Attempt two is an ordinary run of the
    // ordinary provider, given no memory of the first and no hint that it is a
    // retry. If the platform resumed rather than restarted, attempt two would
    // never reach the refund step at all.
    const provider = attempt === 1 ? new DieAfterRefund() : new DefaultMockProvider();

    return advance(payload.runId, { provider, waiter: triggerWaiter });
  },
});
