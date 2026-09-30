/**
 * Cross-language test helper: lets the Python suite drive crypto.ts.
 *
 *   node dist/cryptotool.js encrypt <aad>   # stdin: plaintext hex -> stdout: blob hex
 *   node dist/cryptotool.js decrypt <aad>   # stdin: blob hex      -> stdout: plaintext hex
 *
 * Key material comes from the usual env (USERBOT_MASTER_KEY[_FILE]).
 * Hex in, hex out, so binary survives every shell on every platform.
 */
import { readFileSync } from 'node:fs';
import { decrypt, encrypt } from './crypto.ts';

const [mode, aad] = process.argv.slice(2);
if ((mode !== 'encrypt' && mode !== 'decrypt') || aad === undefined) {
  process.stderr.write('usage: cryptotool (encrypt|decrypt) <aad>\n');
  process.exit(64);
}
const input = Buffer.from(readFileSync(0, 'utf8').trim(), 'hex');
const aadBuf = Buffer.from(aad, 'utf8');
const out = mode === 'encrypt' ? encrypt(input, aadBuf) : decrypt(input, aadBuf);
process.stdout.write(out.toString('hex'));
