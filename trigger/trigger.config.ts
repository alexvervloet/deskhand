import { defineConfig } from "@trigger.dev/sdk";

/**
 * Deskhand's bounds, expressed where the platform can enforce them.
 *
 * `maxDuration` is the wall-clock deadline that `runs.deadline_at` used to be,
 * with one behavioural difference the port had to account for: it bounds an
 * *attempt*, not a run. Deskhand's deadline is absolute and set once at
 * creation precisely so that a crash-looping run cannot earn itself a fresh
 * clock. Here every retry starts a new one, so the absolute deadline is still
 * carried on the run row and checked in `bounds.ts`.
 *
 * `retries.enabledInDev` is on deliberately. It is off by default, and leaving
 * it off would have hidden the whole finding: a retry re-enters `run()` from
 * the top, and that is what the idempotency ledger is still here to survive.
 */
export default defineConfig({
  project: process.env.TRIGGER_PROJECT_REF ?? "proj_deskhand_local",
  dirs: ["./src/trigger"],
  runtime: "node-22",
  maxDuration: 900,
  retries: {
    enabledInDev: true,
    default: {
      maxAttempts: 3,
      minTimeoutInMs: 1_000,
      maxTimeoutInMs: 10_000,
      factor: 2,
      randomize: true,
    },
  },
});
