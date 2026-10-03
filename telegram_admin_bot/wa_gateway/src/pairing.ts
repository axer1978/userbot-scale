/**
 * Linking a number: a temporary socket that streams QR codes (or requests
 * a pairing code), persists the resulting creds/keys in Postgres and then
 * ends cleanly. It does NOT log out (that would unlink the device we just
 * linked). After WhatsApp accepts the scan it always closes the socket
 * with 515 (restartRequired); we reconnect with the same auth state and
 * treat the following 'open' as success.
 */
import type { WASocket } from 'baileys';
import type pino from 'pino';
import { isLinked, PostgresAuthStore, usePostgresAuthState, type PostgresAuthState } from './authstate.ts';
import type { BrowserTuple } from './browser.ts';
import type { Pool } from './db.ts';
import { classifyDisconnect } from './disconnect.ts';
import { baileysLogger } from './log.ts';
import { meFromUser, type Me } from './session.ts';
import { makeGatewaySocket } from './socket.ts';

export type PairMethod = 'qr' | 'code';

export type PairEvent =
  | { v: 1; type: 'qr'; session_id: string; pair_id: string; qr: string }
  | { v: 1; type: 'code'; session_id: string; pair_id: string; code: string }
  | { v: 1; type: 'paired'; session_id: string; pair_id: string; jid: string | null; lid: string | null; push_name: string | null }
  | { v: 1; type: 'failed'; session_id: string; pair_id: string; reason: string };

export type PairingOptions = {
  pool: Pool;
  sessionId: string;
  pairId: string;
  method: PairMethod;
  /** Digits only, with country code, for method "code". */
  phone?: string;
  browser: BrowserTuple;
  log: pino.Logger;
  emit: (event: PairEvent) => void;
  timeoutMs?: number;
  /** After 'open', time given to the phone to push its initial keys before we end. */
  settleMs?: number;
};

export const PAIR_TIMEOUT_MS = 180_000;
export const PAIR_SETTLE_MS = 5_000;
const MAX_RESTARTS = 3;

export function phoneDigits(phone: string | undefined): string | null {
  if (!phone) return null;
  const digits = phone.replace(/\D/g, '');
  return digits.length >= 7 && digits.length <= 15 ? digits : null;
}

export type PairOutcome = { ok: true; me: Me | null } | { ok: false; reason: string };

export class PairingRun {
  private readonly opts: PairingOptions;
  private readonly log: pino.Logger;
  private sock: WASocket | null = null;
  private finished = false;
  private cancelReason: string | null = null;
  private resolveDone: ((o: PairOutcome) => void) | null = null;

  constructor(opts: PairingOptions) {
    this.opts = opts;
    this.log = opts.log.child({ session_id: opts.sessionId, pair_id: opts.pairId });
  }

  cancel(reason = 'cancelled'): void {
    if (this.finished) return;
    this.cancelReason = reason;
    this.settle({ ok: false, reason });
  }

  private settle(outcome: PairOutcome): void {
    if (this.finished) return;
    this.finished = true;
    this.resolveDone?.(outcome);
  }

  private ev(event: PairEvent): void {
    try {
      this.opts.emit(event);
    } catch (exc) {
      this.log.warn({ err: exc }, 'pair event listener failed');
    }
  }

