/**
 * Boot: key material -> Postgres -> singleton lock -> Valkey -> serve.
 * Exits non-zero when another gateway holds the lock (compose restarts us,
 * and the retry is what a failover looks like).
 */
import { Bus } from './bus.ts';
import { loadConfig, SINGLETON_LOCK_KEY } from './config.ts';
import { loadKeyring, MissingKeyError } from './crypto.ts';
import { acquireSingletonLock, createPool } from './db.ts';
import { Gateway } from './gateway.ts';
import { log } from './log.ts';

async function main(): Promise<void> {
  const config = loadConfig();
  try {
    const keyring = loadKeyring();
    log.info({ key_id: keyring.activeId, keys: keyring.keys.size }, 'master keyring loaded');
  } catch (exc) {
    if (exc instanceof MissingKeyError) {
      log.fatal({ err: exc.message }, 'refusing to boot without a master key');
      process.exit(2);
    }
    throw exc;
  }

  const pool = createPool(config.databaseUrl);
  const lockClient = await acquireSingletonLock(pool);
  if (!lockClient) {
    log.fatal({ lock_key: SINGLETON_LOCK_KEY }, 'another wa-gateway holds the singleton advisory lock; exiting');
    await pool.end();
    process.exit(3);
  }
  log.info({ lock_key: SINGLETON_LOCK_KEY }, 'singleton lock acquired');

  const bus = new Bus(config.redisUrl, log);
  await bus.connect();
  const gateway = new Gateway(pool, (channel, payload) => bus.publish(channel, payload), log);
  gateway.startWatchdog();
  await bus.serve(config.commandChannel, (command) => gateway.handle(command));
  log.info('wa-gateway ready');

  let shuttingDown = false;
  const shutdown = async (signal: string): Promise<void> => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.warn({ signal }, 'shutting down');
    const deadline = setTimeout(() => {
      log.error('shutdown took too long; exiting');
      process.exit(1);
    }, 15_000);
    deadline.unref();
    try {
      await gateway.shutdown(signal);
      await bus.close();
      lockClient.release();
      await pool.end();
    } finally {
      process.exit(0);
    }
  };
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('SIGINT', () => void shutdown('SIGINT'));
}

// A stray rejection is almost always one socket's network promise (Baileys
// fires many); the process serves every number at once, so it is logged
// and the process goes on. An uncaught synchronous exception means state
// nobody can vouch for: log it as JSON (not a bare stack on stderr) and
// exit so compose restarts a clean gateway; the Python side re-opens every
// socket on its next 15 s keepalive.
process.on('unhandledRejection', (reason) => {
  log.error({ err: reason }, 'unhandled rejection');
});
process.on('uncaughtException', (exc) => {
  log.fatal({ err: exc }, 'uncaught exception; exiting for a clean restart');
  process.exit(1);
});

main().catch((exc) => {
  log.fatal({ err: exc }, 'wa-gateway failed to start');
  process.exit(1);
});
