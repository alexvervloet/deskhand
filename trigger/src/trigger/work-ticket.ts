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

import { task } from "@trigger.dev/sdk";
import { advance } from "../loop.ts";
import { getProvider } from "../provider.ts";
import { triggerWaiter } from "./waiter.ts";

export interface WorkTicketPayload {
  runId: string;
}

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
