-- Walking a finished run back.
--
-- Every reversible tool has recorded its inverse since 0003. Nothing ever
-- applied one. This is the other half: a plan over the ledger, authorised by a
-- human, applied exactly once, and honest about the acts it cannot touch.
--
-- A compensation is deliberately *not* a run and *not* a set of steps.
--
--   Not a run, because the model must not decide what to walk back. A recovery
--   path that asks a language model which effects to undo has put an untrusted
--   decision at the point where the system is already known to be wrong. The
--   plan here is a pure function of `tool_invocations` rows.
--
--   Not steps, because `steps` is the trajectory. Appending rows to a finished
--   run after the fact would make `replay` describe a conversation that never
--   happened, and the step log is the one thing in this system that is allowed
--   to be believed without qualification.
--
-- The word is "compensation" rather than "undo" or "revert" on purpose. Undo
-- promises something this system cannot deliver for the irreversible half: an
-- email that was read cannot be unread, and money that left is gone. A
-- compensation applies what inverses exist and *reports* the rest.

create type compensation_status as enum (
    'queued',     -- authorised, waiting for a worker
    'running',    -- a worker holds the lease
    'applied',    -- every item in the plan was reverted
    'partial',    -- reverted what it could; the plan contained acts it cannot
    'blocked',    -- an inverse failed, or attempts ran out; a human must look
    'cancelled'
);

-- One row per ledger entry the plan covers, in the order it will be applied.
--
--   pending       not yet attempted
--   reverted      the inverse was applied, exactly once
--   unrevertable  irreversible act; recorded so it is reported, never touched
--   failed        the inverse raised; the compensation stopped here
--   skipped       the plan reached this item after stopping
create type compensation_item_status as enum (
    'pending', 'reverted', 'unrevertable', 'failed', 'skipped'
);

create table compensations (
    id            uuid primary key default gen_random_uuid(),
    org_id        uuid not null references orgs (id) on delete cascade,
    run_id        uuid not null references runs (id) on delete cascade,
    -- Who authorised it. Never null in practice: the endpoint requires an
    -- approver, and `on delete set null` only fires if that person is deleted.
    requested_by  uuid references users (id) on delete set null,
    -- Free text from the person asking. This is the "why" that the audit log
    -- cannot derive, and it is the first thing anyone reads during an incident.
    reason        text not null,

    -- Consent, bound the way an approval is bound.
    --
    -- `approvals.args_hash` stops a human who approved a USD 19 refund from
    -- having approved a USD 1,900 one. This is the same device one level up: a
    -- person authorises a specific *list of items*, and if the plan recomputed
    -- at submit time differs from the plan they were shown, the request is
    -- refused rather than executed. Without it, "revert this run" is a blank
    -- cheque against whatever the ledger happens to say a moment later.
    plan_hash     text not null,

    status        compensation_status not null default 'queued',
    stop_reason   text,
    stop_detail   text,

    -- Lease, identical in spirit to `runs`. A worker that dies stops renewing
    -- and the compensation becomes claimable again.
    lease_owner      text,
    lease_expires_at timestamptz,
    -- Bounded retries. A run is bounded by steps, tokens, spend and a
    -- deadline; a compensation has no model calls to bound, so the only way it
    -- can fail to terminate is by crash-looping. This is that bound.
    attempt          integer not null default 0,
    max_attempts     integer not null,

    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    finished_at  timestamptz
);

-- At most one compensation in flight per run. Two workers walking the same run
-- backwards from two different plans is the one race the item-level guard
-- below cannot describe in a useful error.
create unique index compensations_active_run_key
    on compensations (run_id) where status in ('queued', 'running');

create index compensations_claimable_idx on compensations (status, lease_expires_at);
create index compensations_org_idx on compensations (org_id, created_at desc);
create index compensations_run_idx on compensations (run_id, created_at desc);

create table compensation_items (
    id               uuid primary key default gen_random_uuid(),
    compensation_id  uuid not null references compensations (id) on delete cascade,
    -- Dense, from 1, in application order: newest effect first. See the note on
    -- `plan()` in deskhand/runtime/compensation.py for why the order matters
    -- rather than merely looking tidy.
    seq              integer not null,
    invocation_id    uuid not null references tool_invocations (id) on delete cascade,
    -- The step of the original run this undoes, carried for display so the UI
    -- need not join back through the ledger.
    step_seq         integer not null,
    tool_name        text not null,
    risk             text not null,
    -- The inverse captured at execution time, copied here rather than read
    -- through the ledger at apply time. Same reason bounds are snapshotted onto
    -- a run: the human authorised *this* operation, and a plan that re-reads
    -- its instructions later is not the plan that was approved.
    inverse          jsonb,
    -- 'revert' or 'report'. Derived from whether an inverse exists, which is
    -- derived from the tool's risk class, which is frozen in the registry.
    disposition      text not null check (disposition in ('revert', 'report')),
    status           compensation_item_status not null default 'pending',
    detail           text,
    applied_at       timestamptz
);

create unique index compensation_items_seq_key on compensation_items (compensation_id, seq);
create index compensation_items_comp_idx on compensation_items (compensation_id, seq);

-- Exactly-once, enforced by the database rather than argued for in a comment.
--
-- The runtime already guarantees it: an item flips to `reverted` in the same
-- transaction as the inverse's effect, and the flip is a conditional update
-- that a second attempt loses. This index is what turns a leasing bug, or a
-- second compensation built from a stale plan, into a constraint violation
-- instead of a ticket that gets un-tagged twice.
create unique index compensation_items_once_key
    on compensation_items (invocation_id) where status = 'reverted';
