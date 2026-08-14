-- Runs, the append-only step log, and the approval gate.
--
-- The step log is the source of truth. A run's conversation is not held in
-- memory between steps and is not stored as a blob: it is *rebuilt by replaying
-- these rows* every time a worker picks the run up. That is what makes a run
-- resumable by a different process on a different machine after the first one
-- died, and it is why `steps` is append-only and uniquely ordered.

create type run_status as enum (
    'queued',             -- created, waiting for a worker
    'running',            -- a worker holds the lease
    'awaiting_approval',  -- suspended on a human decision; not a failure
    'succeeded',
    'failed',             -- something broke
    'exhausted',          -- hit a bound; the run was stopped, not broken
    'cancelled'           -- a human stopped it
);

create table runs (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references orgs (id) on delete cascade,
    ticket_id      uuid not null references tickets (id) on delete cascade,
    started_by     uuid references users (id) on delete set null,
    status         run_status not null default 'queued',

    -- The opening user turn, frozen at creation. Deliberately *not* derived
    -- from the ticket at replay time: the ticket will have moved on, and a
    -- trajectory you cannot reproduce is not an audit trail. Note what it does
    -- not contain — the customer's words. Those arrive later, through a tool
    -- result, fenced as untrusted.
    prompt         text not null,

    -- Why it stopped, in a vocabulary the UI and the evals both read. Null
    -- while running. `stop_detail` carries the human-readable specifics.
    stop_reason    text,
    stop_detail    text,

    -- Bounds are copied onto the run at creation rather than read from config
    -- at each step. A deploy that raises the step cap must not silently extend
    -- a run that is already in flight, and a run that is replayed months later
    -- must be bounded the way it originally was.
    max_steps        integer not null,
    max_tokens       bigint not null,
    max_spend_micros bigint not null,
    -- Absolute, set once. A resumed run inherits the original deadline instead
    -- of restarting the clock — otherwise a run that crash-loops every 60s
    -- would never time out.
    deadline_at      timestamptz not null,

    -- Lease. A worker claims the run for a bounded window and must renew;
    -- an expired lease means the worker died and the run is claimable again.
    lease_owner      text,
    lease_expires_at timestamptz,
    attempt          integer not null default 0,

    -- Accounting. Cost is integer micro-dollars: 1_000_000 == USD 1.00. No
    -- float touches money anywhere in this system.
    input_tokens   bigint not null default 0,
    output_tokens  bigint not null default 0,
    cost_micros    bigint not null default 0,
    provider       text,
    model          text,

    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    finished_at  timestamptz
);

create index runs_claimable_idx on runs (status, lease_expires_at);
create index runs_org_idx on runs (org_id, created_at desc);
create index runs_ticket_idx on runs (ticket_id, created_at desc);

create type step_kind as enum (
    'model_call',    -- one request to the model; content is the assistant blocks
    'tool_result',   -- one tool executed; content carries args and output
    'approval',      -- a human decision was requested and recorded
    'final',         -- the agent's closing summary
    'error'          -- an unrecoverable failure, kept so the trajectory is complete
);

create table steps (
    id            uuid primary key default gen_random_uuid(),
    run_id        uuid not null references runs (id) on delete cascade,
    -- Dense, starting at 1. Also the second half of the idempotency key for
    -- any tool this step executed, which is why it must be deterministic on
    -- replay and why nothing is ever deleted from this table.
    seq           integer not null,
    kind          step_kind not null,
    content       jsonb not null,
    tool_name     text,
    input_tokens  integer not null default 0,
    output_tokens integer not null default 0,
    cost_micros   bigint not null default 0,
    latency_ms    integer not null default 0,
    created_at    timestamptz not null default now()
);

create unique index steps_run_seq_key on steps (run_id, seq);
create index steps_run_idx on steps (run_id, seq);

-- Consent.
--
-- A row here is a human saying yes to one specific call: this run, this step,
-- this tool, with these arguments. `args_hash` is what binds it — approving a
-- USD 19.00 refund does not approve a USD 1,900.00 one, because the hash would
-- not match and the runtime refuses to execute.
create type approval_status as enum ('pending', 'approved', 'denied', 'expired');

create table approvals (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null references orgs (id) on delete cascade,
    run_id       uuid not null references runs (id) on delete cascade,
    -- The step the tool call will occupy once it runs.
    step_seq     integer not null,
    -- The model's own id for the tool_use block, so the decision can be tied
    -- back to the exact request even when a turn asks for several at once.
    tool_use_id  text not null,
    tool_name    text not null,
    args         jsonb not null,
    args_hash    text not null,
    -- Rendered for the human. Never derived from a tool result.
    preview      text not null,
    status       approval_status not null default 'pending',
    decided_by   uuid references users (id) on delete set null,
    decided_at   timestamptz,
    -- Why it was denied, fed back to the agent as the tool's result so it can
    -- adapt rather than simply stall.
    reason       text,
    -- An approval nobody answers must expire loudly. A run that fails with
    -- `approval_expired` is distinguishable from one that was denied, because
    -- they mean completely different things about the process around it.
    expires_at   timestamptz not null,
    created_at   timestamptz not null default now()
);

create unique index approvals_run_tool_use_key on approvals (run_id, tool_use_id);
create index approvals_pending_idx on approvals (org_id, status, created_at);

-- Now that `runs` and `steps` exist, the ledger's references become real
-- foreign keys rather than loose uuids.
alter table tool_invocations
    add constraint tool_invocations_run_fk
        foreign key (run_id) references runs (id) on delete cascade,
    add constraint tool_invocations_step_fk
        foreign key (step_id) references steps (id) on delete cascade;

alter table audit_log
    add constraint audit_log_run_fk
        foreign key (run_id) references runs (id) on delete set null;

-- Attribution. Every irreversible act points back at the run that performed it
-- and, through the approval, at the human who allowed it.
alter table refunds
    add column run_id uuid references runs (id) on delete set null;
alter table customer_emails
    add column run_id uuid references runs (id) on delete set null;

create index refunds_run_idx on refunds (run_id);
create index customer_emails_run_idx on customer_emails (run_id);
