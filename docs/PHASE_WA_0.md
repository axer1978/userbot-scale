# Phase WA-0: read, report, decide

Branch `platform/whatsapp`, cut from `origin/platform/phase-1` @ b98f0e5. This phase has no code changes.

## Two things to know first

1. **`docs/FEATURE_LIST.md` does not exist.** It is not on `platform/phase-1` (including b98f0e5), `main`, any local branch or any copy in Downloads. So I could not cite "sections 1–27 / 28". Instead, the parity table below is built from `ARCHITECTURE.md`, `docs/PHASE_1–4.md`, `telegram_admin_bot/README.md` and the code. The row numbers in it are mine. See decision D10.
2. **Most of this has already been built once, locally.**
   - It is on branch `wa/integration` @ 1d9ee69 in `Downloads/userbot-scale`, cut from `main` @ 72f764a. That is `platform/phase-1` minus two documentation-only commits.
   - The work spans per-step branches `wa/1`…`wa/8`, about 13,400 lines added: transport seam, migration 0006, the Node `wa-gateway`, panel pairing, the WhatsApp runtime inbound and outbound, and docs.
   - Its Python suite collects 1,175 tests (1,048 on `main`), and there are 57 Node tests.
   - It differs from this spec in several places, listed in §5. Decision D0 is whether to reuse it.

---

## 1. Parity table

Key:
- **same**: the channel doesn't matter.
- **adapted (P)**: needs transport primitive P (see D4).
- **not possible**: with the reason and what the panel shows instead.

### Admin, tenancy, config, prompts

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 1 | Admin login (password + TOTP), lockout, public HTTPS, CSP | same | |
| 2 | Tenants, industries, **Clients** view, industry folders | same | Add a channel badge and filter (Phase 4) |
| 3 | Three-layer config, strict validation, audited diff, `reload_config` | same | Telegram-only fields: see 3a |
| 3a | `safety.max_flood_wait_seconds`, `safety.halt_on_peer_flood` | adapted (`classify`) | WhatsApp sends no wait time with a rate limit, so any rate limit halts at once. `max_flood_wait_seconds` has no effect on WhatsApp; the field help text says so (D11) |
| 4 | Ask AI config proposals | same | |
| 5 | Prompt layers, versions, pin, rollback, rendered prompt | same | Your stored content says "Telegram" (D9) |
| 6 | Tenant isolation: `tenant_id` + `tenant_for_session` trigger + scoped facade | same | New tables get the trigger (Phase 2) |
| 7 | `customer_ref` HMAC per tenant | adapted (identity map) | D1 |
| 8 | Media library per tenant (`DATA_DIR/tenants/<id>/media`) | same | Sending it: row 30 |
| 9 | Audit log, append-only | same | |
| 10 | LLM metering, `llm_usage` | same | |
| 11 | Secrets AES-GCM under the master key | adapted (auth state) | The Baileys auth state uses the same envelope, and must decrypt in both Python and Node |

### Inbound pipeline (`on_incoming`)

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 12 | Private messages only | adapted (inbound normalisation) | The gateway drops group, status, broadcast and newsletter traffic (§3) |
| 13 | Store every message, push live to the panel | same | Dedupe on a text external id (D1) |
| 14 | Sender display info (name, @username, `is_bot`, access hash) | adapted (`Inbound.peer`) | WhatsApp gives a push name and a phone or LID. No username, no bot flag, no access hash: those fields stay empty |
| 15 | Telegram service account 777000: never answered, triggers a login check | adapted | WhatsApp system and protocol messages never reach the runtime (the gateway drops them). `is_service_chat()` is always false. See §2 for new-login detection |
| 16 | Owner's chat: booking commands, no reply | adapted (`resolve_owner`, `Inbound.reply_to`) | The owner is a phone number. "Reply to the request" uses the quoted message's text id (D1) |
| 17 | Photo → vision description | adapted (`download_media`) | Not done in `wa/integration`: it returns None |
| 18 | Voice, sticker, document, location, contact card | adapted (normalisation) | Stored as `[non-text message]` and not drafted, exactly as Telegram does today (§3) |
| 19 | Escalation keyword → chat paused, owner pinged | same | |
| 20 | Soft-off (holds) and global stop | same | |
| 21 | Chat pause, human takeover | adapted (`Inbound.from_me`) | The gateway keeps `from_me` messages, flagged. The bot's own sends are told apart by their external id, as on Telegram |
| 22 | Staging `test_chats` | adapted | On WhatsApp an entry is a phone number. The validator already accepts digits. Only the help text changes |
| 23 | Booking scan (extraction) | same | |
| 24 | Unanswered queue (every reason) | same | |
| 25 | Drafts: `pending_approval` / auto-send | same | |

