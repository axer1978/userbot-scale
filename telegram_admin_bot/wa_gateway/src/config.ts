/** Environment the gateway reads at boot. Nothing else is configurable. */

export type Config = {
  databaseUrl: string;
  redisUrl: string;
  /** Command channel the gateway subscribes to (commands.py: cmd:<session_id>). */
  commandChannel: string;
};

export const GATEWAY_ADDRESS = '@wa-gateway';

/**
 * Advisory-lock key for the singleton guarantee. 64-bit form of the ASCII
 * bytes "wagw" (0x77616777 = 2003332983). Nothing else in this codebase
 * uses pg_advisory_lock with an integer key (scheduler.py uses its own).
 */
export const SINGLETON_LOCK_KEY = 2003332983;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const databaseUrl = env.DATABASE_URL;
  if (!databaseUrl) throw new Error('DATABASE_URL must be set');
  const redisUrl = env.REDIS_URL;
  if (!redisUrl) throw new Error('REDIS_URL must be set');
  return { databaseUrl, redisUrl, commandChannel: `cmd:${GATEWAY_ADDRESS}` };
}
