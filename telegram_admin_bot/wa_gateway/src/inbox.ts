/**
 * Step 5: delivers qualifying inbound messages to wa_inbox and nudges the
 * runtime. At-least-once: a message stays in memory until its INSERT
 * succeeded; duplicates (offline redelivery, reconnects) are absorbed by
 * ON CONFLICT (session_id, wa_message_id) DO NOTHING. The runtime acks by
 * deleting the row once it has persisted the message.
 *
 * One writer per session_id (not per socket): a message received just
 * before a socket was replaced still has to land. Never drops a message;
 * warns when the backlog passes WARN_AT rows.
 */
import type pino from 'pino';
import type { Pool } from './db.ts';
import type { InboxPayload } from './normalize.ts';

export const INSERT_SQL = `
INSERT INTO wa_inbox (session_id, wa_message_id, payload)
VALUES ($1, $2, $3::jsonb)
ON CONFLICT (session_id, wa_message_id) DO NOTHING`;

export const WARN_AT = 5_000;

export type InboxWriterOptions = {
  baseMs?: number;
  maxMs?: number;
  warnAt?: number;
  sleep?: (ms: number) => Promise<void>;
};

export type Publish = (channel: string, payload: unknown) => Promise<boolean>;

export function retryDelayMs(attempt: number, baseMs = 1_000, maxMs = 30_000): number {
  return Math.min(maxMs, baseMs * 2 ** Math.min(attempt, 20));
}

export class InboxWriter {
  private readonly queue: InboxPayload[] = [];
  private draining: Promise<void> | null = null;
  private attempt = 0;
  private lastWarnSize = 0;
  private readonly log: pino.Logger;
  private readonly opts: Required<InboxWriterOptions>;
  readonly sessionId: string;
  private readonly pool: Pool;
  private readonly publish: Publish;
  /** Rows inserted (including ON CONFLICT no-ops); for status/tests. */
  delivered = 0;

  constructor(pool: Pool, sessionId: string, publish: Publish, log: pino.Logger, opts: InboxWriterOptions = {}) {
    this.pool = pool;
    this.sessionId = sessionId;
    this.publish = publish;
    this.log = log.child({ session_id: sessionId, component: 'inbox' });
    this.opts = {
      baseMs: opts.baseMs ?? 1_000,
      maxMs: opts.maxMs ?? 30_000,
      warnAt: opts.warnAt ?? WARN_AT,
      sleep: opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms))),
    };
  }

  get pending(): number {
    return this.queue.length;
  }

  enqueue(payload: InboxPayload): void {
    this.queue.push(payload);
    if (this.queue.length >= this.opts.warnAt && this.queue.length >= this.lastWarnSize + this.opts.warnAt) {
      this.lastWarnSize = this.queue.length;
      this.log.warn({ pending: this.queue.length }, 'inbox backlog: postgres has been refusing inserts; messages are held in memory, none dropped');
    }
    this.kick();
  }

  /** Resolves once everything queued so far has been inserted. */
  flush(): Promise<void> {
    return this.draining ?? Promise.resolve();
  }

  private kick(): void {
    if (this.draining) return;
    this.draining = this.drain().finally(() => {
      this.draining = null;
      if (this.queue.length) this.kick();
    });
  }

  private async drain(): Promise<void> {
    while (this.queue.length) {
      const payload = this.queue[0]!;
      try {
        await this.pool.query(INSERT_SQL, [this.sessionId, payload.wa_message_id, JSON.stringify(payload)]);
      } catch (exc) {
        const delay = retryDelayMs(this.attempt++, this.opts.baseMs, this.opts.maxMs);
        this.log.warn({ err: exc, pending: this.queue.length, retry_in_ms: delay }, 'wa_inbox insert failed; retrying');
        await this.opts.sleep(delay);
        continue;
      }
      this.queue.shift();
      this.delivered += 1;
      if (this.attempt) this.log.info({ pending: this.queue.length }, 'wa_inbox inserts succeeding again');
      this.attempt = 0;
      if (this.queue.length < this.opts.warnAt) this.lastWarnSize = 0;
      await this.publish(`wa:ev:${this.sessionId}`, { v: 1, type: 'inbox', session_id: this.sessionId, epoch: payload.epoch });
    }
  }
}
