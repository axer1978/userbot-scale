import test from 'node:test';
import assert from 'node:assert/strict';
import { Boom } from '@hapi/boom';
import { classifySendError } from '../src/senderr.ts';
import { isEcho } from '../src/session.ts';
import { LruCache } from '../src/lru.ts';
import type { InboxPayload } from '../src/normalize.ts';

/** Exactly how assertNodeErrorFree builds IQ errors: text as message, code in data. */
const iq = (text: string, code: number) => new Boom(text, { data: code });

test('IQ stanza errors map by their code/text', () => {
  assert.equal(classifySendError(iq('rate-overlimit', 429)).kind, 'rate_limited');
  assert.equal(classifySendError(iq('not-authorized', 403)).kind, 'blocked');
  assert.equal(classifySendError(iq('item-not-found', 404)).kind, 'not_on_whatsapp');
  assert.equal(classifySendError(iq('not-allowed', 405)).kind, 'other');
  assert.equal(classifySendError(iq('rate-overlimit', 429)).detail, 'rate-overlimit (429)');
});

test('socket-level Boom errors map by statusCode', () => {
  assert.equal(classifySendError(new Boom('Connection Closed', { statusCode: 428 })).kind, 'not_connected');
  assert.equal(classifySendError(new Boom('Timed Out', { statusCode: 408 })).kind, 'not_connected');
  assert.equal(classifySendError(new Boom('Intentional Logout', { statusCode: 401 })).kind, 'session_lost');
  assert.equal(classifySendError(new Boom('Not authenticated')).kind, 'session_lost');
  assert.equal(classifySendError(new Boom('All encryptions failed', { statusCode: 500 })).kind, 'other');
});

test('plain errors and junk fall through to other, with a readable detail', () => {
  assert.deepEqual(classifySendError(new Error('boom')), { kind: 'other', detail: 'boom' });
  assert.equal(classifySendError(undefined).kind, 'other');
  assert.equal(classifySendError('rate limit exceeded').kind, 'rate_limited');
});

test('echo suppression: only from_me messages we sent ourselves are echoes', () => {
  const sent = new LruCache<string, true>(10);
  sent.set('OURS', true);
  const base = { v: 1, session_id: 'wa1', epoch: 1, jid: 'x@s.whatsapp.net', jid_alt: null, phone_jid: null, lid: null, push_name: null, type: 'text', text: 'x', quoted_id: null, ts: 0 } as const;
  const p = (id: string, from_me: boolean): InboxPayload => ({ ...base, wa_message_id: id, from_me });
  assert.equal(isEcho(p('OURS', true), sent), true);
  assert.equal(isEcho(p('PHONE', true), sent), false, 'typed on the phone: forwarded, flagged from_me');
  assert.equal(isEcho(p('OURS', false), sent), false, 'an inbound id can never be an echo');
});
