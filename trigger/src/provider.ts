/**
 * The model, and the scripted stand-in that needs no key.
 *
 * Both return the same `ModelReply` and the runtime cannot tell them apart, so
 * the loop, the approval gate, the bounds and the fence are all exercised
 * identically whether or not an API key is set.
 *
 * The mock is **not** a small language model and makes no claim to be. It is a
 * handful of fixed trajectories chosen by keyword, whose job is to drive the
 * runtime through its interesting states — the approval gate, and a retry after
 * a crash. Every run it produces is tagged `provider=mock`.
 *
 * Statelessness is a requirement here, not a simplification, and the port
 * sharpens why. In Python a resumed run rebuilt its history from the step log
 * and asked the provider for the next turn; a provider holding a private
 * counter would have returned the wrong turn. Here a *retried* run rebuilds
 * nothing — it re-enters `run()` from the top with an empty history — and the
 * turn index is still derived from the messages it is handed, so attempt two
 * walks the same trajectory as attempt one. That is what makes the idempotency
 * ledger's key stable across a retry, and it is the assumption a real model
 * does not honour. See the note in `invoke.ts`.
 */

export interface ContentBlock {
  type: string;
  [key: string]: unknown;
}

export interface ModelReply {
  content: ContentBlock[];
  stopReason: string;
  inputTokens: number;
  outputTokens: number;
  costMicros: number;
  provider: string;
  model: string;
  latencyMs: number;
}

export interface Message {
  role: "user" | "assistant";
  content: string | ContentBlock[];
}

export interface Provider {
  name: string;
  model: string;
  complete(
    system: string,
    messages: Message[],
    tools: Array<Record<string, unknown>>,
  ): Promise<ModelReply>;
}

export function toolUses(reply: ModelReply): ContentBlock[] {
  return reply.content.filter((b) => b.type === "tool_use");
}

export function replyText(reply: ModelReply): string {
  return reply.content
    .filter((b) => b.type === "text")
    .map((b) => String(b["text"] ?? ""))
    .join("\n")
    .trim();
}

function text(body: string): ContentBlock[] {
  return [{ type: "text", text: body }];
}

function call(name: string, input: Record<string, unknown>): ContentBlock {
  return { type: "tool_use", name, input };
}

// Order references are four digits (NW-1042); ticket references are one or two
// (NW-1). Crude, and adequate for a fixture-driven demo — the mock's job is to
// reach the interesting states, not to parse English.
const ORDER_REF = /\b([A-Z]{2}-\d{3,})\b/;
const TICKET_REF = /\b([A-Z]{2}-\d{1,2})\b/;
const TOTAL = /total: ([\d,]+)\.(\d{2}) /;

/**
 * The opening prompt plus the first tool result, and nothing after it.
 *
 * Deliberately *not* the whole conversation. The plan below is recomputed from
 * scratch on every turn — it has to be, because the provider is stateless so
 * that a retried run reaches the same decision — and reading the growing
 * transcript made that recomputation unstable: the agent would set off down the
 * "where is my order" path, a knowledge-base search would return an article
 * that happens to contain the word *refund*, and the next turn would decide it
 * had been working a refund all along.
 *
 * That is not a hypothetical. It happened in the Python original and produced a
 * demo in which the agent asked to refund a customer who only wanted a tracking
 * number. The ticket is what the plan is about, so the plan reads the ticket
 * and stops.
 */
function brief(messages: Message[]): string {
  const parts: string[] = [];
  let seenResult = false;
  for (const message of messages) {
    if (typeof message.content === "string") {
      parts.push(message.content);
      continue;
    }
    for (const block of message.content) {
      if (block.type === "text") {
        parts.push(String(block["text"] ?? ""));
      } else if (block.type === "tool_result" && !seenResult) {
        const inner = block["content"];
        parts.push(typeof inner === "string" ? inner : String(inner));
        seenResult = true;
      }
    }
    if (seenResult) break;
  }
  return parts.join("\n");
}

export class ScriptedProvider implements Provider {
  name = "mock";
  model = "mock";

  /** Derived from the history, never from a private counter. */
  static turnIndex(messages: Message[]): number {
    return messages.filter((m) => m.role === "assistant").length;
  }

