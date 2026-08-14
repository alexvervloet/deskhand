-- The world the agent operates on: customers, their orders, the tickets they
-- open, the knowledge base, and the two ledgers that record irreversible acts
-- (refunds issued, emails sent).
--
-- Attribution columns tying these rows back to the run that created them are
-- added in the migration that introduces `runs`, so the foreign keys can be
-- declared rather than implied.

create table customers (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references orgs (id) on delete cascade,
    name        text not null,
    email       text not null,
    created_at  timestamptz not null default now()
);

create unique index customers_org_email_key on customers (org_id, lower(email));

create type order_status as enum ('placed', 'shipped', 'delivered', 'cancelled');

create table orders (
    id            uuid primary key default gen_random_uuid(),
    org_id        uuid not null references orgs (id) on delete cascade,
    customer_id   uuid not null references customers (id) on delete cascade,
    -- The reference a customer actually quotes in a ticket ("order NW-1042").
    reference     text not null,
    status        order_status not null default 'placed',
    -- Money is integer minor units everywhere. No floats touch a currency
    -- amount at any point in this codebase.
    total_cents   bigint not null check (total_cents >= 0),
    currency      char(3) not null default 'USD',
    placed_at     timestamptz not null default now(),
    delivered_at  timestamptz,
    cancelled_at  timestamptz
);

create unique index orders_org_reference_key on orders (org_id, reference);
create index orders_customer_idx on orders (customer_id);

create table order_items (
    id                uuid primary key default gen_random_uuid(),
    order_id          uuid not null references orders (id) on delete cascade,
    sku               text not null,
    description       text not null,
    quantity          integer not null check (quantity > 0),
    unit_price_cents  bigint not null check (unit_price_cents >= 0)
);

create index order_items_order_idx on order_items (order_id);

-- Refunds are the canonical irreversible act. Money leaves; there is no undo
-- handler, only a compensating refund in the other direction that a human has
-- to authorise separately.
create table refunds (
    id            uuid primary key default gen_random_uuid(),
    org_id        uuid not null references orgs (id) on delete cascade,
    order_id      uuid not null references orders (id) on delete cascade,
    amount_cents  bigint not null check (amount_cents > 0),
    currency      char(3) not null default 'USD',
    reason        text not null,
    created_at    timestamptz not null default now()
);

create index refunds_order_idx on refunds (order_id);

-- Outbox for customer email. Rows here mean "this was sent" — the tool writes
-- the row and performs the send in the same transaction-guarded step, so a
-- crash can never leave a sent email unrecorded.
create table customer_emails (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null references orgs (id) on delete cascade,
    customer_id  uuid not null references customers (id) on delete cascade,
    ticket_id    uuid,
    subject      text not null,
    body         text not null,
    sent_at      timestamptz not null default now()
);

create index customer_emails_customer_idx on customer_emails (customer_id);

create type ticket_status as enum ('open', 'pending', 'resolved', 'escalated');
create type ticket_priority as enum ('low', 'normal', 'high', 'urgent');

create table tickets (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null references orgs (id) on delete cascade,
    customer_id  uuid not null references customers (id) on delete cascade,
    reference    text not null,
    subject      text not null,
    status       ticket_status not null default 'open',
    priority     ticket_priority not null default 'normal',
    tags         text[] not null default '{}',
    assignee_id  uuid references users (id) on delete set null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create unique index tickets_org_reference_key on tickets (org_id, reference);
create index tickets_status_idx on tickets (org_id, status);

alter table customer_emails
    add constraint customer_emails_ticket_fk
    foreign key (ticket_id) references tickets (id) on delete set null;

create type message_author as enum ('customer', 'agent', 'system');

create table ticket_messages (
    id           uuid primary key default gen_random_uuid(),
    ticket_id    uuid not null references tickets (id) on delete cascade,
    author_kind  message_author not null,
    -- Set for 'agent' messages written by a human; null for the customer's own
    -- words and for anything the runtime wrote.
    author_id    uuid references users (id) on delete set null,
    -- Internal notes are visible to staff and to the agent, never to the
    -- customer. The distinction matters: an injected instruction hiding in a
    -- customer message must not be able to promote itself to an internal note.
    is_internal  boolean not null default false,
    body         text not null,
    created_at   timestamptz not null default now()
);

create index ticket_messages_ticket_idx on ticket_messages (ticket_id, created_at);

-- The knowledge base the agent searches. Postgres full-text search, not
-- embeddings: this project is not about retrieval, and a lexical index keeps
-- the tool honest and the dependency list short.
create table kb_articles (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references orgs (id) on delete cascade,
    slug        text not null,
    title       text not null,
    body        text not null,
    created_at  timestamptz not null default now(),
    search      tsvector generated always as (
                    setweight(to_tsvector('english', coalesce(title, '')), 'A')
                    || setweight(to_tsvector('english', coalesce(body, '')), 'B')
                ) stored
);

create unique index kb_articles_org_slug_key on kb_articles (org_id, slug);
create index kb_articles_search_idx on kb_articles using gin (search);
