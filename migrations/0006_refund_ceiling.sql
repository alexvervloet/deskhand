-- A ceiling on money paid out, snapshotted onto the run like every other bound.
--
-- The bounds in 0004 are thorough about what a run costs *us*: steps, tokens,
-- wall clock, model spend. None of them bound what it pays out. `issue_refund`
-- checked one thing — that a refund fits inside what remains on its order — so
-- a run touching four orders could refund four times with nothing counting the
-- total.
--
-- That asymmetry is the wrong way round for this project. The bill for a
-- runaway agent was capped at a couple of dollars of inference; the money it
-- could move was capped only by a human reading approval screens carefully,
-- and human attention is not a ceiling.
--
-- Snapshotted rather than read from config at spend time, for the same reason
-- `max_steps` is: raising the cap in a deploy must not retroactively widen a
-- run that is already in flight, and a run replayed months later has to be
-- judged against the ceiling it actually ran under.
-- The default is 0, and 0 means "this run may not pay anything out". That is
-- deliberate and it is the whole point of putting the ceiling on the row: a run
-- created by some future code path that does not know about this column gets no
-- payout authority rather than an assumed one. `runs.create` sets it from
-- config on every real run, so the default is only ever reached by a row that
-- forgot to, which is exactly the row that should not be moving money.
alter table runs
    add column max_refund_cents bigint not null default 0;

comment on column runs.max_refund_cents is
    'Total cents this run may refund across every order it touches, frozen at '
    'creation. 0 means no payout authority.';

-- Existing rows predate the column and were never bound by it. Giving them 0
-- would rewrite history as "these runs were forbidden to refund", which is not
-- what happened, and would make a replay of a real refund look like a
-- violation. They get the ceiling they would have been given.
update runs set max_refund_cents = 100000 where max_refund_cents = 0;

-- "What has this run paid out" is already indexed: 0004 added refunds.run_id
-- with refunds_run_idx over it. "What has this merchant paid out today" is not,
-- and the daily ceiling asks that on every issue_refund call.
create index refunds_org_day_idx on refunds (org_id, created_at);
