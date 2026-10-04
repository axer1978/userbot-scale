import test from 'node:test';
import assert from 'node:assert/strict';
import { WAMessageStatus } from 'baileys';
import { extractFailure, FailureReporter, failureKindForCode } from '../src/failures.ts';

/** Exactly what rc14's handleBadAck emits for <ack class="message" error="..."/>. */
const badAck = (id: string, error: string, jid = '34600000000@s.whatsapp.net') => ({
  key: { remoteJid: jid, fromMe: true, id },
  update: { status: WAMessageStatus.ERROR, messageStubParameters: [error] },
});

test('ack error codes map like senderr', () => {
  assert.equal(failureKindForCode(429), 'rate_limited');
  assert.equal(failureKindForCode(403), 'blocked');
  assert.equal(failureKindForCode(404), 'not_on_whatsapp');
  assert.equal(failureKindForCode(463), 'other');
  assert.equal(failureKindForCode(479), 'other');
  assert.equal(failureKindForCode(null, 'not-authorized'), 'blocked');
  assert.equal(failureKindForCode(null, 'rate-overlimit'), 'rate_limited');
  assert.equal(failureKindForCode(null), 'other');
});

test('a bad ack becomes a failure with its code; normal statuses do not', () => {
  assert.deepEqual(extractFailure(badAck('M1', '429')), { wa_message_id: 'M1', jid: '34600000000@s.whatsapp.net', code: 429, error_kind: 'rate_limited' });
  assert.deepEqual(extractFailure(badAck('M2', '403', '9876@lid')), { wa_message_id: 'M2', jid: '9876@lid', code: 403, error_kind: 'blocked' });
  assert.equal(extractFailure(badAck('M3', '463'))?.error_kind, 'other');
  // rc14 attaches a human text after the code for the timelock case.
  const timelock = extractFailure({ key: { remoteJid: 'x@s.whatsapp.net', fromMe: true, id: 'M4' }, update: { status: WAMessageStatus.ERROR, messageStubParameters: ['400', 'account restricted'] } });
  assert.equal(timelock?.code, 400);
  for (const status of [WAMessageStatus.PENDING, WAMessageStatus.SERVER_ACK, WAMessageStatus.DELIVERY_ACK, WAMessageStatus.READ, WAMessageStatus.PLAYED]) {
    assert.equal(extractFailure({ key: { remoteJid: 'x@s.whatsapp.net', fromMe: true, id: 'S' }, update: { status } }), null, String(status));
  }
  assert.equal(extractFailure({ key: { remoteJid: 'x@s.whatsapp.net', fromMe: true, id: 'E' }, update: { message: null } as never }), null, 'content edits are not failures');
  assert.equal(extractFailure({ key: { remoteJid: 'x@s.whatsapp.net', fromMe: false, id: 'I' }, update: { status: WAMessageStatus.ERROR } }), null, 'not ours');
  assert.equal(extractFailure({ key: { remoteJid: null, fromMe: true, id: 'N' }, update: { status: WAMessageStatus.ERROR } }), null);
  assert.equal(extractFailure(badAck('X', 'weird'))?.code, null, 'non-numeric error text -> code null');
});

test('the reporter emits each message id once, with session and epoch', () => {
  const r = new FailureReporter('wa1', 7);
  const first = r.events([badAck('M1', '429'), badAck('M2', '403'), { key: { remoteJid: 'x@s.whatsapp.net', fromMe: true, id: 'OK' }, update: { status: WAMessageStatus.DELIVERY_ACK } }]);
  assert.deepEqual(first, [
    { v: 1, type: 'message_failed', session_id: 'wa1', epoch: 7, wa_message_id: 'M1', jid: '34600000000@s.whatsapp.net', code: 429, error_kind: 'rate_limited' },
    { v: 1, type: 'message_failed', session_id: 'wa1', epoch: 7, wa_message_id: 'M2', jid: '34600000000@s.whatsapp.net', code: 403, error_kind: 'blocked' },
  ]);
  assert.deepEqual(r.events([badAck('M1', '429'), badAck('M1', '500')]), [], 'repeated acks for the same id are silent');
  assert.equal(r.events([badAck('M3', '404')])[0]?.error_kind, 'not_on_whatsapp');
});
