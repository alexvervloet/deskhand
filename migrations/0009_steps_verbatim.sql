-- The step log keeps what the model said in the order it said it.
--
-- `jsonb` is a parsed form. It sorts object keys by length and then by bytes,
-- and normalises numbers, so `{"order_reference", "amount_cents", "reason"}`
-- goes in and `{"reason", "amount_cents", "order_reference"}` comes out. Every
-- model call is preceded by rebuilding the conversation from this column, so
-- every assistant turn the model was shown again had been rewritten by the
-- storage layer.
--
-- That used to cost nothing visible. It stopped being free when models began
-- binding each thinking block to the exact conversation prefix that produced
-- it: on enforced accounts an earlier message that differs from what the model
-- saw is a 400, and a prompt cache cannot hit across it either. `json` stores
-- the text as given and hands it back unchanged, which makes "the history only
-- grows" a property of the column instead of a hope about it.
--
-- Rows written before this migration were reordered on the way in and cannot
-- be un-reordered. They stay consistent with themselves, which is all a run
-- already in flight needs: every request it has sent was built from the same
-- reordered rows.
alter table steps alter column content type json using content::text::json;

comment on column steps.content is
    'Kept as json, not jsonb, so model output round-trips with its key order '
    'intact. Rebuilding a conversation must reproduce what the model said, not '
    'a normalised equivalent of it. See migrations/0009_steps_verbatim.sql.';
