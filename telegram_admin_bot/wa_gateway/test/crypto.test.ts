import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  aadFor,
  decodeKey,
  decrypt,
  DecryptError,
  encrypt,
  encryptWithNonce,
  keyringFromContent,
  loadKeyring,
  MissingKeyError,
  resetKeyringCache,
  type Keyring,
} from '../src/crypto.ts';

const TEST_KEY = 'MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI='; // same fixed key as tests/conftest.py

type Vector = {
  name: string;
  keyring: { env: string } | { file: string };
  key_id: number;
  aad: string;
  nonce_hex: string;
  plaintext_hex: string;
  blob_hex: string;
};

const golden = JSON.parse(readFileSync(new URL('./golden/crypto_vectors.json', import.meta.url), 'utf8')) as { vectors: Vector[] };

function keyringOf(v: Vector): Keyring {
  return 'env' in v.keyring ? keyringFromContent(v.keyring.env, 'env') : keyringFromContent(v.keyring.file, 'file');
}

test('golden vectors written by crypto.py decrypt and re-encrypt byte for byte', () => {
  assert.ok(golden.vectors.length >= 5);
  for (const v of golden.vectors) {
    const keyring = keyringOf(v);
    const aad = Buffer.from(v.aad, 'utf8');
    const blob = Buffer.from(v.blob_hex, 'hex');
    const plaintext = Buffer.from(v.plaintext_hex, 'hex');
    assert.equal(keyring.activeId, v.key_id, v.name);
    assert.deepEqual(decrypt(blob, aad, keyring), plaintext, v.name);
    assert.equal(encryptWithNonce(plaintext, aad, Buffer.from(v.nonce_hex, 'hex'), keyring).toString('hex'), v.blob_hex, v.name);
  }
});

test('round trip with a random nonce; layout matches crypto.py', () => {
  const keyring = keyringFromContent(TEST_KEY, 'env');
  const aad = aadFor('acct01', 'wa_auth:creds:');
  const blob = encrypt(Buffer.from('secret'), aad, keyring);
  assert.equal(blob[0], 1);
  assert.equal(blob[1], 1);
  assert.equal(blob.length, 2 + 12 + 6 + 16);
  assert.equal(decrypt(blob, aad, keyring).toString(), 'secret');
  const again = encrypt(Buffer.from('secret'), aad, keyring);
  assert.notEqual(again.toString('hex'), blob.toString('hex'), 'nonce must be random');
});

test('wrong AAD, wrong key, unknown key id, bad version and truncation all fail', () => {
  const keyring = keyringFromContent(TEST_KEY, 'env');
  const aad = aadFor('acct01', 'auth_key');
  const blob = encrypt(Buffer.from('secret'), aad, keyring);
  assert.throws(() => decrypt(blob, aadFor('acct02', 'auth_key'), keyring), DecryptError);
  assert.throws(() => decrypt(blob, aadFor('acct01', 'proxy_url'), keyring), DecryptError);
  const other = keyringFromContent(Buffer.alloc(32, 7).toString('base64'), 'env');
  assert.throws(() => decrypt(blob, aad, other), DecryptError);
  const unknownId = Buffer.from(blob);
  unknownId[1] = 9;
  assert.throws(() => decrypt(unknownId, aad, keyring), /unknown key id 9/);
  const badVersion = Buffer.from(blob);
  badVersion[0] = 2;
  assert.throws(() => decrypt(badVersion, aad, keyring), /unknown blob version 2/);
  assert.throws(() => decrypt(blob.subarray(0, blob.length - 5), aad, keyring), DecryptError);
  assert.throws(() => decrypt(blob.subarray(0, 10), aad, keyring), /too short/);
  const tampered = Buffer.from(blob);
  const last = tampered.length - 1;
  tampered[last] = (tampered[last] ?? 0) ^ 1;
  assert.throws(() => decrypt(tampered, aad, keyring), DecryptError);
});

test('keyring file with rotation: old id still decrypts, new blobs carry the active id', () => {
  const key1 = Buffer.alloc(32, '1').toString('base64');
  const key2 = Buffer.alloc(32, '2').toString('base64');
  const before = keyringFromContent(JSON.stringify({ active: 1, keys: { '1': key1 } }), 'f');
  const aad = aadFor('acct01', 'auth_key');
  const blob = encrypt(Buffer.from('secret'), aad, before);
  const after = keyringFromContent(JSON.stringify({ active: 2, keys: { '1': key1, '2': key2 } }), 'f');
  assert.equal(decrypt(blob, aad, after).toString(), 'secret');
  assert.equal(encrypt(Buffer.from('x'), aad, after)[1], 2);
});

test('key validation is strict: base64 alphabet, padding, exactly 32 bytes', () => {
  assert.throws(() => decodeKey('not base64!'), MissingKeyError);
  assert.throws(() => decodeKey(Buffer.alloc(16).toString('base64')), /32 bytes, got 16/);
  assert.throws(() => decodeKey('AAAA'), /got 3/);
  assert.throws(() => keyringFromContent('{"active": 3, "keys": {"1": "' + TEST_KEY + '"}}', 'f'), /active key id 3/);
  assert.throws(() => keyringFromContent('{nope', 'f'), /not valid JSON/);
  assert.equal(decodeKey(TEST_KEY).length, 32);
});

test('env resolution: file wins over env var; nothing set refuses to boot', () => {
  resetKeyringCache();
  const dir = mkdtempSync(join(tmpdir(), 'wagw-'));
  const file = join(dir, 'key.json');
  writeFileSync(file, JSON.stringify({ active: 2, keys: { '1': TEST_KEY, '2': Buffer.alloc(32, 9).toString('base64') } }));
  const fromFile = loadKeyring({ refresh: true, env: { USERBOT_MASTER_KEY_FILE: file, USERBOT_MASTER_KEY: TEST_KEY } });
  assert.equal(fromFile.activeId, 2);
  const fromEnv = loadKeyring({ refresh: true, env: { USERBOT_MASTER_KEY: TEST_KEY } });
  assert.equal(fromEnv.activeId, 1);
  assert.throws(() => loadKeyring({ refresh: true, env: {} }), MissingKeyError);
  assert.throws(() => loadKeyring({ refresh: true, env: { USERBOT_MASTER_KEY_FILE: join(dir, 'missing') } }), /cannot read/);
  resetKeyringCache();
});
