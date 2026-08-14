-- Identity: merchants, the humans who work for them, and login sessions.
--
-- Multi-tenancy is present but deliberately lean here. The tenancy story is
-- told in depth in the companion project (knowledge-desk); this repo's subject
-- is the agent runtime, so orgs exist to make "whose money did it refund" and
-- "who approved it" answerable, not to demonstrate isolation for its own sake.

create table orgs (
    id          uuid primary key default gen_random_uuid(),
    slug        text not null unique,
    name        text not null,
    created_at  timestamptz not null default now()
);

-- owner  : can approve irreversible actions and manage members
-- agent  : can start runs and approve irreversible actions
-- viewer : read-only; can watch a run but never approve one
create type user_role as enum ('owner', 'agent', 'viewer');

create table users (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references orgs (id) on delete cascade,
    email          text not null,
    password_hash  text not null,
    role           user_role not null default 'agent',
    created_at     timestamptz not null default now()
);

-- Emails are compared case-insensitively, so uniqueness has to be too.
create unique index users_org_email_key on users (org_id, lower(email));

create table sessions (
    -- The bearer token is stored hashed. A dump of this table does not let
    -- you log in as anybody.
    token_hash  text primary key,
    user_id     uuid not null references users (id) on delete cascade,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null
);

create index sessions_user_idx on sessions (user_id);
create index sessions_expiry_idx on sessions (expires_at);
