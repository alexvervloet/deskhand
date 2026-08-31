/**
 * Irreversible tools. Money leaves and does not come back.
 *
 * Two things are true of every tool in this module, and neither of them changed
 * when the runtime moved onto Trigger.dev:
 *
 * 1. **It cannot execute without a recorded human approval** bound to this exact
 *    run, step, and argument hash. That gate lives in the runtime, not here, so
 *    that adding a tool to this module is sufficient to protect it — a handler
 *    cannot forget to check.
 *
 * 2. **Its own preconditions are still enforced in code.** The approval gate
 *    stops the agent from acting unilaterally; it does not stop a human from
 *    clicking approve on a refund larger than the order. Policy that must always
 *    hold is a constraint here, not a sentence in the system prompt, because the
 *    prompt is advice and this is arithmetic.
 *
 * The port only carries `issue_refund` across. `send_customer_email` and
 * `cancel_order` are the same shape and would demonstrate nothing further; the
 * slice is deliberately the one path that moves money.
 */

import { RiskClass, ToolError, money, register, schema, type ToolContext, type ToolOutcome } from "./registry.ts";

/** Cents. Read from config only when a run is created, never at payout time. */
const DAILY_REFUND_CENTS_PER_ORG = Number(process.env.DAILY_REFUND_CENTS_PER_ORG ?? 500_000);

/**
 * Refuse a payout that breaches a ceiling, before any money moves.
 *
 * Two ceilings, and neither is the per-order remaining balance — that one is
 * about a single order being refunded twice, and it says nothing about a run
 * that refunds four different orders once each.
 *
 * The run ceiling is read off the run row, not from config, because it was
 * snapshotted at creation. Raising the cap in a deploy must not retroactively
 * widen a run already in flight.
 *
 * This is here rather than in the runtime's bounds check because `bounds.ts`
 * gates *model calls*, and a ceiling checked before the call that proposes a
 * refund is not a ceiling on the refund. It is checked here, at the point of
 * payment, so it holds even when a human has already clicked approve.
 *
 * The org row is locked first, and that is not decoration. The caller holds a
 * lock on the *order*, which serialises two runs fighting over one order and
 * does nothing about two runs refunding different orders of the same merchant —
 * both would read a daily total that leaves room, and both would pay. Locking
 * the merchant serialises every payout it makes.
 */
async function ceilings(ctx: ToolContext, amount: number, currency: string): Promise<void> {
  await ctx.db.query("select id from orgs where id = $1 for update", [ctx.orgId]);

  const row = (
    await ctx.db.query(
      `select r.max_refund_cents,
              coalesce((select sum(amount_cents) from refunds
                         where run_id = r.id), 0) as run_paid,
              coalesce((select sum(amount_cents) from refunds
                         where org_id = r.org_id
                           and created_at >= date_trunc('day', now())), 0) as org_paid
         from runs r where r.id = $1`,
      [ctx.runId],
    )
  ).rows[0]!;

  const runCap = Number(row["max_refund_cents"]);
  const runPaid = Number(row["run_paid"]);
  if (runPaid + amount > runCap) {
    throw new ToolError(
      `this run may refund ${money(runCap, currency)} in total and has already refunded ` +
        `${money(runPaid, currency)}, so it cannot also refund ${money(amount, currency)}. ` +
        "Do not split the payment into smaller refunds to get under the ceiling — escalate " +
        "to a human instead.",
    );
  }

  const orgPaid = Number(row["org_paid"]);
  if (orgPaid + amount > DAILY_REFUND_CENTS_PER_ORG) {
    throw new ToolError(
      `this merchant's daily refund ceiling of ${money(DAILY_REFUND_CENTS_PER_ORG, currency)} ` +
        `would be breached: ${money(orgPaid, currency)} has been refunded today. Escalate to ` +
        "a human rather than refunding.",
    );
  }
}

register({
  name: "issue_refund",
  risk: RiskClass.IRREVERSIBLE,
  description:
    "Refund money against an order, to the original payment method. This moves real money " +
    "and cannot be undone. Check the refund policy and the order's delivery date first, " +
    "and refund only the amount the policy supports — partial refunds are normal and are " +
    "often the right answer. Amounts are in cents: 1900 means nineteen dollars.",
  parameters: schema({
    order_reference: { type: "string", description: "Order reference." },
    amount_cents: {
      type: "integer",
      minimum: 1,
      description: "Refund amount in cents. Must not exceed what remains refundable.",
    },
    reason: {
      type: "string",
      minLength: 3,
      maxLength: 500,
      description: "Why this refund is due, in one line. Appears on the merchant's report.",
    },
  }),
  preview: (a) =>
    `Refund ${money(a["amount_cents"] as number)} against order ${a["order_reference"]}` +
    ` — ${a["reason"]}`,
  handler: async (ctx: ToolContext, args): Promise<ToolOutcome> => {
    const reference = args["order_reference"] as string;
    const amount = args["amount_cents"] as number;

    // `for update` matters: two runs working the same customer's duplicate
    // charge could otherwise both read "nothing refunded yet" and both issue a
    // full refund. The lock makes the read-decide-write sequence atomic against
    // every other writer of this row.
    const order = (
      await ctx.db.query(
        `select id, reference, status::text, total_cents, currency, customer_id
           from orders where org_id = $1 and reference = $2 for update`,
        [ctx.orgId, reference],
      )
    ).rows[0];
    if (!order) throw new ToolError(`no order ${JSON.stringify(reference)} for this merchant`);

    const refunded = Number(
      (
        await ctx.db.query(
          "select coalesce(sum(amount_cents), 0) as refunded from refunds where order_id = $1",
          [order["id"]],
        )
      ).rows[0]!["refunded"],
    );
    const currency = order["currency"] as string;
    const remaining = Number(order["total_cents"]) - refunded;

    if (amount > remaining) {
      throw new ToolError(
        `cannot refund ${money(amount, currency)} against ${order["reference"]}: ` +
          `${money(refunded, currency)} of ${money(order["total_cents"], currency)} is already ` +
          `refunded, leaving ${money(remaining, currency)}`,
      );
    }

    await ceilings(ctx, amount, currency);

    // run_id is stamped on the row itself, so "which run paid this out, and
    // therefore who approved it" is a join and not an investigation.
    await ctx.db.query(
      `insert into refunds (org_id, order_id, amount_cents, currency, reason, run_id)
       values ($1, $2, $3, $4, $5, $6)`,
      [ctx.orgId, order["id"], amount, currency, args["reason"], ctx.runId],
    );

    return {
      result:
        `Refunded ${money(amount, currency)} against ${order["reference"]}.` +
        ` Remaining refundable: ${money(remaining - amount, currency)}.` +
        " The customer sees it on their statement in 5-10 business days.",
    };
  },
});
