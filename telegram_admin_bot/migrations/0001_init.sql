-- Fleet schema: every table below except telegram_sessions itself is scoped
-- by session_id (FK to telegram_sessions, ON DELETE CASCADE). See
-- database.py / leasing.py / crypto.py / config_store.py for the code that
-- reads and writes these tables.

CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ fleet
CREATE TABLE telegram_sessions (
  session_id       TEXT PRIMARY KEY CHECK (session_id ~ '^[a-z0-9][a-z0-9_.-]{0,39}$'),
  label            TEXT        NOT NULL DEFAULT '',
  api_id           INTEGER,
  api_hash_enc     BYTEA,                    -- crypto.py blob
  dc_id            SMALLINT,
  server_address   TEXT,
  port             INTEGER,
  auth_key_enc     BYTEA,                    -- crypto.py blob, AAD = "<sid>:auth_key"
  user_id          BIGINT,
  username         TEXT,
  phone_number     TEXT,
  proxy_url_enc    BYTEA,                    -- crypto.py blob, AAD = "<sid>:proxy_url"
  deepseek_key_enc BYTEA,                    -- optional per-session override
  takeout_id       BIGINT,
  is_active        BOOLEAN     NOT NULL DEFAULT FALSE,
  state            TEXT        NOT NULL DEFAULT 'new',   -- new|ready|running|quarantined|revoked
  state_reason     TEXT        NOT NULL DEFAULT '',
  lease_worker_id  TEXT,
  lease_expires_at TIMESTAMPTZ,
  lease_epoch      BIGINT      NOT NULL DEFAULT 0,
  last_seen_at     TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sessions_claimable
  ON telegram_sessions (lease_expires_at) WHERE is_active;

CREATE TABLE telegram_peers (
  session_id   TEXT   NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  peer_id      BIGINT NOT NULL,
  access_hash  BIGINT,
  peer_type    TEXT   NOT NULL,              -- user|chat|channel
  username     TEXT,
  phone_number TEXT,
  name         TEXT   NOT NULL DEFAULT '',
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, peer_id)
);
CREATE INDEX idx_peers_username ON telegram_peers (session_id, lower(username))
  WHERE username IS NOT NULL;
CREATE INDEX idx_peers_phone    ON telegram_peers (session_id, phone_number)
  WHERE phone_number IS NOT NULL;

-- Telethon needs this for catch_up=True; not in the original spec's table
-- list, added deliberately so a worker restart doesn't miss updates.
CREATE TABLE session_update_state (
  session_id TEXT   NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  entity_id  BIGINT NOT NULL,                -- 0 = global state
  pts        BIGINT NOT NULL DEFAULT 0,
  qts        BIGINT NOT NULL DEFAULT 0,
  seq        BIGINT NOT NULL DEFAULT 0,
  date       TIMESTAMPTZ,
  PRIMARY KEY (session_id, entity_id)
);

-- ------------------------------------------------------- per-session data
CREATE TABLE conversations (
  session_id          TEXT    NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id             BIGINT  NOT NULL,
  display_name        TEXT    NOT NULL DEFAULT '',
  username            TEXT,
  is_bot              BOOLEAN NOT NULL DEFAULT FALSE,
  access_hash         BIGINT,
  automation_paused   BOOLEAN NOT NULL DEFAULT FALSE,
  unread              INTEGER NOT NULL DEFAULT 0,
  last_message_at     TIMESTAMPTZ,
  last_message_preview TEXT   NOT NULL DEFAULT '',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, chat_id)
);
CREATE INDEX idx_conversations_recent
  ON conversations (session_id, COALESCE(last_message_at, created_at) DESC);

