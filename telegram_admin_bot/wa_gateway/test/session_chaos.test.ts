/**
 * Chaos: the socket lifecycle under every Baileys disconnect reason, with
 * a fake socket (an EventEmitter standing in for sock.ev) behind the real
 * Gateway/SessionSocket/InboxWriter code and a fake pool. Nothing here
 * connects to WhatsApp or Postgres. The creds row is real ciphertext so
 * the auth-state path (decrypt, registered check) is the production one.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Boom } from '@hapi/boom';
import { DisconnectReason, initAuthCreds } from 'baileys';
import type { WASocket } from 'baileys';
import pino from 'pino';
import { authAad, serialize } from '../src/authstate.ts';
import { replyFor } from '../src/bus.ts';
import { encrypt, keyringFromContent, resetKeyringCache } from '../src/crypto.ts';
import type { Pool } from '../src/db.ts';
import { Gateway } from '../src/gateway.ts';
import { INSERT_SQL } from '../src/inbox.ts';
import type { SessionEvent } from '../src/session.ts';

const KEY_B64 = 'MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=';
process.env.USERBOT_MASTER_KEY = KEY_B64;
delete process.env.USERBOT_MASTER_KEY_FILE;
resetKeyringCache();
const KEYRING = keyringFromContent(KEY_B64, 'test');

const quiet = pino({ level: 'silent' });
const browser = ['macOS', 'Chrome', '1.0'];
const cmd = (action: string, args: Record<string, unknown> = {}) => ({ command_id: 'ab', action, args });
const boom = (code: number) => new Boom('x', { statusCode: code });
const tick = () => new Promise((r) => setImmediate(r));

function credsBlob(sessionId: string, registered = true): Buffer {
  return encrypt(serialize({ ...initAuthCreds(), registered }), authAad(sessionId, 'creds', ''), KEYRING);
}

type Row = { channel: string; is_active: boolean; lease_epoch: number; live: boolean; lease_expires_at_ms: number | null };

/** A pool answering only what open + the inbox need; records wa_inbox inserts. */
function fakePool(creds: Record<string, Buffer>, opts: { insertError?: (waMessageId: string) => Error | null } = {}) {
  const inserted: Array<[string, string]> = [];
  const rows: Record<string, Row> = {};
  for (const id of Object.keys(creds)) {
    rows[id] = { channel: 'whatsapp', is_active: true, lease_epoch: 4, live: true, lease_expires_at_ms: Date.now() + 20_000 };
  }
  const pool = {
    inserted,
    query: async (sql: string, params: unknown[]) => {
      if (sql.includes('FROM telegram_sessions')) {
        const row = rows[params[0] as string];
        return { rows: row ? [row] : [], rowCount: row ? 1 : 0 };
      }
      if (sql.startsWith('SELECT 1 FROM wa_auth_state')) {
        const has = params[0] as string in creds;
        return { rows: has ? [{ '?column?': 1 }] : [], rowCount: has ? 1 : 0 };
      }
      if (sql.startsWith('SELECT value_enc FROM wa_auth_state')) {
        const blob = creds[params[0] as string];
        return { rows: blob ? [{ value_enc: blob }] : [], rowCount: blob ? 1 : 0 };
      }
      if (sql === INSERT_SQL) {
        const err = opts.insertError?.(params[1] as string);
        if (err) throw err;
        inserted.push([params[0] as string, params[1] as string]);
        return { rows: [], rowCount: 1 };
      }
      if (sql.startsWith('INSERT INTO wa_auth_state')) return { rows: [], rowCount: 1 };
      throw new Error(`unexpected query: ${sql}`);
    },
  };
  return pool as unknown as Pool & { inserted: Array<[string, string]> };
}

type FakeSock = WASocket & { ev: EventEmitter; ended: number };

/** Every socket the gateway makes, in order; each one is a bare emitter. */
function socketFactory() {
  const made: FakeSock[] = [];
  const makeSocket = () => {
    const ev = new EventEmitter();
    const sock = {
      ev,
      ended: 0,
      user: { id: '34600000000:3@s.whatsapp.net', lid: '999@lid', name: 'Salon' },
      end: async () => void (sock.ended += 1),
      logout: async () => undefined,
      sendMessage: async (jid: string) => ({ key: { id: `OUT${made.length}`, remoteJid: jid }, message: { conversation: 'x' }, messageTimestamp: 1 }),
      onWhatsApp: async () => [{ exists: true, jid: 'x' }],
      readMessages: async () => undefined,
      sendPresenceUpdate: async () => undefined,
    } as unknown as FakeSock;
    made.push(sock);
    return sock;
  };
  return { made, makeSocket, last: () => made[made.length - 1]! };
}

