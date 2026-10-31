/**
 * One live socket for one account under one lease epoch. Owns reconnects
 * (transient/restart) and refuses them on session loss. Emits the
 * `wa:ev:<session_id>` events; the gateway publishes them.
 *
 * It never decides to send anything: every primitive here (sendText,
 * read, presence, logout) runs only when Python asks, fenced by the
 * gateway. Inbound messages go to the gateway's InboxWriter (step 5).
 */
import { downloadContentFromMessage } from 'baileys';
import type { WAPresence, WASocket, proto } from 'baileys';
import type pino from 'pino';
import { isLinked, usePostgresAuthState, type PostgresAuthState } from './authstate.ts';
import type { BrowserTuple } from './browser.ts';
import { GatewayError } from './bus.ts';
import type { Pool } from './db.ts';
import { backoffMs, classifyDisconnect, jitterMs, type SessionLostReason } from './disconnect.ts';
import { FailureReporter, type MessageFailedEvent, type MessageUpdateLike } from './failures.ts';
import type { SocketState } from './fencing.ts';
import { isGroupJid, isPnJid, stripDevice } from './jid.ts';
import { baileysLogger } from './log.ts';
import { LruCache } from './lru.ts';
import { collect, MAX_MEDIA_BYTES, type DownloadRequest, type OutboundMedia } from './media.ts';
import { describeForLog, normalizeMessage, timestampSeconds, type InboxPayload, type MessageLike } from './normalize.ts';
import { classifySendError } from './senderr.ts';
import { GET_MESSAGE_CACHE_SIZE, makeGatewaySocket, type SocketDeps } from './socket.ts';

export type Me = { jid: string | null; lid: string | null; name: string | null };

export type SessionEvent =
  | { v: 1; type: 'connection'; session_id: string; epoch: number; state: SocketState; me: Me | null }
  | { v: 1; type: 'session_lost'; session_id: string; epoch: number; reason: SessionLostReason; code: number }
  | { v: 1; type: 'inbox'; session_id: string; epoch: number }
  | MessageFailedEvent;

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
  /** Temporary sockets (logout without a live socket): publish nothing. */
  quiet?: boolean;
  /** The groups set as the booker: their messages come through (setGroups changes it). */
  groups?: readonly string[];
  /** Tests: a fake in place of makeGatewaySocket (never connects). */
  makeSocket?: (deps: SocketDeps) => WASocket;
  /** Tests: a fake in place of Baileys' media download. */
  download?: typeof downloadContentFromMessage;
};

/** How many of our own sent message ids we remember for echo suppression. */
export const SENT_ID_CACHE_SIZE = 2_000;
/** Positive `onWhatsApp` answers remembered per jid. */
export const KNOWN_JID_CACHE_SIZE = 5_000;
export const PRESENCE_STATES: ReadonlySet<string> = new Set(['available', 'unavailable', 'composing', 'paused']);

/** One group the account is in, as `groups` lists it. */
export type GroupEntry = { jid: string; subject: string; size: number | null; announce: boolean };

export function meFromUser(user: { id?: string; lid?: string; phoneNumber?: string; name?: string } | undefined): Me | null {
  if (!user?.id) return null;
  const pn = isPnJid(user.id) ? user.id : (user.phoneNumber ?? null);
  const lid = user.lid ?? (user.id.endsWith('@lid') ? user.id : null);
  return { jid: pn ? stripDevice(pn) : null, lid: lid ? stripDevice(lid) : null, name: user.name ?? null };
}

/**
 * Echo suppression: a from_me message whose id we sent ourselves through
 * `sendText` is the server echoing our own send; the runtime already has
 * it. A from_me message typed on the phone is not in the cache and is
 * forwarded (flagged from_me).
 */
export function isEcho(payload: InboxPayload, sentIds: LruCache<string, true>): boolean {
  return payload.from_me && sentIds.get(payload.wa_message_id) === true;
}

