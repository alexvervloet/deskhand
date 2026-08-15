import { useCallback, useEffect, useState } from "react";
import {
  api,
  hasToken,
  setToken,
  type Approval,
  type Ticket,
  type TicketDetail,
  type User,
} from "./api";
import Login from "./components/Login";
import RunView from "./components/RunView";

type ToolInfo = { name: string; risk: string; description: string };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);

  useEffect(() => {
    if (!hasToken()) {
      setBooting(false);
      return;
    }
    api
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setBooting(false));
  }, []);

  if (booting) return <div className="empty">…</div>;
  if (!user) return <Login onSignedIn={setUser} />;
  return <Desk user={user} onSignedOut={() => setUser(null)} />;
}

function Desk({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TicketDetail | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [health, setHealth] = useState<{ provider: string; model: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [list, queue] = await Promise.all([api.tickets(), api.approvals()]);
    setTickets(list);
    setApprovals(queue);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError((e as Error).message));
    api.tools().then(setTools).catch(() => undefined);
    api.health().then(setHealth).catch(() => undefined);
  }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    api.ticket(selected).then((t) => {
      setDetail(t);
      setRunId(t.open_run_id);
    });
  }, [selected, tickets]);

  const riskOf = useCallback(
    (tool: string | null) => tools.find((t) => t.name === tool)?.risk ?? "read",
    [tools],
  );

  async function start(reference: string) {
    try {
      const run = await api.startRun(reference);
      setRunId(run.id);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <h1>Deskhand</h1>
          <div className="tagline">
            {health?.provider === "mock"
              ? "scripted mock — no model is being called"
              : `${health?.provider ?? "…"} · ${health?.model ?? ""}`}
          </div>
        </div>

        <div className="who">
          <span>
            {user.email}
            <br />
            <span style={{ color: "var(--text-faint)" }}>
              {user.org_name} · {user.role}
              {!user.can_approve && " · cannot approve"}
            </span>
          </span>
          <button
            onClick={async () => {
              await api.logout().catch(() => undefined);
              setToken(null);
              onSignedOut();
            }}
          >
            Sign out
          </button>
        </div>

        <div className="scroll">
          {approvals.length > 0 && (
            <>
              <div className="section-title">
                Waiting on you ({approvals.length})
              </div>
              {approvals.map((a) => (
                <button
                  key={a.id}
                  className="ticket"
                  onClick={() => {
                    setRunId(a.run_id);
                    setSelected(a.ticket_reference);
                  }}
                >
                  <div className="ref">{a.ticket_reference}</div>
                  <div className="subject">{a.preview}</div>
                  <div className="meta">
                    <span className="chip awaiting_approval">{a.tool_name}</span>
                  </div>
                </button>
              ))}
            </>
          )}

          <div className="section-title">Tickets</div>
          {tickets.map((ticket) => (
            <button
              key={ticket.id}
              className={`ticket ${selected === ticket.reference ? "selected" : ""}`}
              onClick={() => setSelected(ticket.reference)}
            >
              <div className="ref">{ticket.reference}</div>
              <div className="subject">{ticket.subject}</div>
              <div className="meta">
                <span className={`chip ${ticket.status}`}>{ticket.status}</span>
                <span className={`chip ${ticket.priority}`}>{ticket.priority}</span>
                {ticket.open_run_id && <span className="chip running">run open</span>}
              </div>
            </button>
          ))}
        </div>

        <Usage />
      </aside>

      <main className="main">
        {error && <div className="error">{error}</div>}

        {!selected && (
          <div className="empty">
            Pick a ticket. Then run the agent on it and watch what it does.
          </div>
        )}

        {selected && detail && !runId && (
          <TicketPane detail={detail} onRun={() => start(detail.reference)} />
        )}

        {runId && (
          <RunView
            runId={runId}
            user={user}
            riskOf={riskOf}
            onChanged={() => refresh().catch(() => undefined)}
          />
        )}

        {runId && detail && (
          <div style={{ marginTop: 36 }}>
            <button onClick={() => setRunId(null)}>← Back to the ticket</button>
          </div>
        )}
      </main>
    </div>
  );
}

function TicketPane({ detail, onRun }: { detail: TicketDetail; onRun: () => void }) {
  return (
    <div>
      <div className="run-head">
        <div>
          <h2>
            {detail.reference} <span className={`chip ${detail.status}`}>{detail.status}</span>
          </h2>
          <div className="sub">
            {detail.subject} · {detail.customer_name} &lt;{detail.customer_email}&gt;
          </div>
        </div>
        <div className="run-actions">
          <button className="primary" onClick={onRun}>
            Run the agent
          </button>
        </div>
      </div>

      {detail.messages.map((message, i) => (
        <div key={i} className="step">
          <div className="step-head">
            <span className="name">{message.author_kind}</span>
            {message.is_internal && <span className="chip">internal</span>}
            <span className="cost">{new Date(message.created_at).toLocaleDateString()}</span>
          </div>
          <div className="step-body">
            <pre>{message.body}</pre>
          </div>
        </div>
      ))}
    </div>
  );
}

function Usage() {
  const [usage, setUsage] = useState<Awaited<ReturnType<typeof api.usage>> | null>(null);

  useEffect(() => {
    const tick = () => api.usage().then(setUsage).catch(() => undefined);
    tick();
    const timer = setInterval(tick, 5000);
    return () => clearInterval(timer);
  }, []);

  if (!usage) return null;
  const pct = (a: number, b: number) => `${Math.min(100, (a / Math.max(b, 1)) * 100)}%`;

  return (
    <div className="usage">
      <div className="row">
        <span>this merchant today</span>
        <span>{usage.org_spend_today_display}</span>
      </div>
      <div className="bar">
        <span style={{ width: pct(usage.org_spend_today_micros, usage.org_daily_budget_micros) }} />
      </div>
      {/* Both ceilings are shown. The per-merchant cap bounds one tenant; the
          service cap is the one that bounds the bill, and a deployment that
          watches only the first gets a surprise. */}
      <div className="row">
        <span>service today</span>
        <span>${(usage.platform_spend_today_micros / 1e6).toFixed(4)}</span>
      </div>
      <div className="bar">
        <span
          style={{
            width: pct(usage.platform_spend_today_micros, usage.platform_daily_budget_micros),
          }}
        />
      </div>
      <div className="row">
        <span>refunded today</span>
        <span>{usage.refunds_today_display}</span>
      </div>
    </div>
  );
}
