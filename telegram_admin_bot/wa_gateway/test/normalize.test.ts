import test from 'node:test';
import assert from 'node:assert/strict';
import { describeForLog, normalizeMessage, timestampSeconds, unwrapContent, type MessageLike } from '../src/normalize.ts';

const opts = { sessionId: 'wa1', epoch: 3 };

function msg(over: Partial<MessageLike> & { key?: MessageLike['key'] }): MessageLike {
  return {
    key: { remoteJid: '34600000000@s.whatsapp.net', fromMe: false, id: 'ABC123', addressingMode: 'pn' },
    messageTimestamp: 1_700_000_000,
    pushName: 'Ana',
    ...over,
  };
}

test('plain conversation text, phone-number addressing', () => {
  const out = normalizeMessage(msg({ message: { conversation: 'hola' } }), opts);
  assert.deepEqual(out, {
    v: 1,
    session_id: 'wa1',
    epoch: 3,
    wa_message_id: 'ABC123',
    jid: '34600000000@s.whatsapp.net',
    jid_alt: null,
    phone_jid: '34600000000@s.whatsapp.net',
    lid: null,
    push_name: 'Ana',
    from_me: false,
    type: 'text',
    text: 'hola',
    quoted_id: null,
    ts: 1_700_000_000,
  });
  const logged = describeForLog(out!);
  assert.equal(logged.text_len, 4);
  assert.ok(!('text' in logged), 'text never reaches the log');
});

test('extended text with a quoted message id', () => {
  const out = normalizeMessage(
    msg({ message: { extendedTextMessage: { text: 'reply', contextInfo: { stanzaId: 'Q1', participant: 'x@s.whatsapp.net' } } } }),
    opts,
  );
  assert.equal(out?.type, 'text');
  assert.equal(out?.text, 'reply');
  assert.equal(out?.quoted_id, 'Q1');
});

test('image caption becomes type image; no caption -> text null', () => {
  const withCaption = normalizeMessage(msg({ message: { imageMessage: { caption: 'look', mimetype: 'image/jpeg', contextInfo: { stanzaId: 'Q2' } } } }), opts);
  assert.equal(withCaption?.type, 'image');
  assert.equal(withCaption?.text, 'look');
  assert.equal(withCaption?.quoted_id, 'Q2');
  const plain = normalizeMessage(msg({ message: { imageMessage: { mimetype: 'image/jpeg' } } }), opts);
  assert.equal(plain?.type, 'image');
  assert.equal(plain?.text, null);
});

test('LID addressing: jid is the LID, phone_jid comes from remoteJidAlt', () => {
  const out = normalizeMessage(
    msg({ key: { remoteJid: '9876@lid', remoteJidAlt: '34600000000@s.whatsapp.net', fromMe: false, id: 'L1', addressingMode: 'lid' }, message: { conversation: 'x' } }),
    opts,
  );
  assert.equal(out?.jid, '9876@lid');
  assert.equal(out?.jid_alt, '34600000000@s.whatsapp.net');
  assert.equal(out?.phone_jid, '34600000000@s.whatsapp.net');
  assert.equal(out?.lid, '9876@lid');
  const pnWithAlt = normalizeMessage(
    msg({ key: { remoteJid: '34600000000@s.whatsapp.net', remoteJidAlt: '9876@lid', fromMe: false, id: 'L2', addressingMode: 'pn' }, message: { conversation: 'x' } }),
    opts,
  );
  assert.equal(pnWithAlt?.phone_jid, '34600000000@s.whatsapp.net');
  assert.equal(pnWithAlt?.lid, '9876@lid');
});

test('from_me messages are kept and flagged', () => {
  const out = normalizeMessage(msg({ key: { remoteJid: '34600000000@s.whatsapp.net', fromMe: true, id: 'M1' }, message: { conversation: 'sent by us' } }), opts);
  assert.equal(out?.from_me, true);
});

test('other content kinds are kept as type other with text null', () => {
  const out = normalizeMessage(msg({ message: { audioMessage: { ptt: true, contextInfo: { stanzaId: 'Q3' } } } }), opts);
  assert.equal(out?.type, 'other');
  assert.equal(out?.text, null);
  assert.equal(out?.quoted_id, 'Q3');
  const doc = normalizeMessage(msg({ message: { documentMessage: { fileName: 'a.pdf' } } }), opts);
  assert.equal(doc?.type, 'other');
});

test('ephemeral / view-once envelopes are unwrapped', () => {
  const out = normalizeMessage(msg({ message: { ephemeralMessage: { message: { extendedTextMessage: { text: 'vanishing' } } } } }), opts);
  assert.equal(out?.text, 'vanishing');
  const vo = normalizeMessage(msg({ message: { viewOnceMessageV2: { message: { imageMessage: { caption: 'once' } } } } }), opts);
  assert.equal(vo?.type, 'image');
  assert.equal(vo?.text, 'once');
  assert.equal(unwrapContent(undefined), undefined);
});

test('dropped: groups, status, broadcast, newsletters, protocol, reactions, stubs, undecryptable, no id', () => {
  const drop = (m: MessageLike) => assert.equal(normalizeMessage(m, opts), null);
  drop(msg({ key: { remoteJid: '123@g.us', id: 'G1', participant: '1@s.whatsapp.net' }, message: { conversation: 'group' } }));
  drop(msg({ key: { remoteJid: 'status@broadcast', id: 'S1' }, message: { conversation: 'status' } }));
  drop(msg({ key: { remoteJid: '123@broadcast', id: 'B1' }, message: { conversation: 'list' } }));
  drop(msg({ key: { remoteJid: '123@newsletter', id: 'N1' }, message: { conversation: 'news' } }));
  drop(msg({ message: { protocolMessage: { type: 0, key: { id: 'X' } } } }));
  drop(msg({ message: { reactionMessage: { text: '👍', key: { id: 'X' } } } }));
  drop(msg({ message: { senderKeyDistributionMessage: {} } }));
  drop(msg({ message: { conversation: 'x' }, messageStubType: 1 }));
  drop(msg({ message: null }));
  drop(msg({ message: undefined }));
  drop(msg({ message: {} }));
  drop(msg({ key: { remoteJid: '34600000000@s.whatsapp.net', id: null }, message: { conversation: 'x' } }));
  drop(msg({ key: { remoteJid: null, id: 'X' }, message: { conversation: 'x' } }));
});

test('timestamps: number, string, Long-like objects', () => {
  assert.equal(timestampSeconds(1.9), 1);
  assert.equal(timestampSeconds('1700000000'), 1_700_000_000);
  assert.equal(timestampSeconds({ toNumber: () => 42 }), 42);
  assert.equal(timestampSeconds({ low: 5, high: 1, unsigned: true }), 4294967301);
  assert.equal(timestampSeconds(undefined), 0);
  assert.equal(timestampSeconds(null), 0);
});
