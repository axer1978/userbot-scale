/**
 * Deterministic browser tuple per session, for parity with the Python side
 * (which will own this mapping later and pass the tuple in `open`/`pair`).
 *
 * Mapping: sha256(utf8(session_id)) -> first 4 bytes as a big-endian uint32
 * -> modulo the candidate list below -> Browsers.<os>(<browser>) from
 * Baileys. The list is small and stable on purpose: WhatsApp shows the
 * tuple in "Linked devices", and a session must keep presenting the same
 * one across restarts (a changed tuple looks like a new device).
 *
 * Python equivalent:
 *   idx = int.from_bytes(hashlib.sha256(session_id.encode()).digest()[:4], "big") % len(CANDIDATES)
 */
import { createHash } from 'node:crypto';
import { Browsers } from 'baileys';

export type BrowserTuple = [string, string, string];

export const BROWSER_CANDIDATES: ReadonlyArray<readonly [os: 'macOS' | 'windows' | 'ubuntu', browser: string]> = [
  ['macOS', 'Chrome'],
  ['macOS', 'Safari'],
  ['windows', 'Chrome'],
  ['windows', 'Edge'],
  ['ubuntu', 'Chrome'],
  ['ubuntu', 'Firefox'],
];

export function candidateIndex(sessionId: string, count: number = BROWSER_CANDIDATES.length): number {
  const digest = createHash('sha256').update(sessionId, 'utf8').digest();
  return digest.readUInt32BE(0) % count;
}

export function browserFor(sessionId: string): BrowserTuple {
  const [os, browser] = BROWSER_CANDIDATES[candidateIndex(sessionId)]!;
  return Browsers[os](browser);
}

/** Validates a tuple received over the bus; returns null when malformed. */
export function parseBrowserTuple(value: unknown): BrowserTuple | null {
  if (!Array.isArray(value) || value.length !== 3) return null;
  if (!value.every((v) => typeof v === 'string' && v.length > 0 && v.length <= 64)) return null;
  return [value[0], value[1], value[2]];
}
