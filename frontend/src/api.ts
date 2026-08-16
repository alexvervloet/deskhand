// The API client, and the one piece of it worth reading: `streamRun`.
//
// EventSource is the obvious way to consume server-sent events and it cannot
// send an Authorization header, so every tutorial reaches for `?token=...`.
// That puts a live session token into access logs, browser history, and any
// Referer the page later emits. Reading the stream through `fetch` keeps the
// token in a header where it belongs, at the cost of parsing the wire format
// by hand — which is about twenty lines.

const BASE = import.meta.env.VITE_API_BASE ?? "";

export type User = {
  id: string;
  email: string;
  role: string;
  org_id: string;
  org_slug: string;
  org_name: string;
  can_approve: boolean;
};

export type Ticket = {
  id: string;
  reference: string;
  subject: string;
  status: string;
  priority: string;
  tags: string[];
  customer_name: string;
  customer_email: string;
  created_at: string;
  open_run_id: string | null;
};

export type TicketMessage = {
  author_kind: string;
  is_internal: boolean;
  body: string;
  created_at: string;
};

export type TicketDetail = Ticket & { messages: TicketMessage[] };

export type Step = {
  seq: number;
  kind: string;
  tool_name: string | null;
  content: Record<string, unknown>;
  input_tokens: number;
  output_tokens: number;
  cost_micros: number;
  cost_display: string;
  latency_ms: number;
  created_at: string;
};

export type ReplayBlock = {
  type: string;
  text?: string;
  name?: string;
  input?: unknown;
  content?: string;
  is_error?: boolean;
};

export type ReplayMessage = { role: string; content: string | ReplayBlock[] };

export type Approval = {
  id: string;
  run_id: string;
  ticket_reference: string | null;
  tool_name: string;
  preview: string;
  args: Record<string, unknown>;
  status: string;
  reason: string | null;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
};

export type Run = {
  id: string;
  ticket_id: string;
  ticket_reference: string | null;
  status: string;
  stop_reason: string | null;
  stop_detail: string | null;
  provider: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_micros: number;
  cost_display: string;
  attempt: number;
  created_at: string;
  finished_at: string | null;
};

export type RunDetail = Run & {
  prompt: string;
  max_steps: number;
  max_tokens: number;
  max_spend_micros: number;
  deadline_at: string;
  steps: Step[];
  approvals: Approval[];
};

export type Usage = {
  org_spend_today_micros: number;
  org_spend_today_display: string;
  org_daily_budget_micros: number;
  platform_spend_today_micros: number;
  platform_daily_budget_micros: number;
  runs_today: number;
  refunds_today_cents: number;
  refunds_today_display: string;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let token: string | null = localStorage.getItem("deskhand.token");

export function setToken(value: string | null): void {
  token = value;
  if (value) localStorage.setItem("deskhand.token", value);
  else localStorage.removeItem("deskhand.token");
}

export function hasToken(): boolean {
  return token !== null;
}

function headers(): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json" };
  if (token) base.Authorization = `Bearer ${token}`;
  return base;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { ...init, headers: headers() });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      // A non-JSON error body is still an error; the status carries the meaning.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  login: (email: string, password: string) =>
    request<{ token: string; expires_at: string; user: User }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<User>("/me"),
  health: () => request<{ ok: boolean; provider: string; model: string }>("/healthz"),
  // The risk model lives in the Python registry; the UI reads it rather
  // than keeping a second copy that could disagree.
  tools: () => request<{ name: string; risk: string; description: string }[]>("/tools"),

  tickets: () => request<Ticket[]>("/tickets"),
  ticket: (reference: string) => request<TicketDetail>(`/tickets/${reference}`),

  runs: () => request<Run[]>("/runs"),
  run: (id: string) => request<RunDetail>(`/runs/${id}`),
  startRun: (ticketReference: string) =>
    request<Run>("/runs", {
      method: "POST",
      body: JSON.stringify({ ticket_reference: ticketReference }),
    }),
  cancelRun: (id: string) => request<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  // What the model saw before a given step. Reconstructed from the step log,
  // which is a pure function of rows — so this is the same bytes today and in
  // a year, and no model is called to produce it.
  replay: (id: string, at: number) =>
    request<{ system: string; messages: ReplayMessage[] }>(`/runs/${id}/replay?at=${at}`),

  approvals: () => request<Approval[]>("/approvals"),
  decide: (id: string, decision: "approved" | "denied", reason?: string) =>
    request<Approval>(`/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify({ decision, reason: reason || null }),
    }),

  usage: () => request<Usage>("/usage"),
};

export type StreamHandlers = {
  onStep?: (step: Step) => void;
  onStatus?: (run: Run) => void;
  onApproval?: (approvals: Approval[]) => void;
  onDone?: () => void;
};

/** Read a run's trajectory as it happens. Returns an abort function. */
export function streamRun(runId: string, handlers: StreamHandlers): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch(`${BASE}/runs/${runId}/stream`, {
        headers: headers(),
        signal: controller.signal,
      });
      if (!response.body) return;

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Events are separated by a blank line. Anything after the last one is
        // a partial event and stays in the buffer until the rest arrives.
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";

        for (const chunk of chunks) {
          const eventLine = chunk.split("\n").find((l) => l.startsWith("event: "));
          const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!eventLine || !dataLine) continue;

          const event = eventLine.slice(7).trim();
          const data = JSON.parse(dataLine.slice(6));

          if (event === "step") handlers.onStep?.(data as Step);
          else if (event === "status") handlers.onStatus?.(data as Run);
          else if (event === "approval") handlers.onApproval?.(data as Approval[]);
          else if (event === "done") handlers.onDone?.();
        }
      }
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        console.error("stream failed", error);
      }
    }
  })();

  return () => controller.abort();
}
