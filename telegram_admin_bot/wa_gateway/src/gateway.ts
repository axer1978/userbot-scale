/**
 * The command handlers and the socket registry. See README for the wire
 * format. Everything that decides is in fencing.ts (pure); this file reads
 * rows, calls those decisions and drives sockets.
 */
import type pino from 'pino';
import { browserFor, parseBrowserTuple, type BrowserTuple } from './browser.ts';
import { GatewayError, type Command } from './bus.ts';
import { hasCreds, ping, readSessionRow, wipeAuthState, type Pool } from './db.ts';
import { decideClose, decideOpen, postgresSilentTooLong, watchdogVerdict, WATCHDOG_INTERVAL_MS, type SocketState } from './fencing.ts';
import { InboxWriter } from './inbox.ts';
import { PairingRun, type PairEvent, type PairMethod } from './pairing.ts';
import { PRESENCE_STATES, SessionSocket, type SessionEvent, type SessionSocketOptions } from './session.ts';

export type Publish = (channel: string, payload: unknown) => Promise<boolean>;

export type StatusEntry = { session_id: string; epoch: number; state: SocketState; lost: string | null; me: unknown; inbox_pending: number };

export const LOGOUT_TIMEOUT_MS = 25_000;
export const MAX_TEXT_LENGTH = 65_536;
export const MAX_READ_IDS = 500;

function str(args: Record<string, unknown>, name: string, max = 128): string {
  const v = args[name];
  if (typeof v !== 'string' || v.length === 0 || v.length > max) throw new GatewayError('bad_request', `${name} must be a non-empty string`);
  return v;
}

function int(args: Record<string, unknown>, name: string): number {
  const v = args[name];
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 0) throw new GatewayError('bad_request', `${name} must be a non-negative integer`);
  return v;
}

function jidArg(args: Record<string, unknown>, name = 'jid'): string {
  const v = str(args, name);
  if (!/^[0-9A-Za-z._:-]+@(s\.whatsapp\.net|lid)$/.test(v)) throw new GatewayError('bad_request', `${name} must be a 1:1 chat jid (…@s.whatsapp.net or …@lid)`);
  return v;
}

function browserArg(args: Record<string, unknown>): BrowserTuple {
  const b = parseBrowserTuple(args.browser);
  if (!b) throw new GatewayError('bad_request', 'browser must be [string, string, string]');
  return b;
}

export class Gateway {
  private readonly sessions = new Map<string, SessionSocket>();
  private readonly pairings = new Map<string, PairingRun>();
  private readonly pairingBySession = new Map<string, string>();
  private readonly inboxes = new Map<string, InboxWriter>();
  private watchdog: NodeJS.Timeout | null = null;
  private lastPgOkMs = Date.now();
  private stopping = false;
  private readonly pool: Pool;
  private readonly publish: Publish;
  private readonly log: pino.Logger;
  private readonly now: () => number;
  private readonly makeSocket: SessionSocketOptions['makeSocket'];

  constructor(pool: Pool, publish: Publish, log: pino.Logger, now: () => number = Date.now, opts: { makeSocket?: SessionSocketOptions['makeSocket'] } = {}) {
    this.pool = pool;
    this.publish = publish;
    this.log = log;
    this.now = now;
    this.makeSocket = opts.makeSocket;
  }

  async handle(command: Command): Promise<unknown> {
    const { action, args } = command;
    if (this.stopping) throw new GatewayError('busy', 'gateway is shutting down');
    switch (action) {
      case 'pair':
        return this.pair(args);
      case 'pair_cancel':
        return this.pairCancel(args);
      case 'open':
        return this.open(args);
      case 'close':
        return this.close(args);
      case 'status':
        return this.status();
      case 'send_text':
        return this.sendText(args);
      case 'read':
        return this.read(args);
      case 'presence':
        return this.presence(args);
      case 'logout':
        return this.logout(args);
      default:
        throw new GatewayError('bad_request', `unknown action ${JSON.stringify(action)}`);
    }
  }

  // ---------------------------------------------------------------- pairing