CREATE TABLE messages (
  id          BIGSERIAL PRIMARY KEY,
  session_id  TEXT   NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id     BIGINT NOT NULL,
  telegram_id BIGINT,
  direction   TEXT   NOT NULL,               -- in|out|system
  status      TEXT   NOT NULL,               -- received|sent|pending_approval|rejected|error|note
  text        TEXT   NOT NULL DEFAULT '',
  attachments INTEGER[] NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_chat ON messages (session_id, chat_id, id);
CREATE UNIQUE INDEX idx_messages_tg
  ON messages (session_id, chat_id, telegram_id) WHERE telegram_id IS NOT NULL;
CREATE INDEX idx_messages_sent_window
  ON messages (session_id, created_at) WHERE status = 'sent' AND direction = 'out';
CREATE INDEX idx_messages_pending
  ON messages (session_id, chat_id) WHERE status = 'pending_approval';

CREATE TABLE outreach (
  id           BIGSERIAL PRIMARY KEY,
  session_id   TEXT   NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id      BIGINT NOT NULL,
  display_name TEXT   NOT NULL DEFAULT '',
  goal         TEXT   NOT NULL,
  status       TEXT   NOT NULL,              -- queued|drafted|sent|failed|cancelled
  message      TEXT,
  error        TEXT,
  draft_id     BIGINT REFERENCES messages(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at      TIMESTAMPTZ
);
CREATE INDEX idx_outreach_status ON outreach (session_id, status, id);
CREATE INDEX idx_outreach_draft  ON outreach (session_id, draft_id) WHERE draft_id IS NOT NULL;

CREATE TABLE chat_links (                    -- still one row per direction
  session_id TEXT   NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id    BIGINT NOT NULL,
  source_id  BIGINT NOT NULL,
  origin     TEXT   NOT NULL,                -- auto|manual|blocked
  reason     TEXT   NOT NULL DEFAULT '',
  confidence REAL   NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, chat_id, source_id),
  CHECK (chat_id <> source_id)
);

CREATE TABLE chat_summaries (
  session_id      TEXT   NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id         BIGINT NOT NULL,
  summary         TEXT   NOT NULL,
  last_message_id BIGINT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, chat_id)
);

CREATE TABLE bookings (
  session_id            TEXT    NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  id                    INTEGER NOT NULL,           -- app-assigned, per session, never reused
  chat_id               BIGINT  NOT NULL,
  client_name           TEXT    NOT NULL DEFAULT '',
  client_username       TEXT,
  start_text            TEXT    NOT NULL,           -- verbatim ISO+offset (Booking.start)
  end_text              TEXT    NOT NULL,           -- verbatim ISO+offset (Booking.end)
  start_at              TIMESTAMPTZ NOT NULL,       -- queryable mirror
  end_at                TIMESTAMPTZ NOT NULL,
  timezone              TEXT    NOT NULL,
  title                 TEXT    NOT NULL DEFAULT '',
  notes                 TEXT    NOT NULL DEFAULT '',
  status                TEXT    NOT NULL,           -- pending|confirmed|declined|superseded
  provider_message_id   BIGINT,
  provider_chat_id      BIGINT,
  calendar_event_id     TEXT,
  replaces_id           INTEGER,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at            TIMESTAMPTZ,
  decided_by            TEXT,
  client_notified       BOOLEAN NOT NULL DEFAULT FALSE,
  reminder_requested_at TIMESTAMPTZ,
  reminder_sent         BOOLEAN NOT NULL DEFAULT FALSE,
  arrived_at            TIMESTAMPTZ,
  instructions_sent_at  TIMESTAMPTZ,
  PRIMARY KEY (session_id, id)
);
CREATE INDEX idx_bookings_active ON bookings (session_id, status, start_at);
CREATE INDEX idx_bookings_chat   ON bookings (session_id, chat_id, status);

CREATE TABLE session_media (
  session_id  TEXT    NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  id          INTEGER NOT NULL,              -- app-assigned, never reused (history references it)
  file        TEXT    NOT NULL,
  kind        TEXT    NOT NULL,              -- photo|video
  description TEXT    NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, id),
  UNIQUE (session_id, file)
);

-- persisted "never reuse an id" counters for the two app-assigned-id tables
CREATE TABLE session_counters (
  session_id TEXT NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  name       TEXT NOT NULL,                  -- 'booking' | 'media'
  next_id    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (session_id, name)
);

CREATE TABLE session_config (
  session_id TEXT PRIMARY KEY REFERENCES telegram_sessions ON DELETE CASCADE,
  config     JSONB       NOT NULL,
  revision   INTEGER     NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE session_halts (                 -- replaces DATA_DIR/last_halt.txt
  id         BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  reason     TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_halts_session ON session_halts (session_id, id DESC);

-- ------------------------------------------------------------ operations
CREATE TABLE worker_heartbeats (
  worker_id      TEXT PRIMARY KEY,
  slot           INTEGER NOT NULL,
  pid            INTEGER NOT NULL,
  host           TEXT NOT NULL DEFAULT '',
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  sessions_owned INTEGER NOT NULL DEFAULT 0,
  status         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE panel_sessions (
  token_hash BYTEA PRIMARY KEY,              -- sha256 of the raw token; raw never stored
  username   TEXT NOT NULL DEFAULT 'admin',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_panel_sessions_expiry ON panel_sessions (expires_at);
