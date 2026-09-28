-- Multi-tenant platform, phase 3: safety and control.
--
-- Soft-off holds, the global stop, billing grace, the health record per
-- tenant, alerts to the operator, escalation and human takeover per chat.

-- ---------------------------------------------------------------- holds
-- Soft-off: while a tenant has at least one hold (or the global stop is
-- on), its account sends nothing on its own. Messages are still received
-- and stored. Each kind is lifted separately, so resuming a manual pause
-- does not also lift a billing suspension.
--   manual     the operator paused it ("Pause all")
--   billing    suspended for non-payment (billing.py)
--   spend_cap  an AI usage limit was reached; lifted by itself when the
--              period rolls over or the limit is raised
--   anomaly    a new Telegram login, a send-volume spike, or a trip-wire
--              match in a reply (anomaly.py)
--   telegram   Telegram pushed back (PeerFlood, a very long FloodWait)
CREATE TABLE tenant_holds (
  tenant_id  INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind       TEXT        NOT NULL
             CHECK (kind IN ('manual', 'billing', 'spend_cap', 'anomaly', 'telegram')),
  reason     TEXT        NOT NULL DEFAULT '',
  created_by TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, kind)
);

-- The global stop (every tenant soft-off) and the billing settings.
INSERT INTO platform_settings (key, value) VALUES
  ('global_stop', '{"on": false}'::jsonb),
  ('billing', '{
     "grace_hours": 48,
     "notice": "Payment for {business} was due on {due}. The assistant will pause on {until} unless the payment is recorded before then."
   }'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- The account-wide "Pause all" used to live in session_config; it is a
-- manual hold now. A halt caused by Telegram becomes a 'telegram' hold.
INSERT INTO tenant_holds (tenant_id, kind, reason, created_by)
SELECT t.id,
       CASE WHEN s.state = 'halted' THEN 'telegram' ELSE 'manual' END,
       CASE WHEN s.state = 'halted' AND s.state_reason <> '' THEN s.state_reason
            ELSE 'Paused before the upgrade' END,
       'migration'
  FROM tenants t
  JOIN session_config c ON c.session_id = t.session_id
  JOIN telegram_sessions s ON s.session_id = t.session_id
 WHERE COALESCE((c.config->'behavior'->>'global_pause')::boolean, false);

UPDATE session_config
   SET config = jsonb_set(config, '{behavior,global_pause}', 'false'::jsonb)
 WHERE COALESCE((config->'behavior'->>'global_pause')::boolean, false);

-- -------------------------------------------------------------- billing
-- status (0002): active -> grace -> suspended. grace_until is when a
-- tenant in grace is suspended; billing_notice_sent_at records that the
-- owner was told (retried until it is set).
ALTER TABLE tenants
  ADD COLUMN grace_until            TIMESTAMPTZ,
  ADD COLUMN billing_notice_sent_at TIMESTAMPTZ;

-- -------------------------------------------------------- conversations
-- human_takeover_until: someone wrote in this chat by hand; the bot stays
-- quiet here until then. paused_reason says why automation_paused is on
-- ('' = paused by hand in the panel).
ALTER TABLE conversations
  ADD COLUMN human_takeover_until TIMESTAMPTZ,
  ADD COLUMN paused_reason        TEXT NOT NULL DEFAULT '';

-- ---------------------------------------------------------------- health
-- One row per tenant, written by its running account (last_seen_at, errors,
-- Telegram's rate limit, the logins it knows) and read by the scheduler's
-- watchdog (health.py), which alerts when the account is not well.
CREATE TABLE sessions_health (
  tenant_id                 INTEGER     PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  session_id                TEXT        REFERENCES telegram_sessions ON DELETE SET NULL,
  status                    TEXT        NOT NULL DEFAULT 'unknown',
  status_reason             TEXT        NOT NULL DEFAULT '',
  status_since              TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The last time the running account confirmed it was connected.
  last_seen_at              TIMESTAMPTZ,
  last_error                TEXT        NOT NULL DEFAULT '',
  last_error_at             TIMESTAMPTZ,
  rate_limited_until        TIMESTAMPTZ,
  -- The account's Telegram logins last seen (hash, device, app, country,
  -- created). NULL until the first check; a hash not in here is new.
  known_session_ids_json    JSONB,
  authorizations_checked_at TIMESTAMPTZ
);

-- ---------------------------------------------------------------- alerts
-- For the operator. One open alert per (tenant, kind): a repeat bumps
-- count and last_at instead of piling up. Acknowledging closes it.
CREATE TABLE alerts (
  id              BIGSERIAL   PRIMARY KEY,
  tenant_id       INTEGER     REFERENCES tenants(id) ON DELETE CASCADE,  -- NULL = platform
  kind            TEXT        NOT NULL,
  severity        TEXT        NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
  message         TEXT        NOT NULL,
  payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  count           INTEGER     NOT NULL DEFAULT 1,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at TIMESTAMPTZ,
  acknowledged_by TEXT
);
CREATE UNIQUE INDEX idx_alerts_open ON alerts (COALESCE(tenant_id, 0), kind) WHERE acknowledged_at IS NULL;
CREATE INDEX idx_alerts_recent ON alerts (id DESC);
