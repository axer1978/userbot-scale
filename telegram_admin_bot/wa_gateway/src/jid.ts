/**
 * Which JIDs this gateway serves: 1:1 chats, and the groups the runtime set
 * as the booker (booking.provider). Every other group, status, broadcast
 * lists and newsletters are ignored at the socket level (shouldIgnoreJid),
 * so Baileys never even decrypts them. Pure string logic, no Baileys import,
 * so it is trivially unit-testable.
 */

export const PN_SUFFIX = '@s.whatsapp.net';
export const LID_SUFFIX = '@lid';
export const GROUP_SUFFIX = '@g.us';

export function isGroupJid(jid: string): boolean {
  return jid.endsWith(GROUP_SUFFIX);
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

/**
 * True when the socket should drop everything about this JID. A group is
 * dropped unless `allowGroup` lets it through (a group set as the booker).
 */
export function shouldIgnoreJid(jid: string | undefined | null, allowGroup?: (jid: string) => boolean): boolean {
  if (!jid) return false;
  if (isGroupJid(jid)) return !allowGroup?.(jid);
  return isBroadcastJid(jid) || isNewsletterJid(jid);
}

/** True for a chat the inbox accepts: a 1:1 chat addressed by PN or LID. */
export function isDirectChatJid(jid: string | null | undefined): boolean {
  if (!jid || shouldIgnoreJid(jid)) return false;
  return isPnJid(jid) || isLidJid(jid);
}

/** "34600123456@s.whatsapp.net" for "34600123456:12@s.whatsapp.net" (a device's JID). */
export function stripDevice(jid: string): string {
  const [user, server] = jid.split('@');
  return `${(user ?? '').split(':')[0]}@${server ?? ''}`;
}
