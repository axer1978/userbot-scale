/**
 * What a closed connection means. Baileys reports the close cause as a Boom
 * error whose statusCode is a DisconnectReason. Pure so it is unit-tested
 * without a socket.
 *
 *   fatal      -> do NOT reconnect; emit session_lost. The linked device is
 *                 gone (logged out / replaced / rejected). Reconnecting here
 *                 is exactly what produces connectionReplaced loops and bans.
 *   restart    -> WhatsApp asked for a fresh socket (515); reconnect at once.
 *   transient  -> network-level drop; reconnect with backoff.
 */

export type SessionLostReason = 'loggedOut' | 'forbidden' | 'badSession' | 'connectionReplaced' | 'multideviceMismatch';

export type DisconnectVerdict =
  | { kind: 'fatal'; reason: SessionLostReason; code: number }
  | { kind: 'restart'; code: number }
  | { kind: 'transient'; code: number | undefined; reason: string };

const FATAL: Record<number, SessionLostReason> = {
  401: 'loggedOut',
  403: 'forbidden',
  500: 'badSession',
  440: 'connectionReplaced',
  411: 'multideviceMismatch',
};

export function statusCodeOf(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const output = (error as { output?: { statusCode?: unknown } }).output;
  const code = output?.statusCode;
  return typeof code === 'number' ? code : undefined;
}

export function classifyDisconnect(error: unknown): DisconnectVerdict {
  const code = statusCodeOf(error);
  if (code !== undefined) {
    const fatal = FATAL[code];
    if (fatal) return { kind: 'fatal', reason: fatal, code };
    if (code === 515) return { kind: 'restart', code };
  }
  const message = error instanceof Error ? error.message : String(error ?? 'unknown');
  // 428 connectionClosed, 408 connectionLost/timedOut, 503 unavailableService,
  // and anything unrecognised (a raw socket error without a status code).
  return { kind: 'transient', code, reason: message };
}

/** Exponential backoff: 2s, 4s, ... capped at 60s. */
export function backoffMs(attempt: number, baseMs = 2_000, maxMs = 60_000): number {
  const exp = Math.min(attempt, 30);
  return Math.min(maxMs, baseMs * 2 ** exp);
}

/** ±20 % so every socket of a fleet does not reconnect in the same second. */
export function jitterMs(ms: number, rng: () => number = Math.random): number {
  return Math.round(ms * (0.8 + 0.4 * rng()));
}
