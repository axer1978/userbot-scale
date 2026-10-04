/**
 * Command handlers over a fake pool: every path that must refuse BEFORE a
 * socket would be created is exercised here without Baileys or Postgres.
 * (Nothing here ever connects to WhatsApp.)
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import pino from 'pino';
import { replyFor } from '../src/bus.ts';
import { Gateway, MAX_DELETE_IDS } from '../src/gateway.ts';
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

/** A stand-in for an open SessionSocket; the gateway only sees this surface. */
function stubSocket(over: Partial<{ epoch: number; state: string; lost: string | null }> = {}) {
  const calls: unknown[] = [];
  const stub = {
    sessionId: 'wa1',
    epoch: 4,
    state: 'open',
    lost: null,
    me: null,
    calls,
    sendText: async (jid: string, text: string) => (calls.push(['send', jid, text]), { message_id: 'MID', ts: 1 }),
    read: async (jid: string, ids: string[]) => void calls.push(['read', jid, ids]),
    deleteMessages: async (jid: string, ids: string[]) => void calls.push(['delete', jid, ids]),
    presence: async (state: string, jid?: string) => void calls.push(['presence', state, jid]),
    logout: async () => (calls.push(['logout']), true),
    close: async () => void calls.push(['close']),
    ...over,
  };
  return stub;
}

function inject(gw: Gateway, stub: ReturnType<typeof stubSocket>): void {
  (gw as unknown as { sessions: Map<string, unknown> }).sessions.set(stub.sessionId, stub);
}

