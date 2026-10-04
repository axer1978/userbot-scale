/**
 * Log hygiene (security audit): key material and pairing credentials never
 * reach a log line, whatever an operator sets WA_GATEWAY_LOG_LEVEL to, and
 * Baileys' own logger stays capped at warn unless debug/trace is asked for.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { Writable } from 'node:stream';
import { baileysLogger, levelFromEnv, makeLogger } from '../src/log.ts';

function capture(): { lines: string[]; stream: Writable } {
  const lines: string[] = [];
  const stream = new Writable({
    write(chunk, _enc, cb) {
      lines.push(chunk.toString());
      cb();
    },
  });
  return { lines, stream };
}

const SECRET = 'S3CR3T-KEY-BYTES';

test('key material is redacted at every level, on the gateway logger and the Baileys child', () => {
  const { lines, stream } = capture();
  const root = makeLogger('trace', stream);
  const creds = {
    noiseKey: { private: SECRET, public: 'pub' },
    pairingEphemeralKeyPair: { private: SECRET, public: 'pub' },
    signedIdentityKey: { private: SECRET, public: 'pub' },
    signedPreKey: { keyPair: { private: SECRET, public: 'pub' }, signature: 'sig', keyId: 1 },
    advSecretKey: SECRET,
    me: { id: '1@s.whatsapp.net' },
  };
  root.info({ creds }, 'creds');
  root.debug({ keys: { 'pre-key': { 1: { private: SECRET } } } }, 'keys');
  root.trace({ node: { attrs: { privKey: SECRET } } }, 'node');
  const baileys = baileysLogger('wa1', root);
  baileys.trace({ qr: SECRET, pairingCode: SECRET, privKey: SECRET }, 'pairing');
  baileys.warn({ creds, update: { noiseKey: { private: SECRET } } }, 'creds update');
  assert.equal(lines.length, 5);
  for (const line of lines) {
    assert.ok(!line.includes(SECRET), line);
    assert.ok(line.includes('[redacted]'), line);
  }
  // The rest of the line is still there for debugging.
  assert.ok(lines[0]?.includes('"msg":"creds"'));
});

test('Baileys gets warn unless the gateway itself is at debug or trace', () => {
  const { stream } = capture();
  assert.equal(baileysLogger('wa1', makeLogger('info', stream)).level, 'warn');
  assert.equal(baileysLogger('wa1', makeLogger('error', stream)).level, 'warn');
  assert.equal(baileysLogger('wa1', makeLogger('debug', stream)).level, 'debug');
  assert.equal(baileysLogger('wa1', makeLogger('trace', stream)).level, 'trace');
});

test('an unknown level falls back to info', () => {
  assert.equal(levelFromEnv({ WA_GATEWAY_LOG_LEVEL: 'verbose' }), 'info');
  assert.equal(levelFromEnv({ WA_GATEWAY_LOG_LEVEL: ' DEBUG ' }), 'debug');
  assert.equal(levelFromEnv({}), 'info');
});
