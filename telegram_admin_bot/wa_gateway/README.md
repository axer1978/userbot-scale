# wa-gateway

The WhatsApp transport for the fleet: the counterpart of the Telethon client
object that the Python runtime holds for Telegram accounts. It speaks the
WhatsApp Web protocol through [Baileys](https://github.com/WhiskeySockets/Baileys)
(7.0.0-rc14, pinned) and nothing else.

**Transport only.** It never decides to send anything and never opens a
socket on its own. Pacing, safety caps, approval, drafting and halts stay in
Python. The gateway is an inbound receptionist: Python takes the lease, tells
the gateway to open the socket, and the gateway reports what arrives.

```
 panel / manager (Python)            wa-gateway (Node)                 WhatsApp
   lease row in Postgres  ──open──▶  one socket per account  ◀──────▶  web.whatsapp.com
   CommandBus.dispatch("@wa-gateway", ...)  over Valkey pub/sub
   Postgres: wa_auth_state (creds+keys, encrypted), wa_inbox (step 5)
```

## Invariants

1. **One live socket per account, ever.** Python acquires the Postgres lease
   on `telegram_sessions` (bumping `lease_epoch`) *before* it sends `open`.
   Every socket-bound command carries that epoch and is fenced against the
   row; a stale worker gets `stale_epoch` and cannot drive a socket another
   worker owns. Two sockets on one number produce `connectionReplaced` loops
   and a ban.
2. **Auth state lives only in Postgres**, encrypted exactly like `crypto.py`
   (same blob format, same keys, same AAD convention). There is no session
   directory on disk, in the image or in a volume.
3. **Session loss is loud.** `loggedOut`, `forbidden`, `badSession`,
   `connectionReplaced`, `multideviceMismatch` mean the linked device is gone:
   no reconnect, a `session_lost` event, and an error-level log line
   `SESSION LOST <session_id>: <reason> (<code>)`. Transient drops and
   `restartRequired` reconnect with backoff (2 s doubling to 60 s), same epoch.
4. **Singleton.** At boot the gateway takes `pg_try_advisory_lock(2003332983)`
   (the ASCII bytes "wagw"; constant `SINGLETON_LOCK_KEY` in `src/config.ts`).
   If another gateway holds it, it logs and exits with status 3; compose
   restarts it, which is what a failover looks like.

## Environment

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres, same URL as the Python services. |
| `REDIS_URL` | Valkey, `redis://valkey:6379/0`. Pub/sub only. |
| `USERBOT_MASTER_KEY` / `USERBOT_MASTER_KEY_FILE` | Same rules as `crypto.py`: the file (JSON keyring `{"active": id, "keys": {"id": "<b64>"}}` or a bare base64 key) wins over the env var; keys are exactly 32 bytes; the process refuses to boot without one (exit 2). |
| `WA_GATEWAY_LOG_LEVEL` | pino level, default `info`. Baileys' own logger is capped at `warn` unless this is `debug`/`trace` (at `trace` Baileys prints decoded frames, which can include message text). |

Log hygiene: message text is never logged at `info` (only lengths); key
material is never logged at any level.

## Postgres

Created by migration 0006 (Python side). `tenant_id` is filled by a BEFORE
INSERT trigger from the account row; the gateway never writes it.

```
wa_auth_state (tenant_id, session_id, kind, key_id, value_enc BYTEA, updated_at, PK (session_id, kind, key_id))
  kind = 'creds' (key_id '') or a Baileys SignalDataTypeMap key:
         pre-key, session, sender-key, sender-key-memory, app-state-sync-key,
         app-state-sync-version, lid-mapping, device-list, tctoken, identity-key
  value_enc = crypto blob of UTF-8 JSON serialised with Baileys' BufferJSON replacer
  AAD       = "<session_id>:wa_auth:<kind>:<key_id>"
wa_inbox (id, tenant_id, session_id, wa_message_id, payload JSONB, created_at, UNIQUE (session_id, wa_message_id))
telegram_sessions.channel  'telegram' | 'whatsapp'
```

Key-store semantics (mirrors `useMultiFileAuthState`): a `set` with a null
value deletes the row; multi-key sets are one transaction; `clear` deletes
the signal keys (not the creds); a re-pair wipes every row of the session.

## Bus wire format v1

Envelope is `commands.py`'s, so Python calls
`CommandBus.dispatch("@wa-gateway", action, args)`.

- Gateway subscribes to `cmd:@wa-gateway`. Message:
  `{"command_id": "<hex>", "action": "<str>", "args": {...}, "v": 1}` (`v` optional).
- Reply on `cmdresp:<command_id>`:
  `{"ok": true, "result": ...}` or
  `{"ok": false, "error": "<kind>: <detail>", "error_kind": "<kind>"}`,
  `error_kind` in `stale_epoch | not_connected | busy | bad_request | not_found | session_lost | other`.
- Each command runs concurrently; a failing handler never stops the loop.
  The subscriber reconnects and re-subscribes with backoff when Valkey drops
  (ioredis does this natively, which is why it was chosen over node-redis).

### Commands

| action | args | result | errors |
|---|---|---|---|
| `pair` | `session_id`, `pair_id`, `method: "qr"\|"code"`, `phone?` (digits, for `code`), `browser: [os, browser, version]` | `{"started": true}` | `busy` (open socket, running pairing, or a live lease in Postgres), `not_found` (no session row), `bad_request` |
| `pair_cancel` | `pair_id` | `{"cancelled": true}` | `not_found` |
| `open` | `session_id`, `epoch`, `browser` | `{"state": "opening"}` (or the existing state on the same epoch) | `stale_epoch`, `not_found` (no creds: pair first), `bad_request` (channel not whatsapp, malformed), `busy` (pairing in progress), `session_lost` (same epoch, device already lost) |
| `close` | `session_id`, `epoch` | `{"closed": bool}`; `false` when nothing to close or `epoch` < the socket's | `bad_request` |
| `status` | | `[{session_id, epoch, state, lost, me, inbox_pending}]` | |

`pair` wipes the session's `wa_auth_state` first (a re-pair is a new linked
device), runs a pairing socket, streams `qr` (each rotation) or requests a
pairing code once the socket is up, and on success (creds registered and
connection open) waits ~5 s for the phone's first key material, emits
`paired` and ends the socket cleanly (no logout). The whole pairing times
out after 3 minutes (`failed`). A failed or cancelled pairing wipes the
partial auth state.