### Drafting and outbound

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 26 | `draft_worker`: `reply_delay`, newer message restarts, quiet hours → `deferred_replies` | same | |
| 27 | AI limits, reply limits, `[NO_REPLY]`, bare-ack skip | same | |
| 28 | `generate_reply` (history, burst, language lock), adaptive style | same | Prompt wording says "Telegram" (D9) |
| 29 | Policy checks, trip-wire → anomaly hold | same | |
| 30 | Media send (photo/video from the library; video offered first and held) | adapted (`send_file`) | Not done in `wa/integration` (raises) |
| 31 | Send text, burst gaps | adapted (`send_text`) | |
| 32 | Typing indicator for a plausible time | adapted (`set_typing`) | `composing` → `paused` |
| 33 | Mark read after the delay | adapted (`mark_read`) | WhatsApp needs the message keys. The transport keeps the last unread keys per chat |
| 34 | Presence: offline between conversations | adapted (`set_presence`) | `available` / `unavailable` |
| 35 | `ensure_may_send` (re-read holds), daily/hourly/peer caps, volume check | same | |
| 36 | Approve, Edit then Send, Reject, manual send over the bus | same | |
| 37 | Failed send stored as `error`, shown red | same (+ `classify`) | Includes delivery failures WhatsApp reports after the send (e.g. 463) |
| 38 | PeerFlood / long FloodWait → `telegram` hold, halt, alert | adapted (`classify`) | WhatsApp 429 / rate-overlimit → the same halt. The hold kind is D3 |
| 39 | Blocked / privacy / deactivated → that chat paused | adapted (`classify`) | "Not on WhatsApp", blocked |
| 40 | Revoked session → logged out, lease released | adapted (`session_lost`) | Reasons in §2 |
| 41 | Disconnect → reconnect with backoff | adapted (gateway) | |
| 42 | Context link (off for clients) | adapted, weaker | No @username signal, so only the full-name match is left, on self-chosen push names. Off for clients anyway. Proposal: same code; on WhatsApp it auto-links less often |
| 43 | Outreach to contacts (off by default, approval, 90–300 s gaps) | **D7** | Recommendation: not possible (signed off). The panel hides the Outreach button by capability and shows "not available on this channel" |
| 44 | Outreach "contacts only" re-check | follows 43 | |

### Bookings

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 45 | State machine, availability, overlap constraint, waitlist, lapse, owner retry | same | |
| 46 | Owner commands YES/NO/propose/CANCEL/DONE/NOSHOW/LIST, bare `yes` as a reply | adapted (`reply_to` text id) | `bookings.provider_message_id` must hold a text id (D1) |
| 47 | Reminders with 1/2, claimed once | same | |
| 48 | Arrival instructions; photo check against *Entrance* media; owner's "door" photo | adapted (`download_media`) | |
| 49 | ICS feed, booking pages, e-mail record, Google Calendar mirror | same | The GCal event text "via Telegram" becomes the channel's display name |
| 50 | Bookings view in the panel | same | |

