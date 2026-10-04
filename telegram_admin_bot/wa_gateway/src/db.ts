/**
 * The gateway's Postgres access. Two tables of its own (wa_auth_state,
 * wa_inbox: migration 0006) and read-only access to telegram_sessions for
 * lease fencing. tenant_id on the gateway's tables is filled by a BEFORE
 * INSERT trigger from the session row, so the gateway never writes it.
 */
import pg from 'pg';
import { SINGLETON_LOCK_KEY } from './config.ts';
import type { SessionRow } from './fencing.ts';

export type Pool = pg.Pool;

/** BIGINT comes back as a string from node-postgres; epochs fit a double. */
pg.types.setTypeParser(20, (v) => Number(v));

export function createPool(databaseUrl: string, opts: { max?: number } = {}): Pool {
  return new pg.Pool({
    connectionString: databaseUrl,
    max: opts.max ?? 5,
    connectionTimeoutMillis: 5_000,
    idleTimeoutMillis: 30_000,
    // A hung query must not outlive the watchdog's danger window.
    query_timeout: 15_000,
    statement_timeout: 15_000,
  });
}

/**
 * The singleton guarantee: one gateway per Postgres. The lock is session
 * scoped, so the client that took it is kept for the life of the process
 * (returned here, never released to the pool). null => another gateway is
 * running; the caller logs and exits non-zero.
 */
export async function acquireSingletonLock(pool: Pool): Promise<pg.PoolClient | null> {
  const client = await pool.connect();
  try {
    const res = await client.query<{ ok: boolean }>('SELECT pg_try_advisory_lock($1) AS ok', [SINGLETON_LOCK_KEY]);
    if (res.rows[0]?.ok) return client;
    client.release();
    return null;
  } catch (exc) {
    client.release(true);
    throw exc;
  }
}

const SESSION_ROW_SQL = `
SELECT channel, is_active, lease_epoch,
       lease_expires_at IS NOT NULL AND lease_expires_at > now() AS live,
       EXTRACT(EPOCH FROM lease_expires_at) * 1000 AS lease_expires_at_ms
  FROM telegram_sessions
 WHERE session_id = $1`;

export async function readSessionRow(pool: Pool, sessionId: string): Promise<SessionRow | null> {
  const res = await pool.query(SESSION_ROW_SQL, [sessionId]);
  const row = res.rows[0];
  if (!row) return null;
  return {
    channel: String(row.channel),
    is_active: Boolean(row.is_active),
    lease_epoch: Number(row.lease_epoch),
    live: Boolean(row.live),
    lease_expires_at_ms: row.lease_expires_at_ms === null ? null : Number(row.lease_expires_at_ms),
  };
}

export async function hasCreds(pool: Pool, sessionId: string): Promise<boolean> {
  const res = await pool.query('SELECT 1 FROM wa_auth_state WHERE session_id = $1 AND kind = $2 AND key_id = $3', [
    sessionId,
    'creds',
    '',
  ]);
  return (res.rowCount ?? 0) > 0;
}

/** Everything Baileys knows about this linked device. Re-pair = new device. */
export async function wipeAuthState(pool: Pool, sessionId: string): Promise<number> {
  const res = await pool.query('DELETE FROM wa_auth_state WHERE session_id = $1', [sessionId]);
  return res.rowCount ?? 0;
}

/** Cheap liveness probe for the watchdog when no sockets are open. */
export async function ping(pool: Pool): Promise<void> {
  await pool.query('SELECT 1');
}
