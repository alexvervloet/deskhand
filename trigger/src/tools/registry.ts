/**
 * Tool definitions, the registry, and the one rule the registry exists for.
 *
 * **A tool's risk class is declared here and can never be changed at runtime.**
 *
 * This file is a near-transliteration of `deskhand/tools/base.py`, and that is
 * the point: nothing about moving onto a durable execution platform changes
 * who is allowed to decide whether `issue_refund` needs a human. The class is a
 * field on a frozen object, looked up by name from a map that is populated at
 * import time and never written to again. Nothing in a model response, a tool
 * argument, or a tool *result* can reach it.
 *
 * Trigger.dev's `chat.agent()` has a `needsApproval: true` flag on a tool that
 * expresses the same idea, and it holds the same property for the same reason:
 * it is declared in backend code, not carried on the call. It is not used here
 * because this port builds on `task()`; see docs/TRIGGER-PORT.md.
 */

import Ajv, { type ValidateFunction } from "ajv";
import { createHash } from "node:crypto";
import type { PoolClient } from "pg";

export const RiskClass = {
  READ: "read",
  REVERSIBLE: "reversible",
  IRREVERSIBLE: "irreversible",
} as const;

export type RiskClass = (typeof RiskClass)[keyof typeof RiskClass];

/**
 * A tool failed in a way the model should see and can react to.
 *
 * Raised for bad arguments, missing records, and policy violations: the
 * ordinary failures of doing the job. It becomes an `is_error` tool result, not
 * a crashed run.
 *
 * The distinction matters more here than it did in Python. Anything that is not
 * a ToolError propagates out of the task's `run()`, and Trigger.dev responds to
 * that by retrying the whole run from the top. So the line between "the model's
 * business" and "our bug" is now also the line between a tool result and a full
 * replay of the trajectory.
 */
export class ToolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolError";
  }
}

/**
 * Everything a handler is allowed to know.
 *
 * Note what is absent: the conversation, the model's reasoning, and the text of
 * the ticket that triggered the run. A handler acts on its arguments and the
 * database.
 *
 * `ticketId` and `customerId` are the run's *subject*. `orgId` alone is a
 * tenancy boundary, not a need-to-know one: it says the agent may not read
 * another merchant's data and says nothing about whether a run working one
 * customer's ticket may read a different customer's history.
 */
export interface ToolContext {
  orgId: string;
  runId: string;
  stepId: string;
  ticketId: string;
  customerId: string;
  db: PoolClient;
}

/**
 * What a handler returns. `result` is the text the model sees; `inverse` is the
 * compensating action, captured now rather than reconstructed later.
 */
export interface ToolOutcome {
  result: string;
  inverse?: Record<string, unknown> | null;
}

export type Handler = (
  ctx: ToolContext,
  args: Record<string, unknown>,
) => Promise<ToolOutcome>;

export interface ToolDef {
  name: string;
  risk: RiskClass;
  description: string;
  /**
   * JSON Schema for the arguments. Always an object with
   * additionalProperties: false, so an unexpected key is a validation error
   * rather than a silently ignored one.
   */
  parameters: Record<string, unknown>;
  handler: Handler;
  /** Human-readable summary of what executing this will do, for the approval
   * screen. Takes the validated arguments. */
  preview?: (args: Record<string, unknown>) => string;
}

const ajv = new Ajv({ allErrors: false, strict: false });

const REGISTRY = new Map<string, ToolDef>();
const VALIDATORS = new Map<string, ValidateFunction>();

export function register(tool: ToolDef): ToolDef {
  if (REGISTRY.has(tool.name)) {
    throw new Error(`tool ${JSON.stringify(tool.name)} is already registered`);
  }
  if (tool.parameters["additionalProperties"] !== false) {
    throw new Error(`tool ${JSON.stringify(tool.name)} must set additionalProperties: false`);
  }
  if (!("required" in tool.parameters)) {
    throw new Error(`tool ${JSON.stringify(tool.name)} must declare \`required\``);
  }
  // Frozen on the way in. The Python original got this from a frozen
  // dataclass; here it has to be asked for, because an ordinary object literal
  // would let any later holder of the reference reassign `risk`.
  const frozen = Object.freeze({ ...tool, parameters: Object.freeze(tool.parameters) });
  REGISTRY.set(tool.name, frozen);
  VALIDATORS.set(tool.name, ajv.compile(tool.parameters));
  return frozen;
}

export function get(name: string): ToolDef {
  const tool = REGISTRY.get(name);
  if (!tool) throw new ToolError(`no such tool: ${JSON.stringify(name)}`);
  return tool;
}

/**
 * Whether this name is a tool at all.
 *
 * `get` throws for an unknown name and `requiresApproval` inherits that, which
 * is right for every caller that has already established the tool exists. The
 * loop has not: the name came from a model, and a model can ask for a tool that
 * was never registered. That is the model's mistake to correct, not a reason to
 * end a run.
 */
export function isRegistered(name: string): boolean {
  return REGISTRY.has(name);
}

export function allTools(): ToolDef[] {
  return [...REGISTRY.values()].sort((a, b) => (a.name < b.name ? -1 : 1));
}

/**
 * Tool definitions for the model, in a stable order.
 *
 * Sorted by name so the serialised tool block is byte-identical between
 * requests. Tools render first in the prompt, so any reordering would
 * invalidate the entire prompt cache on every call.
 */
export function apiSchemas(): Array<Record<string, unknown>> {
  return allTools().map((t) => ({
    name: t.name,
    description: t.description,
    input_schema: t.parameters,
    strict: true,
  }));
}

export function validate(name: string, args: Record<string, unknown>): void {
  const validator = VALIDATORS.get(name);
  if (!validator) throw new ToolError(`no such tool: ${JSON.stringify(name)}`);
  if (!validator(args)) {
    const first = validator.errors?.[0];
    const where = first?.instancePath ? `${first.instancePath} ` : "";
    throw new ToolError(`invalid arguments for ${name}: ${where}${first?.message ?? "invalid"}`);
  }
}

/**
 * The only question the runtime asks about a tool before running it.
 *
 * Answered from the registry, by name. Not from the model's request, not from
 * an argument, and never from a previous tool's output.
 */
export function requiresApproval(name: string): boolean {
  return get(name).risk === RiskClass.IRREVERSIBLE;
}

/**
 * A stable fingerprint of "this exact call".
 *
 * An approval is bound to this value, so a human who approves a $19 refund has
 * not approved a $1,900 one. Keys are sorted so that two objects that are equal
 * hash equally regardless of construction order.
 *
 * This function is the single most load-bearing thing that did *not* delete in
 * the port. A waitpoint token replaces the approvals table's plumbing, the
 * pending state, the expiry and the wake-up, but the token's completion payload
 * arrives from outside the run, and a token id is a capability to resume, not a
 * statement about what was consented to. See `consent.ts`.
 */
export function argsHash(name: string, args: Record<string, unknown>): string {
  const payload = stableStringify({ args, tool: name });
  return createHash("sha256").update(payload).digest("hex");
}

/** JSON with object keys sorted at every depth, so the digest is stable. */
function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([k, v]) => `${JSON.stringify(k)}:${stableStringify(v)}`);
  return `{${entries.join(",")}}`;
}

/**
 * Build a strict-mode argument schema. `required` defaults to every property.
 */
export function schema(
  properties: Record<string, unknown>,
  required?: string[],
): Record<string, unknown> {
  return {
    type: "object",
    properties,
    required: required ?? Object.keys(properties),
    additionalProperties: false,
  };
}

export function money(cents: number, currency = "USD"): string {
  return `${(cents / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${currency}`;
}
