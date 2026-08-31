/**
 * Invariant 4, the half that actually holds: a tool's risk class is declared in
 * code and nothing in a tool result can reach it.
 *
 * The fence removes structural ambiguity. This is the guarantee underneath it.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  allTools,
  argsHash,
  get,
  isRegistered,
  RiskClass,
  requiresApproval,
} from "../src/tools/index.ts";

test("issue_refund is irreversible and therefore gated", () => {
  assert.equal(get("issue_refund").risk, RiskClass.IRREVERSIBLE);
  assert.equal(requiresApproval("issue_refund"), true);
});

test("reads and reversible writes are not gated", () => {
  assert.equal(requiresApproval("get_order"), false);
  assert.equal(requiresApproval("add_internal_note"), false);
});

test("a tool's risk class cannot be reassigned at runtime", () => {
  const tool = get("issue_refund");
  // The registry hands out frozen objects. An assignment is a no-op in sloppy
  // mode and a TypeError under a module's implicit strict mode; either way the
  // class does not move. This is the property the whole integrity argument
  // rests on, so it is asserted rather than assumed.
  assert.throws(() => {
    (tool as { risk: string }).risk = RiskClass.READ;
  });
  assert.equal(get("issue_refund").risk, RiskClass.IRREVERSIBLE);
  assert.equal(requiresApproval("issue_refund"), true);
});

test("an unregistered name is a question the runtime can ask without throwing", () => {
  assert.equal(isRegistered("issue_refund"), true);
  assert.equal(isRegistered("wire_transfer"), false);
  assert.throws(() => get("wire_transfer"), /no such tool/);
});

test("tools render in a stable order, so the prompt prefix stays cacheable", () => {
  const names = allTools().map((t) => t.name);
  assert.deepEqual(names, [...names].sort());
});

test("the argument hash is stable across key order and sensitive to value", () => {
  const a = argsHash("issue_refund", { order_reference: "NW-1042", amount_cents: 1900, reason: "x" });
  const b = argsHash("issue_refund", { reason: "x", amount_cents: 1900, order_reference: "NW-1042" });
  assert.equal(a, b, "two equal argument objects must hash equally however they were built");

  const c = argsHash("issue_refund", { order_reference: "NW-1042", amount_cents: 4800, reason: "x" });
  assert.notEqual(a, c, "a human who approved USD 19.00 has not approved USD 48.00");
});

test("every irreversible tool renders a preview for the human to read", () => {
  for (const tool of allTools()) {
    if (tool.risk !== RiskClass.IRREVERSIBLE) continue;
    assert.ok(tool.preview, `${tool.name} is gated but renders nothing for the approver`);
  }
});
