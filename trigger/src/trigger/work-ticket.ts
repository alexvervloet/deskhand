/**
 * The Trigger.dev task.
 *
 * This file is deliberately the whole of the platform's footprint in the port,
 * and it is worth reading as a measurement rather than as code. Everything
 * Trigger.dev contributes to a durable, resumable, human-gated agent is here:
 * a task definition, a compute ceiling, a queue, a retry policy, and an
 * adapter that turns `wait.createToken` / `wait.forToken` into the four-method
 * `Waiter` the loop asks for.
 *
 * `deskhand/worker.py` has no counterpart here at all: the poll loop, the lease
 * renewal, the signal handling, the crash path that has to explicitly fail a run
 * so it does not sit forever looking alive. Neither does `claim_next`,
 * with its `for update skip locked`. That is the deletion the port was for.
 */

import { idempotencyKeys, logger, task, wait } from "@trigger.dev/sdk";
import { advance, type Waiter } from "../loop.ts";
import { getProvider } from "../provider.ts";

export interface WorkTicketPayload {
  runId: string;
}

/**
 * The adapter. Note what it does *not* do: it does not decide when to wait,
 * what to wait for, or what a decision means. It hands over a way to suspend.
 */
const triggerWaiter: Waiter = {
  async createToken({ key, timeoutSeconds, tags }) {
    // Global scope so that a retried attempt of the same run resolves to the
    // token the first attempt opened, rather than opening a second one and
    // asking a second person the same question. The key already names the run
    // and the tool call, so global is the narrower choice here despite the word.
    const idempotencyKey = await idempotencyKeys.create(key, { scope: "global" });
    const token = await wait.createToken({
      idempotencyKey,
      // Deliberately longer than `timeout`. A retry that lands after the
      // approval has timed out must inherit the expired token and end the run,
      // not mint a fresh waitpoint and ask a second person for consent the
      // process already declared stale. `askHuman` in loop.ts has the full note.
      idempotencyKeyTTL: "7d",
      timeout: `${timeoutSeconds}s`,
      tags,
    });
    return { id: token.id };
  },

  async forToken<T>(tokenId: string) {
    const result = await wait.forToken<T>(tokenId);
    // `ok: false` is a timeout, which the loop reads as the approval expiring.
    // Not unwrapped: `unwrap()` throws on timeout, and a throw here would be
    // retried from the top of the run, turning "nobody answered" into "ask
    // three more times".
    return { ok: result.ok, output: result.ok ? result.output : undefined };
  },

  log(message, fields) {
    logger.info(message, fields);
  },
};

export const workTicket = task({
  id: "work-ticket",
  // A backstop on runaway compute, not the deadline. This counts CPU time
  // within one attempt and excludes time spent suspended, so it can neither
  // bound a run across retries nor notice a ticket that has been open for a
  // day. The wall-clock ceiling is absolute and lives on the row. See the note
  // at the top of `bounds.ts`.
  maxDuration: 900,
  queue: { concurrencyLimit: 4 },
  run: async (payload: WorkTicketPayload) => {
    return advance(payload.runId, { provider: getProvider(), waiter: triggerWaiter });
  },
});
