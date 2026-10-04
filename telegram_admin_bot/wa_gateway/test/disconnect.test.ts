import test from 'node:test';
import assert from 'node:assert/strict';
import { Boom } from '@hapi/boom';
import { DisconnectReason } from 'baileys';
import { backoffMs, classifyDisconnect, jitterMs, statusCodeOf } from '../src/disconnect.ts';

const boom = (code: number) => new Boom('x', { statusCode: code });

test('fatal reasons never reconnect and name the reason', () => {
  assert.deepEqual(classifyDisconnect(boom(DisconnectReason.loggedOut)), { kind: 'fatal', reason: 'loggedOut', code: 401 });
  assert.deepEqual(classifyDisconnect(boom(DisconnectReason.forbidden)), { kind: 'fatal', reason: 'forbidden', code: 403 });
  assert.deepEqual(classifyDisconnect(boom(DisconnectReason.badSession)), { kind: 'fatal', reason: 'badSession', code: 500 });
  assert.deepEqual(classifyDisconnect(boom(DisconnectReason.connectionReplaced)), { kind: 'fatal', reason: 'connectionReplaced', code: 440 });
  assert.deepEqual(classifyDisconnect(boom(DisconnectReason.multideviceMismatch)), { kind: 'fatal', reason: 'multideviceMismatch', code: 411 });
});

test('restartRequired reconnects at once; transient codes reconnect with backoff', () => {
  assert.deepEqual(classifyDisconnect(boom(DisconnectReason.restartRequired)), { kind: 'restart', code: 515 });
  for (const code of [DisconnectReason.connectionClosed, DisconnectReason.connectionLost, DisconnectReason.timedOut, DisconnectReason.unavailableService]) {
    const v = classifyDisconnect(boom(code));
    assert.equal(v.kind, 'transient', String(code));
    assert.equal(v.code, code);
  }
});

test('errors without a status code (raw socket errors, undefined) are transient', () => {
  assert.equal(classifyDisconnect(new Error('ECONNRESET')).kind, 'transient');
  assert.equal(classifyDisconnect(undefined).kind, 'transient');
  assert.equal(statusCodeOf({ output: { statusCode: 'nope' } }), undefined);
  assert.equal(statusCodeOf(boom(428)), 428);
});

test('backoff doubles from 2s and caps at 60s', () => {
  assert.equal(backoffMs(0), 2_000);
  assert.equal(backoffMs(1), 4_000);
  assert.equal(backoffMs(4), 32_000);
  assert.equal(backoffMs(5), 60_000);
  assert.equal(backoffMs(50), 60_000);
});

test('jitter spreads a delay by ±20 % and never below or above that', () => {
  assert.equal(jitterMs(10_000, () => 0), 8_000);
  assert.equal(jitterMs(10_000, () => 1), 12_000);
  assert.equal(jitterMs(10_000, () => 0.5), 10_000);
  for (let i = 0; i < 200; i++) {
    const d = jitterMs(60_000);
    assert.ok(d >= 48_000 && d <= 72_000, String(d));
  }
});