test('send_text / read / presence are fenced: exact epoch, open state, valid args', async () => {
  const gw = new Gateway(fakePool({}, new Set()), async () => true, quiet);
  const s = { session_id: 'wa1', epoch: 4 };
  // No socket at all -> not_connected (for every primitive).
  assert.equal(await kindOf(gw.handle(cmd('send_text', { ...s, jid: '1@s.whatsapp.net', text: 'hi' }))), 'not_connected');
  assert.equal(await kindOf(gw.handle(cmd('read', { ...s, jid: '1@s.whatsapp.net', message_ids: ['a'] }))), 'not_connected');
  assert.equal(await kindOf(gw.handle(cmd('presence', { ...s, state: 'available' }))), 'not_connected');

  const stub = stubSocket();
  inject(gw, stub);
  assert.deepEqual(await gw.handle(cmd('send_text', { ...s, jid: '1@s.whatsapp.net', text: 'hi' })), { message_id: 'MID', ts: 1 });
  assert.deepEqual(await gw.handle(cmd('read', { ...s, jid: '1@lid', message_ids: ['a', 'b'] })), { ok: true });
  assert.deepEqual(await gw.handle(cmd('presence', { ...s, state: 'composing', jid: '1@s.whatsapp.net' })), { ok: true });
  assert.deepEqual(await gw.handle(cmd('presence', { ...s, state: 'unavailable' })), { ok: true });
  assert.deepEqual(stub.calls, [
    ['send', '1@s.whatsapp.net', 'hi'],
    ['read', '1@lid', ['a', 'b']],
    ['presence', 'composing', '1@s.whatsapp.net'],
    ['presence', 'unavailable', undefined],
  ]);

  // Epoch fencing: lower AND higher are stale.
  for (const epoch of [3, 5]) {
    assert.equal(await kindOf(gw.handle(cmd('send_text', { session_id: 'wa1', epoch, jid: '1@s.whatsapp.net', text: 'x' }))), 'stale_epoch');
    assert.equal(await kindOf(gw.handle(cmd('read', { session_id: 'wa1', epoch, jid: '1@s.whatsapp.net', message_ids: ['a'] }))), 'stale_epoch');
    assert.equal(await kindOf(gw.handle(cmd('presence', { session_id: 'wa1', epoch, state: 'available' }))), 'stale_epoch');
    assert.equal(await kindOf(gw.handle(cmd('logout', { session_id: 'wa1', epoch }))), 'stale_epoch');
  }
  // Argument validation happens after fencing.
  assert.equal(await kindOf(gw.handle(cmd('send_text', { ...s, jid: '123@g.us', text: 'x' }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('send_text', { ...s, jid: '1@s.whatsapp.net', text: '' }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('read', { ...s, jid: '1@s.whatsapp.net', message_ids: [] }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('read', { ...s, jid: '1@s.whatsapp.net', message_ids: 'a' }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('presence', { ...s, state: 'recording', jid: '1@s.whatsapp.net' }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('presence', { ...s, state: 'composing' }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('presence', { ...s, state: 'available', jid: '1@s.whatsapp.net' }))), 'bad_request');

  // Not open yet -> not_connected; device lost -> session_lost.
  inject(gw, stubSocket({ state: 'connecting' }));
  assert.equal(await kindOf(gw.handle(cmd('send_text', { ...s, jid: '1@s.whatsapp.net', text: 'x' }))), 'not_connected');
  inject(gw, stubSocket({ state: 'closed', lost: 'loggedOut' }));
  assert.equal(await kindOf(gw.handle(cmd('send_text', { ...s, jid: '1@s.whatsapp.net', text: 'x' }))), 'session_lost');
  assert.equal(await kindOf(gw.handle(cmd('presence', { ...s, state: 'available' }))), 'session_lost');
});

test('delete is fenced like read: no socket, stale epoch, not open, lost', async () => {
  const gw = new Gateway(fakePool({}, new Set()), async () => true, quiet);
  const s = { session_id: 'wa1', epoch: 4 };
  const args = { jid: '1@s.whatsapp.net', message_ids: ['a'] };
  // No socket at all -> not_connected, the same as read.
  assert.equal(await kindOf(gw.handle(cmd('read', { ...s, ...args }))), 'not_connected');
  assert.equal(await kindOf(gw.handle(cmd('delete', { ...s, ...args }))), 'not_connected');

  const stub = stubSocket();
  inject(gw, stub);
  // Epoch fencing: lower AND higher are stale, for read and delete alike.
  for (const epoch of [3, 5]) {
    assert.equal(await kindOf(gw.handle(cmd('read', { session_id: 'wa1', epoch, ...args }))), 'stale_epoch');
    assert.equal(await kindOf(gw.handle(cmd('delete', { session_id: 'wa1', epoch, ...args }))), 'stale_epoch');
  }
  assert.equal(await kindOf(gw.handle(cmd('delete', { session_id: 'wa1', epoch: '4', ...args }))), 'bad_request');
  assert.equal(await kindOf(gw.handle(cmd('delete', { epoch: 4, ...args }))), 'bad_request');
  assert.deepEqual(stub.calls, [], 'nothing reached the socket');

  inject(gw, stubSocket({ state: 'connecting' }));
  assert.equal(await kindOf(gw.handle(cmd('delete', { ...s, ...args }))), 'not_connected');
  inject(gw, stubSocket({ state: 'closed', lost: 'loggedOut' }));
  assert.equal(await kindOf(gw.handle(cmd('delete', { ...s, ...args }))), 'session_lost');
});

test('delete validates jid and message_ids, then hands every id to the socket', async () => {
  const gw = new Gateway(fakePool({}, new Set()), async () => true, quiet);
  const s = { session_id: 'wa1', epoch: 4 };
  const stub = stubSocket();
  inject(gw, stub);
  const del = (args: Record<string, unknown>) => gw.handle(cmd('delete', { ...s, ...args }));

  assert.deepEqual(await del({ jid: '1@lid', message_ids: ['a', 'b'] }), { ok: true });
  const max = Array.from({ length: MAX_DELETE_IDS }, (_, i) => `ID${i}`);
  assert.deepEqual(await del({ jid: '1@s.whatsapp.net', message_ids: max }), { ok: true });
  assert.deepEqual(await del({ jid: '1@s.whatsapp.net', message_ids: ['x'.repeat(128)] }), { ok: true });
  assert.deepEqual(stub.calls, [
    ['delete', '1@lid', ['a', 'b']],
    ['delete', '1@s.whatsapp.net', max],
    ['delete', '1@s.whatsapp.net', ['x'.repeat(128)]],
  ]);
  stub.calls.length = 0;

  const jid = '1@s.whatsapp.net';
  for (const message_ids of [
    [],
    'a',
    undefined,
    null,
    { 0: 'a' },
    ['a', 1],
    ['a', null],
    [''],
    ['x'.repeat(129)],
    [...max, 'one-too-many'],
  ]) {
    assert.equal(await kindOf(del({ jid, message_ids })), 'bad_request', `message_ids=${JSON.stringify(message_ids)}`);
  }
  for (const badJid of ['123@g.us', 'status@broadcast', '', undefined, 'a b@s.whatsapp.net']) {
    assert.equal(await kindOf(del({ jid: badJid, message_ids: ['a'] })), 'bad_request', `jid=${String(badJid)}`);
  }
  assert.deepEqual(stub.calls, [], 'a refused call never reaches the socket');
});

test('logout: with a socket it logs out, wipes and drops; without one it is fenced like open', async () => {
  const rows: Record<string, Row> = {
    wa1: { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: true, lease_expires_at_ms: Date.now() + 20_000 },
    idle: { channel: 'whatsapp', is_active: true, lease_epoch: 2, live: false, lease_expires_at_ms: null },
  };
  const wiped: string[] = [];
  const pool = fakePool(rows, new Set());
  const realQuery = (pool as unknown as { query: (sql: string, p: unknown[]) => Promise<unknown> }).query;
  (pool as unknown as { query: unknown }).query = async (sql: string, params: unknown[]) => {
    if (sql.startsWith('DELETE FROM wa_auth_state')) {
      wiped.push(params[0] as string);
      return { rows: [], rowCount: 1 };
    }
    return realQuery(sql, params);
  };
  const gw = new Gateway(pool, async () => true, quiet);
  const stub = stubSocket();
  inject(gw, stub);
  assert.deepEqual(await gw.handle(cmd('logout', { session_id: 'wa1', epoch: 4 })), { logged_out: true });
  assert.deepEqual(stub.calls, [['logout']]);
  assert.deepEqual(wiped, ['wa1']);
  assert.deepEqual(gw.status(), [], 'socket dropped from the registry');

  // No socket: the lease row is verified like open...
  assert.equal(await kindOf(gw.handle(cmd('logout', { session_id: 'wa1', epoch: 3 }))), 'stale_epoch');
  assert.equal(await kindOf(gw.handle(cmd('logout', { session_id: 'idle', epoch: 2 }))), 'stale_epoch');
  assert.equal(await kindOf(gw.handle(cmd('logout', { session_id: 'nope', epoch: 1 }))), 'not_found');
  // ...and with no stored creds there is nothing to unlink.
  assert.deepEqual(await gw.handle(cmd('logout', { session_id: 'wa1', epoch: 4 })), { logged_out: false });
});

test('open falls back to the derived browser tuple instead of bad_request', async () => {
  const rows: Record<string, Row> = {
    wa1: { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: true, lease_expires_at_ms: Date.now() + 20_000 },
  };
  const gw = new Gateway(fakePool(rows, new Set()), async () => true, quiet);
  // Gets past the tuple check and fails later on the missing creds.
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 4 }))), 'not_found');
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser: ['x'] }))), 'not_found');
});

