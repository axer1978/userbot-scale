import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { GatewayError } from '../src/bus.ts';
import { collect, inboxMedia, longValue, parseDownloadRequest, parseOutboundMedia } from '../src/media.ts';

function kindOf(fn: () => unknown): string | null {
  try {
    fn();
    return null;
  } catch (exc) {
    return exc instanceof GatewayError ? exc.kind : 'thrown';
  }
}

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]).toString('base64');

test('send_media arguments: a photo or video as base64 with a matching mimetype', () => {
  const out = parseOutboundMedia({ kind: 'image', data: JPEG, mimetype: 'image/jpeg', view_once: true });
  assert.equal(out.kind, 'image');
  assert.equal(out.mimetype, 'image/jpeg');
  assert.equal(out.viewOnce, true);
  assert.deepEqual([...out.data.subarray(0, 3)], [0xff, 0xd8, 0xff]);
  assert.equal(parseOutboundMedia({ kind: 'video', data: JPEG, mimetype: 'video/mp4' }).viewOnce, false);
  assert.equal(parseOutboundMedia({ kind: 'image', data: JPEG, mimetype: 'image/png', caption: 'hi' }).caption, 'hi');

  assert.equal(kindOf(() => parseOutboundMedia({ kind: 'audio', data: JPEG, mimetype: 'audio/ogg' })), 'bad_request');
  assert.equal(kindOf(() => parseOutboundMedia({ kind: 'image', data: JPEG, mimetype: 'video/mp4' })), 'bad_request');
  assert.equal(kindOf(() => parseOutboundMedia({ kind: 'image', data: '', mimetype: 'image/jpeg' })), 'bad_request');
  assert.equal(kindOf(() => parseOutboundMedia({ kind: 'image', data: 'not base64!', mimetype: 'image/jpeg' })), 'bad_request');
  assert.equal(kindOf(() => parseOutboundMedia({ kind: 'image', data: JPEG, mimetype: 'image/jpeg', view_once: 'yes' })), 'bad_request');
  assert.equal(kindOf(() => parseOutboundMedia({ kind: 'image', data: JPEG, mimetype: 'image/jpeg', caption: 'x'.repeat(1025) })), 'bad_request');
});

test('an inbound image becomes a download descriptor, unless it cannot be fetched', () => {
  const key = new Uint8Array(32).fill(7);
  const media = inboxMedia({ mimetype: 'image/jpeg', url: 'https://mmg.whatsapp.net/x', directPath: '/v/t62/x', mediaKey: key,
                             fileLength: { low: 1234, high: 0 } });
  assert.deepEqual(media, { kind: 'image', mimetype: 'image/jpeg', url: 'https://mmg.whatsapp.net/x', direct_path: '/v/t62/x',
                            media_key: Buffer.from(key).toString('base64'), file_length: 1234 });
  assert.equal(inboxMedia({ mimetype: 'image/jpeg', directPath: '/v/x' }), null, 'no key (view-once on a linked device)');
  assert.equal(inboxMedia({ mediaKey: key }), null, 'nowhere to fetch it from');
});

test('download_media arguments: the descriptor, checked', () => {
  const descriptor = { kind: 'image', media_key: Buffer.alloc(32, 1).toString('base64'), direct_path: '/v/t62/x', url: 'https://mmg.whatsapp.net/x',
                       mimetype: 'image/jpeg', file_length: 1000 };
  const req = parseDownloadRequest(descriptor);
  assert.equal(req.kind, 'image');
  assert.equal(req.mediaKey.length, 32);
  assert.equal(req.directPath, '/v/t62/x');
  assert.equal(req.mimetype, 'image/jpeg');

  assert.equal(kindOf(() => parseDownloadRequest(null)), 'bad_request');
  assert.equal(kindOf(() => parseDownloadRequest({ ...descriptor, media_key: '' })), 'bad_request');
  assert.equal(kindOf(() => parseDownloadRequest({ ...descriptor, url: 'http://evil.example/x', direct_path: null })), 'bad_request');
  assert.equal(kindOf(() => parseDownloadRequest({ ...descriptor, url: null, direct_path: null })), 'bad_request');
  assert.equal(kindOf(() => parseDownloadRequest({ ...descriptor, file_length: 100 * 1024 * 1024 })), 'bad_request');
});

test('collect joins a stream and refuses one past the limit', async () => {
  assert.equal((await collect(Readable.from([Buffer.from('ab'), Buffer.from('cd')]), 10)).toString(), 'abcd');
  await assert.rejects(collect(Readable.from([Buffer.alloc(6), Buffer.alloc(6)]), 10), (exc: unknown) =>
    exc instanceof GatewayError && exc.kind === 'bad_request');
});

test('longValue reads numbers, strings and protobuf Longs', () => {
  assert.equal(longValue(5), 5);
  assert.equal(longValue('7'), 7);
  assert.equal(longValue({ toNumber: () => 9 }), 9);
  assert.equal(longValue({ low: 1, high: 1 }), 4294967297);
  assert.equal(longValue(null), 0);
});