### Safety and control

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 51 | Holds per cause, each lifted on its own | same | The `telegram` kind: D3. Bug found in `wa/integration`: Safety cannot lift a `whatsapp` hold (`safety_api.py:272`) |
| 52 | Global stop (panel + `controls.py stop`) | same | |
| 53 | Hard-off: typed id → log out → delete key → deactivate → audit | adapted (`log_out`) | Logging out removes the linked device. "Key" = the `wa_auth_state` rows |
| 54 | Billing grace / suspend, owner notice | same | |
| 55 | AI spend cap → `spend_cap` hold, lifts by itself | same | |
| 56 | Anomaly: new login | **D8** | `list_logins`. See §2 |
| 57 | Anomaly: send volume, trip-wire | same | |
| 58 | Health: `not_running`, `disconnected`, `logged_out`, `rate_limited`, `ok` | adapted | `last_seen_at` comes from the transport's connected state; `logged_out` from `session_lost`; `rate_limited` from 429 |
| 59 | Alerts: one open per (tenant, kind), e-mail, webhook | same | The text gets neutral wording (§4.6) |
| 60 | Safety → This client: "Logins on this account" card | adapted, or "not available" | D8 |
| 61 | Safety → proxy card | adapted | The gateway uses the same encrypted per-account proxy (missing in `wa/integration`) |

### Client-facing

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 62 | Client logins, 2FA, scoping | same | |
| 63 | Client dashboard | same | Add a badge. Status labels "Telegram connected / Logged out of Telegram" become neutral |
| 64 | Unanswered queue + promote to FAQ | same | |
| 65 | Weekly digest to the owner's chat + e-mail | same | The `sent_via` label becomes the channel name |
| 66 | Onboarding wizard (6 steps) | adapted | Step 1 is the channel choice + sign-in. The owner field's wording becomes "owner's phone or username" by channel |
| 67 | Review batches, JSONL export | same | |
| 68 | Phone/tablet layouts | same | |

### Accounts and fleet

| # | Feature | WhatsApp | Notes |
|---|---|---|---|
| 69 | + Add account (API ID/hash, phone, code, 2FA password) | adapted (sign-in) | Number + proxy → QR or pairing code |
| 70 | One account per number (`tg<digits>`); re-sign keeps history; a running number is refused | adapted | `wa<digits>`, so one per number per channel |
| 71 | Stable device identity (`device_profiles.py`) | adapted | A Baileys browser tuple, derived from the account id and stored |
| 72 | Per-account proxy, socks5/socks5h/http | adapted | |
| 73 | Lease, one worker per account, adopt within ~15 s, failover in 30 s | same | The gateway is fenced by the lease epoch |
| 74 | Scheduler tick and timed work (steps 0–5) | same | |
| 75 | Take out of rotation (`is_active=false`) | same | |

### Not possible on WhatsApp (for sign-off)

| Item | Why | What the panel shows |
|---|---|---|
| Bot accounts (`is_bot`) | WhatsApp has no bot-account flag on personal chats | Nothing; the column stays false |
| @usernames | Baileys does not expose WhatsApp usernames | Name + phone |
| FloodWait seconds | WhatsApp rate limits carry no wait time | The account halts; you resume |
| Login list with device, app and country | Only device ids are visible to a linked device, if anything (D8) | "Linked devices: n" or "not available on WhatsApp" |
| 2FA cloud password at sign-in | Linking needs no PIN | — |
| Outreach (if D7 = leave out) | Ban risk; no reliable address book on a linked device | Button hidden by capability |

---

## 2. Section 28: Telegram-only items and their WhatsApp counterparts

