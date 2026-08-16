import { useState } from "react";
import { api, type ReplayBlock, type ReplayMessage } from "../api";

// Time travel. Any model step in a trajectory can be asked "what did you
// actually see here?", and the answer is reconstructed from the step log rather
// than remembered — so it is the same answer months later, and it is still the
// answer for a run that happened on a machine that no longer exists.
//
// The fence markers are left in rather than stripped. Everywhere else the UI
// renders untrusted content behind a red rule; here the point is to show the
// model's literal view, delimiters and all, because "could the model tell where
// the customer's words ended?" is exactly the question this panel exists to
// answer.
export default function StepPrompt({ runId, seq }: { runId: string; seq: number }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ReplayMessage[] | null>(null);
  const [system, setSystem] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (messages) return;
    try {
      const data = await api.replay(runId, seq);
      setMessages(data.messages);
      setSystem(data.system);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <>
      <button className="peek" onClick={toggle}>
        {open ? "hide what the model saw" : "what the model saw here"}
      </button>

      {open && (
        <div className="prompt-view">
          {error && <div className="error">{error}</div>}
          {!messages && !error && <div className="empty">reconstructing…</div>}

          {messages && (
            <>
              <details>
                <summary>system prompt ({system.length} chars)</summary>
                <pre>{system}</pre>
              </details>
              {messages.map((message, i) => (
                <div key={i} className="prompt-turn">
                  <div className="prompt-role">{message.role}</div>
                  {typeof message.content === "string" ? (
                    <pre>{message.content}</pre>
                  ) : (
                    message.content.map((block, j) => <Block key={j} block={block} />)
                  )}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </>
  );
}

function Block({ block }: { block: ReplayBlock }) {
  if (block.type === "text") return <pre>{block.text}</pre>;
  if (block.type === "thinking") return <pre className="thinking">[thinking]</pre>;
  if (block.type === "tool_use") {
    return (
      <pre>
        {block.name}({JSON.stringify(block.input, null, 2)})
      </pre>
    );
  }
  if (block.type === "tool_result") {
    return (
      <pre style={block.is_error ? { color: "var(--bad)" } : undefined}>
        {String(block.content ?? "")}
      </pre>
    );
  }
  return <pre>{JSON.stringify(block, null, 2)}</pre>;
}