function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new GatewayError('other', `${what} timed out after ${ms} ms`)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
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
  private readonly sentIds = new LruCache<string, true>(SENT_ID_CACHE_SIZE);
  private readonly knownJids = new LruCache<string, true>(KNOWN_JID_CACHE_SIZE);
  private readonly openWaiters: Array<(outcome: 'open' | 'lost' | 'closed') => void> = [];
  private readonly failures: FailureReporter;
  // Read by the socket's JID filter on every stanza, so a change applies at once.
  private groups: ReadonlySet<string>;

  constructor(opts: SessionSocketOptions) {
    this.opts = opts;
    this.sessionId = opts.sessionId;
    this.epoch = opts.epoch;
    this.log = opts.log.child({ session_id: opts.sessionId, epoch: opts.epoch });
    this.failures = new FailureReporter(opts.sessionId, opts.epoch);
    this.groups = new Set(opts.groups ?? []);
  }

  /** The groups whose messages come through from now on (the booker's). */
  setGroups(groups: readonly string[]): void {
    const next = new Set(groups);
    if (next.size === this.groups.size && [...next].every((g) => this.groups.has(g))) return;
    this.groups = next;
    this.log.info({ groups: [...next] }, 'booker groups set');
  }

  /** Loads the auth state (throws when there are no creds) and connects. */
  async start(): Promise<void> {
    const auth = await usePostgresAuthState(this.opts.pool, this.sessionId, baileysLogger(this.sessionId));
    if (!auth.existed || !isLinked(auth.state.creds)) {
      throw new Error('session has no registered WhatsApp credentials; pair first');
    }
    this.auth = auth;
    this.connect();
  }

  /** Resolves with the first terminal outcome: open, lost, or closed by us. */
  waitForOpen(timeoutMs: number): Promise<'open' | 'lost' | 'closed' | 'timeout'> {
    if (this.state === 'open') return Promise.resolve('open');
    if (this.lost) return Promise.resolve('lost');
    if (this.closedByUs) return Promise.resolve('closed');
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve('timeout'), timeoutMs);
      this.openWaiters.push((outcome) => {
        clearTimeout(timer);
        resolve(outcome);
      });
    });
  }

  private settleWaiters(outcome: 'open' | 'lost' | 'closed'): void {
    const waiters = this.openWaiters.splice(0);
    for (const w of waiters) w(outcome);
  }

  private emit(event: SessionEvent): void {
    if (this.opts.quiet) return;
    this.opts.emit(event);
  }

  private emitConnection(): void {
    this.emit({ v: 1, type: 'connection', session_id: this.sessionId, epoch: this.epoch, state: this.state, me: this.me });
  }

  private connect(): void {
    if (this.closedByUs || !this.auth) return;
    this.state = 'connecting';
    this.emitConnection();
    const sock = (this.opts.makeSocket ?? makeGatewaySocket)({
      state: this.auth.state,
      browser: this.opts.browser,
      logger: baileysLogger(this.sessionId),
      outbox: this.outbox,
      allowGroup: (jid) => this.groups.has(jid),
    });
    this.sock = sock;
    const auth = this.auth;

    // Every listener is bound to the socket it was registered on: after a
    // reconnect the old socket's late events must not reach the inbox a
    // second time, nor overwrite the state of the socket that replaced it.
    sock.ev.on('creds.update', () => {
      if (sock !== this.sock) return;
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
        this.settleWaiters('open');
      } else if (update.connection === 'close') {
        this.onClose(update.lastDisconnect?.error);
      }
    });

    // Delivery failures of our own sends: rc14 reports a server
    // <ack error="..."> as a messages.update entry with status ERROR
    // (see failures.ts). Normal status moves are not forwarded.
    sock.ev.on('messages.update', (entries) => {
      if (sock !== this.sock) return;
      try {
        for (const event of this.failures.events(entries as unknown as MessageUpdateLike[])) {
          this.log.warn({ wa_message_id: event.wa_message_id, jid: event.jid, code: event.code, error_kind: event.error_kind }, 'message delivery failed');
          this.emit(event);
        }
      } catch (exc) {
        this.log.warn({ err: exc }, 'could not process a message update');
      }
    });

    sock.ev.on('messages.upsert', ({ messages, type }) => {
      if (sock !== this.sock) return;
      // 'notify' = live; 'append' = delivered while we were offline (rc14
      // messages-recv sets it from the stanza's offline attribute; its
      // other 'append' sources are newsletter plaintext and notification
      // stubs, which normalizeMessage drops). History sync is off
      // (shouldSyncHistoryMessage -> false) and lands on
      // messaging-history.set, never here, so nothing old is replayed.
      if (type !== 'notify' && type !== 'append') return;
      for (const raw of messages) {
        try {
          const payload = normalizeMessage(raw as unknown as MessageLike, { sessionId: this.sessionId, epoch: this.epoch, groups: this.groups });
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
      this.emit({ v: 1, type: 'session_lost', session_id: this.sessionId, epoch: this.epoch, reason: verdict.reason, code: verdict.code });
      this.sock = null;
      this.settleWaiters('lost');
      this.opts.onLost?.(verdict.reason);
      return;
    }
    // 515 right after pairing: once, at once. A 515 that keeps coming back
    // without an open in between is a loop and backs off like any drop.
    const delay = verdict.kind === 'restart' && this.attempt === 0 ? 1_000 : jitterMs(backoffMs(this.attempt));
    this.attempt += 1;
    this.log.warn({ code: verdict.code, kind: verdict.kind, delay_ms: delay, reason: verdict.kind === 'transient' ? verdict.reason : 'restartRequired' }, 'connection closed; reconnecting');
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /** Normal end (no logout): the lease moved, Python asked, or shutdown. */
  async close(reason: string): Promise<void> {
    const sock = this.beginClose(reason);
    if (sock) {
      try {
        await sock.end(undefined);
      } catch (exc) {
        this.log.warn({ err: exc }, 'error while ending socket');
      }
    }
  }

  /** Marks the socket closed by us and returns the Baileys socket to finish with. */
  private beginClose(reason: string): WASocket | null {
    if (this.closedByUs) return null;
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
    if (wasOpen) this.emitConnection();
    this.settleWaiters('closed');
    return sock;
  }

  // ------------------------------------------------------------ primitives

  private openSock(): WASocket {
    if (this.lost) throw new GatewayError('session_lost', `${this.lost}; re-pair the number`);
    if (this.state !== 'open' || !this.sock) throw new GatewayError('not_connected', `socket is ${this.state}`);
    return this.sock;
  }

  private failure(error: unknown): never {
    if (error instanceof GatewayError) throw error;
    const { kind, detail } = classifySendError(error);
    throw new GatewayError(kind, detail);
  }

  /** One IQ per unknown jid; positive answers are cached. LIDs are not checked. */
  private async assertOnWhatsApp(sock: WASocket, jid: string): Promise<void> {
    if (!isPnJid(jid) || this.knownJids.get(jid)) return;
    const number = jid.split('@')[0] ?? '';
    const results = await sock.onWhatsApp(number);
    const hit = results?.find((r) => r.exists);
    if (!hit) throw new GatewayError('not_on_whatsapp', `${jid} is not on WhatsApp`);
    this.knownJids.set(jid, true);
  }

  /**
   * Sends one text message. Never retried here: once relayMessage has
   * handed the stanza to the socket it may have gone out.
   */
  async sendText(jid: string, text: string): Promise<{ message_id: string; ts: number }> {
    const sock = this.openSock();
    try {
      await this.assertOnWhatsApp(sock, jid);
      const sent = await sock.sendMessage(jid, { text });
      const id = sent?.key?.id;
      if (!id) throw new GatewayError('other', 'sendMessage returned no message id');
      if (sent.message) this.outbox.set(id, sent.message);
      this.sentIds.set(id, true);
      const ts = timestampSeconds(sent.messageTimestamp as MessageLike['messageTimestamp']) || Math.floor(Date.now() / 1000);
      this.log.info({ jid, wa_message_id: id, text_len: text.length }, 'sent text');
      return { message_id: id, ts };
    } catch (error) {
      this.failure(error);
    }
  }

  /** One photo or video. Never retried here, like sendText. */
  async sendMedia(jid: string, media: OutboundMedia): Promise<{ message_id: string; ts: number }> {
    const sock = this.openSock();
    try {
      await this.assertOnWhatsApp(sock, jid);
      const common = { mimetype: media.mimetype, viewOnce: media.viewOnce, ...(media.caption ? { caption: media.caption } : {}) };
      const sent = await sock.sendMessage(jid, media.kind === 'image' ? { image: media.data, ...common } : { video: media.data, ...common });
      const id = sent?.key?.id;
      if (!id) throw new GatewayError('other', 'sendMessage returned no message id');
      if (sent.message) this.outbox.set(id, sent.message);
      this.sentIds.set(id, true);
      const ts = timestampSeconds(sent.messageTimestamp as MessageLike['messageTimestamp']) || Math.floor(Date.now() / 1000);
      this.log.info({ jid, wa_message_id: id, kind: media.kind, bytes: media.data.length, view_once: media.viewOnce }, 'sent media');
      return { message_id: id, ts };
    } catch (error) {
      this.failure(error);
    }
  }

  /** Fetches and decrypts an inbound image (its inbox descriptor). Only the
   * session's live socket does this, so only the lease holder can. */
  async downloadMedia(request: DownloadRequest): Promise<Buffer> {
    this.openSock();
    try {
      const download = this.opts.download ?? downloadContentFromMessage;
      const stream = await download(
        { mediaKey: request.mediaKey, directPath: request.directPath, url: request.url }, request.kind);
      const data = await collect(stream, MAX_MEDIA_BYTES);
      this.log.info({ kind: request.kind, bytes: data.length }, 'downloaded media');
      return data;
    } catch (error) {
      if (error instanceof GatewayError) throw error;
      this.log.warn({ err: error }, 'media download failed');
      throw new GatewayError('other', `media download failed: ${(error as Error)?.message ?? 'unknown error'}`);
    }
  }

  async read(jid: string, messageIds: string[]): Promise<void> {
    const sock = this.openSock();
    try {
      await sock.readMessages(messageIds.map((id) => ({ remoteJid: jid, id, fromMe: false })));
    } catch (error) {
      this.failure(error);
    }
  }

  /** Every group the account is in (one query), for picking the booker's. */
  async listGroups(): Promise<GroupEntry[]> {
    const sock = this.openSock();
    try {
      const all = await sock.groupFetchAllParticipating();
      return Object.values(all)
        .filter((g) => isGroupJid(g.id))
        .map((g) => ({ jid: g.id, subject: g.subject ?? '', size: g.size ?? g.participants?.length ?? null, announce: g.announce === true }))
        .sort((a, b) => a.subject.localeCompare(b.subject));
    } catch (error) {
      this.failure(error);
    }
  }

  /** The group an invite link (its code) is for. Joins nothing. */
  async groupInvite(code: string): Promise<{ jid: string; subject: string }> {
    const sock = this.openSock();
    try {
      const g = await sock.groupGetInviteInfo(code);
      if (!g?.id || !isGroupJid(g.id)) throw new GatewayError('not_found', 'no group for this invite link');
      return { jid: g.id, subject: g.subject ?? '' };
    } catch (error) {
      this.failure(error);
    }
  }

  /** "Delete for everyone", one message at a time (WhatsApp has no batch). */
  async deleteMessages(jid: string, messageIds: string[]): Promise<void> {
    const sock = this.openSock();
    try {
      for (const id of messageIds) {
        await sock.sendMessage(jid, { delete: { remoteJid: jid, fromMe: true, id } });
      }
      this.log.info({ jid, count: messageIds.length }, 'deleted messages');
    } catch (error) {
      this.failure(error);
    }
  }

  async presence(state: WAPresence, jid?: string): Promise<void> {
    const sock = this.openSock();
    try {
      await sock.sendPresenceUpdate(state, jid);
    } catch (error) {
      this.failure(error);
    }
  }

  /**
   * Deliberate hard-off: unlink this device. Returns true when the
   * remove-companion-device request went out; false when there was no
   * socket to send it on (the device was already gone). No session_lost
   * event is emitted for this: the caller asked for it.
   */
  async logout(timeoutMs = 10_000): Promise<boolean> {
    const alreadyLost = this.lost;
    const sock = this.beginClose('logout (deliberate hard-off)');
    if (!sock || alreadyLost) {
      this.log.warn({ lost: alreadyLost }, 'logout requested but the device is already gone');
      return false;
    }
    try {
      await withTimeout(sock.logout('hard-off'), timeoutMs, 'logout');
      this.log.warn('device unlinked (deliberate logout)');
      return true;
    } catch (exc) {
      this.log.warn({ err: exc }, 'logout request failed; ending socket anyway');
      await sock.end(undefined).catch(() => undefined);
      return false;
    }
  }
}