| Telegram | WhatsApp counterpart |
|---|---|
| Session / auth key (`auth_key_enc`, dc, server, port) | **Baileys auth state:** `creds` + signal keys (pre-keys, sessions, sender keys, app-state keys, LID mappings). Stored as rows in `wa_auth_state`, each value encrypted with the `crypto.py` envelope |
| Sign-in: phone → code → 2FA | **QR** (refs rotate about every 20 s; the dialog redraws) or a **pairing code** (8 characters, typed on the phone under Linked devices → Link with phone number). Both use the account's proxy |
| Device identity (device model, OS, app version) | **Browser tuple** `[os, browser, version]`, e.g. `["Windows","Chrome","…"]`. Derived from the account id, stored, never changes for that account. Shown on the phone as the linked device's name |
| Service account 777000 | **Nothing to answer.** WhatsApp's system/protocol traffic (`protocolMessage`, history sync, app-state, key distribution, call offers) is dropped in the gateway. There is no "new login" message to react to |
| New-login detection (`account.getAuthorizations`) | **D8.** Option: list the account's own device ids through a USync device query every 5 minutes, and flag a new companion id. It yields ids only (no names), and I haven't verified it on Baileys 7. Or mark the check unavailable on WhatsApp |
| Flood / rate-limit / revoked errors | 429 `rate-overlimit` → halt + hold (as PeerFlood). Disconnect `loggedOut` 401, `forbidden` 403, `badSession` 500, `connectionReplaced` 440, `multideviceMismatch` 411 → `session_lost` (no reconnect). `restartRequired` 515 → reconnect at once. Anything else → backoff. Per chat: not on WhatsApp, blocked, 463 → the chat is paused |
| Hard-off via `log_out()` | Gateway `logout` command (fenced by the lease) → Baileys `logout()` removes the linked device on the phone → `wa_auth_state` rows deleted. If nothing runs the account, the panel takes the lease and asks the gateway to open, log out and close |
| Typing / read / presence | `sendPresenceUpdate('composing'/'paused', jid)`, `readMessages(keys)`, `sendPresenceUpdate('available'/'unavailable')` |
| Bot accounts | None (see the "not possible" table) |
| Contacts check (`GetContactsRequest`) | No reliable equivalent: a linked device only sees contacts that app-state sync pushes (`contacts.upsert`), and those are partial. Matters only if outreach is kept (D7) |
| Chat ids / usernames | JIDs: `<digits>@s.whatsapp.net` (phone) or `<n>@lid` (privacy id), mapped to a BIGINT `chat_id` (D1). No usernames |
| Proxy wording ("Telegram proxy") | "Proxy for this account", used for sign-in and for the live connection on either channel |

---

## 3. WhatsApp-only message types

The aim is that `on_incoming` sees the same `Inbound` shape as for Telegram and needs no new branch.

| Type | Handling |
|---|---|
| **LID vs phone JID** | Baileys 7 can give either, and often both (`key.remoteJid` + `remoteJidAlt`, plus `lid-mapping.update`). The gateway sends both on every event. The runtime's peer map turns any of them into one `chat_id` and merges when it learns the pair. `customer_ref` is built from the `chat_id`, so it stays fixed whichever form arrives (D1) |
| **Voice notes** (`audioMessage`, ptt) | Stored as `[non-text message]` with no draft, exactly what Telegram's pipeline does with a voice note today (`session_runtime.py:2097`). No transcription: that would be new behaviour |
| **View-once media** | WhatsApp does not deliver the content to linked devices. Stored as `[view-once media]`, never downloaded, no vision |
| **Reactions** | Dropped (not stored). Telegram's pipeline has no reaction handling either |
| **Edits** (`editedMessage` / `MESSAGE_EDIT`) | Dropped; the original stays. Same as Telegram today (no `MessageEdited` handler) |
| **Deletes** (`REVOKE`) | Dropped; our copy is kept. Same as Telegram |
| **Disappearing messages** (`ephemeralMessage` wrapper) | Unwrapped and handled as normal. The gateway sends our replies with the chat's current timer so they disappear too; otherwise WhatsApp shows the reply as "not disappearing". Unverified until Phase 6. Our stored copy remains, as with every message |
| **Status, broadcast, group, newsletter, calls, polls** | Dropped in the gateway. Calls are not stored |
| **History sync on first link** | Not imported and not fed to the pipeline (it would answer old messages). `syncFullHistory: false` |
| **Stickers, documents, location, contact cards** | `[non-text message]`, as on Telegram |