  private async pair(args: Record<string, unknown>): Promise<{ started: true }> {
    const sessionId = str(args, 'session_id');
    const pairId = str(args, 'pair_id');
    const method = args.method;
    if (method !== 'qr' && method !== 'code') throw new GatewayError('bad_request', 'method must be "qr" or "code"');
    const phone = typeof args.phone === 'string' ? args.phone : undefined;
    if (method === 'code' && !phone) throw new GatewayError('bad_request', 'phone is required for method "code"');
    const tuple = browserArg(args);

    const existing = this.sessions.get(sessionId);
    if (existing && existing.state !== 'closed') throw new GatewayError('busy', 'session has an open socket; close it first');
    if (this.pairingBySession.has(sessionId)) throw new GatewayError('busy', 'a pairing is already running for this session');
    if (this.pairings.has(pairId)) throw new GatewayError('busy', 'pair_id already in use');
    const row = await this.pgRead(() => readSessionRow(this.pool, sessionId));
    if (!row) throw new GatewayError('not_found', 'no such session');
    if (row.live) throw new GatewayError('busy', 'session has a live lease; stop its runtime before re-pairing');
    if (existing) this.sessions.delete(sessionId);

    const run = new PairingRun({
      pool: this.pool,
      sessionId,
      pairId,
      method: method as PairMethod,
      phone,
      browser: tuple,
      log: this.log,
      emit: (event: PairEvent) => void this.publish(`wa:pair:${pairId}`, event),
    });
    this.pairings.set(pairId, run);
    this.pairingBySession.set(sessionId, pairId);
    void run.run().finally(() => {
      this.pairings.delete(pairId);
      if (this.pairingBySession.get(sessionId) === pairId) this.pairingBySession.delete(sessionId);
    });
    return { started: true };
  }

  private pairCancel(args: Record<string, unknown>): { cancelled: true } {
    const pairId = str(args, 'pair_id');
    const run = this.pairings.get(pairId);
    if (!run) throw new GatewayError('not_found', 'no such pairing (already finished?)');
    run.cancel('cancelled by operator');
    return { cancelled: true };
  }

  // ---------------------------------------------------------------- sockets

  private inboxFor(sessionId: string): InboxWriter {
    let writer = this.inboxes.get(sessionId);
    if (!writer) {
      writer = new InboxWriter(this.pool, sessionId, this.publish, this.log);
      this.inboxes.set(sessionId, writer);
    }
    return writer;
  }

  private newSession(sessionId: string, epoch: number, browser: BrowserTuple, quiet = false): SessionSocket {
    return new SessionSocket({
      pool: this.pool,
      sessionId,
      epoch,
      browser,
      log: this.log,
      quiet,
      makeSocket: this.makeSocket,
      emit: (event: SessionEvent) => void this.publish(`wa:ev:${sessionId}`, event),
      onMessage: (payload) => this.inboxFor(sessionId).enqueue(payload),
    });
  }

  private async open(args: Record<string, unknown>): Promise<{ state: SocketState | 'opening' }> {
    const sessionId = str(args, 'session_id');
    const epoch = int(args, 'epoch');
    let tuple = parseBrowserTuple(args.browser);
    if (!tuple) {
      tuple = browserFor(sessionId);
      this.log.warn({ session_id: sessionId, browser: tuple }, 'open without a valid browser tuple; using the derived one');
    }
    if (this.pairingBySession.has(sessionId)) throw new GatewayError('busy', 'a pairing is running for this session');

    const row = await this.pgRead(() => readSessionRow(this.pool, sessionId));
    const existing = this.sessions.get(sessionId) ?? null;
    const creds = row ? await this.pgRead(() => hasCreds(this.pool, sessionId)) : false;
    const decision = decideOpen({
      row,
      requestedEpoch: epoch,
      existing: existing ? { epoch: existing.epoch, state: existing.state } : null,
      hasCreds: creds,
    });
    switch (decision.action) {
      case 'reject':
        throw new GatewayError(decision.kind, decision.detail);
      case 'idempotent':
        if (existing?.lost) throw new GatewayError('session_lost', `${existing.lost}; re-pair the number`);
        return { state: decision.state };
      case 'replace_then_open':
        this.log.warn({ session_id: sessionId, old_epoch: existing!.epoch, new_epoch: epoch }, 'newer epoch: closing the older socket first');
        await this.dropSession(existing!, 'superseded by a newer lease epoch');
        break;
      case 'open':
        break;
    }

    const session = this.newSession(sessionId, epoch, tuple);
    this.sessions.set(sessionId, session);
    try {
      await session.start();
    } catch (exc) {
      this.sessions.delete(sessionId);
      throw new GatewayError('not_found', (exc as Error).message);
    }
    return { state: 'opening' };
  }

  private async close(args: Record<string, unknown>): Promise<{ closed: boolean }> {
    const sessionId = str(args, 'session_id');
    const epoch = int(args, 'epoch');
    const existing = this.sessions.get(sessionId) ?? null;
    if (!decideClose(epoch, existing ? { epoch: existing.epoch, state: existing.state } : null)) return { closed: false };
    await this.dropSession(existing!, `close command (epoch ${epoch})`);
    return { closed: true };
  }

