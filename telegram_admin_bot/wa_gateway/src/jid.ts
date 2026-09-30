/**
 * Which JIDs this gateway serves: 1:1 chats only. Groups, status, broadcast
 * lists and newsletters are ignored at the socket level (shouldIgnoreJid),
 * so Baileys never even decrypts them. Pure string logic, no Baileys import,
 * so it is trivially unit-testable.
 */

export const PN_SUFFIX = '@s.whatsapp.net';
export const LID_SUFFIX = '@lid';

export function isGroupJid(jid: string): boolean {
  return jid.endsWith('@g.us');
}

export function isBroadcastJid(jid: string): boolean {
  // Covers status@broadcast and broadcast lists (<id>@broadcast).
  return jid.endsWith('@broadcast');
}

export function isNewsletterJid(jid: string): boolean {
  return jid.endsWith('@newsletter');
}

export function isPnJid(jid: string | null | undefined): jid is string {
  return typeof jid === 'string' && jid.endsWith(PN_SUFFIX);
}

export function isLidJid(jid: string | null | undefined): jid is string {
  return typeof jid === 'string' && jid.endsWith(LID_SUFFIX);
}

/** True when the socket should drop everything about this JID. */
export function shouldIgnoreJid(jid: string | undefined | null): boolean {
  if (!jid) return false;
  return isGroupJid(jid) || isBroadcastJid(jid) || isNewsletterJid(jid);
}

/** True for a chat the inbox accepts: a 1:1 chat addressed by PN or LID. */
export function isDirectChatJid(jid: string | null | undefined): boolean {
  if (!jid || shouldIgnoreJid(jid)) return false;
  return isPnJid(jid) || isLidJid(jid);
}
