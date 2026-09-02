/**
 * The Trigger.dev side of the `Waiter` the loop asks for.
 *
 * Shared by the production task and the crash probe, so that what the probe
 * demonstrates is the real suspension path rather than a lookalike written for
 * the demonstration.
 */

import { idempotencyKeys, logger, wait } from "@trigger.dev/sdk";
import type { Waiter } from "../loop.ts";

/**
 * The adapter. Note what it does *not* do: it does not decide when to wait,
 * what to wait for, or what a decision means. It hands over a way to suspend.
 */
export const triggerWaiter: Waiter = {
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