function inbound(id: string, over: Record<string, unknown> = {}) {
  return {
    key: { remoteJid: '34611111111@s.whatsapp.net', fromMe: false, id },
    message: { conversation: 'hola' },
    messageTimestamp: 1_700_000_000,
    pushName: 'Ana',
    ...over,
  };
}

async function setup(opts: { creds?: Record<string, Buffer>; insertError?: (waMessageId: string) => Error | null } = {}) {
  const creds = opts.creds ?? { wa1: credsBlob('wa1') };
  const pool = fakePool(creds, { insertError: opts.insertError });
  const events: SessionEvent[] = [];
  const factory = socketFactory();
  const gw = new Gateway(pool, async (_c, p) => (events.push(p as SessionEvent), true), quiet, Date.now, { makeSocket: factory.makeSocket });
  return { gw, pool, events, ...factory };
}

const open = (sock: FakeSock) => sock.ev.emit('connection.update', { connection: 'open' });
const close = (sock: FakeSock, error: unknown) => sock.ev.emit('connection.update', { connection: 'close', lastDisconnect: { error } });
const lostEvents = (events: SessionEvent[]) => events.filter((e) => e.type === 'session_lost');

async function kindOf(p: Promise<unknown>): Promise<string> {
  try {
    await p;
    return 'ok';
  } catch (error) {
    const reply = replyFor({ error });
    return reply.ok ? 'ok' : reply.error_kind;
  }
}

/** Reconnect timers are real setTimeouts (1 s+); tests fire them by hand. */
function pendingReconnect(gw: Gateway, sessionId: string): { delay: number; fire: () => void } | null {
  const session = (gw as unknown as { sessions: Map<string, { reconnectTimer: NodeJS.Timeout | null; connect: () => void }> }).sessions.get(sessionId);
  const timer = session?.reconnectTimer;
  if (!session || !timer) return null;
  const delay = (timer as unknown as { _idleTimeout: number })._idleTimeout;
  return {
    delay,
    fire: () => {
      clearTimeout(timer);
      session.reconnectTimer = null;
      session.connect();
    },
  };
}

test.beforeEach(() => resetKeyringCache());

test('transient drops reconnect with capped, jittered backoff and never stack sockets', async () => {
  const { gw, made, last } = await setup();
  assert.deepEqual(await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser })), { state: 'opening' });
  open(last());
  assert.equal(gw.status()[0]!.state, 'open');
  const delays: number[] = [];
  for (const code of [DisconnectReason.connectionLost, DisconnectReason.timedOut, DisconnectReason.connectionClosed, 503, undefined, 428, 428, 428]) {
    close(last(), code === undefined ? new Error('ECONNRESET') : boom(code));
    assert.equal(gw.status()[0]!.state, 'closed');
    const pending = pendingReconnect(gw, 'wa1');
    assert.ok(pending, `reconnect scheduled after ${code}`);
    delays.push(pending.delay);
    pending.fire();
    assert.equal(gw.status()[0]!.state, 'connecting');
  }
  // 2s, 4s, 8s, 16s, 32s, 60s, 60s, 60s (±20 %): bounded, capped at 60 s.
  const expected = [2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000, 60_000];
  delays.forEach((d, i) => assert.ok(d >= expected[i]! * 0.8 && d <= expected[i]! * 1.2, `delay ${i}: ${d}`));
  assert.equal(made.length, 9, 'one new socket per reconnect, none reused');
  open(last());
  // Back to the first step once a connection opened.
  close(last(), boom(DisconnectReason.connectionLost));
  const again = pendingReconnect(gw, 'wa1')!;
  assert.ok(again.delay >= 1_600 && again.delay <= 2_400, `reset to the base delay: ${again.delay}`);
  again.fire();
  assert.equal(gw.status().length, 1, 'one registry entry');
  await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 }));
  assert.equal(pendingReconnect(gw, 'wa1'), null);
});

