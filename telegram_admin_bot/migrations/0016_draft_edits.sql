-- The bot's own draft, kept when someone changed it before approving it
-- (session_runtime.py approve_draft). Until now the approved text replaced
-- it, so every correction an operator made was lost. NULL: the message went
-- out as the bot wrote it, or was never a draft.
--
-- Counted, with the approvals and rejections audit_log now records
-- (draft_approved, draft_rejected), on the Finetune page's Quality tab
-- (stats.quality).

ALTER TABLE messages ADD COLUMN draft_text TEXT;
