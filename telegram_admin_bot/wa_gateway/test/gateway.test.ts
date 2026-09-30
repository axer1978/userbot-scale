/**
 * Command handlers over a fake pool: every path that must refuse BEFORE a
 * socket would be created is exercised here without Baileys or Postgres.
 * (Nothing here ever connects to WhatsApp.)
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import pino from 'pino';
import { replyFor } from '../src/bus.ts';
import { Gateway } from '../src/gateway.ts';
import type { Pool } from '../src/db.ts';

type Row = { channel: string; is_active: boolean; lease_epoch: number; live: boolean; lease_expires_at_ms: number | null };

function fakePool(rows: Record<string, Row>, creds: Set<string>, opts: { fail?: boolean } = {}): Pool {
  return {
    query: async (sql: string, params: unknown[]) => {
      if (opts.fail) throw new Error('connection refused');
      if (sql.includes('FROM telegram_sessions')) {
        const row = rows[params[0] as string];
        return { rows: row ? [row] : [], rowCount: row ? 1 : 0 };
      }
      if (sql.includes('FROM wa_auth_state')) {
        const has = creds.has(params[0] as string);
        return { rows: has ? [{ '?column?': 1 }] : [], rowCount: has ? 1 : 0 };
      }
      if (sql === 'SELECT 1') return { rows: [{ '?column?': 1 }], rowCount: 1 };
      throw new Error(`unexpected query: ${sql}`);
    },
  } as unknown as Pool;
}

const quiet = pino({ level: 'silent' });
const browser = ['macOS', 'Chrome', '1.0'];
const cmd = (action: string, args: Record<string, unknown> = {}) => ({ command_id: 'ab', action, args });

async function kindOf(p: Promise<unknown>): Promise<string> {
  try {
    await p;
    return 'ok';
  } catch (error) {
    // The same mapping the bus applies before replying to Python.
    const reply = replyFor({ error });
    return reply.ok ? 'ok' : reply.error_kind;
  }
}

test('open is fenced by the row before any socket exists', async () => {
  const rows: Record<string, Row> = {
    wa1: { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: true, lease_expires_at_ms: Date.now() + 20_000 },
    tg1: { channel: 'telegram', is_active: true, lease_epoch: 1, live: true, lease_expires_at_ms: Date.now() + 20_000 },
    idle: { channel: 'whatsapp', is_active: true, lease_epoch: 2, live: false, lease_expires_at_ms: null },
  };
  const gw = new Gateway(fakePool(rows, new Set(['wa1'])), async () => true, quiet);
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 3, browser }))), 'stale_epoch');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 5, browser }))), 'stale_epoch');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'idle', epoch: 2, browser }))), 'stale_epoch');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'tg1', epoch: 1, browser }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'nope', epoch: 1, browser }))), 'not_found');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 4 }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: '4', browser }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('open', { epoch: 4, browser }))), 'bad_request');
  assert.deepEqual(gw.status(), []);
});

test('open without creds is not_found; close of an unknown session is a no-op', async () => {
  const rows: Record<string, Row> = {
    wa1: { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: true, lease_expires_at_ms: Date.now() + 20_000 },
  };
  const gw = new Gateway(fakePool(rows, new Set()), async () => true, quiet);
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }))), 'not_found');
  assert.deepEqual(await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 })), { closed: false });
  assert.deepEqual(await gw.handle(cmd('status')), []);
  assert.equal(await kindOf(gw.handle(cmd('nope'))), 'bad_request');
});

test('pair refuses a live lease (busy), validates method/phone, requires the row', async () => {
  const rows: Record<string, Row> = {
    wa1: { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: true, lease_expires_at_ms: Date.now() + 20_000 },
  };
  const gw = new Gateway(fakePool(rows, new Set()), async () => true, quiet);
  assert.equal(await kindOf(gw.handle(cmd('pair', { session_id: 'wa1', pair_id: 'p1', method: 'qr', browser }))), 'busy');
  assert.equal(await kindOf(gw.handle(cmd('pair', { session_id: 'wa1', pair_id: 'p1', method: 'sms', browser }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('pair', { session_id: 'wa1', pair_id: 'p1', method: 'code', browser }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('pair', { session_id: 'nope', pair_id: 'p1', method: 'qr', browser }))), 'not_found');
  assert.equal(await kindOf(gw.handle(cmd('pair_cancel', { pair_id: 'p1' }))), 'not_found');
});

test('postgres outage: commands surface as other, watchdog tick survives', async () => {
  const gw = new Gateway(fakePool({}, new Set(), { fail: true }), async () => true, quiet);
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 1, browser }))), 'other');
  await gw.watchdogTick(); // no sockets, pg down: logs and moves on
  await gw.shutdown('test');
  assert.equal(await kindOf(gw.handle(cmd('status'))), 'busy');
});
