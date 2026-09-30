/**
 * One live socket for one account under one lease epoch. Owns reconnects
 * (transient/restart) and refuses them on session loss. Emits the
 * `wa:ev:<session_id>` events; the gateway publishes them.
 *
 * It never decides to send anything. Qualifying inbound messages go to
 * the gateway's InboxWriter (step 5); their text is never logged.
 */
import type { WASocket, proto } from 'baileys';
import type pino from 'pino';
import { usePostgresAuthState, type PostgresAuthState } from './authstate.ts';
import type { BrowserTuple } from './browser.ts';
import type { Pool } from './db.ts';
import { backoffMs, classifyDisconnect, type SessionLostReason } from './disconnect.ts';
import type { SocketState } from './fencing.ts';
import { isPnJid } from './jid.ts';
import { baileysLogger } from './log.ts';
import { LruCache } from './lru.ts';
import { describeForLog, normalizeMessage, type InboxPayload, type MessageLike } from './normalize.ts';
import { GET_MESSAGE_CACHE_SIZE, makeGatewaySocket } from './socket.ts';

export type Me = { jid: string | null; lid: string | null; name: string | null };

export type SessionEvent =
  | { v: 1; type: 'connection'; session_id: string; epoch: number; state: SocketState; me: Me | null }
  | { v: 1; type: 'session_lost'; session_id: string; epoch: number; reason: SessionLostReason; code: number }
  | { v: 1; type: 'inbox'; session_id: string; epoch: number };

export type SessionSocketOptions = {
  pool: Pool;
  sessionId: string;
  epoch: number;
  browser: BrowserTuple;
  log: pino.Logger;
  emit: (event: SessionEvent) => void;
  /** Qualifying, non-echo inbound messages (step 5: the gateway's InboxWriter). */
  onMessage?: (payload: InboxPayload) => void;
  /** Called once when the socket will not come back (session lost). */
  onLost?: (reason: SessionLostReason) => void;
};

/** How many of our own sent message ids we remember for echo suppression. */
export const SENT_ID_CACHE_SIZE = 2_000;

function stripDevice(jid: string): string {
  const [user, server] = jid.split('@');
  return `${(user ?? '').split(':')[0]}@${server ?? ''}`;
}

export function meFromUser(user: { id?: string; lid?: string; phoneNumber?: string; name?: string } | undefined): Me | null {
  if (!user?.id) return null;
  const pn = isPnJid(user.id) ? user.id : (user.phoneNumber ?? null);
  const lid = user.lid ?? (user.id.endsWith('@lid') ? user.id : null);
  return { jid: pn ? stripDevice(pn) : null, lid: lid ? stripDevice(lid) : null, name: user.name ?? null };
}

/**
 * Echo suppression: a from_me message whose id we sent ourselves (step 6's
 * send_text records it) is the server echoing our own send; the runtime
 * already has it. A from_me message typed on the phone is not in the cache
 * and is forwarded (flagged from_me).
 */
export function isEcho(payload: InboxPayload, sentIds: LruCache<string, true>): boolean {
  return payload.from_me && sentIds.get(payload.wa_message_id) === true;
}

export class SessionSocket {
  readonly sessionId: string;
  readonly epoch: number;
  state: SocketState = 'connecting';
  me: Me | null = null;
  lost: SessionLostReason | null = null;

  private readonly opts: SessionSocketOptions;
  private readonly log: pino.Logger;
  private auth: PostgresAuthState | null = null;
  private sock: WASocket | null = null;
  private closedByUs = false;
  private attempt = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private readonly outbox = new LruCache<string, proto.IMessage>(GET_MESSAGE_CACHE_SIZE);
  protected readonly sentIds = new LruCache<string, true>(SENT_ID_CACHE_SIZE);

  constructor(opts: SessionSocketOptions) {
    this.opts = opts;
    this.sessionId = opts.sessionId;
    this.epoch = opts.epoch;
    this.log = opts.log.child({ session_id: opts.sessionId, epoch: opts.epoch });
  }

