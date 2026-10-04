/**
 * TEMPORARY manual-test CLI (step 3). Runs the same pairing / socket code
 * as the bus handlers, but standalone: no Valkey, and `listen` opens a
 * socket WITHOUT the lease handshake (it only refuses when a live lease
 * exists, so it cannot fight a running worker). Remove once the Python
 * runtime drives the gateway (step 4/5).
 *
 *   node dist/cli.js pair <session_id> [--phone <digits>]
 *   node dist/cli.js listen <session_id>
 */
import qrcode from 'qrcode-terminal';
import { browserFor } from './browser.ts';
import { loadConfig } from './config.ts';
import { loadKeyring } from './crypto.ts';
import { createPool, readSessionRow } from './db.ts';
import { log } from './log.ts';
import { PairingRun } from './pairing.ts';
import { SessionSocket } from './session.ts';

function usage(): never {
  process.stderr.write('usage: node dist/cli.js pair <session_id> [--phone <digits>]\n       node dist/cli.js listen <session_id>\n');
  process.exit(64);
}

function flag(argv: string[], name: string): string | undefined {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
}

async function main(): Promise<number> {
  const [cmd, sessionId, ...rest] = process.argv.slice(2);
  if (!cmd || !sessionId) usage();
  loadKeyring();
  const config = loadConfig();
  const pool = createPool(config.databaseUrl, { max: 3 });
  try {
    const row = await readSessionRow(pool, sessionId);
    if (!row) {
      log.error({ session_id: sessionId }, 'no such session row in telegram_sessions');
      return 1;
    }
    if (row.live) {
      log.error({ session_id: sessionId }, 'session has a LIVE lease: a worker is running it. Refusing (one socket per account).');
      return 1;
    }
    if (row.channel !== 'whatsapp') {
      log.error({ session_id: sessionId, channel: row.channel }, 'session channel is not whatsapp');
      return 1;
    }
    if (cmd === 'pair') {
      const phone = flag(rest, '--phone');
      const run = new PairingRun({
        pool,
        sessionId,
        pairId: 'cli',
        method: phone ? 'code' : 'qr',
        phone,
        browser: browserFor(sessionId),
        log,
        emit: (event) => {
          if (event.type === 'qr') {
            process.stdout.write('\nScan this with WhatsApp > Linked devices > Link a device:\n');
            qrcode.generate(event.qr, { small: true });
          } else if (event.type === 'code') {
            process.stdout.write(`\nPairing code: ${event.code}\n(WhatsApp > Linked devices > Link a device > Link with phone number)\n`);
          } else {
            process.stdout.write(`\n${JSON.stringify(event)}\n`);
          }
        },
      });
      process.on('SIGINT', () => run.cancel('SIGINT'));
      const outcome = await run.run();
      return outcome.ok ? 0 : 1;
    }
    if (cmd === 'listen') {
      log.warn('MANUAL TEST MODE: opening a socket without a lease. Stop it (Ctrl-C) before starting the runtime for this account.');
      const session = new SessionSocket({
        pool,
        sessionId,
        epoch: row.lease_epoch,
        browser: browserFor(sessionId),
        log,
        emit: (event) => log.info({ event }, 'event'),
      });
      await session.start();
      await new Promise<void>((resolve) => {
        process.on('SIGINT', () => void session.close('SIGINT').then(resolve));
        process.on('SIGTERM', () => void session.close('SIGTERM').then(resolve));
      });
      return 0;
    }
    usage();
  } finally {
    await pool.end();
  }
}

main().then(
  (code) => process.exit(code),
  (exc) => {
    log.fatal({ err: exc }, 'cli failed');
    process.exit(1);
  },
);
