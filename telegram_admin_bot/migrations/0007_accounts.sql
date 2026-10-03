-- Accounts: client self sign-up with approval, the terms of service, and
-- manager (moderator) logins.
--
-- Client sign-up: a person creates their own login on /owner/. It starts
-- 'pending' and opens nothing until the admin or a manager approves it.
-- Logins the admin creates are 'active' from the start, as before.
--
-- Terms: every published version is kept as it was published (append-only),
-- and every acceptance is kept as evidence, with the address it came from,
-- even after the login is deleted (so owner_id has no foreign key).
--
-- Managers: a second kind of staff login, below the admin. What a manager
-- may do is fixed in code (manager_api.py), not stored here.

-- ------------------------------------------------------------ sign-up
ALTER TABLE owners
  ADD COLUMN status        TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('pending', 'active', 'rejected')),
  ADD COLUMN email         TEXT NOT NULL DEFAULT '',
  ADD COLUMN company       TEXT NOT NULL DEFAULT '',
  ADD COLUMN phone         TEXT NOT NULL DEFAULT '',
  -- Who approved or rejected a sign-up, when, and the reason they gave.
  ADD COLUMN reviewed_by   TEXT,
  ADD COLUMN reviewed_at   TIMESTAMPTZ,
  ADD COLUMN review_reason TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_owners_pending ON owners (created_at) WHERE status = 'pending';

-- Sign-up is closed until the admin opens it (and terms are published).
INSERT INTO platform_settings (key, value) VALUES ('signup', '{"enabled": false}'::jsonb)
  ON CONFLICT (key) DO NOTHING;

-- -------------------------------------------------------------- terms
CREATE TABLE terms_versions (
  version             SERIAL      PRIMARY KEY,
  title               TEXT        NOT NULL,
  body                TEXT        NOT NULL,
  -- What changed, shown to clients asked to accept again.
  change_note         TEXT        NOT NULL DEFAULT '',
  -- false = a correction (a typo): nobody has to accept it again.
  requires_acceptance BOOLEAN     NOT NULL DEFAULT true,
  published_by        TEXT        NOT NULL,
  published_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE terms_acceptances (
  id          BIGSERIAL   PRIMARY KEY,
  owner_id    INTEGER     NOT NULL,
  username    TEXT        NOT NULL,
  version     INTEGER     NOT NULL REFERENCES terms_versions(version),
  accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip          TEXT        NOT NULL DEFAULT '',
  user_agent  TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX idx_terms_acceptances_owner ON terms_acceptances (owner_id, version DESC);

CREATE FUNCTION append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP;
END;
$$;
CREATE TRIGGER trg_terms_versions_no_update BEFORE UPDATE OR DELETE ON terms_versions
  FOR EACH ROW EXECUTE FUNCTION append_only();
CREATE TRIGGER trg_terms_versions_no_truncate BEFORE TRUNCATE ON terms_versions
  FOR EACH STATEMENT EXECUTE FUNCTION append_only();
CREATE TRIGGER trg_terms_acceptances_no_update BEFORE UPDATE OR DELETE ON terms_acceptances
  FOR EACH ROW EXECUTE FUNCTION append_only();
CREATE TRIGGER trg_terms_acceptances_no_truncate BEFORE TRUNCATE ON terms_acceptances
  FOR EACH STATEMENT EXECUTE FUNCTION append_only();

-- ----------------------------------------------------------- managers
-- Same shape as owners (owner_auth.py), without links to tenants: a
-- manager sees every client. An authenticator is required before any
-- manager route opens (manager_auth.py).
CREATE TABLE managers (
  id                   SERIAL      PRIMARY KEY,
  username             TEXT        NOT NULL,
  display_name         TEXT        NOT NULL DEFAULT '',
  password_hash        TEXT        NOT NULL,
  totp_secret_enc      BYTEA,
  must_change_password BOOLEAN     NOT NULL DEFAULT true,
  disabled             BOOLEAN     NOT NULL DEFAULT false,
  last_login_at        TIMESTAMPTZ,
  created_by           TEXT        NOT NULL DEFAULT 'admin',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_managers_username ON managers (lower(username));

CREATE TABLE manager_sessions (
  token_hash  TEXT        PRIMARY KEY,
  manager_id  INTEGER     NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  ip          TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX idx_manager_sessions_manager ON manager_sessions (manager_id);
