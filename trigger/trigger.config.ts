import { defineConfig } from "@trigger.dev/sdk";

/**
 * Deskhand's bounds, expressed where the platform can enforce them.
 *
 * `maxDuration` is a ceiling on **CPU time within one attempt**, not a
 * wall-clock deadline: the docs are explicit that it "does not include time
 * spent waiting". It is set here as a backstop against runaway compute, and it
 * is not a replacement for `runs.deadline_at`, which is absolute, stamped once
 * at creation, and checked in `bounds.ts`. See the note at the top of that file
 * for why the two are not interchangeable.
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
