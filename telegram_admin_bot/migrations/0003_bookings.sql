-- Multi-tenant platform, phase 2: bookings in Postgres, availability,
-- waitlist, reminders, replies deferred by quiet hours, public tokens.
--
-- The 0001 `bookings` table was never written to (bookings lived in
-- DATA_DIR/.../bookings.json); it is replaced. Each runtime imports its
-- bookings.json once on start (booking_store.import_legacy_file).

-- For the overlap rule on bookings (an integer column in a GiST exclusion
-- constraint). Pinned to public so it never lives inside a schema that
-- might be dropped (the test suite migrates throwaway schemas).
CREATE EXTENSION IF NOT EXISTS btree_gist SCHEMA public;

DROP TABLE bookings;

-- ------------------------------------------------------------- bookings
-- State machine (booking_states.py):
--   requested  the customer asked; checked against availability, not yet
--              put to the owner (e.g. no owner chat resolvable yet)
--   pending    put to the owner, waiting for their YES / NO / new time
--   confirmed  the owner said yes (or the customer took a time the owner
--              proposed)
--   cancelled  declined by the owner, cancelled by either side, superseded,
--              or never answered before it started
--   no_show / completed   marked by a person after the slot
CREATE TABLE bookings (
  id                  BIGSERIAL   PRIMARY KEY,
  tenant_id           INTEGER     NOT NULL REFERENCES tenants(id),
  session_id          TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  -- What people see: "#7". Counted per tenant, starting at 1.
  number              INTEGER     NOT NULL,
  chat_id             BIGINT      NOT NULL,
  customer_ref        TEXT        NOT NULL,
  customer_name       TEXT        NOT NULL DEFAULT '',
  customer_username   TEXT,
  service             TEXT        NOT NULL DEFAULT '',
  notes               TEXT        NOT NULL DEFAULT '',
  starts_at           TIMESTAMPTZ NOT NULL,
  ends_at             TIMESTAMPTZ NOT NULL,
  -- ends_at plus the tenant's buffer at the time of booking; the slot is
  -- held up to here so the next booking can't start inside the buffer.
  blocked_until       TIMESTAMPTZ NOT NULL,
  tz                  TEXT        NOT NULL,
  state               TEXT        NOT NULL
                      CHECK (state IN ('requested', 'pending', 'confirmed', 'cancelled', 'no_show', 'completed')),
  -- A time the owner (or the customer, for a reschedule) put forward and
  -- the other side has not accepted yet.
  proposed_starts_at  TIMESTAMPTZ,
  proposed_ends_at    TIMESTAMPTZ,
  proposed_by         TEXT CHECK (proposed_by IN ('owner', 'customer')),
  provider_chat_id    BIGINT,
  provider_message_id BIGINT,
  calendar_event_id   TEXT,
  -- Secret for the customer's read-only page (/b/<token>); 244 random bits.
  customer_token      TEXT        NOT NULL UNIQUE
                      DEFAULT (replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '')),
  -- Something the customer has not been told yet; the next reply carries
  -- it (booking_store.NOTICES). NULL once told.
  customer_notice     TEXT,
  cancelled_by        TEXT CHECK (cancelled_by IN ('owner', 'customer', 'admin', 'system')),
  cancel_reason       TEXT,
  decided_by          TEXT,
  decided_at          TIMESTAMPTZ,
  attendance_confirmed_at TIMESTAMPTZ,
  arrived_at          TIMESTAMPTZ,
  arrival_photo_match BOOLEAN,
  instructions_sent_at TIMESTAMPTZ,
  -- Imported from bookings.json: kept as history, exempt from the overlap
  -- rule (old data may overlap).
  legacy              BOOLEAN     NOT NULL DEFAULT FALSE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, number),
  CHECK (ends_at > starts_at),
  CHECK (blocked_until >= ends_at),
  -- Two live bookings of one tenant can never overlap, whoever writes them
  -- and however close together.
  EXCLUDE USING gist (tenant_id WITH =, tstzrange(starts_at, blocked_until) WITH &&)
    WHERE (state IN ('requested', 'pending', 'confirmed') AND NOT legacy)
);
CREATE TRIGGER trg_bookings_tenant BEFORE INSERT OR UPDATE OF session_id, tenant_id ON bookings
  FOR EACH ROW EXECUTE FUNCTION tenant_for_session();
CREATE INDEX idx_bookings_tenant_state ON bookings (tenant_id, state, starts_at);
CREATE INDEX idx_bookings_tenant_chat  ON bookings (tenant_id, chat_id, starts_at);

-- Per-tenant counter behind bookings.number.
CREATE TABLE booking_counters (
  tenant_id   INTEGER PRIMARY KEY REFERENCES tenants(id),
  last_number INTEGER NOT NULL DEFAULT 0
);

