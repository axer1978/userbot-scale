/**
 * AES-256-GCM blobs byte-compatible with the Python side (crypto.py).
 *
 *   blob = [version=1][key id 1..255][12-byte nonce][ciphertext || 16-byte tag]
 *   AAD  = utf8 "<session_id>:<field>"
 *
 * The key comes from USERBOT_MASTER_KEY_FILE (a JSON keyring
 * {"active": id, "keys": {"id": "<base64>"}} or a bare base64 key => id 1),
 * which wins over USERBOT_MASTER_KEY (base64, id 1). Keys are exactly 32
 * bytes. There is no key generation here; the process refuses to boot
 * without one, exactly like crypto.py.
 */
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';

export const BLOB_VERSION = 1;
export const NONCE_BYTES = 12;
export const TAG_BYTES = 16;
export const KEY_BYTES = 32;

const ENV_KEY_FILE = 'USERBOT_MASTER_KEY_FILE';
const ENV_KEY = 'USERBOT_MASTER_KEY';

export class CryptoError extends Error {}
export class MissingKeyError extends CryptoError {}
export class DecryptError extends CryptoError {}

export type Keyring = {
  readonly activeId: number;
  readonly keys: ReadonlyMap<number, Buffer>;
};

let cache: Keyring | null = null;

/** Strict base64 (like Python's b64decode(validate=True)): alphabet only, proper padding. */
export function decodeKey(b64: string): Buffer {
  const s = b64.trim();
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s) || s.length % 4 !== 0) {
    throw new MissingKeyError('master key is not valid base64');
  }
  const raw = Buffer.from(s, 'base64');
  if (raw.toString('base64') !== s) {
    throw new MissingKeyError('master key is not valid (canonical) base64');
  }
  if (raw.length !== KEY_BYTES) {
    throw new MissingKeyError(`master key must decode to ${KEY_BYTES} bytes, got ${raw.length}`);
  }
  return raw;
}

export function keyringFromContent(content: string, source: string): Keyring {
  const text = content.trim();
  if (text.startsWith('{')) {
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch (exc) {
      throw new MissingKeyError(`${source} is not valid JSON: ${(exc as Error).message}`);
    }
    const obj = data as { active?: unknown; keys?: unknown };
    const active = Number(obj.active);
    const keysObj = obj.keys;
    if (!Number.isInteger(active) || typeof keysObj !== 'object' || keysObj === null) {
      throw new MissingKeyError(`${source} must be {"active": <id>, "keys": {"<id>": "<b64>"}}`);
    }
    const keys = new Map<number, Buffer>();
    for (const [k, v] of Object.entries(keysObj as Record<string, unknown>)) {
      const id = Number(k);
      if (!Number.isInteger(id) || typeof v !== 'string') {
        throw new MissingKeyError(`${source} must be {"active": <id>, "keys": {"<id>": "<b64>"}}`);
      }
      keys.set(id, decodeKey(v));
    }
    if (!keys.has(active)) {
      throw new MissingKeyError(`${source}: active key id ${active} not present in keys`);
    }
    return { activeId: active, keys };
  }
  return { activeId: 1, keys: new Map([[1, decodeKey(text)]]) };
}

export function loadKeyring(opts: { refresh?: boolean; env?: NodeJS.ProcessEnv } = {}): Keyring {
  if (cache && !opts.refresh) return cache;
  const env = opts.env ?? process.env;
  const keyFile = env[ENV_KEY_FILE];
  if (keyFile) {
    let content: string;
    try {
      content = readFileSync(keyFile, 'utf8');
    } catch (exc) {
      throw new MissingKeyError(`cannot read ${ENV_KEY_FILE}=${JSON.stringify(keyFile)}: ${(exc as Error).message}`);
    }
    cache = keyringFromContent(content, keyFile);
    return cache;
  }
  const keyEnv = env[ENV_KEY];
  if (keyEnv) {
    cache = { activeId: 1, keys: new Map([[1, decodeKey(keyEnv)]]) };
    return cache;
  }
  throw new MissingKeyError(
    `set ${ENV_KEY_FILE} (path to a key file) or ${ENV_KEY} (base64 key) before starting this process. ` +
      'There is no key-generation path in this codebase; ops must supply the key material.',
  );
}

/** Tests only. */
export function resetKeyringCache(): void {
  cache = null;
}

export function aadFor(sessionId: string, field: string): Buffer {
  return Buffer.from(`${sessionId}:${field}`, 'utf8');
}

/**
 * Internal: encrypt with a caller-supplied nonce. Only for the golden-vector
 * tests (a fixed nonce makes the output reproducible). Production code must
 * call `encrypt`, which draws a fresh random nonce every time.
 */
export function encryptWithNonce(plaintext: Buffer, aad: Buffer, nonce: Buffer, keyring: Keyring = loadKeyring()): Buffer {
  const keyId = keyring.activeId;
  if (!(keyId >= 1 && keyId <= 255)) throw new CryptoError(`key id ${keyId} does not fit in one byte`);
  if (nonce.length !== NONCE_BYTES) throw new CryptoError(`nonce must be ${NONCE_BYTES} bytes`);
  const key = keyring.keys.get(keyId);
  if (!key) throw new CryptoError(`active key id ${keyId} missing from keyring`);
  const cipher = createCipheriv('aes-256-gcm', key, nonce, { authTagLength: TAG_BYTES });
  cipher.setAAD(aad);
  const body = Buffer.concat([cipher.update(plaintext), cipher.final(), cipher.getAuthTag()]);
  return Buffer.concat([Buffer.from([BLOB_VERSION, keyId]), nonce, body]);
}

export function encrypt(plaintext: Buffer, aad: Buffer, keyring: Keyring = loadKeyring()): Buffer {
  return encryptWithNonce(plaintext, aad, randomBytes(NONCE_BYTES), keyring);
}

export function decrypt(blob: Buffer, aad: Buffer, keyring: Keyring = loadKeyring()): Buffer {
  if (blob.length < 2 + NONCE_BYTES + TAG_BYTES) throw new DecryptError('ciphertext blob is too short to be valid');
  const version = blob[0];
  const keyId = blob[1] as number;
  if (version !== BLOB_VERSION) throw new DecryptError(`unknown blob version ${version}`);
  const key = keyring.keys.get(keyId);
  if (!key) throw new DecryptError(`unknown key id ${keyId} (key not present in the keyring)`);
  const nonce = blob.subarray(2, 2 + NONCE_BYTES);
  const body = blob.subarray(2 + NONCE_BYTES);
  const ciphertext = body.subarray(0, body.length - TAG_BYTES);
  const tag = body.subarray(body.length - TAG_BYTES);
  const decipher = createDecipheriv('aes-256-gcm', key, nonce, { authTagLength: TAG_BYTES });
  decipher.setAAD(aad);
  decipher.setAuthTag(tag);
  try {
    return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
  } catch {
    throw new DecryptError('authentication failed (wrong key, AAD, or tampered ciphertext)');
  }
}

export function encryptText(text: string, aad: Buffer, keyring?: Keyring): Buffer {
  return encrypt(Buffer.from(text, 'utf8'), aad, keyring);
}

export function decryptText(blob: Buffer, aad: Buffer, keyring?: Keyring): string {
  return decrypt(blob, aad, keyring).toString('utf8');
}