test('postgres outage: commands surface as other, watchdog tick survives', async () => {
  const gw = new Gateway(fakePool({}, new Set(), { fail: true }), async () => true, quiet);
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 1, browser }))), 'other');
  await gw.watchdogTick(); // no sockets, pg down: logs and moves on
  await gw.shutdown('test');
  assert.equal(await kindOf(gw.handle(cmd('status'))), 'busy');
});

test('pair_cancel answers only once the pairing has let go, so "start again" is not refused busy', async () => {
  const rows: Record<string, Row> = {
    wa1: { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: false, lease_expires_at_ms: null },
  };
  const gw = new Gateway(fakePool(rows, new Set()), async () => true, quiet);
  // A pairing run whose socket takes a moment to end after cancel (endSocket + wipe).
  let release!: () => void;
  const ended = new Promise<void>((resolve) => (release = resolve));
  const run = { cancel: () => void setTimeout(release, 50) };
  const internals = gw as unknown as {
    pairings: Map<string, unknown>;
    pairingBySession: Map<string, string>;
    pairingDone: Map<string, Promise<unknown>>;
  };
  internals.pairings.set('p1', run);
  internals.pairingBySession.set('wa1', 'p1');
  internals.pairingDone.set('p1', ended.then(() => {
    internals.pairings.delete('p1');
    internals.pairingBySession.delete('wa1');
  }));
  assert.deepEqual(await gw.handle(cmd('pair_cancel', { pair_id: 'p1' })), { cancelled: true });
  assert.equal(internals.pairingBySession.has('wa1'), false);
});
