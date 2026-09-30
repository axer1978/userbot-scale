import test from 'node:test';
import assert from 'node:assert/strict';
import pino from 'pino';
import { InboxWriter, retryDelayMs } from '../src/inbox.ts';
import type { InboxPayload } from '../src/normalize.ts';
import type { Pool } from '../src/db.ts';
import { pgReachable, withSchema } from './pg_helper.ts';

const quiet = pino({ level: 'silent' });

function payload(id: string, over: Partial<InboxPayload> = {}): InboxPayload {
  return {
    v: 1,
    session_id: 'wa1',
    epoch: 2,
    wa_message_id: id,
    jid: '34600000000@s.whatsapp.net',
    jid_alt: null,
    phone_jid: '34600000000@s.whatsapp.net',
    lid: null,
    push_name: 'Ana',
    from_me: false,
    type: 'text',
    text: 'hola',
    quoted_id: null,
    ts: 1_700_000_000,
    ...over,
  };
}

test('retry delays double from 1 s to 30 s', () => {
  assert.equal(retryDelayMs(0), 1_000);
  assert.equal(retryDelayMs(3), 8_000);
  assert.equal(retryDelayMs(10), 30_000);
});

test('inserts in order, publishes one nudge per row, retries with backoff while postgres is down, never drops', async () => {
  let failures = 3;
  const inserted: string[] = [];
  const pool = {
    query: async (_sql: string, params: unknown[]) => {
      if (failures > 0) {
        failures -= 1;
        throw new Error('connection refused');
      }
      inserted.push(params[1] as string);
      return { rows: [], rowCount: 1 };
    },
  } as unknown as Pool;
  const published: unknown[] = [];
  const sleeps: number[] = [];
  const writer = new InboxWriter(pool, 'wa1', async (_c, p) => (published.push(p), true), quiet, {
    baseMs: 10,
    maxMs: 40,
    sleep: async (ms) => void sleeps.push(ms),
  });
  writer.enqueue(payload('m1'));
  writer.enqueue(payload('m2'));
  writer.enqueue(payload('m3'));
  assert.equal(writer.pending, 3);
  await writer.flush();
  assert.deepEqual(inserted, ['m1', 'm2', 'm3']);
  assert.deepEqual(sleeps, [10, 20, 40]);
  assert.equal(writer.pending, 0);
  assert.equal(writer.delivered, 3);
  assert.deepEqual(published, Array(3).fill({ v: 1, type: 'inbox', session_id: 'wa1', epoch: 2 }));
});

test('the backlog warning fires at the threshold and nothing is dropped', async () => {
  const warnings: unknown[] = [];
  const log = pino({ level: 'warn' }, { write: (line: string) => void warnings.push(JSON.parse(line)) });
  let down = true;
  const pool = {
    query: async () => {
      if (down) throw new Error('down');
      return { rows: [], rowCount: 1 };
    },
  } as unknown as Pool;
  const writer = new InboxWriter(pool, 'wa1', async () => true, log, { baseMs: 1, maxMs: 1, warnAt: 5, sleep: async () => undefined });
  for (let i = 0; i < 12; i++) writer.enqueue(payload(`m${i}`));
  assert.equal(warnings.filter((w) => String((w as { msg: string }).msg).includes('inbox backlog')).length, 2); // at 5 and at 10
  down = false;
  await writer.flush();
  assert.equal(writer.pending, 0);
  assert.equal(writer.delivered, 12);
});

const reachable = await pgReachable();

test('ON CONFLICT: offline redelivery and reconnects insert each message exactly once; tenant filled by trigger', { skip: !reachable && 'postgres unreachable' }, async () => {
  await withSchema(async (pool) => {
    const published: unknown[] = [];
    const writer = new InboxWriter(pool, 'wa1', async (_c, p) => (published.push(p), true), quiet, { baseMs: 1, maxMs: 1 });
    writer.enqueue(payload('A'));
    writer.enqueue(payload('B', { from_me: true, text: 'typed on the phone' }));
    writer.enqueue(payload('A')); // redelivered after a reconnect
    await writer.flush();
    const rows = await pool.query('SELECT tenant_id, session_id, wa_message_id, payload FROM wa_inbox ORDER BY id');
    assert.deepEqual(
      rows.rows.map((r) => [r.tenant_id, r.session_id, r.wa_message_id, r.payload.from_me, r.payload.text]),
      [
        [1, 'wa1', 'A', false, 'hola'],
        [1, 'wa1', 'B', true, 'typed on the phone'],
      ],
    );
    assert.equal(rows.rows[0].payload.v, 1);
    assert.equal(published.length, 3, 'a nudge per attempt is harmless; the runtime dedupes by row');
    // The runtime's ack: delete after persisting. A later redelivery of A
    // would then insert again, and the runtime dedupes by wa_message_id.
    await pool.query('DELETE FROM wa_inbox WHERE session_id = $1 AND wa_message_id = $2', ['wa1', 'A']);
    writer.enqueue(payload('A'));
    await writer.flush();
    const again = await pool.query('SELECT wa_message_id FROM wa_inbox ORDER BY id');
    assert.deepEqual(again.rows.map((r) => r.wa_message_id), ['B', 'A']);
  });
});
