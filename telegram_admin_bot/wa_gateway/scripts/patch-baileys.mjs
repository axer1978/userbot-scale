// View-once photos and videos sent through Baileys 7.0.0-rc14 never reach
// the other phone: relayMessage reads the media type off the outer
// viewOnceMessage wrapper, finds none, and the `mediatype` attribute every
// media stanza needs is left out, so WhatsApp drops the message without an
// error. WhatsApp Web also tags a view-once send with <meta view_once="true"/>
// on the stanza, which Baileys does not. Both fixes are open upstream and
// unreleased (WhiskeySockets/Baileys #2836, #2837); this applies them to the
// installed copy. Run by npm after every install (the postinstall script).
// A Baileys update that moves this code fails the install here, instead of
// silently shipping view-once that does not arrive.
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const BAILEYS_VERSION = '7.0.0-rc14';
export const MARKER = '/* userbot: view-once patch */';

const PATCHES = [
  {
    what: 'media type read through the view-once wrapper',
    from: 'const mediaType = getMediaType(message);',
    to: `${MARKER} const mediaType = getMediaType(normalizeMessageContent(message) ?? message);`,
  },
  {
    what: 'view-once stanza tag',
    from: '            if (additionalNodes && additionalNodes.length > 0) {',
    to: `            ${MARKER}
            {
                const inner = message.ephemeralMessage?.message ?? message;
                const once = (inner.viewOnceMessage || inner.viewOnceMessageV2 || inner.viewOnceMessageV2Extension)?.message;
                if (once?.imageMessage || once?.videoMessage || once?.ptvMessage || once?.audioMessage) {
                    stanza.content.push({ tag: 'meta', attrs: { view_once: 'true' } });
                }
            }
            if (additionalNodes && additionalNodes.length > 0) {`,
  },
];

/** The patched source of messages-send.js; the same text when already patched. */
export function patchSource(source) {
  if (source.includes(MARKER)) return source;
  let out = source;
  for (const p of PATCHES) {
    const count = out.split(p.from).length - 1;
    if (count !== 1) {
      throw new Error(`patch-baileys: "${p.what}" expected 1 match, found ${count}. ` +
        'Baileys changed: check whether the view-once fixes are released and update or drop this patch.');
    }
    out = out.replace(p.from, p.to);
  }
  return out;
}

function baileysRoot() {
  const require = createRequire(import.meta.url);
  // The package's own package.json: its exports may not allow the deep path.
  let dir = dirname(require.resolve('baileys'));
  for (;;) {
    try {
      const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
      if (pkg.name === 'baileys') return { dir, version: pkg.version };
    } catch { /* not this level */ }
    const up = dirname(dir);
    if (up === dir) throw new Error('patch-baileys: baileys is not installed');
    dir = up;
  }
}

export function main() {
  const { dir, version } = baileysRoot();
  if (version !== BAILEYS_VERSION) {
    throw new Error(`patch-baileys: written for baileys ${BAILEYS_VERSION}, found ${version}. ` +
      'Check whether the view-once fixes are released and update or drop this patch.');
  }
  const file = join(dir, 'lib', 'Socket', 'messages-send.js');
  const before = readFileSync(file, 'utf8');
  const after = patchSource(before);
  if (after !== before) writeFileSync(file, after);
  console.log(after === before ? 'patch-baileys: already applied' : 'patch-baileys: view-once fix applied');
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
