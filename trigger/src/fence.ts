/**
 * Fencing what the model is allowed to trust.
 *
 * A straight port of the `quarantine` half of `deskhand/runtime/transcript.py`.
 * The other half of that module — rebuilding the conversation by replaying step
 * rows — is gone, because the messages array now lives in a variable that
 * Trigger.dev checkpoints across a wait. This is the part that stays, because
 * it has nothing to do with durability: it is about where untrusted bytes enter
 * the model's context, and they enter in the same place either way.
 */

import { createHash } from "node:crypto";

/**
 * A per-run marker for the untrusted region.
 *
 * Derived from the run id rather than randomly generated, because two workers
 * (or two attempts of the same run) have to produce byte-identical messages.
 * Derived rather than fixed, because a constant delimiter published in an
 * open-source repository is one a customer can type into a ticket body and
 * close early.
 */
export function fenceToken(runId: string): string {
  return createHash("sha256").update(`deskhand-fence:${runId}`).digest("hex").slice(0, 12);
}

/**
 * What a forged marker inside the body is replaced with. Deliberately contains
 * no angle bracket, which is what makes one pass enough — see `quarantine`.
 */
export const STRIPPED_MARKER = "[fence marker stripped]";

/**
 * Wrap tool output as data.
 *
 * Two things happen here, and the second is the one that matters:
 *
 * 1. The output is delimited with a marker the model is told about in its
 *    system prompt.
 * 2. Any occurrence of that marker *inside* the output is neutralised first, so
 *    content cannot close its own fence and continue as if it were the system
 *    talking.
 *
 * Step 2 replaces rather than deletes, and that is a correctness requirement
 * rather than a courtesy. Deleting joins the text on either side of the marker,
 * and the join can spell the marker that was just removed:
 *
 *     body = "<<</untrusted:" + closer + token + ">>>"
 *
 * A single delete-the-closer pass there returns `closer`, so the body ends up
 * closing the fence after all. Substituting a placeholder keeps the two halves
 * apart, and because the placeholder contains no `<` or `>`, no marker can ever
 * span it. That is why one pass is sufficient and there is no loop to run to a
 * fixed point. `tests/fence.test.ts` is the regression.
 *
 * This does not make the content safe. A model can still be persuaded by text
 * inside the fence. What it does is remove the *structural* ambiguity, and pair
 * it with the guarantee that actually holds: nothing in here can change a
 * tool's risk class, so the worst a persuasive ticket achieves is a refund
 * request that a human is still asked to approve.
 */
export function quarantine(runId: string, body: string): string {
  const token = fenceToken(runId);
  const opener = `<<<untrusted:${token}>>>`;
  const closer = `<<</untrusted:${token}>>>`;
  const cleaned = body.split(opener).join(STRIPPED_MARKER).split(closer).join(STRIPPED_MARKER);
  return `${opener}\n${cleaned}\n${closer}`;
}
