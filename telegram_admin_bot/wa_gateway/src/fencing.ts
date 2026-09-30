/**
 * Every decision that keeps "one live socket per account" true, as pure
 * functions over plain data, so they are unit-tested without Postgres or
 * WhatsApp. The gateway (gateway.ts) reads the row, calls these, acts.
 *
 * Vocabulary (mirrors leasing.py): Python takes the lease (bumping
 * lease_epoch) BEFORE it asks us to open. The epoch in every socket-bound
 * command must equal the row's current lease_epoch, or the caller is a
 * stale worker and gets stale_epoch.
 */

export type SessionRow = {
  channel: string;
  is_active: boolean;
  lease_epoch: number;
  /** lease_expires_at > now() evaluated by Postgres, so clocks agree. */
  live: boolean;
  /** Epoch milliseconds, null when the lease was released. */
  lease_expires_at_ms: number | null;
};

export type SocketState = 'connecting' | 'open' | 'closed';

export type ExistingSocket = { epoch: number; state: SocketState };

export type OpenDecision =
  | { action: 'reject'; kind: 'stale_epoch' | 'not_found' | 'bad_request'; detail: string }
  | { action: 'idempotent'; state: SocketState }
  | { action: 'replace_then_open' }
  | { action: 'open' };

export const LEASE_SECONDS = 30;
export const WATCHDOG_INTERVAL_MS = 10_000;
/** Postgres silent for this long -> close everything (leasing.DANGER_SECONDS). */
export const DANGER_SECONDS = 22;

export function decideOpen(input: {
  row: SessionRow | null;
  requestedEpoch: number;
  existing: ExistingSocket | null;
  hasCreds: boolean;
}): OpenDecision {
  const { row, requestedEpoch, existing, hasCreds } = input;
  if (!Number.isInteger(requestedEpoch) || requestedEpoch < 0) {
    return { action: 'reject', kind: 'bad_request', detail: 'epoch must be a non-negative integer' };
  }
  if (!row) return { action: 'reject', kind: 'not_found', detail: 'no such session' };
  if (row.channel !== 'whatsapp') {
    return { action: 'reject', kind: 'bad_request', detail: `session channel is ${row.channel}, not whatsapp` };
  }
  if (!row.is_active) return { action: 'reject', kind: 'stale_epoch', detail: 'session is not active' };
  if (!row.live) return { action: 'reject', kind: 'stale_epoch', detail: 'no live lease on the session row' };
  if (row.lease_epoch !== requestedEpoch) {
    return {
      action: 'reject',
      kind: 'stale_epoch',
      detail: `lease epoch is ${row.lease_epoch}, command carries ${requestedEpoch}`,
    };
  }
  if (existing) {
    if (existing.epoch > requestedEpoch) {
      return { action: 'reject', kind: 'stale_epoch', detail: `a socket with epoch ${existing.epoch} already exists` };
    }
    if (existing.epoch === requestedEpoch) return { action: 'idempotent', state: existing.state };
  }
  if (!hasCreds) return { action: 'reject', kind: 'not_found', detail: 'session has no WhatsApp credentials; pair first' };
  return existing ? { action: 'replace_then_open' } : { action: 'open' };
}

/** close: only the current (or a newer) epoch may close a socket. */
export function decideClose(requestedEpoch: number, existing: ExistingSocket | null): boolean {
  if (!existing) return false;
  return requestedEpoch >= existing.epoch;
}

export type WatchdogVerdict = { close: false } | { close: true; reason: string };

/**
 * Applied to every open socket every WATCHDOG_INTERVAL_MS with a fresh row.
 * `nowMs` is the gateway's clock; `row.live` came from Postgres' clock, and
 * the 30 s grace uses lease_expires_at, which Python wrote with Postgres'
 * now(), so a skewed gateway clock cannot make us close early: Python's own
 * keeper self-fences 22 s after its last confirmed renewal, i.e. before
 * the lease even expires.
 */
export function watchdogVerdict(input: { row: SessionRow | null; socketEpoch: number; nowMs: number }): WatchdogVerdict {
  const { row, socketEpoch, nowMs } = input;
  if (!row) return { close: true, reason: 'session row disappeared' };
  if (row.channel !== 'whatsapp') return { close: true, reason: `channel changed to ${row.channel}` };
  if (!row.is_active) return { close: true, reason: 'session deactivated' };
  if (row.lease_epoch !== socketEpoch) {
    return { close: true, reason: `lease epoch moved from ${socketEpoch} to ${row.lease_epoch}` };
  }
  if (row.live) return { close: false };
  if (row.lease_expires_at_ms === null) return { close: true, reason: 'lease released' };
  const expiredForMs = nowMs - row.lease_expires_at_ms;
  if (expiredForMs >= LEASE_SECONDS * 1000) {
    return { close: true, reason: `lease expired ${Math.round(expiredForMs / 1000)}s ago without renewal` };
  }
  return { close: false };
}

/** Postgres unreachable: after DANGER_SECONDS we can no longer prove any lease. */
export function postgresSilentTooLong(lastOkMs: number, nowMs: number): boolean {
  return nowMs - lastOkMs >= DANGER_SECONDS * 1000;
}
