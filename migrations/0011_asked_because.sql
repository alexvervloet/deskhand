-- Why a person is being asked, when it isn't because the tool is irreversible.
--
-- Until now there was one reason an approval existed: the registry said the
-- tool was irreversible. A per-call rule can now send a reversible call to a
-- person too (deskhand/runtime/policy.py), and the card has to say why,
-- or an approver looking at "set_ticket_status" wonders what's wrong with the
-- gate.
--
-- Null means the registry's own reason, which is every approval recorded
-- before this migration and every irreversible call since.
alter table approvals add column if not exists asked_because text;

comment on column approvals.asked_because is
    'The rule that sent this call to a person, in one sentence. Null when the '
    'tool is irreversible and the registry asked. See deskhand/runtime/policy.py.';
