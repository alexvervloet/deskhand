-- The one schema change the port needed.
--
-- An approval row now carries the id of the waitpoint token that will wake the
-- run when somebody answers it. In the Python runtime there was nothing to
-- carry: the run was parked in `awaiting_approval` and a worker picked it back
-- up by polling, so the decision only had to be written down, not delivered
-- anywhere. Here the decision has an address, and the row is where the approval
-- UI finds it.
--
-- Nullable, because the column describes how a run is woken and not whether it
-- was approved. Every approval written by `deskhand.worker` predates waitpoints
-- and is still a valid record of a human saying yes; backfilling those with a
-- token id would invent a resume path that never existed.
alter table approvals add column if not exists waitpoint_token_id text;

comment on column approvals.waitpoint_token_id is
    'Trigger.dev waitpoint token that resumes the run when this approval is '
    'answered. Null for approvals recorded by the Python worker, which resumed '
    'by polling instead.';

-- Answering an approval means looking up its token, so the lookup gets an
-- index. Partial, because a decided approval is never resumed and the pending
-- set is a rounding error next to the history.
create index if not exists approvals_waitpoint_idx
    on approvals (waitpoint_token_id) where status = 'pending';
