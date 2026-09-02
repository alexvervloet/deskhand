import { defineConfig } from "@trigger.dev/sdk";
import { syncEnvVars } from "@trigger.dev/build/extensions/core";

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
  // A literal, not an env lookup, and the first version of this file got that
  // wrong. This config is bundled and re-executed *inside the build container*
  // during indexing, where nothing from the deploying shell exists, so a
  // `requireEnv` here threw and killed the build rather than the deploy. A
  // project ref is a public identifier anyway; the secret is the API key.
  // Override with TRIGGER_PROJECT_REF to deploy a fork into your own project.
  project: process.env.TRIGGER_PROJECT_REF ?? "proj_jdcxknupwfnetfusrhic",
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
  build: {
    extensions: [
      /**
       * Push the database URL to the deployed environment at deploy time.
       *
       * A deployed task runs on Trigger.dev's infrastructure and cannot reach
       * the `localhost:5437` Postgres that `docker compose up -d db` starts, so
       * the deployed slice needs a database reachable over the public internet.
       * Marked secret, so the value is redacted in the dashboard and cannot be
       * read back out of it.
       *
       * This runs on the machine doing the deploy, not in the task, so
       * `DATABASE_URL` has to be set in that shell. It is deliberately not
       * defaulted: silently deploying an agent that moves money against
       * whatever database happens to be configured is not a mistake worth
       * making convenient.
       */
      syncEnvVars(async () => {
        const url = process.env.DATABASE_URL;
        if (!url) {
          throw new Error(
            "DATABASE_URL is not set, so the deployed task would have no database to reach. " +
              "Set it to a Postgres the public internet can resolve, and run the migrations " +
              "and seed against it first.",
          );
        }
        if (url.includes("localhost") || url.includes("127.0.0.1")) {
          throw new Error(
            `DATABASE_URL points at ${url.includes("localhost") ? "localhost" : "127.0.0.1"}, ` +
              "which a deployed task cannot reach. Use a hosted Postgres for a deploy.",
          );
        }
        return [{ name: "DATABASE_URL", value: url, isSecret: true }];
      }),
    ],
  },
});

