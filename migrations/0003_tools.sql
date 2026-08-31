-- The idempotency ledger and the audit trail.
--
-- `tool_invocations` is the mechanism that makes "never re-execute a completed
-- side effect" true. Every invocation claims a key derived from (run, step),
-- and the claim is written in the SAME transaction as the tool's effect. That
-- single fact removes the usual limbo:
--
--   crash before commit -> neither the effect nor the claim exists, so a
--                          resumed run re-executes cleanly
--   crash after commit  -> both exist, so a resumed run reads the recorded
--                          result and does not touch the world again
--
-- This works because every side effect in this system is a write to this same
-- database. A tool that called an external payment API could not do it, and
-- would need a third `claimed` state plus reconciliation against the provider.
-- That is a real difference, not a detail.
--
-- run_id and step_id are unconstrained uuids here; the foreign keys are added
-- by the migration that introduces `runs` and `steps`.

create type invocation_status as enum ('succeeded', 'failed');

create table tool_invocations (
    id               uuid primary key default gen_random_uuid(),
    org_id           uuid not null references orgs (id) on delete cascade,
    run_id           uuid not null,
    step_id          uuid not null,
    tool_name        text not null,
    risk             text not null,
    -- Derived from (run_id, step seq). Deterministic on purpose: a resumed run
    -- recomputes the identical key, and this unique constraint is what turns
    -- the second attempt into a read.
    idempotency_key  text not null unique,
    -- Hash of the canonical arguments. An approval is bound to this value, so
    -- an approved refund cannot be executed with a different amount.
    args_hash        text not null,
    args             jsonb not null,
    status           invocation_status not null,
    -- What the model is shown. Always a string; never re-parsed as instruction.
    result           text not null,
    -- For reversible tools, the inverse operation captured at execution time
    -- rather than derived later, so a revert never has to guess prior state.
    inverse          jsonb,
    duration_ms      integer not null default 0,
    created_at       timestamptz not null default now()
);

create index tool_invocations_run_idx on tool_invocations (run_id, created_at);
create index tool_invocations_org_idx on tool_invocations (org_id, created_at desc);

-- Who did what, including the humans. Tool invocations are recorded above;
-- this table carries everything else that needs to be answerable later:
-- approvals granted and denied, runs started and cancelled, reverts applied.
create table audit_log (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references orgs (id) on delete cascade,
    -- 'human' rows carry actor_id; 'agent' and 'system' rows do not.
    actor_kind  text not null check (actor_kind in ('human', 'agent', 'system')),
    actor_id    uuid references users (id) on delete set null,
    run_id      uuid,
    action      text not null,
    detail      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

create index audit_log_org_idx on audit_log (org_id, created_at desc);
create index audit_log_run_idx on audit_log (run_id, created_at);
