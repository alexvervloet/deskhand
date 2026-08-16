import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamRun, type Approval, type RunDetail, type Step, type User } from "../api";
import Trajectory from "./Trajectory";

export default function RunView({
  runId,
  user,
  riskOf,
  onChanged,
}: {
  runId: string;
  user: User;
  riskOf: (tool: string | null) => string;
  onChanged: () => void;
}) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [pending, setPending] = useState<Approval[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seen = useRef(new Set<number>());

  const reload = useCallback(async () => {
    const detail = await api.run(runId);
    setRun(detail);
    setSteps(detail.steps);
    seen.current = new Set(detail.steps.map((s) => s.seq));
    setPending(detail.approvals.filter((a) => a.status === "pending"));
  }, [runId]);

  useEffect(() => {
    seen.current = new Set();
    setSteps([]);
    setPending([]);
    reload().catch((e) => setError((e as Error).message));

    // The stream is the point of this screen: steps land as they happen, so
    // the moment the agent stops and waits is something you watch rather than
    // something you discover by refreshing.
    const stop = streamRun(runId, {
      onStep: (step) => {
        if (seen.current.has(step.seq)) return;
        seen.current.add(step.seq);
        setSteps((prev) => [...prev, step].sort((a, b) => a.seq - b.seq));
      },
      onStatus: (summary) =>
        setRun((prev) => (prev ? { ...prev, ...summary } : prev)),
      onApproval: setPending,
      onDone: () => {
        reload().catch(() => undefined);
        onChanged();
      },
    });
    return stop;
  }, [runId, reload, onChanged]);

  if (error) return <div className="error">{error}</div>;
  if (!run) return <div className="empty">Loading…</div>;

  const live = ["queued", "running", "awaiting_approval"].includes(run.status);

  return (
    <div>
      <div className="run-head">
        <div>
          <h2>
            {run.ticket_reference} <span className={`chip ${run.status}`}>{run.status}</span>
          </h2>
          <div className="sub">
            {run.provider === "mock" ? (
              <>
                scripted mock provider — no model was called
              </>
            ) : (
              <>
                {run.provider} · {run.model}
              </>
            )}
            {run.attempt > 1 && ` · attempt ${run.attempt}`}
            {run.stop_reason && ` · stopped: ${run.stop_reason}`}
          </div>
          {run.stop_detail && <div className="sub">{run.stop_detail}</div>}
        </div>
        <div className="run-actions">
          {live && (
            <button
              className="danger"
              onClick={async () => {
                await api.cancelRun(run.id);
                await reload();
                onChanged();
              }}
            >
              Cancel run
            </button>
          )}
        </div>
      </div>

      <div className="bounds">
        <Bound label="steps" value={`${steps.length} / ${run.max_steps}`} />
        <Bound label="spend" value={`${run.cost_display} / $${(run.max_spend_micros / 1e6).toFixed(2)}`} />
        <Bound label="tokens" value={`${run.input_tokens + run.output_tokens} / ${run.max_tokens}`} />
        <Bound label="deadline" value={new Date(run.deadline_at).toLocaleTimeString()} />
      </div>

      {pending.map((approval) => (
        <ApprovalCard
          key={approval.id}
          approval={approval}
          user={user}
          onDecided={async () => {
            await reload();
            onChanged();
          }}
        />
      ))}

      <Trajectory runId={runId} steps={steps} riskOf={riskOf} />
    </div>
  );
}

function Bound({ label, value }: { label: string; value: string }) {
  return (
    <div className="bound">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

function ApprovalCard({
  approval,
  user,
  onDecided,
}: {
  approval: Approval;
  user: User;
  onDecided: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: "approved" | "denied") {
    setBusy(true);
    setError(null);
    try {
      await api.decide(approval.id, decision, reason);
      onDecided();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="approval">
      <div className="kicker">The agent has stopped and is waiting for a person</div>
      {/* The preview is the sentence being approved. It is rendered on the
          server from the arguments the model supplied, and the approval is
          bound to a hash of those exact arguments — so what is agreed to here
          is what runs, or nothing runs. */}
      <div className="preview">{approval.preview}</div>
      <div className="tool">
        {approval.tool_name} · irreversible · expires{" "}
        {new Date(approval.expires_at).toLocaleString()}
      </div>

      {user.can_approve ? (
        <>
          <div className="controls">
            <input
              placeholder="Reason (sent back to the agent on a denial)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <button className="primary" disabled={busy} onClick={() => decide("approved")}>
              Approve
            </button>
            <button className="danger" disabled={busy} onClick={() => decide("denied")}>
              Deny
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </>
      ) : (
        <div className="cannot">
          Your role is <code>{user.role}</code>, which can watch a run spend money but
          cannot authorise it. Sign in as an owner or agent to decide.
        </div>
      )}
    </div>
  );
}
