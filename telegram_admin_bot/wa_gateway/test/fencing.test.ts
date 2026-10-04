import test from 'node:test';
import assert from 'node:assert/strict';
import { DANGER_SECONDS, decideClose, decideOpen, postgresSilentTooLong, watchdogVerdict, type SessionRow } from '../src/fencing.ts';

const NOW = 1_800_000_000_000;
const live: SessionRow = { channel: 'whatsapp', is_active: true, lease_epoch: 7, live: true, lease_expires_at_ms: NOW + 25_000 };

test('open: happy path, then idempotent on the same epoch', () => {
  assert.deepEqual(decideOpen({ row: live, requestedEpoch: 7, existing: null, hasCreds: true }), { action: 'open' });
  assert.deepEqual(decideOpen({ row: live, requestedEpoch: 7, existing: { epoch: 7, state: 'open' }, hasCreds: true }), { action: 'idempotent', state: 'open' });
  assert.deepEqual(decideOpen({ row: live, requestedEpoch: 7, existing: { epoch: 7, state: 'connecting' }, hasCreds: false }), { action: 'idempotent', state: 'connecting' });
});

test('open: a lower existing epoch is replaced, a higher one wins', () => {
  assert.deepEqual(decideOpen({ row: live, requestedEpoch: 7, existing: { epoch: 6, state: 'open' }, hasCreds: true }), { action: 'replace_then_open' });
  const higher = decideOpen({ row: { ...live, lease_epoch: 7 }, requestedEpoch: 7, existing: { epoch: 8, state: 'open' }, hasCreds: true });
  assert.equal(higher.action, 'reject');
  assert.equal((higher as { kind: string }).kind, 'stale_epoch');
});

test('open: row must be whatsapp, active, live and on the same epoch', () => {
  const kind = (d: ReturnType<typeof decideOpen>) => (d.action === 'reject' ? d.kind : d.action);
  assert.equal(kind(decideOpen({ row: null, requestedEpoch: 7, existing: null, hasCreds: true })), 'not_found');
  assert.equal(kind(decideOpen({ row: { ...live, channel: 'telegram' }, requestedEpoch: 7, existing: null, hasCreds: true })), 'bad_request');
  assert.equal(kind(decideOpen({ row: { ...live, is_active: false }, requestedEpoch: 7, existing: null, hasCreds: true })), 'stale_epoch');
  assert.equal(kind(decideOpen({ row: { ...live, live: false }, requestedEpoch: 7, existing: null, hasCreds: true })), 'stale_epoch');
  assert.equal(kind(decideOpen({ row: live, requestedEpoch: 6, existing: null, hasCreds: true })), 'stale_epoch');
  assert.equal(kind(decideOpen({ row: live, requestedEpoch: 8, existing: null, hasCreds: true })), 'stale_epoch');
  assert.equal(kind(decideOpen({ row: live, requestedEpoch: 7, existing: null, hasCreds: false })), 'not_found');
  assert.equal(kind(decideOpen({ row: live, requestedEpoch: -1, existing: null, hasCreds: true })), 'bad_request');
  assert.equal(kind(decideOpen({ row: live, requestedEpoch: 1.5, existing: null, hasCreds: true })), 'bad_request');
});

test('close: only the socket epoch or newer may close; nothing to close is false', () => {
  assert.equal(decideClose(7, { epoch: 7, state: 'open' }), true);
  assert.equal(decideClose(8, { epoch: 7, state: 'open' }), true);
  assert.equal(decideClose(6, { epoch: 7, state: 'open' }), false);
  assert.equal(decideClose(7, null), false);
});

test('watchdog: keeps a socket whose lease is live on the same epoch', () => {
  assert.deepEqual(watchdogVerdict({ row: live, socketEpoch: 7, nowMs: NOW }), { close: false });
});

test('watchdog: closes on epoch change, deactivation, channel change, missing row, released lease', () => {
  const reason = (row: SessionRow | null, epoch = 7) => {
    const v = watchdogVerdict({ row, socketEpoch: epoch, nowMs: NOW });
    return v.close ? v.reason : 'KEEP';
  };
  assert.match(reason({ ...live, lease_epoch: 8 }), /epoch moved from 7 to 8/);
  assert.match(reason({ ...live, is_active: false }), /deactivated/);
  assert.match(reason({ ...live, channel: 'telegram' }), /channel changed/);
  assert.match(reason(null), /disappeared/);
  assert.match(reason({ ...live, live: false, lease_expires_at_ms: null }), /released/);
});

test('watchdog: an expired lease gets 30 s of grace, then closes', () => {
  const expired = (ago: number): SessionRow => ({ ...live, live: false, lease_expires_at_ms: NOW - ago });
  assert.deepEqual(watchdogVerdict({ row: expired(5_000), socketEpoch: 7, nowMs: NOW }), { close: false });
  assert.deepEqual(watchdogVerdict({ row: expired(29_999), socketEpoch: 7, nowMs: NOW }), { close: false });
  const v = watchdogVerdict({ row: expired(30_000), socketEpoch: 7, nowMs: NOW });
  assert.equal(v.close, true);
  assert.match((v as { reason: string }).reason, /expired 30s ago/);
});

test('postgres silence: DANGER_SECONDS mirrors leasing.DANGER_SECONDS', () => {
  assert.equal(DANGER_SECONDS, 22);
  assert.equal(postgresSilentTooLong(NOW - 21_999, NOW), false);
  assert.equal(postgresSilentTooLong(NOW - 22_000, NOW), true);
});