`open` verifies the row: `channel = 'whatsapp'`, `is_active`,
`lease_expires_at > now()`, `lease_epoch = epoch`. An existing socket with a
lower epoch is closed first; equal epoch is idempotent; higher epoch wins
(`stale_epoch`). `close` is a normal end, never a logout.

### Events

`wa:pair:<pair_id>`:
```
{"v":1,"type":"qr","session_id","pair_id","qr"}
{"v":1,"type":"code","session_id","pair_id","code"}
{"v":1,"type":"paired","session_id","pair_id","jid","lid","push_name"}
{"v":1,"type":"failed","session_id","pair_id","reason"}
```

`wa:ev:<session_id>`:
```
{"v":1,"type":"connection","session_id","epoch","state":"connecting"|"open"|"closed","me":{"jid","lid","name"}|null}
{"v":1,"type":"session_lost","session_id","epoch","reason":"loggedOut"|"forbidden"|"badSession"|"connectionReplaced"|"multideviceMismatch","code"}
{"v":1,"type":"inbox","session_id","epoch"}      (nudge; not emitted until step 5)
```

### Inbox payload v1 (built now, persisted from step 5)

```
{"v":1, "session_id", "epoch", "wa_message_id", "jid", "jid_alt", "phone_jid", "lid",
 "push_name", "from_me", "type":"text"|"image"|"other", "text", "quoted_id", "ts"}
```
`jid` is `key.remoteJid` as WhatsApp addressed the chat (PN or LID),
`jid_alt` is `key.remoteJidAlt`; `phone_jid` (`…@s.whatsapp.net`) and `lid`
(`…@lid`) are the two forms split out whichever way they arrived. `text` is
`conversation`, `extendedTextMessage.text` or the image caption;
`quoted_id` is `contextInfo.stanzaId`; `ts` is WhatsApp's message timestamp
in Unix seconds. Ephemeral/view-once envelopes are unwrapped. Dropped: groups,
`status@broadcast`, broadcast lists, newsletters, protocol/reaction/stub
messages, undecryptable messages. `from_me` messages are kept and flagged.