---

## 4. Telegram assumptions outside the transport

Full file:line list: [`PHASE_WA_0_inventory.md`](PHASE_WA_0_inventory.md). The main ones:

### 4.1 Schema
- `chat_id BIGINT` on 13 tables: conversations, messages, outreach, chat_links (+ `source_id`), chat_summaries, bookings (+ `provider_chat_id`), waitlist, deferred_replies, unanswered_queue, review_items.
- `messages.telegram_id BIGINT` + unique `idx_messages_tg`, used as the dedupe target.
- `bookings.provider_message_id BIGINT`.
- `conversations.access_hash`, `username`, `is_bot`.
- `telegram_sessions`: `api_id`, `api_hash_enc`, `dc_id`, `server_address`, `port`, `auth_key_enc`, `takeout_id`. Every account table has a foreign key to it.
- `telegram_peers` and `session_update_state`: covered by triggers, but no code reads them.
- Hold kind `telegram`: a CHECK constraint (`0004:17`), `controls.TELEGRAM`, `safety_api.py:272`.
- `sessions_health.known_session_ids_json` (+ `authorizations_checked_at`).
- `tenants.channel` exists but is never written on create (`tenants.py:226`).

### 4.2 `customer_ref`
`crypto.customer_ref(tenant, channel, id)` is already channel-aware. Its callers hard-code `"telegram"`:
- `database.py:469`
- `booking_store.py:183,377,498`
- the `tenants.backfill()` step at `tenants.py:714-723`

### 4.3 Telethon calls
- `session_runtime.py`: every call. Factory, connect, `send_message`, `send_file`, `action`, `send_read_acknowledge`, `UpdateStatusRequest`, `GetContactsRequest`, `GetAuthorizationsRequest`, `get_input_entity`, `download_media`, the error classes, `events.NewMessage`.
- `login_flow.py`: the whole module.
- `booking_flow.py:343-365`: `get_entity` for the owner, plus `row["telegram_id"]` as `provider_message_id`.
- `anomaly.login_record`: reads Telethon `Authorization` attributes.
- `proxies.telethon_tuple`.
- `controls.hard_off` → `log_out_session`.

### 4.4 Handles and service account
- 777000: three branches in `session_runtime`.
- `is_bot`: also used in `context_link`.
- @username: `context_link`, the staging validator, `bookings.py:108`, `customer_username`, and the JS for style, conversations, outreach and bookings.
- "Not in your Telegram contacts": `panel.py:1024-1034`.

### 4.5 Forms and proxy wording
- `index.html:78-95`: "Add a Telegram account", API ID / API hash, "my.telegram.org", the proxy hint, the cloud password line.
- `panel.py:561-635`: `AuthStartBody` and the API ID validation.
- Onboarding (`onboarding.js` lines 27, 130, 132, 310, 334, 363, 394, 397, 470, 477): "Telegram account", "Owner's Telegram", "@username or chat id".
- `safety.js:263-356`: "Telegram" and "Telegram proxy" cards; `proxies.py` docstring.

### 4.6 Human-facing text
- **Alerts and health:** `health.py:55-58`; `session_runtime.py` lines 785, 791, 1082–1111, 1156, 1712, 2213, 2295.
- **Controls and Safety:** `controls.py` lines 60, 227, 265–266; `safety_api.py:213`.
- **Panel UI:**
  - `socket.js`: 99, 172, 175–182.
  - `owner.js`: 42–44.
  - The page title and brand "Telegram AI Assistant" (`index.html:10,17`, `panel.py:146`).
- **Audit:** "New Telegram account added" (`database.py:218`).
- **Digest:** `sent_via` (`digest.py`).
- **Google Calendar:** the "via Telegram" event text.

### 4.7 Status keys
- `telegram_connected` / `telegram_error` in the status dictionary, the panel API and the JS.
- `telegram_state` used in `booking_flow`.

