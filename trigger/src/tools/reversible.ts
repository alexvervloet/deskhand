/**
 * Reversible tools. They change state, they run without a human, and each one
 * records its own inverse.
 *
 * "Records its own inverse" is exactly what it says: the undo is captured, not
 * wired up. The prior value of whatever was overwritten is knowable at write
 * time and expensive to guess afterwards, so it is written into the ledger row
 * while it is still cheap.
 */

import { RiskClass, ToolError, register, schema, type ToolContext, type ToolOutcome } from "./registry.ts";

const STATUSES = ["open", "pending", "resolved", "escalated"];

async function ticketByRef(ctx: ToolContext, reference: string) {
  const row = (
    await ctx.db.query(
      `select id, reference, status::text, assignee_id from tickets
        where org_id = $1 and reference = $2`,
      [ctx.orgId, reference],
    )
  ).rows[0];
  if (!row) throw new ToolError(`no ticket ${JSON.stringify(reference)} for this merchant`);
  return row;
}

register({
  name: "set_ticket_status",
  risk: RiskClass.REVERSIBLE,
  description:
    "Set a ticket's status. Use 'resolved' only when the customer's problem is actually " +
    "settled, 'pending' when waiting on the customer, and 'escalated' when the request " +
    "needs a human decision you are not authorised to make.",
  parameters: schema({
    reference: { type: "string", description: "Ticket reference." },
    status: { type: "string", enum: STATUSES },
  }),
  preview: (a) => `Set ${a["reference"]} status to ${a["status"]}`,
  handler: async (ctx: ToolContext, args): Promise<ToolOutcome> => {
    const ticket = await ticketByRef(ctx, args["reference"] as string);
    const before = ticket["status"] as string;
    const after = args["status"] as string;
    if (before === after) return { result: `${ticket["reference"]} is already ${after}.` };

    await ctx.db.query(
      "update tickets set status = $1::ticket_status, updated_at = now() where id = $2",
      [after, ticket["id"]],
    );
    return {
      result: `Status of ${ticket["reference"]} changed from ${before} to ${after}.`,
      inverse: { op: "set_status", ticket_id: String(ticket["id"]), status: before },
    };
  },
});

register({
  name: "add_internal_note",
  risk: RiskClass.REVERSIBLE,
  description:
    "Write an internal note on a ticket. Staff and future runs can read it; the customer " +
    "never can. Use it to record what you checked and why you reached a conclusion, " +
    "especially before escalating — the next person to open the ticket should not have to " +
    "redo your work. This does not reply to the customer.",
  parameters: schema({
    reference: { type: "string", description: "Ticket reference." },
    body: {
      type: "string",
      minLength: 1,
      maxLength: 4000,
      description: "The note, in plain prose.",
    },
  }),
  preview: (a) => `Add an internal note to ${a["reference"]}`,
  handler: async (ctx: ToolContext, args): Promise<ToolOutcome> => {
    const ticket = await ticketByRef(ctx, args["reference"] as string);
    // Authored as 'agent', not 'system', and the difference is not cosmetic.
    // This body is model output, and the model wrote it after reading a ticket
    // somebody outside the company typed. 'system' is the most authoritative
    // label in the vocabulary, so filing model prose under it launders text a
    // customer influenced into text a colleague trusts.
    const row = (
      await ctx.db.query(
        `insert into ticket_messages (ticket_id, author_kind, is_internal, body)
         values ($1, 'agent', true, $2) returning id`,
        [ticket["id"], args["body"]],
      )
    ).rows[0]!;
    return {
      result: `Added an internal note to ${ticket["reference"]}. The customer cannot see it.`,
      inverse: { op: "delete_message", message_id: String(row["id"]) },
    };
  },
});