  status(): StatusEntry[] {
    return [...this.sessions.values()].map((s) => ({
      session_id: s.sessionId,
      epoch: s.epoch,
      state: s.state,
      lost: s.lost,
      me: s.me,
      inbox_pending: this.inboxes.get(s.sessionId)?.pending ?? 0,
    }));
  }

  private async dropSession(session: SessionSocket, reason: string): Promise<void> {
    if (this.sessions.get(session.sessionId) === session) this.sessions.delete(session.sessionId);
    await session.close(reason);
  }

  // ------------------------------------------------------------- primitives

  /**
   * Fencing for socket-bound primitives: the socket must exist under
   * exactly this epoch (else stale_epoch) and be open (else not_connected).
   */
  private fencedSocket(args: Record<string, unknown>): SessionSocket {
    const sessionId = str(args, 'session_id');
    const epoch = int(args, 'epoch');
    const existing = this.sessions.get(sessionId);
    if (!existing) throw new GatewayError('not_connected', 'no socket for this session');
    if (existing.epoch !== epoch) {
      throw new GatewayError('stale_epoch', `socket epoch is ${existing.epoch}, command carries ${epoch}`);
    }
    if (existing.lost) throw new GatewayError('session_lost', `${existing.lost}; re-pair the number`);
    if (existing.state !== 'open') throw new GatewayError('not_connected', `socket is ${existing.state}`);
    return existing;
  }

  private async sendText(args: Record<string, unknown>): Promise<{ message_id: string; ts: number }> {
    const session = this.fencedSocket(args);
    const jid = jidArg(args);
    const text = str(args, 'text', MAX_TEXT_LENGTH);
    return session.sendText(jid, text);
  }

  private async read(args: Record<string, unknown>): Promise<{ ok: true }> {
    const session = this.fencedSocket(args);
    const jid = jidArg(args);
    const ids = args.message_ids;
    if (!Array.isArray(ids) || ids.length === 0 || ids.length > MAX_READ_IDS || !ids.every((id) => typeof id === 'string' && id.length > 0 && id.length <= 128)) {
      throw new GatewayError('bad_request', `message_ids must be 1..${MAX_READ_IDS} non-empty strings`);
    }
    await session.read(jid, ids as string[]);
    return { ok: true };
  }

  private async presence(args: Record<string, unknown>): Promise<{ ok: true }> {
    const session = this.fencedSocket(args);
    const state = args.state;
    if (typeof state !== 'string' || !PRESENCE_STATES.has(state)) {
      throw new GatewayError('bad_request', 'state must be available | unavailable | composing | paused');
    }
    const chatBound = state === 'composing' || state === 'paused';
    if (chatBound && typeof args.jid !== 'string') throw new GatewayError('bad_request', `state ${state} needs a jid`);
    if (!chatBound && args.jid !== undefined && args.jid !== null) throw new GatewayError('bad_request', `state ${state} takes no jid`);
    await session.presence(state as 'available' | 'unavailable' | 'composing' | 'paused', chatBound ? jidArg(args) : undefined);
    return { ok: true };
  }

  /**
   * Deliberate hard-off. With a socket on this epoch: logout, wipe, drop.
   * Without one: verify the caller's lease like `open`, connect a
   * temporary quiet socket from the stored creds, logout, wipe.
   */
  private async logout(args: Record<string, unknown>): Promise<{ logged_out: boolean }> {
    const sessionId = str(args, 'session_id');
    const epoch = int(args, 'epoch');
    const deadline = new Promise<never>((_, reject) => {
      const t = setTimeout(() => reject(new GatewayError('other', `logout timed out after ${LOGOUT_TIMEOUT_MS} ms`)), LOGOUT_TIMEOUT_MS);
      t.unref();
    });
    return Promise.race([this.logoutInner(sessionId, epoch), deadline]);
  }

