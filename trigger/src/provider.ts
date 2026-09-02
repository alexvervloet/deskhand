/**
 * The scripted model.
 *
 * **There is no real provider here, and that is a real limitation of the port
 * rather than a detail.** The Python service has a working `ClaudeProvider`
 * next to its mock; this file has only the mock, because the port exists to
 * compare *runtimes* and every claim it makes is about what happens around the
 * model call rather than inside it. `getProvider` returns the scripted one
 * unconditionally. Nothing here has ever spoken to an API.
 *
 * What that costs is stated in docs/TRIGGER-PORT.md: the port has not been run
 * against a model that can produce a genuinely unexpected turn, so the
 * trajectory is reproducible by construction rather than by luck. That
 * assumption is load-bearing for the idempotency key, and `invoke.ts` says what
 * happens when it does not hold.
 *
 * The mock is not a small language model and makes no claim to be. It is a
 * handful of fixed trajectories chosen by keyword, whose job is to drive the
 * runtime through its interesting states: the approval gate, and a retry after
 * a crash. Every run it produces is tagged `provider=mock`.
 *
 * Statelessness is a requirement here, not a simplification, and the port
 * sharpens why. In Python a resumed run rebuilt its history from the step log
 * and asked the provider for the next turn; a provider holding a private
 * counter would have returned the wrong turn. Here a *retried* run rebuilds
 * nothing. It re-enters `run()` from the top with an empty history, and the
 * turn index is still derived from the messages it is handed, so attempt two
 * walks the same trajectory as attempt one.
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
// (NW-1). Crude, and adequate for a fixture-driven demo: the mock's job is to
// reach the interesting states, not to parse English.
const ORDER_REF = /\b([A-Z]{2}-\d{3,})\b/;
const TICKET_REF = /\b([A-Z]{2}-\d{1,2})\b/;
const TOTAL = /total: ([\d,]+)\.(\d{2}) /;

/**
 * The opening prompt plus the first tool result, and nothing after it.
 *
 * Deliberately *not* the whole conversation. The plan below is recomputed from
 * scratch on every turn. It has to be, because the provider is stateless so that
 * a retried run reaches the same decision, and reading the growing transcript
 * made that recomputation unstable: the agent would set off down the
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

/**
 * Whether a human declined something earlier in this conversation.
 *
 * Matches the opening of the denial text `loop.ts` feeds back as a tool
 * result. Coupled to that string on purpose: a mock that guessed at the shape
 * of a denial could drift away from what the runtime actually sends and quietly
 * stop noticing.
 */
function wasDeclined(messages: Message[]): boolean {
  return messages.some(
    (m) =>
      typeof m.content !== "string" &&
      m.content.some(
        (b) =>
          b.type === "tool_result" &&
          String(b["content"] ?? "").includes("A human reviewed this action and declined it"),
      ),
  );
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
 * The default trajectory.
 *
 * It picks one of three shapes from the ticket text and fills in references and
 * amounts by reading them back out of earlier tool results. That is enough to
 * walk the runtime through a full run, including suspending on an irreversible
 * call and resuming after a human decides, with no key and no network.
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

    // Did a human decline the refund earlier in this conversation?
    //
    // This is the one thing the mock reads from the whole transcript rather
    // than from `brief`, and it is worth the exception. Without it the closing
    // turn is a fixed string that claims a refund happened, so a denied run
    // ends with "Refunded NW-1101" written next to zero refunds. The runtime
    // was right and the summary was a lie, which on a public demo is worse
    // than a bug: it is a bug that looks like working software.
    //
    // Reading it does not destabilise the plan the way reading the whole
    // transcript did, because it changes only the text of turns that are
    // already fixed in number. The script keeps its length and every earlier
    // index still resolves to the same call, which is what a retried run
    // depends on.
    const declined = wasDeclined(messages);

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
          body: declined
            ? `Refund against ${orderRef} was declined by a human. Leaving the order untouched ` +
              "and escalating rather than retrying the same action."
            : `Refund processed against ${orderRef} after human approval.`,
        }),
      ],
      [
        call("set_ticket_status", {
          reference: ticketRef,
          status: declined ? "escalated" : "resolved",
        }),
      ],
      text(
        declined
          ? `A human declined the refund against ${orderRef}, so no money moved. I left a note ` +
            `on ${ticketRef} and escalated it for a person to decide.`
          : `Refunded ${orderRef} and resolved ${ticketRef}.`,
      ),
    );
    return script;
  }
}

/**
 * A provider that walks a trajectory you hand it, for tests that need one
 * specific shape: an obedient model that does what an injected instruction tells
 * it, say.
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
  // Unconditional. There is no real provider in the port to fall back from, and
  // pretending otherwise would be the one place this repository lied about what
  // it does. Every run is tagged provider=mock, in the run row and in the logs.
  return new DefaultMockProvider();
}
