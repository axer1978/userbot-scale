/**
 * One pino logger for the process. Level from WA_GATEWAY_LOG_LEVEL
 * (default "info"). Rules enforced by the call sites, not here:
 *   - never log message text at info level (lengths only);
 *   - never log key material at any level (creds/keys are opaque blobs to
 *     every log line in this codebase).
 * Baileys itself gets a child logger capped at "warn" unless the gateway is
 * at debug/trace: at trace Baileys prints decoded frames, which can include
 * message text. That is a deliberate operator choice, not the default.
 *
 * Belt and braces for the second rule: the fields Baileys' auth state and
 * pairing use for private keys, the QR and the pairing code are redacted by
 * pino itself (top level or up to two levels down), so a log line handed a
 * creds object, ours or Baileys' at any level, never prints the key bytes.
 */
import pino from 'pino';

const LEVELS = new Set(['fatal', 'error', 'warn', 'info', 'debug', 'trace', 'silent']);

// Baileys AuthenticationCreds / KeyPair field names, plus the pairing credentials.
const SENSITIVE = [
  'creds', 'keys', 'noiseKey', 'pairingEphemeralKeyPair', 'signedIdentityKey', 'signedPreKey',
  'advSecretKey', 'privKey', 'private', 'qr', 'pairingCode',
];
export const REDACT_PATHS = SENSITIVE.flatMap((name) => [name, `*.${name}`, `*.*.${name}`]);

export function levelFromEnv(env: NodeJS.ProcessEnv = process.env): string {
  const raw = (env.WA_GATEWAY_LOG_LEVEL ?? 'info').trim().toLowerCase();
  return LEVELS.has(raw) ? raw : 'info';
}

export function makeLogger(level: string, destination?: pino.DestinationStream): pino.Logger {
  const options: pino.LoggerOptions = {
    level,
    base: { service: 'wa-gateway' },
    redact: { paths: REDACT_PATHS, censor: '[redacted]' },
  };
  return destination ? pino(options, destination) : pino(options);
}

export const log = makeLogger(levelFromEnv());

/** Logger handed to Baileys sockets. */
export function baileysLogger(sessionId: string, root: pino.Logger = log): pino.Logger {
  const gatewayLevel = root.level;
  const level = gatewayLevel === 'debug' || gatewayLevel === 'trace' ? gatewayLevel : 'warn';
  return root.child({ lib: 'baileys', session_id: sessionId }, { level });
}
