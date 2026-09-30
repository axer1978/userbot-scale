/**
 * A throwaway schema on the test Postgres with migration 0006's DDL (plus
 * the tenant trigger production has). Tests skip when it is unreachable.
 */
import { randomBytes } from 'node:crypto';
import pg from 'pg';

export const DSN = process.env.PG_TEST_DSN ?? 'postgresql://postgres@127.0.0.1:55432/userbot_test';

export const DDL = `
CREATE TABLE telegram_sessions (
  session_id TEXT PRIMARY KEY, tenant_id INTEGER NOT NULL DEFAULT 1,
  is_active BOOLEAN NOT NULL DEFAULT FALSE, channel TEXT NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram','whatsapp')),
  lease_worker_id TEXT, lease_expires_at TIMESTAMPTZ, lease_epoch BIGINT NOT NULL DEFAULT 0);
CREATE TABLE wa_auth_state (
  tenant_id INTEGER NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL, key_id TEXT NOT NULL,
  value_enc BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (session_id, kind, key_id));
CREATE TABLE wa_inbox (
  id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, session_id TEXT NOT NULL, wa_message_id TEXT NOT NULL,
  payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (session_id, wa_message_id));
CREATE FUNCTION fill_tenant() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS NULL THEN
    SELECT tenant_id INTO NEW.tenant_id FROM telegram_sessions WHERE session_id = NEW.session_id;
    IF NEW.tenant_id IS NULL THEN RAISE EXCEPTION 'no session %', NEW.session_id; END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_wa_auth_tenant BEFORE INSERT ON wa_auth_state FOR EACH ROW EXECUTE FUNCTION fill_tenant();
CREATE TRIGGER trg_wa_inbox_tenant BEFORE INSERT ON wa_inbox FOR EACH ROW EXECUTE FUNCTION fill_tenant();
INSERT INTO telegram_sessions (session_id, is_active, channel) VALUES ('wa1', TRUE, 'whatsapp'), ('wa2', TRUE, 'whatsapp'), ('tg1', TRUE, 'telegram');
`;

export async function pgReachable(): Promise<boolean> {
  try {
    const probe = new pg.Client({ connectionString: DSN, connectionTimeoutMillis: 3_000 });
    await probe.connect();
    await probe.end();
    return true;
  } catch {
    return false;
  }
}

export async function withSchema(fn: (pool: pg.Pool) => Promise<void>): Promise<void> {
  const schema = `wa_t_${randomBytes(6).toString('hex')}`;
  const admin = new pg.Client({ connectionString: DSN, connectionTimeoutMillis: 3_000 });
  await admin.connect();
  await admin.query(`CREATE SCHEMA "${schema}"`);
  const pool = new pg.Pool({ connectionString: DSN, max: 3, options: `-c search_path=${schema}` });
  try {
    await pool.query(DDL);
    await fn(pool);
  } finally {
    await pool.end();
    await admin.query(`DROP SCHEMA "${schema}" CASCADE`);
    await admin.end();
  }
}
