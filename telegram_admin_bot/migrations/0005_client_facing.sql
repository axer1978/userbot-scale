-- Multi-tenant platform, phase 4: client-facing.
--
-- Client logins (a master account per client, linked to one or more
-- tenants), the unanswered queue, review batches for the trainer, and the
-- weekly digest's send log.

-- ---------------------------------------------------------------- owners
-- One login per client (a person or a company). A client with several
-- userbots has one owner linked to several tenants and switches between
-- them on the dashboard. Every owner query is scoped to the tenants in
-- owner_tenants; an owner never reaches the admin API.
CREATE TABLE owners (
  id                   SERIAL      PRIMARY KEY,
  username             TEXT        NOT NULL,
  display_name         TEXT        NOT NULL DEFAULT '',
  -- scrypt$<n>$<r>$<p>$<salt b64>$<hash b64> (owner_auth.py)
  password_hash        TEXT        NOT NULL,
  -- Base32 TOTP secret, AES-GCM encrypted under USERBOT_MASTER_KEY; NULL = off.
  totp_secret_enc      BYTEA,
  must_change_password BOOLEAN     NOT NULL DEFAULT true,
  disabled             BOOLEAN     NOT NULL DEFAULT false,
  last_login_at        TIMESTAMPTZ,
  created_by           TEXT        NOT NULL DEFAULT 'admin',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_owners_username ON owners (lower(username));

CREATE TABLE owner_tenants (
  owner_id   INTEGER     NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
  tenant_id  INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, tenant_id)
);
CREATE INDEX idx_owner_tenants_tenant ON owner_tenants (tenant_id);

-- Server-side sessions: only the SHA-256 of the cookie token is stored, so
-- a database leak does not hand out live logins.
CREATE TABLE owner_sessions (
  token_hash  TEXT        PRIMARY KEY,
  owner_id    INTEGER     NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  ip          TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX idx_owner_sessions_owner ON owner_sessions (owner_id);

-- ------------------------------------------------------ unanswered queue
-- A customer message that got no reply sent or drafted, or whose reply was
-- a fallback; decided in code (session_runtime.py), never by the model.
CREATE TABLE unanswered_queue (
  id          BIGSERIAL   PRIMARY KEY,
  tenant_id   INTEGER     NOT NULL REFERENCES tenants(id),
  session_id  TEXT        NOT NULL REFERENCES telegram_sessions ON DELETE CASCADE,
  chat_id     BIGINT      NOT NULL,
  message_id  BIGINT      REFERENCES messages(id) ON DELETE SET NULL,
  -- skipped | ai_error | soft_off | paused | escalated | policy_hold |
  -- fallback | staging
  reason      TEXT        NOT NULL,
  detail      TEXT        NOT NULL DEFAULT '',
  status      TEXT        NOT NULL DEFAULT 'open'
              CHECK (status IN ('open', 'reviewed', 'added_to_template')),
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One entry per customer message.
CREATE UNIQUE INDEX idx_unanswered_message ON unanswered_queue (tenant_id, message_id) WHERE message_id IS NOT NULL;
CREATE INDEX idx_unanswered_open ON unanswered_queue (tenant_id, status, id DESC);
CREATE TRIGGER trg_unanswered_tenant BEFORE INSERT OR UPDATE OF session_id, tenant_id ON unanswered_queue
  FOR EACH ROW EXECUTE FUNCTION tenant_for_session();

-- ------------------------------------------------------- review batches
CREATE TABLE review_batches (
  id          SERIAL      PRIMARY KEY,
  tenant_id   INTEGER     NOT NULL REFERENCES tenants(id),
  name        TEXT        NOT NULL,
  date_from   DATE        NOT NULL,
  date_to     DATE        NOT NULL,
  status      TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
  created_by  TEXT        NOT NULL DEFAULT 'admin',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (date_to >= date_from)
);

CREATE TABLE review_items (
  id          BIGSERIAL   PRIMARY KEY,
  batch_id    INTEGER     NOT NULL REFERENCES review_batches(id) ON DELETE CASCADE,
  tenant_id   INTEGER     NOT NULL REFERENCES tenants(id),
  chat_id     BIGINT      NOT NULL,
  message_id  BIGINT      REFERENCES messages(id) ON DELETE SET NULL,
  -- The conversation up to the reply: [{"role": "user"|"assistant", "content"}]
  context     JSONB       NOT NULL DEFAULT '[]'::jsonb,
  reply       TEXT        NOT NULL,
  decision    TEXT        CHECK (decision IN ('approve', 'reject', 'edit')),
  edited_text TEXT,
  decided_by  TEXT,
  decided_at  TIMESTAMPTZ,
  UNIQUE (batch_id, message_id)
);
CREATE INDEX idx_review_items_batch ON review_items (batch_id, id);

-- --------------------------------------------------------------- digest
-- One weekly digest per tenant and week, whatever retries or restarts.
CREATE TABLE digest_log (
  tenant_id   INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  week_start  DATE        NOT NULL,
  sent_via    TEXT        NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, week_start)
);
