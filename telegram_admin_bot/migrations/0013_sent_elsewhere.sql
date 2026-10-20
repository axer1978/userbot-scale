-- Messages written by hand outside this platform (session_runtime.handle_own_echo).
--
-- The account's own phone, or another app on the same account, sends a
-- message; the network reports it back and it is stored like any other sent
-- message, so the chat shows it and the bot reads it. The send limits and
-- the volume anomaly count only what this platform sent (bot replies and
-- panel sends), so these rows are marked.
--
-- Rows from before this migration stay false and count as they did.

ALTER TABLE messages
  ADD COLUMN sent_elsewhere BOOLEAN NOT NULL DEFAULT false;
