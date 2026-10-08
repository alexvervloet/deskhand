-- What the record said about each argument, at the moment a person was asked.
--
-- An approval binds to `args_hash`, so what a person agrees to is exactly what
-- runs. That says nothing about whether the arguments were true. A ticket can
-- carry a false fact as easily as a forged instruction, "the bags were $22, so
-- refund me the difference", and a model that believes it proposes a refund
-- that is perfectly well formed, inside every ceiling, and wrong.
--
-- Each entry is {arg, status, note}: `supported`, `unsupported` or `unchecked`,
-- and one sentence saying why. Written once, when the request is recorded, so
-- the run viewer can show later what the approver was shown then. The handler
-- still re-checks everything that must hold at the moment money moves.
--
-- Defaults to empty so approvals recorded before this migration, and the ones
-- the Trigger.dev port writes, stay valid rows that simply carry no checks.
alter table approvals add column if not exists basis jsonb not null default '[]'::jsonb;

comment on column approvals.basis is
    'Per-argument checks against the system of record, computed when the '
    'approval was requested. See deskhand/tools/base.py, Support.';
