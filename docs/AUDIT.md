# Audit and crash tests: WhatsApp (2026-10-01)

Branch `platform/whatsapp`, after the WhatsApp build was merged in (782287d).

Four sets of changes, each with its own tests:
- three audits, run in parallel in separate worktrees: security, runtime chaos, and deploy and fresh install;
- follow-ups I made after merging them.

Nothing touched a real WhatsApp or Telegram number: the Baileys socket, Postgres, Valkey and the LLM were all simulated.

**Tests:**
- Python: 1171 passed and 4 skipped before → **1209 passed, 0 skipped, 0 failed (with Caddy, shellcheck and the built gateway available)** after.
- Node: 57 → **72** passed, and the typecheck is clean.

What to do when something breaks: [RUNBOOK.md](../RUNBOOK.md), sections *A WhatsApp account*, *WhatsApp session dropped* and *WhatsApp: messages and replies*.

The audit prompt assumed an HTTP webhook from the sidecar, a `/send` endpoint and session files on disk. None of those exist here:
- commands travel over the Valkey bus;
- inbound messages go into the Postgres `wa_inbox` table;
- the linked-device keys live encrypted in Postgres (`wa_auth_state`).

Each item was audited against that real path.

## Findings

| Severity | What | Where | Fixed | Test |
|---|---|---|---|---|
| **High** | **Listener leak on reconnect.** A late event from a replaced socket was forwarded again: after 5 reconnects, 1 message was handed on 6 times. Only the inbox dedupe hid it, and only when the ids matched | `wa_gateway/src/session.ts` | Yes: every handler is bound to its own socket | `wa_gateway/test/session_chaos.test.ts` "5 reconnects, 1 inbound message" |
| **High** | **The WhatsApp keepalive died on any unexpected error** (e.g. a Postgres blip while marking the account connected). After that, nothing reopened the socket after a gateway restart and the inbox was never drained, silently, until the worker restarted | `whatsapp_transport.py` `_keep_open` | Yes | `tests/test_chaos_wa_runtime.py::test_the_keepalive_survives_a_failing_round` |
| **High** | **One bad inbox row blocked an account.** A row Postgres refuses for good (not an outage) was retried for ever at the head of the queue, so no later message for that account arrived | `wa_gateway/src/inbox.ts` | Yes. Outages are still retried for ever. A row refused 5 times for another reason is dropped and logged as `MESSAGE LOST <account>`, with id and sender, never the text | `inbox.test.ts`, `session_chaos.test.ts` "postgres refusing one row" |
| Medium | **A missed `session_lost` event** (Valkey dropped it, or the worker was restarting) was never acted on. Every 15 s the gateway refused `open` as *session lost*, but the account was never halted and nothing showed in the panel | `whatsapp_transport.py` `_open_once` | Yes: that refusal now halts like the event | `test_chaos_wa_runtime.py::test_open_refused_as_session_lost_halts_even_when_the_event_was_missed` |
| Medium | **Lifting a `whatsapp` hold** in Safety left its critical alert, and any `whatsapp:*` alert, open | `safety_api.py:272` | Yes | `tests/test_safety_api.py::test_resuming_a_whatsapp_halt_closes_its_alerts` |
| Medium | **`restartRequired` (515) repeated:** it reconnected every second for ever | `wa_gateway/src/session.ts` | Yes: the first is immediate, repeats back off (2 s → 60 s) | `session_chaos.test.ts` "515 restartRequired" |
| Medium | **`wa-gateway` had no healthcheck** | `docker-compose.yml` | Yes. It asks Valkey whether the gateway is subscribed to `cmd:@wa-gateway`, which it is only after the master key, the singleton lock and the bus are all in hand. A frozen but subscribed process is not caught | `tests/test_deploy_files.py::test_wa_gateway_healthcheck_asks_valkey_for_the_gateways_own_subscription` (against a fake Valkey) |
| Medium | **The deploy guide cloned `platform/phase-1`,** so a fresh install from this branch had no gateway | `DEPLOY_TODAY.md` | Yes: `platform/whatsapp` | `test_deploy_doc_defaults_to_sslip_and_covers_the_whole_path` |
| Low | **No redaction in the gateway's logger.** Baileys logs raw stanzas and credential-shaped objects; nothing stopped `creds`, keys, `qr` or `pairingCode` from reaching a line | `wa_gateway/src/log.ts` | Yes: pino `redact` on those fields, top level and two levels down | `wa_gateway/test/sec_log.test.ts` |
| Low | **`wa-gateway` received the whole `.env`:** the admin password, TOTP secret, SMTP, vision and DeepSeek keys, none of which it reads. It is the one service that parses hostile network input | `docker-compose.yml` | Yes: only `DATABASE_URL`, `REDIS_URL`, `USERBOT_MASTER_KEY`, `WA_GATEWAY_LOG_LEVEL` | `test_deploy_files.py::test_wa_gateway_gets_only_the_variables_it_reads` |
| Low | **"Cancel" then "start again" on pairing** was refused *already has a connection open*. The gateway answered *cancelled* before the old socket had closed | `wa_gateway/src/gateway.ts` | Yes: it answers once the run has let go (at most 4 s, under the panel's 5 s) | `wa_gateway/test/gateway.test.ts` "pair_cancel answers only once…" |
| Low | **No jitter on reconnects,** so many numbers reconnected in lockstep | `wa_gateway/src/disconnect.ts` | Yes, ±20 % | `disconnect.test.ts` |
| Low | **No `uncaughtException` handler:** a sync throw killed the gateway with a bare stack | `wa_gateway/src/main.ts` | Yes: one JSON log line, then exit for the restart policy. Unhandled rejections are logged and the gateway keeps going, so one socket can't take every number down | none: the module boots on import |
| Low | **Read-receipt bookkeeping** grew one entry per chat for ever | `whatsapp_transport.py` `mark_read` | Yes, bounded at 5,000 | `test_chaos_wa_runtime.py::test_read_receipt_memory_is_bounded` |
| Low | **`.gitattributes` forced LF only for scripts, Caddyfiles and SQL.** The index was LF everywhere, but a Windows edit could have committed CRLF Dockerfiles or YAML | `.gitattributes` | Yes: every Linux-side file type | `test_files_used_on_linux_are_forced_to_lf`, `test_git_checks_the_linux_files_out_with_lf` |
| Low | **`LOG_LEVEL` read by the scheduler** but missing from `.env.example` | `.env.example` | Yes, plus a test that every variable the code reads is documented | `test_every_variable_the_code_reads_is_documented_for_the_operator` |
| Low | **Valkey has no password** and shares the default compose network with the internet-facing Caddy and booking pages. Anything on that network could send gateway commands (e.g. `pair` wipes an unleased account's keys) | `docker-compose.yml` | **No**: see below | — |
| Low | **`/api/wa/pair/start` has no time-based rate limit.** It is admin-only, at most 5 at once, one per number | `panel.py`, `wa_pairing.py` | No: see below | — |
| Info | **A ban (`forbidden`) shows as state `revoked`,** which the health watchdog reads as "a person did it". The critical `whatsapp` alert and hold are still raised, so it is loud | `session_runtime.py`, `health.py` | No: as documented | `test_chaos_wa_runtime.py::test_a_lost_session_is_loud_in_the_panel` |
| Info | **A send whose bus answer timed out, but which the gateway did send,** stays red in the thread and is never sent again (correct). The thread never learns it went | `session.ts` echo suppression | No | `test_chaos_wa_runtime.py::test_a_send_the_gateway_never_answers_is_red_and_never_sent_twice` |
| Info | **Account ids contain the full phone number** (`wa<digits>`, `tg<digits>`) and appear in every log line. The gateway logs customer JIDs at info | design-wide | No | — |
| Info | **`cli.ts` (manual test tool) ships in the image.** It prints the QR or code, and `listen` opens a socket without a lease | `wa_gateway/src/cli.ts` | No | — |

## Checked and sound

- **Every route** (`tests/test_sec_routes.py`):
  - all 100+ API routes refuse anonymous callers;
  - a client cookie opens no admin route, and the admin cookie opens no `/api/owner/*` route;
  - the public allow-list is explicit, so a new unauthenticated route fails the test;
  - every changing request is refused cross-origin, including the three `/api/wa/*` routes;
  - every `/api/` answer is `no-store`;
  - the public booking app serves only `/healthz` and its token routes.
- **The QR or pairing code:**
  - only an admin sees it (`GET /api/wa/pair/{id}`, 128-bit id, `no-store`);
  - it is never logged and never sent on the live channel;
  - it is cleared when the pairing ends, after 5 min at most in the panel and 3 min in the gateway.
  - A `paired` event only activates the account the panel started, never the one named in the event.
- **XSS:** no `innerHTML` with data anywhere in `static/`. The QR is drawn on a canvas, and the CSP is unchanged (`script-src 'self'`).
- **Tenant isolation:** a client sees only their own businesses' phone numbers and names.
- **Disconnects:**
  - short drops back off, up to 60 s, reset after an open;
  - `loggedOut`, `forbidden`, `badSession`, `connectionReplaced` and `multideviceMismatch` are reported once, never reconnected, and refuse later `open` / `send_text`;
  - `connectionReplaced` does not ping-pong: the 15 s keepalive does not reopen a lost account.
- **Inbound:**
  - groups, status, broadcast, newsletters, protocol messages, reactions, edits, deletes, undecryptable stubs and junk payloads are dropped without breaking the handler;
  - ephemeral and view-once wrappers are unwrapped;
  - LID and phone JIDs both map;
  - Baileys `append` is only messages received while offline, which are meant to be kept and answered. History sync is off and never reaches the pipeline.
- **Duplicates:** three concurrent drains, or an ack failing after the store, give one stored message and one draft.
- **Sends:** gateway down, not connected or timed out → a red row, never retried, never sent twice.
- **Bursts:** 50 messages from 50 contacts → 50 drafts, maps emptied afterwards. 50 from one contact → one live draft.
- **Auth state:** a crash mid-write comes back without a re-pair. Corrupt, undecryptable or missing keys refuse that one account cleanly; the others keep running.
- **Migrations:**
  - all six apply to an empty database, then again with no changes (catalog snapshot identical);
  - the real `migrate_entrypoint.py` runs 4 times;
  - a database at 0005 filled with phase-4 data upgrades to 0006 with every row unchanged.
- **Compose:**
  - only Caddy publishes 80/443, and the panel listens on loopback only;
  - Postgres, Valkey and the gateway are unpublished;
  - every long-running service has `restart: unless-stopped` and log caps;
  - Postgres is on a named volume;
  - the gateway runs as `node` and writes nothing to disk.
- **Real binaries:** Caddy v2.11.4 validates all three Caddyfiles; shellcheck v0.11.0 passes both deploy scripts.

## Not fixed, and why

- **Valkey password and network split.** The fix: a `VALKEY_PASSWORD` in `.env` and in every `REDIS_URL`, and two networks, so Caddy and the booking pages can't reach Postgres or Valkey. It needs a new `.env` value on the server, which breaks an update until you add it, and a compose layout I can't start here (no Docker). Your call; I can do it with the exact `.env` step in the deploy guide.
- **Pairing rate limit.** It is admin-only and already bounded to 5 at once. A per-number cooldown would get in the way of "try again" after a failure.
- **A heartbeat for a frozen gateway.** Optional: the gateway would write a timestamp to Valkey each watchdog tick, and the healthcheck would check its age.
- **Phone numbers in account ids and logs.** That is the id scheme; changing it is a migration of its own.
- **`cli.ts` in the image.** Remove it before going live, or keep it for support.
- **The fresh Ubuntu 24.04 walk ran on paper only.** WSL can't start on this machine (virtualization is off in the firmware), so `docker compose build/up`, the bootstrap script and the backup script were reviewed and shellchecked, not executed.

# Audit and crash tests (2026-09-29)

Three audits ran over the code as it was after phase 4, each fixing what it found and adding tests. Tests: **1051 passed**, 4 skipped (924 before). See [RUNBOOK.md](../RUNBOOK.md) for what to do when something breaks.

## Security (admin panel and client dashboard)

| Severity | Finding | Fixed |
|---|---|---|
| **High** | On an `*.sslip.io` address, another website on the same service counts as the same site to a browser, so it could act as a logged-in admin or client, or read chats live. | Yes. Requests that change anything, and the live connection, are refused when the browser reports another origin. Form-type bodies are refused on the API. |
| Medium | A sibling `*.sslip.io` site could plant a login cookie. | Yes. Cookies are `__Host-` prefixed when Secure; logging in again ends the old session. |
| Medium | No request size limit: anyone could send gigabytes to the login route. | Yes. 2 MB for everything, 200 MB for media uploads. |
| Medium | Error text (which can hold a query or a stored value) was shown to anyone. | Yes. Only a logged-in admin sees it; everything is still logged. |
| Low | The API map (`/docs`, `/openapi.json`) was public. | Yes, off. |
| Low | The page policy allowed live connections to any host. | Yes, this host only. |
| Low | A media file named like `x.png/` crashed the upload. | Yes. |
| Low | Non-ASCII digits in an authenticator code caused a crash. | Yes. |
| Low | A huge id on the client dashboard gave a crash instead of a clean answer. | Yes. |
| Info | The failed-login table could grow without limit under an attack from many addresses. | Yes, swept at 10,000. |

Checked and sound: every API route needs a login; a client cookie opens no admin route and the admin cookie opens no client route; every client query is limited to that client's businesses; no script injection sinks; 256-bit tokens; sessions stored as hashes; each authenticator code works once; passwords hashed with scrypt; nothing logs secrets.

Left as is, your call:
- Anyone can lock a client out for 15 minutes by failing their login 5 times. That is the price of stopping password guessing.
- A client can turn on 2FA without re-entering the password. You can remove it from Client logins.

## Crash tests (50 scenarios, `tests/test_chaos.py`)

What was simulated and now survives:
- **Postgres gone for a moment:** the scheduler keeps ticking; an incoming message is kept and answered when it's back; a reply interrupted before sending is written again and sent once, and never retried after it may have gone out; the lease keeper fences before the lease expires; stopping tears everything down even with the database gone.
- **Valkey gone:** the command server reconnects by itself with backoff; billing, digests and switches degrade to best effort; live panel events failing never break message handling; a hanging bus never holds a message up.
- **Scheduler:** one account's broken tick doesn't stop the others; a hanging account doesn't hold up the round; a scheduler that loses its lock connection never ticks alongside another; one stopped mid-round lets the next take over.
- **Restarts mid-flow:** a reminder claimed just before a crash is never sent twice; a booking created just before a crash still reaches the owner; two digest rounds at once send one digest; a suspension is never left without its hold.
- **DeepSeek:** rate limits and server errors retried within a bounded time; a call that never answers gives up; garbage output is an AI error, not a crash; garbage from the booking extraction books nothing.
- **Clock:** the digest and billing across a clock change; quiet hours never negative or endless on that night; a hand-edited invalid timezone keeps the last good config.
- **Odd data:** very long messages with emoji and right-to-left text; a NUL character; a sticker or photo alone; a deleted account; Telegram's service account; the owner writing in Saved Messages.
- **Memory:** drafts and scans leave nothing behind; per-chat throttles are pruned; finished handler tasks are dropped.

## Fresh install and deploy

- All five migrations apply to an empty database, twice (idempotent), through the same entry point the `migrate` service uses (`tests/test_fresh_install.py`).
- The three Caddy configs validate with the real Caddy (v2.11.4); the combined file now uses relative imports.
- The two shell scripts pass shellcheck (v0.11.0). The bootstrap now survives a provider's edited sshd config, a missing `universe` section, fail2ban without rsyslog, and tells you when a reboot is needed.
- docker-compose: profiles, published ports (only Caddy's 80/443; the panel on loopback; Postgres and Valkey never), log caps on every service, Valkey without disk writes.
- The image is Python 3.13, which has ready-made wheels for every dependency on both amd64 and arm64.
- The `.env` one-liner in DEPLOY_TODAY.md produces keys the code accepts (tested).

Not done on a real server yet: Let's Encrypt issuance, the bootstrap on a live Ubuntu 24.04, the backup script end to end.
