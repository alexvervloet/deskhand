/**
 * The tool registry.
 *
 * Importing this module registers every tool. The split by file is the risk
 * split, and it is not cosmetic: which class a tool belongs to decides whether
 * it can run without a human, and that decision is made here, once, at import
 * time.
 */

import "./read.ts";
import "./reversible.ts";
import "./irreversible.ts";

export * from "./registry.ts";