### 4.8 Bot behaviour content (yours; I won't change it without your answer to D9)
- The seeded base prompt says "in a Telegram chat" (`0002_tenants.sql:141`). That migration has already run, so the live text is in `prompt_versions`, where you can edit it.
- `ai_responder.py:20,26,448`: "a real person's Telegram account", "private Telegram conversation".
- `wa/integration` added `Transport.adapt_prompt()`, which rewrites "Telegram" in prompt text for WhatsApp. That changes prompt wording, so it needs your decision.

---

## 5. The existing `wa/integration` work against this spec

| Spec phase | Already there | Missing or different | Verdict |
|---|---|---|---|
| 1 Seam | `transport.py`, `telegram_transport.py`; the runtime imports no Telethon; `FakeTransport` used by 9 tests | Fake lives in one test file; no `PHASE_WA_1.md`; ARCHITECTURE edit is in a later commit | Reuse with changes |
| 2 Schema | `0006_whatsapp.sql`: `telegram_sessions.channel`, `wa_peers` (BIGINT chat_id from a sequence), `messages.wa_message_id`, `bookings.provider_wa_message_id`, `wa_auth_state`, `wa_inbox`, hold kind `whatsapp`; channel-aware backfill; 12 migration tests | WhatsApp-specific id columns, not a neutral external id; `wa_inbox` (vs Valkey stream); no `test_tenant_isolation` coverage; upgrade test uses Telegram rows only, not a phase-4 copy | Reuse with changes |
| 3 Gateway | TS, `baileys@7.0.0-rc14` exact, auth state in PG, crypto vectors both ways, stable browser tuple, `pair`/`open`/`close`/`logout`, `session_lost` reasons, private chats only, `from_me` flagged, lease-epoch fencing + watchdog, compose service | **No proxy**; inbox via Postgres; a manual `cli.ts listen` bypasses the lease; `engines >=20` but tests need Node 24 | Reuse with changes |
| 4 Panel | Channel step in + Add account, QR/pairing code with rotation, `wa<digits>`, running number refused, re-pair after loss, picker badge | **No onboarding channel choice**, no proxy field, no filter, no badges outside the picker, **no jsdom tests** | Reuse with substantial additions |
| 5 Inbound | Transport chosen from `telegram_sessions.channel`; `open` after lease; dedupe; ack after store | `download_photo` returns None (no vision or door photo); `list_logins` silently empty | Reuse with changes |
| 6 Outbound | Text, composing/paused, read, presence; error mapping as specified; failed send → red; `session_lost` → halt + hold + alert; hard-off logout | **No media send**; no "unavailable" marking for new-login; health parity unchecked; Safety can't lift a `whatsapp` hold | Reuse with changes |
| 7 Parity / e2e | Owner replies on WA resolve bookings | Both-channel stack tests, restarts, hand-off, exactly-once through downtime | New work |
| 8 Docs | README, ARCHITECTURE, RUNBOOK, gateway README | Inbox, proxy and stream parts; FEATURE_LIST column | Reuse with changes |

Channel branches it has outside the transport, which break the definition of done:
- `controls.py:231,250-276`: hard-off by channel.
- `booking_flow.py:388,419,490`: `id_field == "wa_message_id"`.
- `session_runtime.py:2225`: `wa_store` imported in the core.
- `health.py:160`: a `wa_auth_state` clause.
- `accounts.js:220`: outreach hidden by channel.
- `core.js:35-45`: `channelName`.

In a re-cut, capabilities and neutral columns replace all of these.

---

## 6. Decisions for you

Each decision below has a recommendation and its trade-off. Proposed rule for the definition of done: **only sign-in, the transport factory and seeding an account's first config may look at `channel`.** Everything else asks the transport about **capabilities**, e.g. `logins`, `contacts`, `files`, plus the channel's display name.

