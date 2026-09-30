import test from 'node:test';
import assert from 'node:assert/strict';
import { isDirectChatJid, isLidJid, isPnJid, shouldIgnoreJid } from '../src/jid.ts';
import { browserFor, candidateIndex, parseBrowserTuple, BROWSER_CANDIDATES } from '../src/browser.ts';
import { LruCache } from '../src/lru.ts';

test('groups, status, broadcast lists and newsletters are ignored at the socket', () => {
  for (const jid of ['123-456@g.us', '120363000000@g.us', 'status@broadcast', '1234567890@broadcast', '120363@newsletter']) {
    assert.equal(shouldIgnoreJid(jid), true, jid);
    assert.equal(isDirectChatJid(jid), false, jid);
  }
});

test('1:1 chats by phone number or LID pass', () => {
  assert.equal(shouldIgnoreJid('34600000000@s.whatsapp.net'), false);
  assert.equal(shouldIgnoreJid('987654321@lid'), false);
  assert.equal(isDirectChatJid('34600000000@s.whatsapp.net'), true);
  assert.equal(isDirectChatJid('987654321@lid'), true);
  assert.equal(isDirectChatJid('weird@c.us'), false);
  assert.equal(isDirectChatJid(undefined), false);
  assert.equal(shouldIgnoreJid(undefined), false);
  assert.equal(isPnJid('1@s.whatsapp.net'), true);
  assert.equal(isPnJid('1@lid'), false);
  assert.equal(isLidJid('1@lid'), true);
});

test('browser tuple is deterministic per session and one of the candidates', () => {
  const a = browserFor('acct01');
  assert.deepEqual(a, browserFor('acct01'));
  assert.equal(a.length, 3);
  // int.from_bytes(sha256(s)[:4], "big") % 6, computed independently in Python.
  assert.equal(BROWSER_CANDIDATES.length, 6);
  assert.equal(candidateIndex('acct01'), 60206502 % 6);
  assert.equal(candidateIndex('a'), 3398926610 % 6);
  assert.equal(candidateIndex('c'), 779955203 % 6);
  assert.equal(candidateIndex('wa-demo'), 1425709145 % 6);
  const seen = new Set(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'].map((s) => browserFor(s).join('|')));
  assert.ok(seen.size > 1, 'different sessions spread over candidates');
  assert.deepEqual(parseBrowserTuple(['macOS', 'Chrome', '1.0']), ['macOS', 'Chrome', '1.0']);
  assert.equal(parseBrowserTuple(['macOS', 'Chrome']), null);
  assert.equal(parseBrowserTuple(['macOS', 1, 'x']), null);
  assert.equal(parseBrowserTuple('macOS'), null);
});

test('LRU cache is bounded and refreshes on read', () => {
  const c = new LruCache<string, number>(2);
  c.set('a', 1);
  c.set('b', 2);
  assert.equal(c.get('a'), 1);
  c.set('c', 3); // evicts b (least recently used)
  assert.equal(c.get('b'), undefined);
  assert.equal(c.get('a'), 1);
  assert.equal(c.get('c'), 3);
  assert.equal(c.size, 2);
  assert.throws(() => new LruCache(0));
});