  private async logoutInner(sessionId: string, epoch: number): Promise<{ logged_out: boolean }> {
    const existing = this.sessions.get(sessionId);
    if (existing) {
      if (existing.epoch !== epoch) throw new GatewayError('stale_epoch', `socket epoch is ${existing.epoch}, command carries ${epoch}`);
      this.sessions.delete(sessionId);
      const ok = await existing.logout();
      await this.pgRead(() => wipeAuthState(this.pool, sessionId));
      this.log.warn({ session_id: sessionId, epoch, logged_out: ok }, 'HARD-OFF: device unlinked and auth state wiped');
      return { logged_out: ok };
    }
    if (this.pairingBySession.has(sessionId)) throw new GatewayError('busy', 'a pairing is running for this session');
    const row = await this.pgRead(() => readSessionRow(this.pool, sessionId));
    const creds = row ? await this.pgRead(() => hasCreds(this.pool, sessionId)) : false;
    const decision = decideOpen({ row, requestedEpoch: epoch, existing: null, hasCreds: true });
    if (decision.action === 'reject') throw new GatewayError(decision.kind, decision.detail);
    if (!creds) {
      this.log.warn({ session_id: sessionId, epoch }, 'HARD-OFF: no stored credentials; nothing to log out');
      return { logged_out: false };
    }
    const temp = this.newSession(sessionId, epoch, browserFor(sessionId), true);
    try {
      await temp.start();
    } catch (exc) {
      throw new GatewayError('not_found', (exc as Error).message);
    }
    const outcome = await temp.waitForOpen(LOGOUT_TIMEOUT_MS - 5_000);
    if (outcome === 'open') {
      const ok = await temp.logout();
      await this.pgRead(() => wipeAuthState(this.pool, sessionId));
      this.log.warn({ session_id: sessionId, epoch, logged_out: ok }, 'HARD-OFF: device unlinked via a temporary socket; auth state wiped');
      return { logged_out: ok };
    }
    if (outcome === 'lost') {
      await this.pgRead(() => wipeAuthState(this.pool, sessionId));
      this.log.warn({ session_id: sessionId, epoch, reason: temp.lost }, 'HARD-OFF: device was already gone; auth state wiped');
      return { logged_out: false };
    }
    await temp.close('logout: could not connect in time');
    throw new GatewayError('other', `could not connect to log out (${outcome}); credentials kept`);
  }

  // --------------------------------------------------------------- watchdog

  private async pgRead<T>(fn: () => Promise<T>): Promise<T> {
    const out = await fn();
    this.lastPgOkMs = this.now();
    return out;
  }

  startWatchdog(intervalMs = WATCHDOG_INTERVAL_MS): void {
    if (this.watchdog) return;
    this.lastPgOkMs = this.now();
    this.watchdog = setInterval(() => {
      this.watchdogTick().catch((exc) => this.log.error({ err: exc }, 'watchdog tick failed'));
    }, intervalMs);
    this.watchdog.unref();
  }

  /** Exposed for tests. */
  async watchdogTick(): Promise<void> {
    const sessions = [...this.sessions.values()];
    let pgFailed = false;
    if (sessions.length === 0) {
      try {
        await this.pgRead(() => ping(this.pool));
      } catch (exc) {
        pgFailed = true;
        this.log.warn({ err: exc }, 'watchdog: postgres unreachable');
      }
    }
    for (const session of sessions) {
      let verdict;
      try {
        const row = await this.pgRead(() => readSessionRow(this.pool, session.sessionId));
        verdict = watchdogVerdict({ row, socketEpoch: session.epoch, nowMs: this.now() });
      } catch (exc) {
        pgFailed = true;
        this.log.warn({ err: exc, session_id: session.sessionId }, 'watchdog: postgres unreachable');
        continue;
      }
      if (verdict.close) {
        this.log.error({ session_id: session.sessionId, epoch: session.epoch, reason: verdict.reason }, `WATCHDOG closing ${session.sessionId}: ${verdict.reason}`);
        await this.dropSession(session, `watchdog: ${verdict.reason}`);
      }
    }
    if (pgFailed && postgresSilentTooLong(this.lastPgOkMs, this.now())) {
      const open = [...this.sessions.values()];
      if (open.length) this.log.error({ count: open.length }, 'WATCHDOG postgres silent too long; closing ALL sockets');
      await Promise.all(open.map((s) => this.dropSession(s, 'watchdog: postgres unreachable, leases unprovable')));
    }
  }

  async shutdown(reason: string, inboxFlushMs = 5_000): Promise<void> {
    this.stopping = true;
    if (this.watchdog) clearInterval(this.watchdog);
    this.watchdog = null;
    for (const run of this.pairings.values()) run.cancel(`gateway shutdown: ${reason}`);
    await Promise.all([...this.sessions.values()].map((s) => this.dropSession(s, `gateway shutdown: ${reason}`)));
    // Give queued inbox rows a bounded chance to land; memory-only, so a
    // Postgres outage across a restart loses what is still pending.
    const pending = [...this.inboxes.values()].filter((w) => w.pending > 0);
    if (pending.length) {
      await Promise.race([Promise.all(pending.map((w) => w.flush())), new Promise((r) => setTimeout(r, inboxFlushMs))]);
      const left = pending.reduce((n, w) => n + w.pending, 0);
      if (left) this.log.error({ pending: left }, 'shutting down with inbox rows still unwritten (postgres unreachable)');
    }
  }
}
