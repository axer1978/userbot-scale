/**
 * Turns a Baileys WAMessage into the inbox payload v1 (see README). Pure:
 * takes plain objects, returns a payload or null (= not an inbox message).
 * Step 3 only logs the result; step 5 will persist it in wa_inbox.
 *
 * Baileys v7 addressing: key.remoteJid is the chat as WhatsApp addressed
 * it (PN "<digits>@s.whatsapp.net" or LID "<id>@lid"), key.remoteJidAlt
 * is the other form when known, key.addressingMode says which one
 * remoteJid is ("pn" | "lid"). We keep all of it and also split into
 * phone_jid / lid so consumers never have to guess.
 */
import { isDirectChatJid, isLidJid, isPnJid } from './jid.ts';

export type InboxType = 'text' | 'image' | 'other';

export type InboxPayload = {
  v: 1;
  session_id: string;
  epoch: number;
  wa_message_id: string;
  jid: string;
  jid_alt: string | null;
  phone_jid: string | null;
  lid: string | null;
  push_name: string | null;
  from_me: boolean;
  type: InboxType;
  text: string | null;
  quoted_id: string | null;
  /** Unix seconds as WhatsApp stamped the message. */
  ts: number;
};

/** The subset of WAMessage we read. Structural so tests can pass literals. */
export type MessageLike = {
  key?: {
    remoteJid?: string | null;
    remoteJidAlt?: string | null;
    fromMe?: boolean | null;
    id?: string | null;
    participant?: string | null;
    addressingMode?: string | null;
  } | null;
  message?: Record<string, unknown> | null;
  messageTimestamp?: number | { toNumber(): number } | { low: number; high: number; unsigned: boolean } | string | null;
  pushName?: string | null;
  messageStubType?: number | null;
};

type ContextInfo = { stanzaId?: string | null } | null | undefined;

/** Message kinds that carry no user content and never reach the inbox. */
const NON_CONTENT_KEYS = new Set([
  'protocolMessage',
  'reactionMessage',
  'senderKeyDistributionMessage',
  'messageContextInfo',
  'pollUpdateMessage',
  'encReactionMessage',
  'keepInChatMessage',
  'peerDataOperationRequestMessage',
  'peerDataOperationRequestResponseMessage',
  'placeholderMessage',
  'encEventResponseMessage',
  'encCommentMessage',
  'stickerSyncRmrMessage',
  'deviceSentMessage',
]);

/** Unwraps the envelopes WhatsApp nests real content in. */
export function unwrapContent(message: Record<string, unknown> | null | undefined): Record<string, unknown> | undefined {
  let content = message ?? undefined;
  for (let i = 0; i < 6 && content; i++) {
    const wrapped =
      (content.ephemeralMessage as { message?: Record<string, unknown> } | undefined)?.message ??
      (content.viewOnceMessage as { message?: Record<string, unknown> } | undefined)?.message ??
      (content.viewOnceMessageV2 as { message?: Record<string, unknown> } | undefined)?.message ??
      (content.viewOnceMessageV2Extension as { message?: Record<string, unknown> } | undefined)?.message ??
      (content.documentWithCaptionMessage as { message?: Record<string, unknown> } | undefined)?.message ??
      (content.editedMessage as { message?: Record<string, unknown> } | undefined)?.message ??
      (content.deviceSentMessage as { message?: Record<string, unknown> } | undefined)?.message;
    if (!wrapped) break;
    content = wrapped;
  }
  return content;
}

export function contentTypeOf(content: Record<string, unknown> | undefined): string | undefined {
  if (!content) return undefined;
  return Object.keys(content).find((k) => k !== 'messageContextInfo' && content[k] !== null && content[k] !== undefined);
}

export function timestampSeconds(ts: MessageLike['messageTimestamp']): number {
  if (ts === null || ts === undefined) return 0;
  if (typeof ts === 'number') return Math.floor(ts);
  if (typeof ts === 'string') return Math.floor(Number(ts)) || 0;
  if ('toNumber' in ts && typeof ts.toNumber === 'function') return Math.floor(ts.toNumber());
  if ('low' in ts) {
    const low = ts.low >>> 0;
    return ts.high * 4294967296 + low;
  }
  return 0;
}

function textOf(kind: string | undefined, content: Record<string, unknown>): { type: InboxType; text: string | null; ctx: ContextInfo } {
  switch (kind) {
    case 'conversation':
      return { type: 'text', text: String(content.conversation ?? ''), ctx: null };
    case 'extendedTextMessage': {
      const ext = content.extendedTextMessage as { text?: string | null; contextInfo?: ContextInfo };
      return { type: 'text', text: ext.text ?? '', ctx: ext.contextInfo };
    }
    case 'imageMessage': {
      const img = content.imageMessage as { caption?: string | null; contextInfo?: ContextInfo };
      return { type: 'image', text: img.caption ?? null, ctx: img.contextInfo };
    }
    default: {
      const inner = kind ? (content[kind] as { contextInfo?: ContextInfo } | undefined) : undefined;
      return { type: 'other', text: null, ctx: inner?.contextInfo };
    }
  }
}

export type NormalizeOptions = { sessionId: string; epoch: number };

/**
 * Returns null for anything the inbox does not want: non-direct chats,
 * stub/system rows, protocol/reaction messages, undecryptable messages
 * (message == null) and rows without an id.
 */
export function normalizeMessage(msg: MessageLike, opts: NormalizeOptions): InboxPayload | null {
  const key = msg.key;
  if (!key?.id || !key.remoteJid) return null;
  if (!isDirectChatJid(key.remoteJid)) return null;
  if (msg.messageStubType) return null;
  const content = unwrapContent(msg.message);
  if (!content) return null;
  const kind = contentTypeOf(content);
  if (!kind || NON_CONTENT_KEYS.has(kind)) return null;

  const jid = key.remoteJid;
  const alt = key.remoteJidAlt ?? null;
  const phoneJid = isPnJid(jid) ? jid : isPnJid(alt) ? alt : null;
  const lid = isLidJid(jid) ? jid : isLidJid(alt) ? alt : null;
  const { type, text, ctx } = textOf(kind, content);

  return {
    v: 1,
    session_id: opts.sessionId,
    epoch: opts.epoch,
    wa_message_id: key.id,
    jid,
    jid_alt: alt,
    phone_jid: phoneJid,
    lid,
    push_name: msg.pushName ?? null,
    from_me: key.fromMe === true,
    type,
    text,
    quoted_id: ctx?.stanzaId ?? null,
    ts: timestampSeconds(msg.messageTimestamp),
  };
}

/** What gets logged at info level: never the text itself. */
export function describeForLog(p: InboxPayload): Record<string, unknown> {
  return {
    session_id: p.session_id,
    jid: p.jid,
    wa_message_id: p.wa_message_id,
    from_me: p.from_me,
    type: p.type,
    text_len: p.text?.length ?? 0,
    quoted: p.quoted_id !== null,
  };
}