test('515 restartRequired reconnects once at once; a 515 loop backs off instead of hammering', async () => {
  const { gw, events, last } = await setup();
  await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }));
  // Right after pairing: no open yet, first 515 -> 1 s.
  close(last(), boom(DisconnectReason.restartRequired));
  let pending = pendingReconnect(gw, 'wa1')!;
  assert.equal(pending.delay, 1_000);
  pending.fire();
  // Keeps coming back without an open in between: exponential, not 1 s for ever.
  const delays: number[] = [];
  for (let i = 0; i < 4; i++) {
    close(last(), boom(DisconnectReason.restartRequired));
    pending = pendingReconnect(gw, 'wa1')!;
    delays.push(pending.delay);
    pending.fire();
  }
  assert.ok(delays.every((d, i) => d >= [4_000, 8_000, 16_000, 32_000][i]! * 0.8), String(delays));
  assert.equal(lostEvents(events).length, 0, 'a restart is not a lost session');
  await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 }));
});

test('connectionReplaced (440): stop, session_lost once, no reconnect, and a later open does not ping-pong', async () => {
  const { gw, events, made, last } = await setup();
  await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }));
  open(last());
  close(last(), boom(DisconnectReason.connectionReplaced));
  assert.equal(pendingReconnect(gw, 'wa1'), null, 'no reconnect timer');
  assert.deepEqual(lostEvents(events), [{ v: 1, type: 'session_lost', session_id: 'wa1', epoch: 4, reason: 'connectionReplaced', code: 440 }]);
  assert.equal(gw.status()[0]!.lost, 'connectionReplaced');
  // The runtime's 15 s keepalive re-sends open with the same epoch: refused as
  // session_lost, no new socket (that would be the ping-pong WhatsApp bans for).
  assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }))), 'session_lost');
  assert.equal(await kindOf(gw.handle(cmd('send_text', { session_id: 'wa1', epoch: 4, jid: '1@s.whatsapp.net', text: 'x' }))), 'session_lost');
  assert.equal(made.length, 1, 'no second socket was made');
  // A late event from the dead socket changes nothing.
  close(last(), boom(DisconnectReason.connectionLost));
  assert.equal(pendingReconnect(gw, 'wa1'), null);
  assert.equal(lostEvents(events).length, 1);
});

test('loggedOut / badSession / forbidden / multideviceMismatch: fatal, named, never reconnected', async () => {
  for (const [code, reason] of [[401, 'loggedOut'], [500, 'badSession'], [403, 'forbidden'], [411, 'multideviceMismatch']] as const) {
    const { gw, events, made, last } = await setup();
    await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }));
    close(last(), boom(code));
    assert.equal(pendingReconnect(gw, 'wa1'), null, reason);
    assert.deepEqual(lostEvents(events).map((e) => (e as { reason: string }).reason), [reason]);
    assert.equal(made.length, 1);
    assert.equal(await kindOf(gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }))), 'session_lost');
  }
});

test('5 reconnects, 1 inbound message: exactly one wa_inbox insert, old sockets stay deaf', async () => {
  const { gw, pool, events, made, last } = await setup();
  await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }));
  open(last());
  for (let i = 0; i < 5; i++) {
    close(last(), boom(DisconnectReason.connectionLost));
    pendingReconnect(gw, 'wa1')!.fire();
    open(last());
  }
  assert.equal(made.length, 6);
  last().ev.emit('messages.upsert', { type: 'notify', messages: [inbound('M1')] });
  await tick();
  assert.deepEqual(pool.inserted, [['wa1', 'M1']]);
  assert.equal(events.filter((e) => e.type === 'inbox').length, 1);
  // Baileys may flush late events on a socket we already replaced: ignored.
  for (const old of made.slice(0, 5)) {
    old.ev.emit('messages.upsert', { type: 'notify', messages: [inbound('M2')] });
    old.ev.emit('messages.update', [{ key: { remoteJid: '1@s.whatsapp.net', fromMe: true, id: 'OUTX' }, update: { status: 0, messageStubParameters: ['403'] } }]);
    old.ev.emit('connection.update', { connection: 'open' });
  }
  await tick();
  assert.deepEqual(pool.inserted, [['wa1', 'M1']]);
  assert.equal(events.filter((e) => e.type === 'message_failed').length, 0);
  assert.equal(gw.status()[0]!.state, 'open');
  await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 }));
  assert.equal(last().ended, 1, 'close ends the live socket');
});

