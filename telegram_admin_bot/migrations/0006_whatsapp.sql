-- WhatsApp accounts next to the Telegram ones.
--
-- A WhatsApp account is a row in telegram_sessions like any other (the
-- table keeps its name: it is the fleet's account registry, and the lease,
-- the tenant link and every foreign key hang off it); telegram_sessions.channel
-- says which network it is on. Its conversations, messages and bookings go
-- in the existing tables, keyed by an integer chat_id like on Telegram. What
-- WhatsApp needs on top:
--   telegram_sessions.channel  'telegram' | 'whatsapp'; tenants.channel
--                              follows it (triggers below)
--   wa_peers                   WhatsApp contacts: the integer chat_id the rest
--                              of the schema uses <-> the contact's JIDs
--   messages.wa_message_id     WhatsApp's message id (dedupe)
--   bookings.provider_wa_message_id
--                              the owner-facing booking message on WhatsApp
--   wa_auth_state              the Baileys auth state (creds and signal keys),
--                              encrypted
--   wa_inbox                   inbound messages handed from the WhatsApp
--                              gateway to the account's runtime
--   tenant_holds 'whatsapp'    WhatsApp pushed back; the counterpart of
--                              'telegram'

-- -------------------------------------------------------------- channel
-- Existing accounts are all Telegram.
ALTER TABLE telegram_sessions
  ADD COLUMN channel TEXT NOT NULL DEFAULT 'telegram'
             CHECK (channel IN ('telegram', 'whatsapp'));

-- tenants.channel (0002) is a copy of its account's channel, kept by the two
-- triggers below: a tenant with an account always takes that account's
-- channel, whatever the statement wrote, and a change of an account's
-- channel reaches its tenant. A tenant whose account was deleted
-- (session_id NULL) keeps the last channel it had.
CREATE FUNCTION tenant_channel_from_session() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  account_channel TEXT;
BEGIN
  IF NEW.session_id IS NOT NULL THEN
    SELECT channel INTO account_channel FROM telegram_sessions WHERE session_id = NEW.session_id;
    IF account_channel IS NOT NULL THEN
      NEW.channel := account_channel;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER trg_tenants_channel
  BEFORE INSERT OR UPDATE OF session_id, channel ON tenants
  FOR EACH ROW EXECUTE FUNCTION tenant_channel_from_session();

CREATE FUNCTION sync_tenant_channel_from_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE tenants SET channel = NEW.channel
   WHERE session_id = NEW.session_id AND channel IS DISTINCT FROM NEW.channel;
  RETURN NULL;
END;
$$;
CREATE TRIGGER trg_telegram_sessions_channel
  AFTER INSERT OR UPDATE OF channel ON telegram_sessions
  FOR EACH ROW EXECUTE FUNCTION sync_tenant_channel_from_session();

-- Existing tenants (all 'telegram' already, but make sure).
UPDATE tenants t SET channel = s.channel
  FROM telegram_sessions s
 WHERE s.session_id = t.session_id AND t.channel IS DISTINCT FROM s.channel;

-- ---------------------------------------------------------------- peers
-- One row per WhatsApp contact of an account. chat_id is handed out here
-- (from one sequence for all accounts; an account is on one channel only, so
-- it never meets a Telegram id) and is what conversations/messages/bookings
-- use. WhatsApp addresses a person by a phone JID, a LID, or both once it
-- has learnt the mapping; jid is where sends go (the phone JID when known,
-- else the LID).
CREATE SEQUENCE wa_chat_id_seq;

CREATE TABLE wa_peers (
  tenant_id  INTEGER     NOT NULL REFERENCES tenants(id),
  session_id TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id    BIGINT      NOT NULL DEFAULT nextval('wa_chat_id_seq'),
  jid        TEXT        NOT NULL,
  phone_jid  TEXT,                          -- 34600123456@s.whatsapp.net
  lid        TEXT,                          -- 123456789@lid
  push_name  TEXT        NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, chat_id),
  CHECK (phone_jid IS NOT NULL OR lid IS NOT NULL)
);
ALTER SEQUENCE wa_chat_id_seq OWNED BY wa_peers.chat_id;
CREATE UNIQUE INDEX idx_wa_peers_phone ON wa_peers (session_id, phone_jid) WHERE phone_jid IS NOT NULL;
CREATE UNIQUE INDEX idx_wa_peers_lid   ON wa_peers (session_id, lid)       WHERE lid IS NOT NULL;

-- ------------------------------------------------------------- messages
-- WhatsApp's id of the message; the counterpart of telegram_id, unique per
-- chat so a redelivered message is stored once.
ALTER TABLE messages ADD COLUMN wa_message_id TEXT;
CREATE UNIQUE INDEX idx_messages_wa
  ON messages (session_id, chat_id, wa_message_id) WHERE wa_message_id IS NOT NULL;

-- The owner's booking message on WhatsApp (provider_message_id on Telegram).
ALTER TABLE bookings ADD COLUMN provider_wa_message_id TEXT;

-- ------------------------------------------------------------ auth state
-- Baileys' auth state, one row per key. kind is 'creds' (key_id '') or a
-- Baileys SignalDataTypeMap key (pre-key, session, sender-key,
-- sender-key-memory, app-state-sync-key, app-state-sync-version,
-- lid-mapping, device-list, tctoken, identity-key). value_enc is a
-- crypto.py blob of the UTF-8 JSON value, AAD
-- "<session_id>:wa_auth:<kind>:<key_id>".
CREATE TABLE wa_auth_state (
  tenant_id  INTEGER     NOT NULL REFERENCES tenants(id),
  session_id TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  kind       TEXT        NOT NULL,
  key_id     TEXT        NOT NULL,
  value_enc  BYTEA       NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, kind, key_id)
);

-- ----------------------------------------------------------------- inbox
-- Durable handoff of inbound messages: the WhatsApp gateway inserts one row
-- per message (a redelivery hits the UNIQUE and is dropped), the account's
-- runtime deletes it once the message is stored. Whatever is still here is
-- read back per account in arrival (id) order.
CREATE TABLE wa_inbox (
  id            BIGSERIAL   PRIMARY KEY,
  tenant_id     INTEGER     NOT NULL REFERENCES tenants(id),
  session_id    TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  wa_message_id TEXT        NOT NULL,
  payload       JSONB       NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (session_id, wa_message_id)
);
CREATE INDEX idx_wa_inbox_session ON wa_inbox (session_id, id);

-- tenant_id on the new tables, kept consistent with session_id (0002).
DO $$
DECLARE
  t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['wa_peers', 'wa_auth_state', 'wa_inbox'] LOOP
    EXECUTE format(
      'CREATE TRIGGER trg_%s_tenant BEFORE INSERT OR UPDATE OF session_id, tenant_id ON %I '
      'FOR EACH ROW EXECUTE FUNCTION tenant_for_session()', t, t);
  END LOOP;
END;
$$;

-- ----------------------------------------------------------------- holds
-- 'whatsapp': WhatsApp pushed back (the counterpart of 'telegram', 0004).
ALTER TABLE tenant_holds DROP CONSTRAINT tenant_holds_kind_check;
ALTER TABLE tenant_holds ADD CONSTRAINT tenant_holds_kind_check
  CHECK (kind IN ('manual', 'billing', 'spend_cap', 'anomaly', 'telegram', 'whatsapp'));
