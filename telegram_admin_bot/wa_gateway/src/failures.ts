/**
 * Delivery failures of messages this socket already sent.
 *
 * Where they surface in Baileys 7.0.0-rc14 (read from the source):
 * `relayMessage` never waits for the server ack, so a rejected message
 * comes back later as a stanza `<ack class="message" error="<code>">`.
 * messages-recv.js `handleBadAck` turns it into a `messages.update` entry
 *   { key: { remoteJid, fromMe: true, id }, update: { status: WAMessageStatus.ERROR,
 *     messageStubParameters: ["<code>", ...] } }
 * That is the only place rc14 sets status ERROR from the server side
 * (receipts only move a message forward: server_ack, delivered, read).
 * The gateway turns those entries into `message_failed` events, once per
 * message id.
 */
import { WAMessageStatus } from 'baileys';
import type { ErrorKind } from './bus.ts';
import { LruCache } from './lru.ts';

export type FailureKind = Extract<ErrorKind, 'rate_limited' | 'blocked' | 'not_on_whatsapp' | 'other'>;

export type MessageFailedEvent = {
  v: 1;
  type: 'message_failed';
  session_id: string;
  epoch: number;
  wa_message_id: string;
  jid: string;
  code: number | null;
  error_kind: FailureKind;
};

/** The subset of a `messages.update` entry we read. Structural, for tests. */
export type MessageUpdateLike = {
  key?: { remoteJid?: string | null; fromMe?: boolean | null; id?: string | null } | null;
  update?: { status?: number | null; messageStubParameters?: (string | null)[] | null } | null;
};

export const FAILURE_DEDUPE_SIZE = 2_000;

/**
 * Same mapping as senderr.ts, applied to the ack error code. Codes rc14
 * itself names stay `other` (with the code attached): 463 account
 * restricted / "reachout timelocked" (new chats blocked, existing ones keep
 * working), 479 stanza rejected (stale device session).
 */
export function failureKindForCode(code: number | null, text?: string): FailureKind {
  const t = (text ?? '').toLowerCase();
  if (code === 429 || t.includes('rate-overlimit')) return 'rate_limited';
  if (code === 403 || t.includes('not-authorized') || t.includes('forbidden')) return 'blocked';
  if (code === 404 || t.includes('item-not-found')) return 'not_on_whatsapp';
  return 'other';
}

export function extractFailure(entry: MessageUpdateLike): { wa_message_id: string; jid: string; code: number | null; error_kind: FailureKind } | null {
  const key = entry.key;
  const update = entry.update;
  if (!key?.id || !key.remoteJid || !update) return null;
  const params = (update.messageStubParameters ?? []).filter((p): p is string => typeof p === 'string');
  const codeText = params[0];
  const code = codeText !== undefined && /^\d+$/.test(codeText) ? Number(codeText) : null;
  const isError = update.status === WAMessageStatus.ERROR || (code !== null && update.status === undefined);
  if (!isError) return null;
  // Failures are about what WE sent: a bad ack is always fromMe in rc14.
  if (key.fromMe === false) return null;
  return { wa_message_id: key.id, jid: key.remoteJid, code, error_kind: failureKindForCode(code, params.join(' ')) };
}

/** Turns update entries into events, at most one per message id. */
export class FailureReporter {
  private readonly seen = new LruCache<string, true>(FAILURE_DEDUPE_SIZE);
  private readonly sessionId: string;
  private readonly epoch: number;

  constructor(sessionId: string, epoch: number) {
    this.sessionId = sessionId;
    this.epoch = epoch;
  }

  events(entries: MessageUpdateLike[]): MessageFailedEvent[] {
    const out: MessageFailedEvent[] = [];
    for (const entry of entries) {
      const failure = extractFailure(entry);
      if (!failure || this.seen.get(failure.wa_message_id)) continue;
      this.seen.set(failure.wa_message_id, true);
      out.push({ v: 1, type: 'message_failed', session_id: this.sessionId, epoch: this.epoch, ...failure });
    }
    return out;
  }
}
