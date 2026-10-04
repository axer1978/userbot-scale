/**
 * Step 6: what a failed socket primitive means to the runtime.
 *
 * How Baileys surfaces failures (7.0.0-rc14, read from the source):
 * - `relayMessage` only `sendNode`s the stanza; it never waits for the
 *   server's ack. A rejected message therefore does NOT throw from
 *   `sendMessage`; it shows up later as a receipt/status update. What CAN
 *   throw around a send are the IQ queries it makes first: the USync
 *   device lookup and the pre-key fetch (`assertSessions`), plus the
 *   `onWhatsApp` existence check the gateway performs itself.
 * - IQ errors are `Boom(errNode.attrs.text, { data: +errNode.attrs.code })`
 *   (WABinary/generic-utils assertNodeErrorFree): `message` is the stanza
 *   text ("rate-overlimit", "not-authorized", "item-not-found", ...) and
 *   `data` the numeric code (429, 403, 404). `output.statusCode` is 500
 *   by default for those, so the code is read from `data` too.
 * - A closed socket throws Boom 428 "Connection Closed"; a query that
 *   never got its answer throws Boom 408 "Timed Out"; a logged-out device
 *   gives Boom 401 / "Not authenticated".
 * - A recipient that is not on WhatsApp does not throw at all: the USync
 *   lookup returns no devices and the stanza goes to our own devices only.
 *   That is why `sendText` calls `onWhatsApp` first (cached per jid).
 */
import type { ErrorKind } from './bus.ts';
import { statusCodeOf } from './disconnect.ts';

export type SendFailure = { kind: ErrorKind; detail: string };

function numericData(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const data = (error as { data?: unknown }).data;
  return typeof data === 'number' && Number.isFinite(data) ? data : undefined;
}

export function classifySendError(error: unknown): SendFailure {
  const message = error instanceof Error ? error.message : String(error ?? 'unknown error');
  const text = message.toLowerCase();
  const boomCode = statusCodeOf(error);
  // Boom's default statusCode is 500; a stanza code in `data` is more specific.
  const dataCode = numericData(error);
  const code = dataCode ?? (boomCode !== 500 ? boomCode : undefined);
  const detail = `${message}${code !== undefined ? ` (${code})` : ''}`;

  if (code === 429 || text.includes('rate-overlimit') || text.includes('rate limit')) return { kind: 'rate_limited', detail };
  if (code === 401 || text.includes('not authenticated') || text.includes('logged out') || text.includes('intentional logout')) {
    return { kind: 'session_lost', detail };
  }
  if (code === 403 || text.includes('not-authorized') || text.includes('forbidden')) return { kind: 'blocked', detail };
  if (code === 404 || text.includes('item-not-found') || text.includes('not on whatsapp')) return { kind: 'not_on_whatsapp', detail };
  if (code === 428 || code === 408 || code === 503 || text.includes('connection closed') || text.includes('connection terminated') || text.includes('timed out')) {
    return { kind: 'not_connected', detail };
  }
  return { kind: 'other', detail };
}
