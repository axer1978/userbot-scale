/**
 * Baileys auth state (creds + signal keys) in Postgres, encrypted per row
 * with crypto.ts, i.e. exactly like every other secret in the fleet. No
 * session directory on disk, ever.
 *
 * Row: wa_auth_state(session_id, kind, key_id, value_enc)
 *   kind    = 'creds' (key_id '') or a SignalDataTypeMap key
 *   value   = UTF-8 JSON serialised with Baileys' BufferJSON replacer
 *   AAD     = "<session_id>:wa_auth:<kind>:<key_id>"
 *
 * Semantics follow useMultiFileAuthState: a null value in `set` deletes the
 * row, 'app-state-sync-key' values are revived into the protobuf class,
 * `clear` drops the keys (not the creds). Multi-key sets are one
 * transaction so a crash cannot leave half a signal session behind.
 */
import { BufferJSON, initAuthCreds, makeCacheableSignalKeyStore, proto } from 'baileys';
import type {
  AuthenticationCreds,
  AuthenticationState,
  SignalDataSet,
  SignalDataTypeMap,
  SignalKeyStore,
} from 'baileys';
import type pino from 'pino';
import { aadFor, decrypt, encrypt, loadKeyring, type Keyring } from './crypto.ts';
import type { Pool } from './db.ts';

export const CREDS_KIND = 'creds';

export function authAad(sessionId: string, kind: string, keyId: string): Buffer {
  return aadFor(sessionId, `wa_auth:${kind}:${keyId}`);
}

export function serialize(value: unknown): Buffer {
  return Buffer.from(JSON.stringify(value, BufferJSON.replacer), 'utf8');
}

export function deserialize<T = unknown>(buf: Buffer): T {
  return JSON.parse(buf.toString('utf8'), BufferJSON.reviver) as T;
}

const UPSERT_SQL = `
INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc)
VALUES ($1, $2, $3, $4)
ON CONFLICT (session_id, kind, key_id)
DO UPDATE SET value_enc = EXCLUDED.value_enc, updated_at = now()`;

export class PostgresAuthStore {
  private readonly keyring: Keyring;
  private readonly pool: Pool;
  readonly sessionId: string;

  constructor(pool: Pool, sessionId: string, keyring?: Keyring) {
    this.pool = pool;
    this.sessionId = sessionId;
    this.keyring = keyring ?? loadKeyring();
  }

  private encryptValue(kind: string, keyId: string, value: unknown): Buffer {
    return encrypt(serialize(value), authAad(this.sessionId, kind, keyId), this.keyring);
  }

  private decryptValue<T>(kind: string, keyId: string, blob: Buffer): T {
    return deserialize<T>(decrypt(blob, authAad(this.sessionId, kind, keyId), this.keyring));
  }

  async readCreds(): Promise<AuthenticationCreds | null> {
    const res = await this.pool.query<{ value_enc: Buffer }>(
      'SELECT value_enc FROM wa_auth_state WHERE session_id = $1 AND kind = $2 AND key_id = $3',
      [this.sessionId, CREDS_KIND, ''],
    );
    const row = res.rows[0];
    if (!row) return null;
    return this.decryptValue<AuthenticationCreds>(CREDS_KIND, '', row.value_enc);
  }

  async writeCreds(creds: AuthenticationCreds): Promise<void> {
    await this.pool.query(UPSERT_SQL, [this.sessionId, CREDS_KIND, '', this.encryptValue(CREDS_KIND, '', creds)]);
  }

  async getKeys<T extends keyof SignalDataTypeMap>(type: T, ids: string[]): Promise<{ [id: string]: SignalDataTypeMap[T] }> {
    const out: { [id: string]: SignalDataTypeMap[T] } = {};
    if (ids.length === 0) return out;
    const res = await this.pool.query<{ key_id: string; value_enc: Buffer }>(
      'SELECT key_id, value_enc FROM wa_auth_state WHERE session_id = $1 AND kind = $2 AND key_id = ANY($3::text[])',
      [this.sessionId, type, ids],
    );
    for (const row of res.rows) {
      let value = this.decryptValue<unknown>(type, row.key_id, row.value_enc);
      if (type === 'app-state-sync-key' && value) {
        value = proto.Message.AppStateSyncKeyData.fromObject(value as Record<string, unknown>);
      }
      out[row.key_id] = value as SignalDataTypeMap[T];
    }
    return out;
  }

  async setKeys(data: SignalDataSet): Promise<void> {
    const writes: Array<[kind: string, keyId: string, blob: Buffer | null]> = [];
    for (const [kind, entries] of Object.entries(data)) {
      if (!entries) continue;
      for (const [keyId, value] of Object.entries(entries)) {
        writes.push([kind, keyId, value === null || value === undefined ? null : this.encryptValue(kind, keyId, value)]);
      }
    }
    if (writes.length === 0) return;
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      for (const [kind, keyId, blob] of writes) {
        if (blob === null) {
          await client.query('DELETE FROM wa_auth_state WHERE session_id = $1 AND kind = $2 AND key_id = $3', [
            this.sessionId,
            kind,
            keyId,
          ]);
        } else {
          await client.query(UPSERT_SQL, [this.sessionId, kind, keyId, blob]);
        }
      }
      await client.query('COMMIT');
    } catch (exc) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw exc;
    } finally {
      client.release();
    }
  }

  /** Baileys' `keys.clear`: every signal key, creds untouched. */
  async clearKeys(): Promise<void> {
    await this.pool.query('DELETE FROM wa_auth_state WHERE session_id = $1 AND kind <> $2', [this.sessionId, CREDS_KIND]);
  }

  /** Re-pair: forget the linked device entirely. */
  async wipeAll(): Promise<void> {
    await this.pool.query('DELETE FROM wa_auth_state WHERE session_id = $1', [this.sessionId]);
  }

  asSignalKeyStore(): SignalKeyStore {
    return {
      get: (type, ids) => this.getKeys(type, ids),
      set: (data) => this.setKeys(data),
      clear: () => this.clearKeys(),
    };
  }
}

/**
 * Whether these creds belong to a linked device. Baileys sets `registered`
 * only on the pairing-code path; a QR link never does, so a linked device is
 * also one that WhatsApp gave an identity (`me`) and a signed device
 * identity (`account`), which is what pair-success writes on both paths.
 */
export function isLinked(creds: AuthenticationCreds): boolean {
  return creds.registered || (!!creds.me?.id && !!creds.account);
}

export type PostgresAuthState = {
  state: AuthenticationState;
  saveCreds: () => Promise<void>;
  store: PostgresAuthStore;
  /** False when no creds row existed and fresh (unregistered) creds were made. */
  existed: boolean;
};

/**
 * Loads (or, for pairing, initialises) the auth state. Baileys mutates
 * `state.creds` in place and fires creds.update; saveCreds persists the
 * whole object, like useMultiFileAuthState does.
 */
export async function usePostgresAuthState(pool: Pool, sessionId: string, logger: pino.Logger, keyring?: Keyring): Promise<PostgresAuthState> {
  const store = new PostgresAuthStore(pool, sessionId, keyring);
  const existing = await store.readCreds();
  const creds = existing ?? initAuthCreds();
  const state: AuthenticationState = {
    creds,
    keys: makeCacheableSignalKeyStore(store.asSignalKeyStore(), logger),
  };
  return { state, saveCreds: () => store.writeCreds(creds), store, existed: existing !== null };
}
