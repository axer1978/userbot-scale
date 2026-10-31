-- The booker can be a group (booking.provider): a WhatsApp or Telegram
-- group the account is in. Requests go to the group, and what anyone in it
-- writes is read as the booker's answer.
--
-- A Telegram group needs nothing here: it is a chat with its own (negative)
-- chat_id like any other. A WhatsApp group gets its chat_id from wa_peers,
-- like a person, keyed by the group's JID (120363…@g.us) instead of a phone
-- JID or LID. push_name holds the group's name. Only groups set as the
-- booker get a row: wa-gateway lets messages through from these groups and
-- no others.

ALTER TABLE wa_peers ADD COLUMN group_jid TEXT;  -- 120363012345678901@g.us
ALTER TABLE wa_peers DROP CONSTRAINT wa_peers_check;
ALTER TABLE wa_peers ADD CONSTRAINT wa_peers_check
  CHECK (phone_jid IS NOT NULL OR lid IS NOT NULL OR group_jid IS NOT NULL);
CREATE UNIQUE INDEX idx_wa_peers_group ON wa_peers (session_id, group_jid) WHERE group_jid IS NOT NULL;
