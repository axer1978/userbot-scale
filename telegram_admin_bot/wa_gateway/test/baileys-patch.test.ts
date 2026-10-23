import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { MARKER, patchSource } from '../scripts/patch-baileys.mjs';

const SAMPLE = [
  '        await authState.keys.transaction(async () => {',
  '            const mediaType = getMediaType(message);',
  '            if (mediaType) {',
  "                extraAttrs['mediatype'] = mediaType;",
  '            }',
  '            if (additionalNodes && additionalNodes.length > 0) {',
  '                stanza.content.push(...additionalNodes);',
  '            }',
].join('\n');

test('the media type is read through the view-once wrapper, and view-once sends are tagged', () => {
  const out = patchSource(SAMPLE);
  assert.match(out, /getMediaType\(normalizeMessageContent\(message\) \?\? message\)/);
  assert.doesNotMatch(out, /getMediaType\(message\)/);
  assert.match(out, /tag: 'meta', attrs: \{ view_once: 'true' \}/);
  assert.ok(out.indexOf("view_once: 'true'") < out.indexOf('if (additionalNodes'));
});

test('patching twice changes nothing', () => {
  const once = patchSource(SAMPLE);
  assert.equal(patchSource(once), once);
});

test('a Baileys that moved the code fails the install instead of shipping a broken view once', () => {
  assert.throws(() => patchSource(SAMPLE.replace('getMediaType(message)', 'mediaTypeOf(message)')),
    /media type read through the view-once wrapper.*found 0/);
  assert.throws(() => patchSource(SAMPLE.replace('if (additionalNodes', 'if (extraNodes')),
    /view-once stanza tag.*found 0/);
});

test('the installed Baileys carries the patch (npm ran the postinstall script)', () => {
  const require = createRequire(import.meta.url);
  const lib = dirname(require.resolve('baileys'));
  const source = readFileSync(join(lib, 'Socket', 'messages-send.js'), 'utf8');
  assert.ok(source.includes(MARKER), 'run `npm install` (or node scripts/patch-baileys.mjs)');
  assert.match(source, /getMediaType\(normalizeMessageContent\(message\) \?\? message\)/);
  assert.match(source, /view_once: 'true'/);
});