-- Every state change, with who and why (the audit log gets one too).
CREATE TABLE booking_events (
  id          BIGSERIAL   PRIMARY KEY,
  tenant_id   INTEGER     NOT NULL REFERENCES tenants(id),
  booking_id  BIGINT      NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  from_state  TEXT,
  to_state    TEXT        NOT NULL,
  action      TEXT        NOT NULL,
  actor       TEXT        NOT NULL,
  reason      TEXT        NOT NULL DEFAULT '',
  payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_booking_events_booking ON booking_events (tenant_id, booking_id, id);

-- One row per reminder that went out (or was claimed). The unique key is
-- what makes the reminder job idempotent: a second run, a restart or a
-- second scheduler cannot send the same reminder twice.
CREATE TABLE booking_reminders (
  tenant_id      INTEGER     NOT NULL REFERENCES tenants(id),
  booking_id     BIGINT      NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  minutes_before INTEGER     NOT NULL,
  starts_at      TIMESTAMPTZ NOT NULL,
  claimed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at        TIMESTAMPTZ,
  PRIMARY KEY (booking_id, minutes_before, starts_at)
);
CREATE INDEX idx_booking_reminders_tenant ON booking_reminders (tenant_id, booking_id);

-- --------------------------------------------------------- availability
-- Weekly opening hours in the tenant's timezone. weekday: 0 = Monday.
-- Several rows per weekday are allowed (a lunch break = two rows). No rows
-- at all means no hours are enforced; only overlaps are refused.
CREATE TABLE availability_rules (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      INTEGER  NOT NULL REFERENCES tenants(id),
  weekday        SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  start_time     TIME     NOT NULL,
  end_time       TIME     NOT NULL,
  slot_minutes   INTEGER  NOT NULL DEFAULT 60 CHECK (slot_minutes BETWEEN 5 AND 1440),
  buffer_minutes INTEGER  NOT NULL DEFAULT 0  CHECK (buffer_minutes BETWEEN 0 AND 1440),
  CHECK (end_time > start_time)
);
CREATE INDEX idx_availability_tenant ON availability_rules (tenant_id, weekday);

-- -------------------------------------------------------------- waitlist
CREATE TABLE waitlist (
  id            BIGSERIAL   PRIMARY KEY,
  tenant_id     INTEGER     NOT NULL REFERENCES tenants(id),
  session_id    TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  customer_ref  TEXT        NOT NULL,
  chat_id       BIGINT      NOT NULL,
  customer_name TEXT        NOT NULL DEFAULT '',
  wanted_from   TIMESTAMPTZ NOT NULL,
  wanted_to     TIMESTAMPTZ NOT NULL,
  service       TEXT        NOT NULL DEFAULT '',
  state         TEXT        NOT NULL DEFAULT 'waiting'
                CHECK (state IN ('waiting', 'offered', 'booked', 'expired', 'removed')),
  offered_starts_at TIMESTAMPTZ,
  offered_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (wanted_to > wanted_from)
);
CREATE TRIGGER trg_waitlist_tenant BEFORE INSERT OR UPDATE OF session_id, tenant_id ON waitlist
  FOR EACH ROW EXECUTE FUNCTION tenant_for_session();
CREATE INDEX idx_waitlist_tenant ON waitlist (tenant_id, state, created_at);

-- ------------------------------------------------------ deferred replies
-- A reply that would land in quiet hours is written down here instead of
-- only sleeping in memory, so a restart overnight doesn't lose it.
CREATE TABLE deferred_replies (
  tenant_id  INTEGER     NOT NULL REFERENCES tenants(id),
  session_id TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id    BIGINT      NOT NULL,
  due_at     TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, chat_id)
);
CREATE TRIGGER trg_deferred_replies_tenant BEFORE INSERT OR UPDATE OF session_id, tenant_id ON deferred_replies
  FOR EACH ROW EXECUTE FUNCTION tenant_for_session();
CREATE INDEX idx_deferred_replies_due ON deferred_replies (due_at);

-- --------------------------------------------------------------- tenants
-- Secret for the tenant's calendar feed (/cal/<token>.ics).
ALTER TABLE tenants ADD COLUMN calendar_token TEXT NOT NULL UNIQUE
  DEFAULT (replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', ''));

-- ------------------------------------------------ config keys that moved
-- auto_confirm is gone: only a person's YES confirms a booking.
-- booking.reminder_minutes_before became booking.reminders (a list).
UPDATE tenants SET config_json = config_json - 'auto_confirm' WHERE config_json ? 'auto_confirm';
UPDATE industries SET default_config = default_config - 'auto_confirm' WHERE default_config ? 'auto_confirm';

UPDATE tenants
   SET config_json = jsonb_set(
         config_json #- '{booking,reminder_minutes_before}', '{booking,reminders}',
         CASE WHEN (config_json #>> '{booking,reminder_minutes_before}')::int > 0
              THEN jsonb_build_array(jsonb_build_object(
                     'minutes_before', (config_json #>> '{booking,reminder_minutes_before}')::int,
                     'instruction', ''))
              ELSE '[]'::jsonb END)
 WHERE config_json #> '{booking,reminder_minutes_before}' IS NOT NULL;
UPDATE industries
   SET default_config = jsonb_set(
         default_config #- '{booking,reminder_minutes_before}', '{booking,reminders}',
         CASE WHEN (default_config #>> '{booking,reminder_minutes_before}')::int > 0
              THEN jsonb_build_array(jsonb_build_object(
                     'minutes_before', (default_config #>> '{booking,reminder_minutes_before}')::int,
                     'instruction', ''))
              ELSE '[]'::jsonb END)
 WHERE default_config #> '{booking,reminder_minutes_before}' IS NOT NULL;

-- Which purpose a metered call was for is already stored; this makes the
-- per-tenant cap sums cheap.
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_purpose ON llm_usage (tenant_id, created_at, purpose);