  /** Resolves when pairing ended either way; never throws. */
  async run(): Promise<PairOutcome> {
    const { sessionId, pairId } = this.opts;
    const timeoutMs = this.opts.timeoutMs ?? PAIR_TIMEOUT_MS;
    let auth: PostgresAuthState | null = null;
    const timer = setTimeout(() => this.settle({ ok: false, reason: 'timeout' }), timeoutMs);
    let outcome: PairOutcome;
    try {
      const done = new Promise<PairOutcome>((resolve) => {
        this.resolveDone = resolve;
      });
      await this.wipe();
      if (!this.finished) {
        // (a cancel during the wipe has already settled `done`)
        auth = await usePostgresAuthState(this.opts.pool, sessionId, baileysLogger(sessionId));
        if (auth.existed) {
          // Someone wrote creds between our wipe and our load: not ours to pair over.
          this.settle({ ok: false, reason: 'auth state reappeared during pairing' });
        } else {
          await auth.saveCreds();
          this.log.info({ method: this.opts.method }, 'pairing started');
          this.connect(auth, 0);
        }
      }
      outcome = await done;
    } catch (exc) {
      outcome = { ok: false, reason: `error: ${(exc as Error).message}` };
      this.finished = true;
    } finally {
      clearTimeout(timer);
    }
    await this.endSocket();
    if (outcome.ok) {
      this.log.info({ me: outcome.me }, 'paired');
      this.ev({ v: 1, type: 'paired', session_id: sessionId, pair_id: pairId, jid: outcome.me?.jid ?? null, lid: outcome.me?.lid ?? null, push_name: outcome.me?.name ?? null });
    } else {
      this.log.warn({ reason: outcome.reason }, 'pairing failed');
      await this.wipe();
      this.ev({ v: 1, type: 'failed', session_id: sessionId, pair_id: pairId, reason: outcome.reason });
    }
    return outcome;
  }

  private async wipe(): Promise<void> {
    await new PostgresAuthStore(this.opts.pool, this.opts.sessionId).wipeAll();
  }

  private async endSocket(): Promise<void> {
    const sock = this.sock;
    this.sock = null;
    if (!sock) return;
    try {
      await sock.end(undefined);
    } catch (exc) {
      this.log.warn({ err: exc }, 'error while ending pairing socket');
    }
  }

  private connect(auth: PostgresAuthState, restarts: number): void {
    if (this.finished) return;
    const { sessionId, pairId } = this.opts;
    const sock = makeGatewaySocket({ state: auth.state, browser: this.opts.browser, logger: baileysLogger(sessionId) });
    this.sock = sock;
    let codeRequested = false;

    sock.ev.on('creds.update', () => {
      auth.saveCreds().catch((exc) => this.log.error({ err: exc }, 'could not persist creds during pairing'));
    });

    sock.ev.on('connection.update', (update) => {
      if (sock !== this.sock || this.finished) return;
      if (update.qr) {
        if (this.opts.method === 'qr') {
          this.ev({ v: 1, type: 'qr', session_id: sessionId, pair_id: pairId, qr: update.qr });
        } else if (!codeRequested) {
          // The first QR means the socket is registered and may request a code.
          codeRequested = true;
          const digits = phoneDigits(this.opts.phone);
          if (!digits) {
            this.settle({ ok: false, reason: 'phone must be 7..15 digits for method "code"' });
            return;
          }
          sock
            .requestPairingCode(digits)
            .then((code) => this.ev({ v: 1, type: 'code', session_id: sessionId, pair_id: pairId, code }))
            .catch((exc) => this.settle({ ok: false, reason: `pairing code request failed: ${(exc as Error).message}` }));
        }
      }
      if (update.connection === 'open') {
        const me = meFromUser(sock.user);
        if (!isLinked(auth.state.creds)) {
          this.settle({ ok: false, reason: 'connection opened but creds are not registered' });
          return;
        }
        if (!auth.state.creds.registered) {
          // A QR link: record it as registered too, like the pairing-code path does.
          auth.state.creds.registered = true;
          auth.saveCreds().catch((exc) => this.log.error({ err: exc }, 'could not persist creds during pairing'));
        }
        // Let the phone push its first app-state/prekey material before we end.
        setTimeout(() => this.settle({ ok: true, me }), this.opts.settleMs ?? PAIR_SETTLE_MS);
      } else if (update.connection === 'close') {
        const verdict = classifyDisconnect(update.lastDisconnect?.error);
        if (verdict.kind === 'restart' && restarts < MAX_RESTARTS) {
          this.log.info({ restarts }, 'restart requested after pairing; reconnecting');
          this.sock = null;
          void sock.end(undefined).catch(() => undefined);
          setTimeout(() => this.connect(auth, restarts + 1), 500);
          return;
        }
        const reason = verdict.kind === 'fatal' ? `${verdict.reason} (${verdict.code})` : verdict.kind === 'restart' ? 'too many restarts' : `connection closed: ${verdict.reason}`;
        this.settle({ ok: false, reason });
      }
    });
  }
}