test('offline redelivery ("append") is accepted, anything else on messages.upsert is not; the handler survives junk', async () => {
  const { gw, pool, last } = await setup();
  await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }));
  open(last());
  last().ev.emit('messages.upsert', { type: 'append', messages: [inbound('OFF1')] });
  last().ev.emit('messages.upsert', { type: 'prepend', messages: [inbound('H1')] });
  last().ev.emit('messages.upsert', { type: 'notify', messages: [null, 42, {}, { key: null }, inbound('BAD', { message: null, messageStubType: 2 }), inbound('OK2')] });
  await tick();
  assert.deepEqual(pool.inserted, [['wa1', 'OFF1'], ['wa1', 'OK2']]);
  // Our own echo (id from sendText) is not forwarded; a hand-typed from_me is.
  const sent = (await gw.handle(cmd('send_text', { session_id: 'wa1', epoch: 4, jid: '34611111111@s.whatsapp.net', text: 'hi' }))) as { message_id: string };
  last().ev.emit('messages.upsert', { type: 'notify', messages: [inbound(sent.message_id, { key: { remoteJid: '34611111111@s.whatsapp.net', fromMe: true, id: sent.message_id } })] });
  last().ev.emit('messages.upsert', { type: 'notify', messages: [inbound('PHONE1', { key: { remoteJid: '34611111111@s.whatsapp.net', fromMe: true, id: 'PHONE1' } })] });
  await tick();
  assert.deepEqual(pool.inserted.map(([, id]) => id), ['OFF1', 'OK2', 'PHONE1']);
  await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 }));
});

test('a corrupt or unregistered creds row refuses that one open cleanly; other accounts still open', async () => {
  const { gw, last, made } = await setup({
    creds: {
      wa1: credsBlob('wa1'),
      bad: Buffer.from('not ciphertext at all'),
      wrongkey: encrypt(Buffer.from('{}'), authAad('other', 'creds', ''), KEYRING),
      unreg: credsBlob('unreg', false),
    },
  });
  for (const sid of ['bad', 'wrongkey', 'unreg']) {
    assert.equal(await kindOf(gw.handle(cmd('open', { session_id: sid, epoch: 4, browser }))), 'not_found', sid);
  }
  assert.deepEqual(gw.status(), [], 'nothing half-registered');
  assert.equal(made.length, 0, 'no socket made for a bad auth state');
  assert.deepEqual(await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser })), { state: 'opening' });
  open(last());
  assert.equal(gw.status()[0]!.state, 'open');
  await gw.watchdogTick();
  await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 }));
});

test('postgres refusing one row does not wedge the account; a network outage retries without loss', async () => {
  let outage = 3;
  let refusals = 0;
  const { gw, pool, last } = await setup({
    insertError: (id) => {
      if (id === 'POISON') {
        refusals += 1;
        return Object.assign(new Error('no session'), { code: 'P0001' }); // the trigger raising, for ever
      }
      if (outage > 0) {
        outage -= 1;
        return new Error('connection refused'); // network: no SQLSTATE
      }
      return null;
    },
  });
  await gw.handle(cmd('open', { session_id: 'wa1', epoch: 4, browser }));
  open(last());
  // The writer is the gateway's; shorten its sleeps for the test.
  const writer = (gw as unknown as { inboxFor: (s: string) => { opts: { sleep: (ms: number) => Promise<void> }; dropped: number } }).inboxFor('wa1');
  writer.opts.sleep = async () => undefined;
  last().ev.emit('messages.upsert', { type: 'notify', messages: [inbound('FIRST'), inbound('POISON'), inbound('AFTER')] });
  for (let i = 0; i < 50 && pool.inserted.length < 2; i++) await tick();
  assert.deepEqual(pool.inserted, [['wa1', 'FIRST'], ['wa1', 'AFTER']], 'the outage was ridden out; the row behind the poison one landed');
  assert.equal(refusals, 5, 'the poison row was given POISON_ATTEMPTS chances');
  assert.equal(writer.dropped, 1);
  assert.equal(gw.status()[0]!.inbox_pending, 0);
  await gw.handle(cmd('close', { session_id: 'wa1', epoch: 4 }));
});
