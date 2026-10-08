/**
 * Photos and videos. Out: `send_media` (the runtime sends the file's bytes,
 * the gateway sends them as a WhatsApp image or video). In: an inbound
 * image carries a download descriptor in its inbox payload (`media`), and
 * `download_media` fetches and decrypts it when the runtime wants it (an
 * arrival photo, the owner's entrance photo). Nothing is kept on disk.
 *
 * Pure helpers: argument checks, the descriptor, collecting a stream. The
 * socket work is in session.ts.
 */
import type { Readable } from 'node:stream';
import { GatewayError } from './bus.ts';

/** WhatsApp's own limit for photos is lower; this bounds what the bus carries. */
export const MAX_MEDIA_BYTES = 16 * 1024 * 1024;
export const MAX_CAPTION_LENGTH = 1024;
const MAX_BASE64_LENGTH = Math.ceil(MAX_MEDIA_BYTES / 3) * 4;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

export type MediaKind = 'image' | 'video';

/** In an inbox payload: what download_media needs to fetch the image. */
export type InboxMedia = {
  kind: 'image';
  mimetype: string | null;
  url: string | null;
  direct_path: string | null;
  /** base64; decrypts the file. Only ever in wa_inbox and on the bus, never logged. */
  media_key: string;
  file_length: number;
};

export type OutboundMedia = { kind: MediaKind; data: Buffer; mimetype: string; viewOnce: boolean; caption?: string };

export type DownloadRequest = { kind: MediaKind; mediaKey: Buffer; directPath?: string; url?: string; mimetype: string | null };

export function base64Of(value: unknown): string | null {
  if (typeof value === 'string') return value.length ? value : null;
  if (value instanceof Uint8Array) return value.length ? Buffer.from(value).toString('base64') : null;
  return null;
}

/** A protobuf Long, a number or a numeric string as a plain number (0 if none). */
export function longValue(value: unknown): number {
  if (value === null || value === undefined) return 0;
  if (typeof value === 'number') return Math.floor(value);
  if (typeof value === 'string') return Math.floor(Number(value)) || 0;
  if (typeof value === 'object') {
    const v = value as { toNumber?: () => number; low?: number; high?: number };
    if (typeof v.toNumber === 'function') return Math.floor(v.toNumber());
    if (typeof v.low === 'number' && typeof v.high === 'number') return v.high * 4294967296 + (v.low >>> 0);
  }
  return 0;
}

type ImageFields = { mimetype?: string | null; url?: string | null; directPath?: string | null; mediaKey?: unknown; fileLength?: unknown };

/** The descriptor of an inbound image, or null when it can't be fetched
 * (no key: a view-once photo, say, which WhatsApp keeps from linked devices). */
export function inboxMedia(image: ImageFields): InboxMedia | null {
  const mediaKey = base64Of(image.mediaKey);
  const url = image.url || null;
  const directPath = image.directPath || null;
  if (!mediaKey || (!url && !directPath)) return null;
  return { kind: 'image', mimetype: image.mimetype || null, url, direct_path: directPath, media_key: mediaKey, file_length: longValue(image.fileLength) };
}

function bad(detail: string): never {
  throw new GatewayError('bad_request', detail);
}

/** send_media's arguments (after fencing). */
export function parseOutboundMedia(args: Record<string, unknown>): OutboundMedia {
  const kind = args.kind;
  if (kind !== 'image' && kind !== 'video') bad('kind must be "image" or "video"');
  const mimetype = args.mimetype;
  if (typeof mimetype !== 'string' || !/^(image|video)\/[A-Za-z0-9.+-]{1,60}$/.test(mimetype) || !mimetype.startsWith(`${kind}/`)) {
    bad(`mimetype must be ${kind}/<type>`);
  }
  const data = args.data;
  if (typeof data !== 'string' || data.length === 0) bad('data must be the file as base64');
  if (data.length > MAX_BASE64_LENGTH) bad(`the file is larger than ${MAX_MEDIA_BYTES} bytes`);
  if (!BASE64.test(data)) bad('data is not base64');
  const bytes = Buffer.from(data, 'base64');
  if (bytes.length === 0) bad('data is empty');
  const viewOnce = args.view_once;
  if (viewOnce !== undefined && viewOnce !== null && typeof viewOnce !== 'boolean') bad('view_once must be true or false');
  const caption = args.caption;
  if (caption !== undefined && caption !== null && (typeof caption !== 'string' || caption.length > MAX_CAPTION_LENGTH)) {
    bad(`caption must be text of at most ${MAX_CAPTION_LENGTH} characters`);
  }
  return { kind, data: bytes, mimetype, viewOnce: viewOnce === true, ...(caption ? { caption } : {}) };
}

/** download_media's `media` argument: an inbox descriptor. */
export function parseDownloadRequest(media: unknown): DownloadRequest {
  if (!media || typeof media !== 'object' || Array.isArray(media)) bad('media must be an inbox media descriptor');
  const m = media as Record<string, unknown>;
  const kind = m.kind;
  if (kind !== 'image' && kind !== 'video') bad('media.kind must be "image" or "video"');
  const key = m.media_key;
  if (typeof key !== 'string' || !key.length || key.length > 200 || !BASE64.test(key)) bad('media.media_key must be base64');
  const url = typeof m.url === 'string' && m.url ? m.url : undefined;
  const directPath = typeof m.direct_path === 'string' && m.direct_path ? m.direct_path : undefined;
  if (!url && !directPath) bad('media needs a url or a direct_path');
  if (url && !url.startsWith('https://')) bad('media.url must be https');
  if (directPath && !directPath.startsWith('/')) bad('media.direct_path must start with /');
  if (longValue(m.file_length) > MAX_MEDIA_BYTES) bad(`the file is larger than ${MAX_MEDIA_BYTES} bytes`);
  const mimetype = typeof m.mimetype === 'string' ? m.mimetype : null;
  return { kind, mediaKey: Buffer.from(key, 'base64'), ...(url ? { url } : {}), ...(directPath ? { directPath } : {}), mimetype };
}

/** A download stream into one buffer, refused past `max` bytes. */
export async function collect(stream: Readable | AsyncIterable<Uint8Array>, max = MAX_MEDIA_BYTES): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of stream as AsyncIterable<Uint8Array>) {
    total += chunk.length;
    if (total > max) {
      (stream as Readable).destroy?.();
      bad(`the file is larger than ${max} bytes`);
    }
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}