### Delivery to the runtime (step 5)

For every qualifying message (`notify` and offline `append` alike) the gateway
runs `INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES
($1,$2,$3::jsonb) ON CONFLICT (session_id, wa_message_id) DO NOTHING` (the
tenant trigger fills `tenant_id`), then publishes
`{"v":1,"type":"inbox","session_id","epoch"}` on `wa:ev:<session_id>`. The
runtime reads the rows, persists the messages into its own tables, deletes
the rows (the ack) and dedupes by `wa_message_id`. At-least-once delivery:
a message stays in memory until its insert succeeded; while Postgres is down
the insert is retried with backoff (1 s doubling to 30 s) and nothing is
dropped; a backlog of 5000 or more rows per session is logged at warn. The
writer is per session, not per socket, so a message received just before a
socket was replaced still lands. Offline redelivery after downtime inserts
each message exactly once thanks to the conflict clause. Pending rows exist
only in memory: a restart during a Postgres outage loses them (logged at
error on shutdown).

Echo suppression: a `from_me` message whose id this socket produced itself
(the send primitive of step 6 records it) is the server echoing our own send; it is not forwarded (the
runtime already stored it). Other `from_me` messages, typed on the phone,
are forwarded with `from_me: true`.

Log lines carry session, jid, message id, from_me, type and text length,
never the text.


## Watchdog

Every 10 s, for each open socket, the row is re-read and the socket is closed
(logged at error level) when `lease_epoch` changed, the lease has been
expired for 30 s or more, the lease was released, the row is gone, or
`channel`/`is_active` no longer allow it. If Postgres is unreachable for 22 s
(`leasing.DANGER_SECONDS`) all sockets are closed: without Postgres no lease
can be proven.

## Socket options

`markOnlineOnConnect: false`, `syncFullHistory: false`, no history sync at
all, `shouldIgnoreJid` for groups / status / broadcast / newsletters, the
browser tuple supplied in the command (`src/browser.ts` documents the
deterministic `session_id -> tuple` mapping via sha256 for parity), a bounded
in-memory LRU for `getMessage` (recent outbound messages, for retry
receipts). The WhatsApp Web version is Baileys' pinned default; the gateway
does not fetch a version at boot.

Baileys v7 note: messages delivered while the device was offline arrive in
`messages.upsert` with `type: "append"` (the stanza's `offline` attribute),
live ones with `"notify"`. Both are accepted. History-sync appends cannot
occur because history sync is disabled.

## Temporary manual-test CLI (step 3 only)

Standalone, no Valkey; meant for the operator's first pairing of a secondary
number. Both refuse to run when the session has a live lease in Postgres.
`listen` opens a socket **without** the lease handshake and is a manual-test
tool only: stop it before starting the runtime for that account.

```bash
docker compose run --rm wa-gateway node dist/cli.js pair <session_id>                    # QR in the terminal
docker compose run --rm wa-gateway node dist/cli.js pair <session_id> --phone 34600000000 # pairing code
docker compose run --rm wa-gateway node dist/cli.js listen <session_id>                  # logs inbound messages
```

## Development

```bash
npm ci
npm run build        # tsc -> dist/
npm test             # node --test test/ (runs the .ts tests directly on Node 24)
npm run typecheck    # src + tests
```

`test/authstate.test.ts` needs a Postgres (`PG_TEST_DSN`, default
`postgresql://postgres@127.0.0.1:55432/userbot_test`); it creates and drops
its own schema and skips when unreachable. Nothing in the tests ever
connects to WhatsApp. The Python suite's `tests/test_wa_crypto_compat.py`
checks the same golden vectors and, when `dist/cryptotool.js` exists, does a
live Python<->Node round trip.
