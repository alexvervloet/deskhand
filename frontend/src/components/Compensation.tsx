import { useCallback, useEffect, useState } from "react";
import {
  api,
  type Compensation,
  type CompensationItem,
  type CompensationPlan,
  type User,
} from "../api";

/**
 * Walking a finished run back.
 *
 * The screen is built around one refusal: there is no button that just says
 * "undo". A person reads the plan, and the request carries a hash of the exact
 * plan they read. If the ledger moved underneath them the server refuses,
 * which is the same device the approval gate uses one level down.
 *
 * The other thing this screen exists to say out loud is the half that cannot
 * be walked back. Those items are rendered first and in red, because a list
 * that quietly showed only what it could revert would read as a clean undo,
 * and that is the most misleading sentence this system could produce after an
 * incident.
 */
export default function CompensationPanel({
  runId,
  runStatus,
  user,
  onChanged,
}: {
  runId: string;
  runStatus: string;
  user: User;
  onChanged: () => void;
}) {
  const [plan, setPlan] = useState<CompensationPlan | null>(null);
  const [history, setHistory] = useState<Compensation[]>([]);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const [next, past] = await Promise.all([
      api.compensationPlan(runId),
      api.compensations(runId),
    ]);
    setPlan(next);
    setHistory(past);
  }, [runId]);

  useEffect(() => {
    setOpen(false);
    setError(null);
    reload().catch((e) => setError((e as Error).message));
  }, [runId, runStatus, reload]);

  // A compensation is applied by the worker, not by this request, so the
  // screen polls while one is in flight rather than pretending the POST
  // finished the job.
  useEffect(() => {
    if (!history.some((c) => c.status === "queued" || c.status === "running")) return;
    const timer = setInterval(() => void reload().catch(() => undefined), 1500);
    return () => clearInterval(timer);
  }, [history, reload]);

  if (!plan) return null;
  if (!plan.compensable && history.length === 0) {
    return (
      <div className="compensation quiet">
        <div className="kicker">Nothing to walk back</div>
        <div className="sub">{plan.blocked_reason}</div>
        {/* Still list what is in the plan. A run whose only mark on the world
            was a refund has nothing to press and something worth reading. */}
        {plan.items.length > 0 && (
          <ol className="comp-plan">
            {plan.items.map((item) => (
              <PlanRow key={item.seq} item={item} />
            ))}
          </ol>
        )}
      </div>
    );
  }

  async function submit() {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      await api.compensate(runId, plan.plan_hash, reason);
      setOpen(false);
      setReason("");
      await reload();
      onChanged();
    } catch (e) {
      // The likeliest failure here is the interesting one: somebody else acted
      // on this run between the preview and the submit, so the plan no longer
      // hashes to what is on screen.
      setError((e as Error).message);
      await reload().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="compensation">
      {history.map((past) => (
        <AppliedCompensation key={past.id} compensation={past} />
      ))}

      {plan.compensable && (
        <>
          <div className="comp-head">
            <div>
              <div className="kicker">This run changed things that can be walked back</div>
              <div className="sub">
                {plan.revertable} to revert
                {plan.unrevertable > 0 && (
                  <>
                    {" · "}
                    <strong className="irreversible">
                      {plan.unrevertable} that cannot be taken back
                    </strong>
                  </>
                )}
              </div>
            </div>
            <button onClick={() => setOpen(!open)}>{open ? "Close" : "Review plan"}</button>
          </div>

          {open && (
            <>
              <ol className="comp-plan">
                {plan.items.map((item) => (
                  <PlanRow key={item.seq} item={item} />
                ))}
              </ol>

              {user.can_approve ? (
                <div className="controls">
                  <input
                    placeholder="Why is this being walked back?"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                  <button
                    className="danger"
                    disabled={busy || reason.trim() === ""}
                    onClick={() => void submit()}
                  >
                    Walk back these {plan.revertable}
                  </button>
                </div>
              ) : (
                <div className="cannot">
                  Your role is <code>{user.role}</code>. It can read what a run did and what
                  could be taken back, and cannot authorise taking it back.
                </div>
              )}
              {error && <div className="error">{error}</div>}
            </>
          )}
        </>
      )}
    </div>
  );
}

function PlanRow({ item }: { item: CompensationItem }) {
  const cannot = item.disposition === "report";
  return (
    <li className={cannot ? "irreversible" : ""}>
      <span className="step">step {item.step_seq}</span>
      <span className="tool">{item.tool_name}</span>
      <span className="what">{item.describe}</span>
      {item.status !== "pending" && <span className={`chip ${item.status}`}>{item.status}</span>}
      {item.detail && <div className="detail">{item.detail}</div>}
    </li>
  );
}

function AppliedCompensation({ compensation }: { compensation: Compensation }) {
  const reverted = compensation.items.filter((i) => i.status === "reverted").length;
  const stuck = compensation.items.filter((i) => i.status === "unrevertable").length;
  return (
    <div className="comp-applied">
      <div className="comp-head">
        <div>
          <div className="kicker">
            Walked back by {compensation.requested_by_email ?? "a deleted account"}{" "}
            <span className={`chip ${compensation.status}`}>{compensation.status}</span>
          </div>
          <div className="sub">
            {compensation.reason} · {reverted} reverted
            {stuck > 0 && (
              <>
                {" · "}
                <strong className="irreversible">{stuck} could not be taken back</strong>
              </>
            )}
          </div>
        </div>
      </div>
      {compensation.stop_detail && <div className="sub">{compensation.stop_detail}</div>}
      <ol className="comp-plan">
        {compensation.items.map((item) => (
          <PlanRow key={item.seq} item={item} />
        ))}
      </ol>
    </div>
  );
}