**D0 — Starting point.**
- Recommendation: rebuild the phases on `platform/whatsapp` by porting `wa/integration` phase by phase. Each phase is still reviewed and committed on its own, and the gaps in §5 are closed in the phase they belong to.
- Trade-off: a fresh build re-derives about 13k lines that are already tested, but has no legacy design to unpick. Porting is faster, but its shapes (`wa_inbox`, the `wa_message_id` columns) have to be undone where you decide differently below.

**D1 — Chat identity and message ids.**
- Recommendation: keep `chat_id BIGINT` everywhere. Add a tenant-scoped peer map `jid / phone_jid / lid ↔ chat_id`, with ids from a sequence; `chat_id` is unique per account, and an account is one channel.
- `customer_ref = HMAC("whatsapp:<chat_id>")`, so it stays fixed whichever JID form arrives.
- Add a **neutral** `messages.external_id TEXT`, filled from `telegram_id::text`, with a unique `(session_id, chat_id, external_id)` index. Do the same for `bookings.provider_external_id TEXT`. The core uses only these.
- Trade-off on BIGINT vs text: widening `chat_id` to text touches 13 tables, every index, the exclusion constraints' tables and all panel JS, which handles ids as numbers. That is a large Telegram change for no Telegram gain. The map costs one lookup per inbound message.
- Trade-off on the `customer_ref` source: a phone-based ref would be meaningful across tenants but would change when a LID-only contact later reveals their phone. A chat_id-based ref is fixed, but if the same person arrives first as LID and later as phone *without* WhatsApp sending the mapping, they become two chats. That is rare with Baileys 7, which receives LID mappings, and the two rows are merged when the mapping arrives.

**D2 — Account table.**
- Recommendation: add `channel` to `telegram_sessions` and keep the name. Make the Telegram-only columns nullable.
- Trade-off: the name will mislead future readers. A rename to `accounts` with a `telegram_sessions` compatibility view (simple views are updatable in Postgres) is clean, but touches about 20 statements in 10 modules and many tests. That conflicts with "change Telegram code only as far as the seam needs". It can be a later, mechanical migration.

**D3 — Hold and alert kinds.**
- Recommendation: generalise `telegram` into a neutral **`channel`** kind, labelled "stopped after a channel error: <reason>", where the reason names the network. A migration rewrites existing holds; the audit history keeps the old word.
- Trade-off: adding `whatsapp` alongside `telegram` changes no live rows, but every list of kinds must then know both. The bug in `wa/integration` at `safety_api.py:272` is exactly that failure. Health alert kinds (`health:<status>`) are already neutral.

**D4 — Transport interface.** Proposed:
- **Lifecycle:** `prepare()`, `start()`, `disconnect()`, `log_out() → bool`, `halt_updates()`, `reload_login()`.
- **State:** `connected`, `error`, `me` (id, name, phone), `display_name`, `capabilities: frozenset`.
- **Peers:** `resolve_peer(chat_id)`, `resolve_owner(text) → (chat_id, PeerInfo)`, `is_service_chat(chat_id)`.
- **Send:** `send_text(chat_id, text) → external_id`, `send_file(chat_id, path, kind, caption) → external_id`.
- **Signals:** `set_typing(chat_id, on)`, `mark_read(chat_id, upto)`, `set_presence(online)`.
- **Optional, by capability:** `list_contacts()`, `list_logins()`, `download_media(inbound) → bytes`.
- **Inbound** (normalised): `chat_id`, `external_id`, `from_me`, `kind`, `text`, `reply_to`, `peer: PeerInfo`, `at`.
- **Errors:** `classify(exc) → Failure(kind, seconds, reason)`. The kinds are `halt_flood`, `session_lost`, `rate_limited(s)`, `chat_unreachable` and `transient`, with one outcome table in the core: halt + hold, logged_out + halt, backoff or halt, pause chat, retry.
- **Selection:** the manager picks the transport from `telegram_sessions.channel` through a registry `{telegram: TelegramTransport, whatsapp: WhatsAppTransport}`; tests register `fake`.
- Drop `adapt_prompt` (see D9).

