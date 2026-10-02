# Architecture

State as of phase 4 of the multi-tenant platform (branch `platform/phase-1`),
plus WhatsApp accounts (migration 0006, see "WhatsApp").
Code lives in `telegram_admin_bot/`; module names below are files there.

## Processes

```
 browser ──SSH tunnel / HTTPS──▶ panel (panel.py + platform_api.py + booking_api.py + safety_api.py)
                                    │  reads/writes Postgres directly
                                    │  commands + live events over Valkey
                                    ▼
 Postgres ◀──────────────▶ manager (manager.py)
 (all state)                  └─ worker processes, each running up to N
 Valkey                          SessionRuntime (session_runtime.py) =
 (command bus, events)           one account + its Transport (transport.py):
      ▲                            Telegram: a live Telethon client
      │                            WhatsApp: commands to wa-gateway ──▶ wa-gateway (Node, Baileys)
      │                              + BookingFlow (booking_flow.py)      one socket per number
      │ scheduler_tick, once a minute, to every account with a live lease
 scheduler (scheduler.py, one at a time: Postgres advisory lock)
      + health watchdog (health.py), billing (billing.py), heartbeat
      └──▶ alerts (alerts.py) ──▶ Postgres, panel, ALERT_EMAIL, ALERT_WEBHOOK_URL

 internet ──HTTPS (caddy-booking)──▶ booking-pages (public_app.py)
      /cal/<tenant token>.ics, /b/<booking token>   (optional profile)
```

- **panel** is the control plane and holds no Telegram or WhatsApp connection. Anything that needs a live client (send, approve a draft) goes over Valkey (Redis-compatible) to the worker holding that account's lease.
- **manager** runs workers. A worker must hold an account's **lease** in Postgres (leasing.py) before connecting, so one account never runs twice.
- **migrate** is a one-shot job: SQL migrations, then `tenants.backfill()` (the Python-side data steps).
- **scheduler** holds no Telegram or WhatsApp connection. It sends `scheduler_tick` to every running account; each account then does its own timed work idempotently (see "Timed work"). It also runs the platform's own rounds, which need no account: the health watchdog, billing, and a heartbeat the panel watches.
- **wa-gateway** (`wa_gateway/`, Node) holds the WhatsApp sockets. It takes orders over Valkey, fenced by the lease epoch, and hands inbound messages over through Postgres. It decides nothing. One copy only (a Postgres advisory lock). See "WhatsApp".
- **booking-pages** (optional) is a separate small FastAPI app with no admin routes. It reads Postgres by unguessable token and passes a customer's cancel / "I'm coming" to the running account over the bus. GET never changes anything, because messaging apps fetch link previews.

## Tenancy