  protected plan(_messages: Message[]): ContentBlock[][] {
    return [];
  }

  async complete(
    _system: string,
    messages: Message[],
    _tools: Array<Record<string, unknown>>,
  ): Promise<ModelReply> {
    const script = this.plan(messages);
    const index = ScriptedProvider.turnIndex(messages);
    const blocks: ContentBlock[] =
      index < script.length
        ? script[index]!.map((b) => ({ ...b }))
        : text("Nothing further to do.");

    // Deterministic ids. A uuid here would break the approval binding: the
    // tool_use id is what a decision is tied to, and a retried run must produce
    // the same one or the human's answer would no longer match anything.
    blocks.forEach((block, position) => {
      if (block.type === "tool_use" && !block["id"]) block["id"] = `toolu_mock_${index}_${position}`;
    });

    const hasTools = blocks.some((b) => b.type === "tool_use");
    return {
      content: blocks,
      stopReason: hasTools ? "tool_use" : "end_turn",
      inputTokens: 0,
      outputTokens: 0,
      costMicros: 0,
      provider: this.name,
      model: this.model,
      latencyMs: 0,
    };
  }
}

/**
 * The trajectory used when there is no API key.
 *
 * It picks one of three shapes from the ticket text and fills in references and
 * amounts by reading them back out of earlier tool results. That is enough to
 * walk the runtime through a full run — including suspending on an irreversible
 * call and resuming after a human decides — with no key and no network.
 */
export class DefaultMockProvider extends ScriptedProvider {
  protected override plan(messages: Message[]): ContentBlock[][] {
    const seen = brief(messages);
    const ticketRef = TICKET_REF.exec(seen)?.[1] ?? "NW-1";

    const wantsRefund = ["refund", "charged twice", "money back"].some((word) =>
      seen.toLowerCase().includes(word),
    );

    const script: ContentBlock[][] = [[call("get_ticket", { reference: ticketRef })]];

    if (!wantsRefund) {
      script.push(
        [call("search_kb", { query: "shipping times tracking delay" })],
        [
          call("add_internal_note", {
            reference: ticketRef,
            body:
              "Checked the knowledge base: this is inside the published turnaround, " +
              "so no action is due yet.",
          }),
        ],
        [call("set_ticket_status", { reference: ticketRef, status: "pending" })],
        text(
          `${ticketRef} is within the published turnaround. I left an internal note ` +
            "and moved it to pending.",
        ),
      );
      return script;
    }

    const orderRef = ORDER_REF.exec(seen)?.[1];
    if (!orderRef) {
      script.push(
        [call("search_kb", { query: "refund policy window" })],
        text("I could not find an order reference on this ticket."),
      );
      return script;
    }

    const total = TOTAL.exec(seen);
    const amount = total
      ? Number.parseInt(total[1]!.replaceAll(",", ""), 10) * 100 + Number.parseInt(total[2]!, 10)
      : 1900;

    script.push(
      [call("get_order", { reference: orderRef })],
      [call("search_kb", { query: "refund policy window delivered" })],
      [
        call("issue_refund", {
          order_reference: orderRef,
          amount_cents: amount,
          reason: "Quality complaint inside the published refund window.",
        }),
      ],
      [
        call("add_internal_note", {
          reference: ticketRef,
          body: `Refund processed against ${orderRef} after human approval.`,
        }),
      ],
      [call("set_ticket_status", { reference: ticketRef, status: "resolved" })],
      text(`Refunded ${orderRef} and resolved ${ticketRef}.`),
    );
    return script;
  }
}

/**
 * A provider that walks a trajectory you hand it, for tests that need one
 * specific shape — an obedient model that does what an injected instruction
 * tells it, say.
 */
export class FixedScriptProvider extends ScriptedProvider {
  readonly script: ContentBlock[][];

  constructor(script: ContentBlock[][]) {
    super();
    this.script = script;
  }

  protected override plan(): ContentBlock[][] {
    return this.script;
  }
}

export function getProvider(): Provider {
  // Falls back to the mock rather than failing, because running keyless is a
  // supported mode — but the choice is surfaced on every run, so it is never a
  // silent substitution.
  return new DefaultMockProvider();
}
