/**
 * The one place a Baileys socket is configured. Both the pairing run and
 * the session socket use it so the two can never drift apart (same
 * browser tuple handling, same JID filter, same no-history settings).
 */
import makeWASocket from 'baileys';
import type { AuthenticationState, WAMessageKey, WASocket, proto } from 'baileys';
import type pino from 'pino';
import type { BrowserTuple } from './browser.ts';
import { shouldIgnoreJid } from './jid.ts';
import { LruCache } from './lru.ts';

export const GET_MESSAGE_CACHE_SIZE = 500;

export type SocketDeps = {
  state: AuthenticationState;
  browser: BrowserTuple;
  logger: pino.Logger;
  /** Recent outbound messages by id, for retry receipts. Transport-level only. */
  outbox?: LruCache<string, proto.IMessage>;
};

export function makeGatewaySocket(deps: SocketDeps): WASocket {
  const outbox = deps.outbox ?? new LruCache<string, proto.IMessage>(GET_MESSAGE_CACHE_SIZE);
  return makeWASocket({
    auth: deps.state,
    browser: deps.browser,
    logger: deps.logger,
    // Presence is business logic (humanlike.py decides when to look online).
    markOnlineOnConnect: false,
    // The inbox starts at pairing time; no history import, ever.
    syncFullHistory: false,
    shouldSyncHistoryMessage: () => false,
    shouldIgnoreJid: (jid) => shouldIgnoreJid(jid),
    getMessage: async (key: WAMessageKey) => (key.id ? outbox.get(key.id) : undefined),
    // Our own sends will be echoed back through messages.upsert as from_me.
    emitOwnEvents: true,
  });
}