A **tenant** is one business client. It owns exactly one channel account, Telegram or WhatsApp (`tenants.session_id → telegram_sessions`; `tenants.channel` follows the account's `channel`) and belongs to one **industry**. A new account becomes a new tenant in the default industry (`SessionRegistry.create`).

**Isolation** is enforced at three levels:

1. Every table with account data has `tenant_id NOT NULL`. A test fails if a new table keyed by `session_id` lacks it (`test_tenant_isolation.py`).
2. A trigger on each of those tables (`tenant_for_session`, migration 0002) fills `tenant_id` from the row's account and **rejects** a row whose `tenant_id` and `session_id` belong to different tenants.
3. Every read, update and delete in `database.Database` filters on `tenant_id`. The facade is bound to one account and has no method that takes a tenant or account parameter. Tests with two tenants talking to the same Telegram user prove that one tenant's facade cannot read or change the other's rows by id.

Per-tenant files (the media library) live under `DATA_DIR/tenants/<tenant id>/`, and a media index entry cannot resolve to a path outside that folder. Bookings moved to Postgres in phase 2; a tenant's old `bookings.json` is imported once when its account starts, keeping its numbers.

The public tokens (`tenants.calendar_token`, `bookings.customer_token`) are 244 random bits each. A calendar token returns only its tenant's bookings; a booking token only that booking, without the customer's name.

`customer_ref` is an HMAC of `<channel>:<chat id>` (`telegram:…` or `whatsapp:…`) under a key derived per tenant from the master key, so the same person has unrelated refs under different tenants.

## Data model (Postgres)

Migrations: `migrations/0001_init.sql` (fleet), `0002_tenants.sql` (platform), `0003_bookings.sql` (bookings), `0004_safety.sql` (safety and control), `0005_client_facing.sql` (client logins, unanswered queue, review, digest), `0006_whatsapp.sql` (WhatsApp), `0007_accounts.sql` (client sign-up, terms of service, manager logins), `0008_review.sql` (verification videos, photo review), `0009_staff.sql` (staff roles, approval queue).

| Table | Key columns | Notes |
|---|---|---|
| `industries` | id, name, template_version, default_config, config_revision | `template_version` points at the live industry prompt version |
| `tenants` | id, name, industry_id, status (active/grace/suspended), channel (telegram/whatsapp), session_id, config_json, config_revision, prompt_version, prompt_pin_version, billing_next_due, grace_until, billing_notice_sent_at | `config_json` = client-layer config overrides; `prompt_version` = client prompt version in use; `prompt_pin_version` pins an industry template version. Billing: see "Safety and control" |
| `tenant_holds` | tenant_id, kind (manual/billing/spend_cap/anomaly/telegram/whatsapp), reason, created_by | Soft-off: the tenant sends nothing on its own while it has any hold. One row per cause, each lifted on its own |
| `prompt_versions` | layer (base/industry/client), ref_id, tenant_id (client rows), version, content, note, created_by, created_at | Immutable; unique (layer, ref_id, version) |
| `platform_settings` | key, value | `base_prompt_version`, `llm_prices`, `global_stop`, `billing` (grace hours, owner notice), `scheduler_heartbeat` |
| `alerts` | tenant_id (NULL = platform), kind, severity, message, count, last_at, acknowledged_at/by | For the operator. At most one open alert per (tenant, kind); a repeat counts, it is not re-sent |
| `sessions_health` | tenant_id, session_id, status, last_seen_at, last_error, rate_limited_until, known_session_ids_json | What the running account reports and the watchdog's verdict. `known_session_ids_json` = the account's Telegram logins (hash, device, app, country; no IP) |
| `audit_log` | tenant_id (NULL = platform), actor, event, reason, payload, created_at | Append-only: UPDATE, DELETE and TRUNCATE are refused by triggers |
| `llm_usage` | tenant_id (NULL = platform), purpose, model, cache-hit / cache-miss / completion tokens, cost_eur | One row per LLM call |
| `telegram_sessions` | session_id, channel (telegram/whatsapp), encrypted credentials, is_active, state, lease_* (incl. lease_epoch) | One per account on either network (the name is historical). A WhatsApp row has no Telegram credentials; its login is in `wa_auth_state` |
| `conversations` | tenant_id, session_id, chat_id, customer_ref, display_name, automation_paused, paused_reason, human_takeover_until, … | `paused_reason` = why (an escalation keyword, or "" = by hand); `human_takeover_until` = a person wrote here by hand, the bot is quiet until then |
| `messages` | id, tenant_id, session_id, chat_id, direction, status, text, telegram_id / wa_message_id, llm_model, prompt_version | `prompt_version` e.g. `b1/i1v3/c2`. `wa_message_id` is unique per chat: a redelivered WhatsApp message is stored once |
| `wa_peers`, `wa_auth_state`, `wa_inbox` | tenant_id, session_id, … | WhatsApp only (0006); see "WhatsApp" |
| `bookings` | id, tenant_id, session_id, number, chat_id, customer_ref, customer_name, service, starts_at, ends_at, blocked_until, tz, state, proposed_*, customer_notice, customer_token, arrival fields, legacy | `number` counts per tenant from 1 (`booking_counters`). An exclusion constraint refuses two live bookings of one tenant whose `[starts_at, blocked_until)` overlap; `blocked_until` = end + the gap after it |
| `booking_events` | tenant_id, booking_id, from_state, to_state, action, actor, reason, payload | Every transition, besides its audit_log row |
| `booking_reminders` | tenant_id, booking_id, minutes_before, starts_at, claimed_at, sent_at | Primary key = one reminder per booking, offset and start time: what makes reminders idempotent |
| `availability_rules` | tenant_id, weekday, start_time, end_time, slot_minutes, buffer_minutes | Weekly opening hours, local wall time. None = hours not enforced |
| `waitlist` | tenant_id, session_id, customer_ref, chat_id, wanted_from, wanted_to, state, offered_starts_at | One live entry per customer |
| `deferred_replies` | tenant_id, session_id, chat_id, due_at | A reply held back by quiet hours, one per chat |
| `outreach`, `chat_links`, `chat_summaries`, `session_media`, `session_counters`, `session_halts`, `telegram_peers`, `session_update_state` | tenant_id, session_id, … | Pre-platform tables, now tenant-scoped. `session_media` is not used yet (files are) |
| `session_config` | tenant_id, session_id, config | Now only the account's own state: device identity, per-contact style overrides. The old pause switch became the `manual` hold (migration 0004) |
| `worker_heartbeats`, `panel_sessions`, `schema_migrations` | | Operational, not tenant data |

| `owners`, `owner_tenants`, `owner_sessions` | owner: username, scrypt password hash, encrypted TOTP; links to tenants; sessions by token hash | Client master logins (phase 4). Every owner route is scoped to the owner's linked tenants |
| `owners` (0007 columns) | status (pending/active/rejected), email, company, phone, reviewed_by/at, review_reason | A self sign-up is `pending` until approved; admin-created logins are `active` |
| `terms_versions`, `terms_acceptances` | version, title, body, change_note, requires_acceptance; owner_id, username, version, ip, user_agent | Both append-only (trigger `append_only()`). Acceptances have no foreign key, so they outlive a deleted login |
| `managers`, `manager_sessions` | like `owners`/`owner_sessions`, no tenant links | Moderator logins (manager_auth.py) |
| `unanswered_queue` | tenant_id, chat_id, message_id, reason, status (open/reviewed/added_to_template) | Customer messages that got no reply, decided in code |
| `review_batches`, `review_items` | tenant, date range; item context, reply, decision, edited_text | Review for the trainer; JSONL export |
| `digest_log` | tenant_id, week_start | At most one weekly digest per tenant and week |

Not built yet (later phases): `customer_flags`, `conversations.language`.

## Client-facing (phase 4)

- **Two logins, two cookies.** The admin (`admin_token`, password + TOTP, required when `PANEL_DOMAIN` is set) reaches everything under `/api/` except `/api/owner/*`. A client (`owner_token`, `owner_auth.py`) reaches only `/api/owner/*`, and every query there is limited to the tenants in `owner_tenants`. The client page is `static/owner/`.
- **Unanswered queue** is written by `SessionRuntime.queue_unanswered()` wherever a customer message ends without a reply (see `unanswered.py` for the reasons); promoting one appends to the industry FAQ as a new template version.
- **Staging** (`staging.enabled`): `on_incoming` answers only `staging.test_chats`, after escalation and soft-off.
- **Digest** (`digest.py`) runs in the scheduler's platform round next to health and billing.
- **Public exposure**: Caddy (TLS, HSTS) → panel, which adds CSP and the other headers itself (`panel.SECURITY_HEADERS`). Postgres and Valkey are never published.

## Accounts: sign-up, terms, managers

Three logins, three cookies, none valid for another's routes (`test_security_audit.py` sweeps every route with each):

| Login | Cookie | Routes | Module |
|---|---|---|---|
| admin | `admin_token` | `/api/*` except below | panel.py |
| client (owner) | `owner_token` | `/api/owner/*`, page `/owner/` | owner_auth.py, owner_api.py |
| manager | `manager_token` | `/api/manager/*`, page `/manager/` | manager_auth.py, manager_api.py |

Public without any login: `/api/terms` (page `/terms/`), `/api/owner/signup-options`, `/api/owner/signup`, and the three login/logout pairs.

- **Sign-up** (`POST /api/owner/signup`) is closed until the admin opens it (`platform_settings.signup`, Terms overlay), and can't be opened before a terms version is published. It creates a `pending` login linked to no business, records the accepted terms version, raises one platform alert (`signup_pending`) and signs the person in. Limits: 3 per address and hour, at most 50 waiting at once. The admin (`/api/owners/{id}/approve|reject`) or a manager approves or rejects; only the admin links businesses. With SMTP set, the applicant gets an e-mail either way.
- **The gate** (`owner_auth.gate`), checked by every owner route in this order: temporary password → `change_password`; pending → `pending_approval`; rejected → `rejected`; not accepted the newest version with `requires_acceptance` → `accept_terms`. `/api/owner/account` and `/api/owner/terms(/accept)` answer whatever the gate says, so the page can show the right screen.
- **Terms** (`terms.py`): a version can't be edited or deleted; publishing is the only change. A version published without `requires_acceptance` (a correction) asks nobody to accept again. A body still containing `[[FILL IN` is refused, which is how the starter text (`terms.STARTER_BODY`) marks the parts only the platform owner can write. Format: `## ` heading, `- ` bullet, blank line = paragraph; rendered with textContent only.
- **Managers** are created by the admin (`/api/managers`) with a temporary password and a **role**; at first sign-in they choose their own password and must set up an authenticator before anything opens. See "Staff roles".

## Staff roles (staff.py)

A role (`staff_roles`, ☰ → Staff → Roles) sets every action of `staff.ACTIONS` to **off**, **allow** or **approve**, and says whether its members may sign in to the admin panel (username + their password + authenticator on the normal sign-in) or only to `/manager/`. Two roles are seeded: "Moderator" (what managers could do before) and "Senior moderator" (admin panel, sees most things, changes need approval).

- Every route a manager can reach maps to one action (`staff.ROUTES`; `test_staff.py` fails on an unmapped route). `require_auth` lets the admin token through; for a manager it calls `staff.gate()`. Reads are off/allow only. Staff, roles and the queue (`/api/managers`, `/api/staff/`) are admin only, whatever the role.
- **approve** = silent: the change is stored in `staff_requests` and **not** done, and the manager gets a plain success answer. The admin approves (`staff.run_approved` replays the stored request through the app: an admin-panel route with a short-lived admin token, a `/manager/` route with the manager's own session, so it is attributed to them) or rejects it. A reload shows the manager the real state. Protective changes (pause a bot or a chat, disable a login, reject a draft, cancel outreach, the global stop, ask to verify again) run at once even at **approve**.
- Every change a manager makes is logged in `staff_requests` (status applied / pending / approved / rejected / failed); waiting ones raise the alert `staff_approval_pending`.

## Verification and photo review (review.py)

For the escort market: the admin marks an industry `requires_review` (☰ → Verification → Businesses).

- **Verification video.** A client login linked to such a business, or one the admin asked to verify again, gets the gate `verify_identity` (after `accept_terms`). It asks for a challenge (a 6-character code to write on paper and a random gesture, valid 30 minutes) and uploads a video showing both. The video is stored only AES-GCM encrypted (`DATA_DIR/review/verifications/`), served only to the admin, and deleted 30 days after the decision. The newest `verifications` row is the login's state.
- **The `verification` hold.** `review.sync_holds()` (after every decision and on every scheduler tick) holds a tenant when its industry requires review and no linked login is verified, or when a linked login was asked to verify again. It can't be resumed by hand; approving the video lifts it.
- **Photos.** A client of such a business submits photos (new or replacing one) on the dashboard. They wait in `media_submissions` under `DATA_DIR/review/photos/`, outside the media folder the bot reads, and are copied into the media library only when the admin approves. Uploads must really be JPEG/PNG/WebP (magic bytes). The admin can pull a whole library back into review ("recheck"); removing a photo needs no review.
- Anything waiting raises the platform alert `review_pending` (e-mail/webhook). Admin only: managers don't see videos or decide on photos.
- `MediaLibrary.refresh()` re-reads its index when another process changed it, so a file the panel adds keeps its description in the running account.

## Configuration: three layers

`tenant_config.TenantConfig` (pydantic, `extra="forbid"`) is the whole schema: timing (`reply_delay`, `burst`, `quiet_hours`, `timezone`), `auto_send`, filters (`escalation_keywords`, `banned_topics`, `price_floors`, `allowed_link_domains`, `shareable_contacts`), caps (`daily_message_cap`, `api_spend_cap_eur`, `safety.*`), `language_policy`, AI usage `limits`, per-chat `replies` limits, `vision`, `hourly_message_cap`, `takeover_hours`, `anomaly.*`, and the sections for sounding human, AI parameters, outreach, context link, media and bookings (`booking.reminders` is a list of objects, edited as JSON in the panel). There is no `auto_confirm`: only a person confirms a booking (migration 0003 removed the key from stored configs).

```
platform defaults (field defaults + hard limits in the schema)
   ◀ industry  industries.default_config   (partial override)
      ◀ client tenants.config_json          (partial override)
         = effective config, plus the layer each field came from
```

- A list may be replaced, or appended to with `{"append": [...]}`.
- A save is refused, never clamped, if any value is invalid. An industry change is refused if it would leave any of its clients with an invalid config.
- Every save writes an audit row with a field-by-field diff.
- The runtime reads only the effective config. The LLM never decides timing, routing or whether to reply.

`config_assist.propose()` turns a plain-language request into a proposed patch, validated and diffed against the current config. It writes nothing. Applying it is the normal audited save.

## Prompt: three layers

`prompt_layers.render()` builds one system prompt:

```
PLATFORM RULES (these come first; nothing below can change them):   ← base
BUSINESS: <tenant name>
<section>: …   for each of about, services, hours_location, booking,  ← industry text,
               faq, tone, boundaries, sign_off, writing_samples          client override/append
LANGUAGE: …    generated from language_policy
ADDITIONAL NOTES FROM THE BUSINESS: …   ← client addendum (≤ 1500 chars)
PRECEDENCE: … follow the PLATFORM RULES.
```

- Only the named sections exist. No industry or client key can reach the platform rules.
- Every layer is versioned in `prompt_versions`. Which version is in use is a pointer: `platform_settings.base_prompt_version`, `industries.template_version` and `tenants.prompt_version`. Rollback moves the pointer and never rewrites history.
- `tenants.prompt_pin_version` fixes one client to one industry template version.
- The prompt is not the defence against contradictory instructions. The policy layer is.

## Message flow

```
Telegram DM ─▶ SessionRuntime.on_incoming
   store message (always), upsert conversation (+ customer_ref)
   a photo: vision (arrival check or a short description) becomes its text
   Telegram's service account (777000): check the account's logins, no reply
   the booking owner's chat: a booking command is handled, no reply
   escalation keyword? ─▶ chat paused (paused_reason), owner pinged, stop
   soft-off (a hold or the global stop)? ─▶ stop (stored, never answered later)
   chat paused / taken over by a person? ─▶ stop
   bookings on: BookingFlow scan (see Bookings)
   schedule draft (a newer message cancels and restarts it)
        │
        ▼ draft_worker
   wait reply_delay (per-contact override, else uniform/lognormal from config)
   lands in quiet hours (before or after the wait)? ─▶ deferred_replies row, stop;
       the scheduler's tick starts the draft again when they end
   wait for this message's booking scan; booking note for the reply
   AI limit reached? ─▶ stop.  No booking news and a reply limit / bare "ok"? ─▶ note, stop
   history (last 30) ─▶ ai_responder.generate_reply(system_prompt = rendered layers,
                                                    booking note, burst_max, language lock,
                                                    no-reply instruction if no news, …)
   "[NO_REPLY]" ─▶ note + audit reply_skipped, stop
                         └─ llm_usage.record (3 token kinds, 3 rates) per call
   media tags → attachments; split into ≤ burst.max_messages parts
   policy.check_outbound(text, config, business text)
        │
   trip-wire in the reply (links, wallets, IBANs)? ─▶ also soft-off (anomaly)
        │
        ├─ auto_send on, no video hold, policy OK ─▶ send_burst
        │     every send: kill switches + send-volume anomaly, re-read from Postgres
        │     check_daily_quota (daily_message_cap, hourly_message_cap, daily_peer_cap)
        │     typing indicator, burst gaps from config
        │     store as sent (llm_model, prompt_version)
        │     audit_log: message_sent, actor=bot, reason="automatic reply"
        │
        └─ otherwise ─▶ store as pending draft (llm_model, prompt_version)
              policy failure: note in the chat + audit_log policy_hold
              operator approves in the panel ─▶ send_burst, actor=admin
```

Telegram errors (`PeerFloodError`, long `FloodWait`, revoked session) halt the account: a `telegram` hold, queued outreach cancelled, `account_halted` audited, an alert. Resuming is manual. WhatsApp errors do the same with a `whatsapp` hold (see "WhatsApp").

A message the account sends that this runtime did not (typed on a phone, or sent by hand from the panel) starts a **human takeover** of that chat: `human_takeover_until = now + takeover_hours`. The bot's own sends are told apart by their Telegram message id (already stored) or by being in flight.

## WhatsApp

A WhatsApp account is a linked device of the number (WhatsApp Web's protocol, spoken by Baileys 7.0.0-rc14 in `wa-gateway`). Everything above applies to it unchanged; this is what is different.

**The transport seam.** `SessionRuntime` holds all the business logic and talks to its network only through a `Transport` (`transport.py`): connect, send a text or a file, typing, read receipts, presence, and every received message handed back in one neutral shape (`Inbound`). Send errors are translated by `Transport.classify` into four kinds (`peer_flood`, `session_rejected`, `rate_limited`, `unreachable`) that `handle_send_failure` acts on the same way for both networks. `_choose_transport` picks `TelegramTransport` (`telegram_transport.py`, Telethon) or `WhatsAppTransport` (`whatsapp_transport.py`) from `telegram_sessions.channel`. Timing, caps, approval, holds and halts never live in a transport.

**The gateway** (`wa_gateway/`, TypeScript on Node 24) is the WhatsApp counterpart of a Telethon client object, for every number at once: one socket per account, the Baileys auth state (creds and signal keys) encrypted in `wa_auth_state` with `crypto.py`'s key, format and AAD convention, nothing on disk. It takes commands on the command bus (`CommandBus.dispatch("@wa-gateway", …)`: `pair`, `pair_cancel`, `open`, `close`, `status`, `send_text`, `read`, `presence`, `logout`) and publishes events on `wa:ev:<session_id>` (`connection`, `session_lost`, `inbox`, `message_failed`) and `wa:pair:<pair_id>`. It never opens a socket on its own and never sends on its own. A singleton: `pg_try_advisory_lock` at boot; a second copy exits (status 3) and compose restarts it. Wire format: `wa_gateway/README.md`.

**Lease and epoch fencing.** The worker takes the account's lease first (`leasing.py`, which bumps `lease_epoch`), and only then sends `open` with that epoch. The gateway checks the row (`channel = 'whatsapp'`, `is_active`, lease live, `lease_epoch = epoch`) and fences every later socket-bound command with it: a lower epoch gets `stale_epoch`, a higher one closes the older socket first. Its watchdog re-reads the row every 10 s and closes a socket whose epoch changed or whose lease has been expired for 30 s, and closes every socket when Postgres has been unreachable for 22 s. The runtime re-sends `open` every 15 s (idempotent for the same epoch), which is how sockets come back after a gateway restart without pairing. Two sockets on one number are what this prevents: WhatsApp answers them with `connectionReplaced` and a ban risk.

**Pairing** (`wa_pairing.py`, `/api/wa/pair/start|{pair_id}|{pair_id}/cancel` in `panel.py`). The panel creates the `wa<digits>` row (`channel = 'whatsapp'`), seeds a brand-new client's config layer with `tenant_config.WHATSAPP_CLIENT_DEFAULTS` (audited), fixes the browser tuple (`wa_device_profiles.py`, stored in the account's identity), subscribes to `wa:pair:<pair_id>` and dispatches `pair`. The gateway wipes the old auth state, streams QR codes or asks for a pairing code, and emits `paired`; the panel then stores the DeepSeek key and marks the row active, and a worker adopts it like any other. Refused while the row has a live lease.

**The `wa_inbox` handoff.** Received messages cross from Node to Python through Postgres, not the bus, so none is lost if the runtime is down. The gateway inserts one row per message (`ON CONFLICT (session_id, wa_message_id) DO NOTHING`, so WhatsApp's redelivery after downtime lands once), retrying with backoff while Postgres refuses (held in its memory meanwhile; warned at 5000 per session), then publishes `inbox`. The runtime drains `wa_inbox` in id order on that event, on connect and on every 15 s keepalive: it stores the message, then deletes the row (at-least-once delivery, deduped by `messages.wa_message_id`).

**Chat identity: `wa_peers`.** The rest of the schema keys a chat by an integer `chat_id`, as on Telegram. `wa_peers` hands one out per contact (one sequence for all accounts) and maps it to the contact's JIDs: the phone JID (`34600123456@s.whatsapp.net`), the LID (`123456789@lid`, WhatsApp's privacy-preserving id), or both once a message reveals that they belong together. `jid` is where sends go: the phone JID when known, else the LID. If one person was first seen as two chats (once by phone JID, once by LID), both histories stay and replies go to the phone JID's chat.

**Migration 0006** adds `telegram_sessions.channel` (and triggers keeping `tenants.channel` equal to it), `wa_peers`, `messages.wa_message_id` (unique per chat), `bookings.provider_wa_message_id` (the owner-facing request, so a quoted reply finds its booking), `wa_auth_state`, `wa_inbox`, and the hold kind `whatsapp`. The three new tables carry `tenant_id` through the `tenant_for_session` trigger like every other.

**An inbound WhatsApp message:**

```
customer's phone ─▶ WhatsApp ─▶ wa-gateway socket (messages.upsert, notify or offline append)
   drop groups, status, broadcasts, newsletters, reactions, protocol stubs, undecryptable
   our own send echoed back (id from send_text)? ─▶ drop
   normalise ─▶ INSERT wa_inbox ON CONFLICT DO NOTHING (retried while Postgres refuses)
   publish {"type":"inbox"} on wa:ev:<session_id>
        │
        ▼ WhatsAppTransport.drain (on the event, on connect, every 15 s)
   wa_store.chat_for(phone_jid, lid, push_name) ─▶ chat_id (wa_peers; created or completed)
   messages.wa_message_id already there? ─▶ delete the row, done
   from_me (typed on the phone) ─▶ handle_own_echo: stored, human takeover
   otherwise ─▶ SessionRuntime.handle_inbound ─▶ the same path as "Message flow" above
   DELETE the wa_inbox row
        │
        ▼ a reply (auto-send or approved)
   ensure_may_send, caps ─▶ presence composing (typing on) ─▶ send_text {session_id, epoch, jid, text}
        ─▶ gateway: fenced, onWhatsApp check for a phone JID, Baileys sendMessage ─▶ {message_id}
   stored as sent with wa_message_id; a refusal raised now is stored red (keep_failed_send)
   a refusal WhatsApp reports later ─▶ message_failed event ─▶ the row turns red, handle_send_failure
```

**What halts.** `session_lost` (`loggedOut`, `forbidden`, `badSession`, `connectionReplaced`, `multideviceMismatch`) is never reconnected: `SessionRuntime.on_session_lost` deletes the stored login, sets the state to `needs_login` (`revoked` for `forbidden`) and calls `halt_everything`, which adds the `whatsapp` hold, audits `account_halted`, raises an alert and logs `HALTING ALL AUTOMATION:`. The runtime keeps its lease, so the account stays visibly red until an operator pairs it again: `/api/wa/pair/start` accepts such an account (state `needs_login`/`revoked`, no stored login), deactivates it, waits for the runtime to fail its next renewal and let go, then pairs; a successful pairing reactivates it. A healthy running number is still refused. A `rate_limited` send error, or a later `message_failed` with code 463 (account restricted), classifies as `peer_flood` and halts the same way; `blocked` and `not_on_whatsapp` are `unreachable` and pause the one chat.

**Not on WhatsApp** (the transport says so): sending files (`can_send_files = False`), outreach (`list_contacts` refuses; the panel hides and refuses it), downloading photos (so no vision), the Telegram login list behind the new-login anomaly, and proxies.

## Bookings

```
customer message ─▶ on_incoming ─▶ BookingFlow.on_customer_message
   bare "1"/"2" after a reminder ─▶ confirm attendance / cancel   (code, no model)
   otherwise scan: ai_responder.extract_booking(history, their bookings)
        intent book ─▶ availability.check_slot(hours, closed days, notice, busy)
             not free ─▶ reply is told: not available + nearest free times (+ waitlist)
             free, new ─▶ booking_store.create (requested, next number)
                          ─▶ owner gets "Booking request #n …" (from this account)
                          ─▶ submitted: pending
             free, open request ─▶ change_request (same number) ─▶ owner again
             free, confirmed ─▶ propose(by customer) ─▶ owner asked to move it
        cancel / accept_proposal / decline_proposal / confirm_attendance / waitlist
owner message ─▶ bookings.parse_owner_reply: YES n | NO n | n HH:MM | CANCEL n | DONE n | NOSHOW n | LIST
panel ─▶ booking_api ─▶ bus: booking_action ─▶ BookingFlow.admin_action
booking page ─▶ public_app ─▶ bus: booking_customer_action
```

- Every transition is a function in `booking_states.py` with guards on state, actor and time; `booking_store.apply()` writes it with `WHERE state = <from>`, so two racing changes can't both win. Nothing confirms without the owner or an admin; `accept_proposal` confirms only a time the other side already put forward in writing.
- A change the customer must hear about sets `customer_notice`; the next reply in that chat carries it (`BookingFlow.reply_note`), and a change that didn't come from the customer's own message starts that reply. The notice is cleared once the reply was sent or saved for approval.
- After every change: a note in the chat, the owner told what they didn't do themselves, the Google Calendar mirror, the e-mail record (SMTP), and a freed time offered to the waitlist.
- Arrival: "I'm here" in the hour before to half an hour after the slot sends `arrival_instructions` once. With the photo check, `vision.compare_to_reference` compares the customer's photo with the media items marked as the entrance; the result is stored as a boolean and audited with the model's confidence.

## Timed work

The scheduler sends `scheduler_tick` to every account with a live lease once a minute. The account then:

0. re-reads its switches, lifts a `spend_cap` hold whose limit is no longer reached, checks send volume, and every 5 minutes its Telegram logins;
1. starts replies whose quiet-hours hold is due (`deferred_replies`, deleted as taken; dropped while soft-off);
2. lets requests nobody answered before their start lapse (`expire`);
3. sends due reminders: each is claimed by inserting its `booking_reminders` row first, so a second tick, a restart or a second scheduler can't send it twice (at most once);
4. moves on waitlist offers that were not taken in time;
5. retries requests that could not reach the owner, every five minutes (not while soft-off).

Then, in the scheduler itself: the heartbeat, the health watchdog and the billing round, each guarded on its own.

## Safety and control

```
controls.py   holds (soft-off per cause) + global stop + hard-off
              off_reason(tenant) = global stop, else every hold
anomaly.py    new login | volume spike | trip-wire  ──▶ 'anomaly' hold + audit + alert
billing.py    active ─(due date passed, tenant tz)─▶ grace ─(grace_hours)─▶ suspended = 'billing' hold
ai_limits.py  a limit reached ──▶ 'spend_cap' hold; lifted by the tick once no longer reached
health.py     account reports (seen, errors, rate limit, logins) ──▶ watchdog ──▶ alerts
alerts.py     one open alert per (tenant, kind) ──▶ panel, e-mail, webhook
```

- **Soft-off is enforced at the last step.** `SessionRuntime.ensure_may_send()` re-reads the switches and the send-volume check from Postgres before every send by the bot (actor bot or system), so a hold added by another process, or the global stop set from the shell, takes effect on the next send whether or not the running account was told. A person sending from the panel is not blocked. The cached `off_reason` only decides earlier exits (no drafting, no AI call); a cached "off" is re-read before a message is ignored, so a missed resume never costs a reply.
- **Resuming replays nothing.** Entering soft-off cancels drafts in progress and deletes held quiet-hours replies; a reminder that falls due while off is claimed and noted as not sent; messages received while off are stored and never answered later.
- **Global stop** is `platform_settings.global_stop`, reachable through the admin login only (and `python controls.py stop|resume` on the server). Phase 4's owner login must not get it.
- **Hard-off** asks the running account to `log_out()` (Telegram invalidates the key; on WhatsApp the gateway unlinks the device), or, when nothing runs it, takes its lease and logs out directly (WhatsApp: `logout` with that lease's epoch, through a temporary socket); then the key (and `wa_auth_state`) is deleted, the account deactivated (`state = revoked`) and audited. Lease renewal requires `is_active`, so any deactivation fences the worker within `RENEW_SECONDS`, and the worker drops finished runtimes and their leases (`SessionRuntime.finished`).
- **Anomalies.** New login (Telegram only): `account.getAuthorizations` every 5 minutes (and at once on a message from 777000), compared with `sessions_health.known_session_ids_json`; the first check only records. Volume: sent messages in the last hour against the tenant's own average hour over `volume_baseline_days`, tripping at `volume_multiplier` ×, never below `volume_min_messages`; checked before every bot send and on every tick (so hand-typed or hijacker sends count too). Trip-wire: `policy.Verdict.tripwire`. Every trigger writes `anomaly_detected` with the reason; the hold stays until a person resumes it.
- **Billing.** The scheduler moves a tenant to grace the day after `billing_next_due` in its own timezone and asks its account to message the owner (`owner_notice`, to `booking.provider`), retrying each tick until sent. After `grace_hours` it is suspended. Recording a payment or setting the status by hand (with a reason) is always possible; every change is `billing_changed`.
- **Health.** The account writes `last_seen_at` once a minute while connected (its own loop, not the scheduler's tick), errors, and Telegram's rate limits. The watchdog derives one status per tenant: `not_running` (no live lease for 3 minutes), `disconnected` (lease, but not connected for 3 minutes), `logged_out`, `rate_limited`, `ok`. A change to a bad status opens an alert `health:<status>`; back to `ok` closes it and sends "back to normal". FloodWaits Telethon sleeps through itself (under `max_flood_wait_seconds`) are not seen.
- **Escalation and takeover** are per chat and in code: `policy.escalation_match` (word start, any case) pauses the chat with a `paused_reason` and pings the owner; a hand-written message sets `human_takeover_until`. Both keep the booking scan, replies and reminders out of that chat.

## Limits before every reply

`draft_worker` waits for the booking scan of the same message, then:

- AI usage (`ai_limits.limit_reached`): tokens and EUR per day and per month in the tenant's zone, summed from `llm_usage`. At a limit no model call is made; the panel and audit log are told once.
- Replies (`ai_limits.reply_limit`, only when there is no booking news): AI-written messages per chat per hour/24 h and the least gap, counted from `messages` on the database clock; bare acknowledgements; and the tenant's `no_reply_instruction`, where the model may answer `[NO_REPLY]`. Every skip is a note and a `reply_skipped` audit row.

## Where changes reach a running account

A panel save writes Postgres, then sends `reload_config` to every affected account that has a live lease, all at once. Affected means one tenant, every tenant in an industry, or every tenant for the base rules. The worker calls `bind_tenant()`: it reloads the effective config and the rendered prompt. As a backstop, each runtime rebinds on its own every 5 minutes (`REBIND_SECONDS`).
