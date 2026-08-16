import type { Step } from "../api";
import StepPrompt from "./StepPrompt";

// Rendering rule: untrusted content looks untrusted.
//
// *Every* tool result is untrusted — that is what a tool result is. It is a
// ticket somebody typed, a record somebody else's system returned, text from
// outside this program. So the viewer marks all of them, rather than trying to
// detect which ones are dangerous, which would be the same losing game the
// approval gate exists to avoid playing.
//
// The run's fence is a mechanism for the *model*, applied when the conversation
// is rebuilt; the step log stores the raw result. Stripping the markers here
// too keeps the display honest if that ever changes.
const FENCE = /^<<<untrusted:[0-9a-f]{12}>>>\n?([\s\S]*?)\n?<<<\/untrusted:[0-9a-f]{12}>>>$/;

function unfence(text: string): string {
  const match = FENCE.exec(text.trim());
  return match ? match[1] : text;
}

type Block = { type: string; text?: string; thinking?: string; name?: string; input?: unknown };

export default function Trajectory({
  runId,
  steps,
  riskOf,
}: {
  runId: string;
  steps: Step[];
  riskOf: (tool: string | null) => string;
}) {
  if (steps.length === 0) {
    return <div className="empty">No steps yet. The agent has not started thinking.</div>;
  }
  return (
    <div>
      {steps.map((step) => (
        <StepRow key={step.seq} runId={runId} step={step} risk={riskOf(step.tool_name)} />
      ))}
    </div>
  );
}

function StepRow({ runId, step, risk }: { runId: string; step: Step; risk: string }) {
  const kindClass = step.kind === "tool_result" ? risk : step.kind;
  return (
    <div className={`step ${kindClass}`}>
      <div className="step-head">
        <span className="seq">{step.seq}</span>
        <span className="name">{label(step)}</span>
        {step.kind === "tool_result" && <span className={`chip ${risk}`}>{risk}</span>}
        {Boolean(step.content.replayed) && (
          <span className="chip" title="This step was already recorded; it was not executed again.">
            replayed
          </span>
        )}
        <span className="cost">
          {step.latency_ms > 0 && `${step.latency_ms}ms`}
          {step.cost_micros > 0 && ` · ${step.cost_display}`}
        </span>
      </div>
      <div className="step-body">
        {body(step)}
        {step.kind === "model_call" && <StepPrompt runId={runId} seq={step.seq} />}
      </div>
    </div>
  );
}

function label(step: Step): string {
  switch (step.kind) {
    case "model_call":
      return "model";
    case "tool_result":
      return step.tool_name ?? "tool";
    case "approval":
      return `approval ${String(step.content.decision ?? "")}`;
    case "final":
      return "final";
    default:
      return step.kind;
  }
}

function body(step: Step) {
  if (step.kind === "model_call") {
    const blocks = (step.content.blocks ?? []) as Block[];
    const thinking = blocks.filter((b) => b.type === "thinking");
    const text = blocks.filter((b) => b.type === "text").map((b) => b.text ?? "");
    const calls = blocks.filter((b) => b.type === "tool_use");

    return (
      <>
        {thinking.length > 0 && (
          <div className="thinking">
            {/* On current models the raw chain of thought is never returned, so
                this is a summary when one was requested and empty otherwise.
                Shown rather than hidden, because "it thought here" is itself
                information when you are reading a trajectory. */}
            {thinking.map((b) => b.thinking).filter(Boolean).join("\n") || "thought"}
          </div>
        )}
        {text.map((t, i) => t && <p key={i}>{t}</p>)}
        {calls.map((c, i) => (
          <pre key={i}>
            {c.name}({JSON.stringify(c.input, null, 2)})
          </pre>
        ))}
      </>
    );
  }

  if (step.kind === "tool_result") {
    const args = step.content.args as unknown;
    const ok = step.content.ok !== false;
    const text = unfence(String(step.content.result ?? ""));

    return (
      <>
        {args != null && Object.keys(args as object).length > 0 && (
          <pre>{JSON.stringify(args, null, 2)}</pre>
        )}
        <div className="fenced">
          <div className="fence-label">
            {ok ? "untrusted · from outside this program" : "tool failed"}
          </div>
          <pre style={ok ? undefined : { color: "var(--bad)" }}>{text}</pre>
        </div>
      </>
    );
  }

  if (step.kind === "approval") {
    return (
      <p>
        A human {String(step.content.decision)} <code>{String(step.content.tool_name)}</code>
        {step.content.reason ? ` — ${String(step.content.reason)}` : ""}
      </p>
    );
  }

  if (step.kind === "final") {
    return <p>{String(step.content.summary ?? "")}</p>;
  }

  return <pre>{JSON.stringify(step.content, null, 2)}</pre>;
}
