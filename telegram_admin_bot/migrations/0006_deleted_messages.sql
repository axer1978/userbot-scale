-- Messages the bot deleted in Telegram under the client's delete instruction
-- (replies.delete_instruction). The row stays, so the panel still shows what
-- was deleted and when; the AI no longer sees it as part of the chat.
ALTER TABLE messages ADD COLUMN deleted_at TIMESTAMPTZ;
