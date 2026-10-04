/**
 * The Postgres key store against a real Postgres (PG_TEST_DSN or the
 * shared test server), in a throwaway schema with migration 0006's DDL
 * (plus the tenant trigger production has). Skips when unreachable.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import pg from 'pg';
import { proto } from 'baileys';
import { authAad, PostgresAuthStore, serialize, usePostgresAuthState } from '../src/authstate.ts';
import { keyringFromContent } from '../src/crypto.ts';
import { hasCreds, readSessionRow, wipeAuthState } from '../src/db.ts';
import pino from 'pino';

const DSN = process.env.PG_TEST_DSN ?? 'postgresql://postgres@127.0.0.1:55432/userbot_test';
const KEYRING = keyringFromContent('MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=', 'env');
const quiet = pino({ level: 'silent' });

const DDL = `
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

async function withSchema(fn: (pool: pg.Pool) => Promise<void>): Promise<void> {
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

let reachable = true;
try {
  const probe = new pg.Client({ connectionString: DSN, connectionTimeoutMillis: 3_000 });
  await probe.connect();
  await probe.end();
} catch {
  reachable = false;
}

test('creds: fresh when absent, persisted by saveCreds, reloaded with Buffers intact', { skip: !reachable && 'postgres unreachable' }, async () => {
  await withSchema(async (pool) => {
    assert.equal(await hasCreds(pool, 'wa1'), false);
    const first = await usePostgresAuthState(pool, 'wa1', quiet, KEYRING);
    assert.equal(first.existed, false);
    assert.equal(first.state.creds.registered, false);
    first.state.creds.registered = true;
    first.state.creds.me = { id: '34600000000@s.whatsapp.net', lid: '1234@lid', name: 'Test' };
    await first.saveCreds();
    assert.equal(await hasCreds(pool, 'wa1'), true);

    const second = await usePostgresAuthState(pool, 'wa1', quiet, KEYRING);
    assert.equal(second.existed, true);
    assert.equal(second.state.creds.registered, true);
    assert.deepEqual(second.state.creds.me, first.state.creds.me);
    assert.ok(Buffer.isBuffer(second.state.creds.noiseKey.private));
    assert.deepEqual(Buffer.from(second.state.creds.noiseKey.private), Buffer.from(first.state.creds.noiseKey.private));
    assert.equal(second.state.creds.registrationId, first.state.creds.registrationId);

    // The trigger filled tenant_id; the gateway never wrote it.
    const row = await pool.query('SELECT tenant_id, kind, key_id FROM wa_auth_state');
    assert.deepEqual(row.rows, [{ tenant_id: 1, kind: 'creds', key_id: '' }]);
  });
});

test('signal keys: set/get, null deletes, clear keeps creds, app-state-sync-key revives to proto', { skip: !reachable && 'postgres unreachable' }, async () => {
  await withSchema(async (pool) => {
    const store = new PostgresAuthStore(pool, 'wa1', KEYRING);
    const pub = Buffer.from([1, 2, 3]);
    const priv = Buffer.from([4, 5, 6]);
    await store.setKeys({
      'pre-key': { '1': { public: pub, private: priv }, '2': { public: pub, private: priv } },
      session: { 'a@s.whatsapp.net.0': Buffer.from('sess') },
      'app-state-sync-key': { k1: { keyData: Buffer.from('kd'), fingerprint: { rawId: 7, currentIndex: 1, deviceIndexes: [0] }, timestamp: 12 } },
      'lid-mapping': { '34600000000': '1234@lid' },
    });
    const pre = await store.getKeys('pre-key', ['1', '2', '3']);
    assert.deepEqual(Object.keys(pre).sort(), ['1', '2']);
    assert.deepEqual(Buffer.from(pre['1']!.public), pub);
    assert.deepEqual(Buffer.from(pre['2']!.private), priv);
    const sess = await store.getKeys('session', ['a@s.whatsapp.net.0']);
    assert.equal(Buffer.from(sess['a@s.whatsapp.net.0']!).toString(), 'sess');
    const ask = await store.getKeys('app-state-sync-key', ['k1']);
    assert.ok(ask.k1 instanceof proto.Message.AppStateSyncKeyData);
    assert.equal(ask.k1!.fingerprint!.rawId, 7);
    assert.deepEqual(await store.getKeys('lid-mapping', ['34600000000']), { '34600000000': '1234@lid' });
    assert.deepEqual(await store.getKeys('pre-key', []), {});

    await store.setKeys({ 'pre-key': { '1': null } });
    assert.deepEqual(Object.keys(await store.getKeys('pre-key', ['1', '2'])), ['2']);

    await store.writeCreds({ registered: true } as never);
    await store.clearKeys();
    assert.deepEqual(await store.getKeys('pre-key', ['2']), {});
    assert.equal(await hasCreds(pool, 'wa1'), true);
    assert.equal(await wipeAuthState(pool, 'wa1'), 1);
    assert.equal(await hasCreds(pool, 'wa1'), false);
  });
});

test('a multi-key set is one transaction: a bad row rolls the whole batch back', { skip: !reachable && 'postgres unreachable' }, async () => {
  await withSchema(async (pool) => {
    const store = new PostgresAuthStore(pool, 'wa1', KEYRING);
    // Test-only constraint so the second row of the batch is refused by Postgres.
    await pool.query(`ALTER TABLE wa_auth_state ADD CONSTRAINT no_bad_key CHECK (key_id <> 'bad')`);
    await assert.rejects(
      store.setKeys({ 'pre-key': { '1': { public: Buffer.from('a'), private: Buffer.from('b') } }, session: { bad: Buffer.from('z') } }),
      /no_bad_key/,
    );
    assert.deepEqual(await store.getKeys('pre-key', ['1']), {});
    await store.setKeys({ 'pre-key': { '1': { public: Buffer.from('a'), private: Buffer.from('b') } } });
    assert.deepEqual(Object.keys(await store.getKeys('pre-key', ['1'])), ['1']);
  });
});

test('rows are bound to their session and kind through the AAD', { skip: !reachable && 'postgres unreachable' }, async () => {
  await withSchema(async (pool) => {
    const store1 = new PostgresAuthStore(pool, 'wa1', KEYRING);
    await store1.setKeys({ 'pre-key': { '1': { public: Buffer.from('a'), private: Buffer.from('b') } } });
    // Copy wa1's row onto wa2 (same kind/key id): must fail to decrypt.
    await pool.query(`INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc)
                      SELECT 'wa2', kind, key_id, value_enc FROM wa_auth_state WHERE session_id = 'wa1'`);
    const store2 = new PostgresAuthStore(pool, 'wa2', KEYRING);
    await assert.rejects(store2.getKeys('pre-key', ['1']), /authentication failed/);
    // And relabelling the kind within the same session fails too.
    await pool.query(`UPDATE wa_auth_state SET kind = 'identity-key' WHERE session_id = 'wa1'`);
    await assert.rejects(store1.getKeys('identity-key', ['1']), /authentication failed/);
    assert.equal(authAad('wa1', 'pre-key', '1').toString(), 'wa1:wa_auth:pre-key:1');
    assert.equal(serialize({ b: Buffer.from([0, 1]) }).toString(), '{"b":{"type":"Buffer","data":"AAE="}}');
  });
});

test('readSessionRow reports channel, activity, epoch and lease liveness', { skip: !reachable && 'postgres unreachable' }, async () => {
  await withSchema(async (pool) => {
    assert.equal(await readSessionRow(pool, 'nope'), null);
    const idle = await readSessionRow(pool, 'wa1');
    assert.deepEqual(idle, { channel: 'whatsapp', is_active: true, lease_epoch: 0, live: false, lease_expires_at_ms: null });
    await pool.query(`UPDATE telegram_sessions SET lease_expires_at = now() + interval '30 seconds', lease_epoch = 5 WHERE session_id = 'wa1'`);
    const leased = await readSessionRow(pool, 'wa1');
    assert.equal(leased!.live, true);
    assert.equal(leased!.lease_epoch, 5);
    assert.ok(typeof leased!.lease_expires_at_ms === 'number' && leased!.lease_expires_at_ms > Date.now() - 60_000);
    await pool.query(`UPDATE telegram_sessions SET lease_expires_at = now() - interval '40 seconds' WHERE session_id = 'wa1'`);
    const expired = await readSessionRow(pool, 'wa1');
    assert.equal(expired!.live, false);
    assert.ok(expired!.lease_expires_at_ms! < Date.now());
    assert.equal((await readSessionRow(pool, 'tg1'))!.channel, 'telegram');
  });
});