  /** Loads the auth state (throws when there are no creds) and connects. */
  async start(): Promise<void> {
    const auth = await usePostgresAuthState(this.opts.pool, this.sessionId, baileysLogger(this.sessionId));
    if (!auth.existed || !auth.state.creds.registered) {
      throw new Error('session has no registered WhatsApp credentials; pair first');
    }
    this.auth = auth;
    this.connect();
  }

  private emitConnection(): void {
    this.opts.emit({ v: 1, type: 'connection', session_id: this.sessionId, epoch: this.epoch, state: this.state, me: this.me });
  }

  private connect(): void {
    if (this.closedByUs || !this.auth) return;
    this.state = 'connecting';
    this.emitConnection();
    const sock = makeGatewaySocket({
      state: this.auth.state,
      browser: this.opts.browser,
      logger: baileysLogger(this.sessionId),
      outbox: this.outbox,
    });
    this.sock = sock;
    const auth = this.auth;

    sock.ev.on('creds.update', () => {
      auth.saveCreds().catch((exc) => this.log.error({ err: exc }, 'could not persist creds'));
    });

    sock.ev.on('connection.update', (update) => {
      if (sock !== this.sock) return; // an older socket of ours; ignore
      if (update.connection === 'open') {
        this.attempt = 0;
        this.state = 'open';
        this.me = meFromUser(sock.user);
        this.log.info({ me: this.me }, 'connection open');
        this.emitConnection();
      } else if (update.connection === 'close') {
        this.onClose(update.lastDisconnect?.error);
      }
    });

    sock.ev.on('messages.upsert', ({ messages, type }) => {
      // 'notify' = live; 'append' = delivered while we were offline (v7
      // sets it from the stanza's offline attribute). History sync is off
      // (shouldSyncHistoryMessage -> false), so nothing else lands here.
      if (type !== 'notify' && type !== 'append') return;
      for (const raw of messages) {
        try {
          const payload = normalizeMessage(raw as unknown as MessageLike, { sessionId: this.sessionId, epoch: this.epoch });
          if (!payload) continue;
          if (isEcho(payload, this.sentIds)) {
            this.log.debug({ wa_message_id: payload.wa_message_id }, 'own send echoed back; not forwarded');
            continue;
          }
          this.log.info(describeForLog(payload), 'inbound message');
          this.opts.onMessage?.(payload);
        } catch (exc) {
          this.log.warn({ err: exc }, 'could not normalise a message');
        }
      }
    });
  }

  private onClose(error: unknown): void {
    if (this.closedByUs) return;
    const verdict = classifyDisconnect(error);
    this.state = 'closed';
    this.emitConnection();
    if (verdict.kind === 'fatal') {
      this.lost = verdict.reason;
      this.log.error({ code: verdict.code, reason: verdict.reason }, `SESSION LOST ${this.sessionId}: ${verdict.reason} (${verdict.code})`);
      this.opts.emit({ v: 1, type: 'session_lost', session_id: this.sessionId, epoch: this.epoch, reason: verdict.reason, code: verdict.code });
      this.sock = null;
      this.opts.onLost?.(verdict.reason);
      return;
    }
    const delay = verdict.kind === 'restart' ? 1_000 : backoffMs(this.attempt++);
    this.log.warn({ code: verdict.code, kind: verdict.kind, delay_ms: delay, reason: verdict.kind === 'transient' ? verdict.reason : 'restartRequired' }, 'connection closed; reconnecting');
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /** Normal end (no logout): the lease moved, Python asked, or shutdown. */
  async close(reason: string): Promise<void> {
    if (this.closedByUs) return;
    this.closedByUs = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const sock = this.sock;
    this.sock = null;
    const wasOpen = this.state !== 'closed';
    this.state = 'closed';
    this.log.info({ reason }, 'closing socket');
    if (sock) {
      try {
        await sock.end(undefined);
      } catch (exc) {
        this.log.warn({ err: exc }, 'error while ending socket');
      }
    }
    if (wasOpen) this.emitConnection();
  }
}
