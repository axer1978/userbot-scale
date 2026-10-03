/**
 * isLinked: Baileys sets `registered` only for a pairing-code link, so a QR
 * link has to count as linked by its identity alone (the bug was that QR
 * pairings were thrown away right after the phone accepted them).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { initAuthCreds } from 'baileys';
import { isLinked } from '../src/authstate.ts';

const ACCOUNT = { details: Buffer.alloc(1), accountSignatureKey: Buffer.alloc(1), accountSignature: Buffer.alloc(1), deviceSignature: Buffer.alloc(1) };

test('fresh creds are not linked', () => {
  assert.equal(isLinked(initAuthCreds()), false);
});

test('a QR link (me + account, registered still false) is linked', () => {
  const creds = { ...initAuthCreds(), me: { id: '34600000000:3@s.whatsapp.net', lid: '1234:3@lid' }, account: ACCOUNT };
  assert.equal(creds.registered, false);
  assert.equal(isLinked(creds), true);
});

test('a pairing code requested but never confirmed (me, no account) is not linked', () => {
  const creds = { ...initAuthCreds(), me: { id: '34600000000@s.whatsapp.net', name: '~' } };
  assert.equal(isLinked(creds), false);
});

test('registered creds are linked', () => {
  assert.equal(isLinked({ ...initAuthCreds(), registered: true }), true);
});
