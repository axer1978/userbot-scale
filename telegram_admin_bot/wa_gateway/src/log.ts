/**
 * One pino logger for the process. Level from WA_GATEWAY_LOG_LEVEL
 * (default "info"). Rules enforced by the call sites, not here:
 *   - never log message text at info level (lengths only);
 *   - never log key material at any level (creds/keys are opaque blobs to
 *     every log line in this codebase).
 * Baileys itself gets a child logger capped at "warn" unless the gateway is
 * at debug/trace: at trace Baileys prints decoded frames, which can include
 * message text. That is a deliberate operator choice, not the default.
 */
import pino from 'pino';

const LEVELS = new Set(['fatal', 'error', 'warn', 'info', 'debug', 'trace', 'silent']);

export function levelFromEnv(env: NodeJS.ProcessEnv = process.env): string {
  const raw = (env.WA_GATEWAY_LOG_LEVEL ?? 'info').trim().toLowerCase();
  return LEVELS.has(raw) ? raw : 'info';
}

export const log = pino({ level: levelFromEnv(), base: { service: 'wa-gateway' } });

/** Logger handed to Baileys sockets. */
export function baileysLogger(sessionId: string): pino.Logger {
  const gatewayLevel = log.level;
  const level = gatewayLevel === 'debug' || gatewayLevel === 'trace' ? gatewayLevel : 'warn';
  return log.child({ lib: 'baileys', session_id: sessionId }, { level });
}