**D5 — Gateway ↔ runtime wire format and the inbound buffer.**
- Commands use the existing `commands.py` envelope plus `v: 1`, targeted at `@wa-gateway`: `pair`, `pair_cancel`, `open{session_id, epoch, browser, proxy}`, `close`, `logout{epoch}`, `send_text`, `send_file`, `read`, `typing`, `presence`. Each reply is `{ok, error_kind?, reason?}`.
- Events go on `wa:ev:<session_id>`, each with `v: 1`: `connected`, `disconnected`, `session_lost{reason}`, `delivery_failed{external_id, code}`, `pair:*`.
- **The inbound buffer is the real choice.** Your spec says a bounded per-account Valkey stream. Earlier you chose Postgres `wa_inbox`, because Valkey runs with persistence off (`--save "" --appendonly no`, no volume).
  - Recommendation: **keep Postgres `wa_inbox`**: insert-or-ignore on `(session_id, external_id)`, the runtime deletes rows after storing them, a pub/sub nudge, and a per-account cap that raises an alert.
  - Trade-off: a Valkey stream needs AOF on, a volume and `MAXLEN`. "Bounded" then means the oldest messages are *dropped* when a runtime is down for long. `appendfsync everysec` can lose about 1 s of messages on a Valkey crash, after WhatsApp already counts them as delivered. Postgres is already the store of record and loses nothing. Its cost is one table and polling writes.

**D6 — Repo layout.**
- Recommendation: your default. `telegram_admin_bot/wa_gateway/` with its own Dockerfile (`node:24-slim`; Node 24 is LTS), the service `wa-gateway` in the same compose file, and `baileys` pinned at `7.0.0-rc14`, still npm `latest` today (`legacy` = 6.7.24).

**D7 — Outreach on WhatsApp.**
- Recommendation: **leave it out** (signed off). The capability is absent and the button is hidden.
- Trade-off: parity would mean a contacts source a linked device doesn't reliably have, and unsolicited messages from a Baileys-linked number are the most likely ban trigger. Keeping it at parity (off by default, approval) is possible, but only "contacts" that app-state sync happens to deliver could be offered.

**D8 — New-login anomaly on WhatsApp.**
- Option A: a USync device-id check every 5 minutes. It flags a new companion device and shows "Linked devices: n" in Safety. The cost is unverified Baileys behaviour, and I can only build it on your test number in Phase 6.
- Option B: mark it "not available on WhatsApp" in Safety and in the anomaly settings.
- Recommendation: B now, and A as a follow-up once a real number shows the device ids are reliable.

**D9 — The word "Telegram" in bot content.**
- The base prompt (stored, yours to edit) and three strings in `ai_responder.py` say Telegram.
- Option A: you edit the base prompt to be neutral, and I replace only the word "Telegram" in `ai_responder.py` with the transport's display name, no other change.
- Option B: leave all prompt text as it is, so WhatsApp customers' replies are generated under a "Telegram" framing.
- Recommendation: A, but only with your yes, because it is prompt text.

**D10 — FEATURE_LIST.md.**
- Do you have it, maybe unpushed? If not, I'll write it in Phase 8 from §1, with a channel column. Until then, §1's row numbers are the reference.

**D11 — Channel-specific defaults and fields.**
- `wa/integration` seeds stricter defaults on a new WhatsApp account: auto-send off, caps 60/day and 15/hour, quiet hours 21–09. Keep that, as a sign-in-time exception? I recommend it, given the ban risk. You may prefer to set them per client in the panel instead.
- `max_flood_wait_seconds` stays in the shared schema. Its help text will say it has no effect on WhatsApp.

## Not checked
- Nothing was run: this phase only read code and the two branches.
- Baileys behaviour noted as "unverified" in §2 and §3 (USync device ids, disappearing-message timers) needs your test number.
