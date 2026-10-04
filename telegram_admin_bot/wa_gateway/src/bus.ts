/**
 * Valkey (Redis protocol) pub/sub, wire-compatible with commands.py so the
 * Python side calls CommandBus.dispatch("@wa-gateway", action, args):
 *
 *   SUBSCRIBE cmd:@wa-gateway     {"command_id", "action", "args", "v"?}
 *   PUBLISH   cmdresp:<command_id> {"ok": true, "result"} |
 *                                  {"ok": false, "error": "<Kind>: <detail>", "error_kind"}
 *   PUBLISH   wa:pair:<pair_id> / wa:ev:<session_id>   events
 *
 * Why ioredis: a subscriber connection re-subscribes to its channels by
 * itself after a reconnect (with retryStrategy backoff), which is exactly
 * commands.py's serve() loop; node-redis needs that written by hand. Two
 * connections because a subscribed Redis connection cannot PUBLISH.
 */
import { Redis } from 'ioredis';
import type pino from 'pino';

export type ErrorKind =
  | 'stale_epoch'
  | 'not_connected'
  | 'busy'
  | 'bad_request'
  | 'not_found'
  | 'session_lost'
  | 'rate_limited'
  | 'not_on_whatsapp'
  | 'blocked'
  | 'other';

export class GatewayError extends Error {
  readonly kind: ErrorKind;
  constructor(kind: ErrorKind, message: string) {
    super(message);
    this.kind = kind;
    this.name = 'GatewayError';
  }
}

export type Command = { command_id: string; action: string; args: Record<string, unknown> };

export type Reply = { ok: true; result: unknown } | { ok: false; error: string; error_kind: ErrorKind };

export function parseCommand(raw: string): Command | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  const obj = data as Record<string, unknown>;
  if (typeof obj.command_id !== 'string' || !/^[0-9a-f]{1,64}$/i.test(obj.command_id)) return null;
  if (typeof obj.action !== 'string' || obj.action.length === 0) return null;
  if (obj.v !== undefined && obj.v !== 1) return null;
  const args = obj.args === undefined || obj.args === null ? {} : obj.args;
  if (typeof args !== 'object' || Array.isArray(args)) return null;
  return { command_id: obj.command_id, action: obj.action, args: args as Record<string, unknown> };
}

export function replyFor(outcome: { result: unknown } | { error: unknown }): Reply {
  if ('result' in outcome) return { ok: true, result: outcome.result ?? null };
  const err = outcome.error;
  if (err instanceof GatewayError) return { ok: false, error: `${err.kind}: ${err.message}`, error_kind: err.kind };
  const name = err instanceof Error ? err.name || 'Error' : 'Error';
  const detail = err instanceof Error ? err.message : String(err);
  return { ok: false, error: `${name}: ${detail}`, error_kind: 'other' };
}

export function responseChannel(commandId: string): string {
  return `cmdresp:${commandId}`;
}

export type CommandHandler = (command: Command) => Promise<unknown>;

function makeClient(url: string, log: pino.Logger, role: string): Redis {
  const client = new Redis(url, {
    lazyConnect: true,
    connectTimeout: 5_000,
    maxRetriesPerRequest: 1,
    // Publishes while disconnected fail fast (best-effort, like commands.py).
    enableOfflineQueue: false,
    retryStrategy: (times) => Math.min(30_000, 1_000 * 2 ** Math.min(times, 5)),
  });
  client.on('error', (err) => log.warn({ err, role }, 'valkey connection error'));
  client.on('reconnecting', (ms: number) => log.warn({ role, delay_ms: ms }, 'valkey reconnecting'));
  client.on('ready', () => log.info({ role }, 'valkey ready'));
  return client;
}

export class Bus {
  private readonly log: pino.Logger;
  private readonly sub: Redis;
  private readonly pub: Redis;
  private inflight = new Set<Promise<void>>();

  constructor(redisUrl: string, log: pino.Logger) {
    this.log = log.child({ component: 'bus' });
    this.sub = makeClient(redisUrl, this.log, 'subscriber');
    this.pub = makeClient(redisUrl, this.log, 'publisher');
  }

  async connect(): Promise<void> {
    await Promise.all([this.sub.connect(), this.pub.connect()]);
  }

  /** Every command runs in its own task; a failing handler never stops the loop. */
  async serve(channel: string, handler: CommandHandler): Promise<void> {
    this.sub.on('message', (chan: string, raw: string) => {
      if (chan !== channel) return;
      const command = parseCommand(raw);
      if (!command) {
        this.log.warn({ chan, bytes: raw.length }, 'malformed command payload');
        return;
      }
      const task = this.handleOne(command, handler).finally(() => this.inflight.delete(task));
      this.inflight.add(task);
    });
    await this.sub.subscribe(channel);
    this.log.info({ channel }, 'serving commands');
  }

  private async handleOne(command: Command, handler: CommandHandler): Promise<void> {
    let reply: Reply;
    try {
      reply = replyFor({ result: await handler(command) });
    } catch (error) {
      if (!(error instanceof GatewayError)) this.log.error({ err: error, action: command.action }, 'command failed');
      reply = replyFor({ error });
    }
    await this.publish(responseChannel(command.command_id), reply);
  }

  /** Best-effort, bounded, never throws. */
  async publish(channel: string, payload: unknown): Promise<boolean> {
    try {
      await this.pub.publish(channel, JSON.stringify(payload));
      return true;
    } catch (exc) {
      this.log.warn({ err: exc, channel }, 'could not publish');
      return false;
    }
  }

  async close(): Promise<void> {
    await Promise.allSettled([...this.inflight]);
    await Promise.allSettled([this.sub.quit(), this.pub.quit()]);
  }
}
