# Phase WA-0 appendix: Telegram assumptions outside the transport

State: `platform/phase-1` @ b98f0e5. Paths are relative to `telegram_admin_bot/`.

Each item carries one tag:
- **(a)** transport: moves behind `TelegramTransport`;
- **(b)** schema or naming;
- **(c)** text shown to people;
- **(d)** logic that needs to know the channel.

## 1. Schema (`migrations/`)

**`chat_id BIGINT` columns (b).** Telegram user ids; WhatsApp JIDs are strings.
- `0001_init.sql`:
  - `:72` `conversations.chat_id` (PK with `session_id`, `:82`); `:76` `access_hash`; `:75` `is_bot`; `:74` `username`.
  - `:90` `messages.chat_id`; `:91` `messages.telegram_id`; `:99-100` unique `idx_messages_tg (session_id, chat_id, telegram_id)`, the ON CONFLICT dedupe target.
  - `:109` `outreach.chat_id`; `:124-125,131` `chat_links.chat_id` / `source_id`; `:136` `chat_summaries.chat_id`.
  - `:146,148,157-158` legacy `bookings`: `chat_id`, `client_username`, `provider_message_id`, `provider_chat_id`.
- `0003_bookings.sql`:
  - `:31-32,50-51` `bookings.chat_id`, `customer_ref`, `provider_chat_id`, `provider_message_id`; `:15` `customer_username`.
  - `:142` `waitlist.chat_id`; `:164,167` `deferred_replies.chat_id` (PK `tenant_id, chat_id`).
- `0005_client_facing.sql`: `:55` `unanswered_queue.chat_id`; `:90` `review_items.chat_id`.
- `0002_tenants.sql:229-242`: tenant-scoped indexes on `chat_id`.

**`telegram_sessions`** (`0001_init.sql:13-39`) (b).
- MTProto-only columns: `api_id`, `api_hash_enc`, `dc_id`, `server_address`, `port`, `auth_key_enc`, `user_id`, `username`, `takeout_id`.
- Every per-account table has a foreign key to it.
- Code references:
  - `database.py:148-376` (SessionRegistry)
  - `leasing.py:44,56,68,74`
  - `controls.py:194,197,253`
  - `scheduler.py:102`
  - `digest.py:97`
  - `platform_api.py:64`
  - `health.py:157`
  - `review_api.py:431`
  - `safety_api.py:47,103`
  - `tenants.py:127`
  - `login_flow.py:5,170`
  - `session_runtime.py:6,2400`

**`telegram_peers`** (`0001:41-55`) and **`session_update_state`** (`0001:57-67`) (b): in the trigger list (`0002:201`); no code reads them.

**Hold kind `telegram`** (b/d).
- `0004_safety.sql:17,21`: the CHECK constraint; `:38-47`: backfill from `halted`.
- `controls.py:52-53,60`: the constant and its label.
- Set at `session_runtime.py:1020`; read at `safety_api.py:272`.

**`sessions_health`** (`0004:74-88`) (b): `known_session_ids_json`, `authorizations_checked_at`, `rate_limited_until`.
- Read in `health.py:95,104,204`; written at `session_runtime.py:781`.

**Alert kinds** (free text) (b):
- `"telegram"`: `session_runtime.py:1029`
- `"anomaly:new_login"`: `:791`
- `"health:logged_out"`: `:2302`
- `health:{status}`: `health.py:184,189`
- `"hard_off"`, `"send_cap"`, `"escalation_unrouted"`

**`tenant_for_session` trigger** (`0002:177-214`; `0003:81,155,170`; `0005:71`) (b): looks the tenant up through `tenants.session_id`.

**`tenants.channel`** (`0002:33-34`) (d): read at `tenants.py:58`, never written on create (`tenants.py:226`).

**Seeded base prompt** (`0002:141`) (c, your content): "in a Telegram chat".

## 2. `customer_ref`

`crypto.py:158` `customer_ref(tenant_id, channel, external_id)` is already channel-aware. Callers that hard-code `"telegram"` (d):
- `database.py:469`
- `booking_store.py:183,377,498`
- `tenants.py:714-723`, the backfill, run from `migrate_entrypoint.py:16,54`

## 3. Telethon calls

**`session_runtime.py`** (imports at `:64-69`), all (a):

| Primitive | Lines |
|---|---|
| Client factory | `_client_from_auth` `:172-205` |
| Connect | `connect` `:2292,2385`; `is_user_authorized` `:2293,2386` |
| Run and disconnect | `run_until_disconnected` `:2333`; `disconnect` `:2282,2392` |
| Log out | `log_out` `:803,2388` |
| Send text | `send_message` `:1288,1299,1311` |
| Typing | `action(chat,"typing")` `:1296` |
| Send media | `send_file` `:1376,1379,1388` |
| Mark read | `send_read_acknowledge` `:1317` |
| Presence | `UpdateStatusRequest` `:1233` |
| Sender info | `describe_sender` `:154-168` (also `booking_flow.py:353`) |
| Own account | `get_me` `:2309-2314` |
| Contacts | `GetContactsRequest` `:1900` |
| Logins | `GetAuthorizationsRequest` `:774` |
| Peer resolution | `get_input_entity` `:1158` + `InputPeerUser` fallback `:1160-1165` |
| Media download | `download_media` `:1996`, `:2027` |
| Error classes | `PeerFlood` `:1079`; `UserDeactivatedBan` / `AuthKeyUnregistered` / `SessionRevoked` `:1091`; `FloodWait` / `SlowModeWait` `:1100`; `UserIsBlocked` / `UserPrivacyRestricted` / `InputUserDeactivated` / `ChatWriteForbidden` `:1117`; `RPCError` `:1385` |
| Events | `NewMessage` incoming / outgoing `:2239-2240`; `is_private`, `get_sender`, `get_chat`, `raw_text`, `message.id`, `photo`, `reply_to_msg_id` |
| Status keys | `telegram_connected` / `telegram_error` `:2352`; `telegram_state` `:284` (also `booking_flow.py:346`) (b/d) |

**Elsewhere** (a):
- `login_flow.py`: the whole module. Client, `send_code_request`, `sign_in`, the 2FA password, session export, every login error class, the SentCodeType texts.
- `proxies.py:61-66`: `telethon_tuple`.
- `booking_flow.py:343-365`: `get_entity` for the owner; `:386-387,412` keeps `telegram_id` as `provider_message_id` (b).
- `controls.py:246`: `log_out_session`.
- `anomaly.py:32-44`: reads Telethon `Authorization` attributes.

## 4. Service account, bots, contacts, handles

- **777000:** `session_runtime.py:118-120`; branches at `:855,922,2113-2117` (d).
- **`is_bot`:** `session_runtime.py:158,2110`; `database.py:445-467,1163`; `context_link.py:68,92` (d).
- **Contacts-only outreach:**
  - `panel.py:999-1001` and `:1024-1034` ("not in your Telegram contacts").
  - `session_runtime.py:1131-1139` (d).
- **@usernames:**
  - `context_link.py:9,71-74`
  - staging: `session_runtime.py:955-960`, `tenant_config.py:303-316`
  - `bookings.py:108-109`, `booking_flow.py:313`, `session_runtime.py:2313`
  - JS: `style.js:8`, `conversations.js:94-95`, `outreach.js:12,33,99`, `bookings.js:129` (d/c)

## 5. Panel text (c)

- **`static/index.html`:**
  - `:10,17` "Telegram AI Assistant"
  - `:78` "Add a Telegram account"
  - `:84-89` API ID, API hash, "my.telegram.org"
  - `:92-95` the proxy hint
  - `:124` the cloud password line
  - `:250-252` the outreach contacts text
  - `:262` "@username"
- **`static/js/accounts.js`:**
  - `:56-57` the code-delivery texts
  - `:93-98` posts `api_id` / `api_hash`
  - `:190-194` the dot colour from `telegram_connected`
- **`static/js/onboarding.js`:** `:27,130,132,310,334,363,394,397,470,477`.
- **`static/js/safety.js`:** `:112,263,275,284-304,315,328,353-356`.
- **`static/js/socket.js`:** `:99,172,175-182`.
- **`static/owner/owner.js`:** `:42-44`.
- **`panel.py`:**
  - `:146` the title
  - `:544,745-746` the status keys
  - `:561-564,632,635` `AuthStartBody` and the API ID texts
  - `:585-591` `session_id_for_phone`
  - `:1173`

## 6. Text from Python (c)

| File | Lines |
|---|---|
| `health.py` | `:55-58` |
| `controls.py` | `:60,227,265-266` |
| `safety_api.py` | `:213` |
| `session_runtime.py` | `:785,791,1082-1111,1156,1712,2213,2295-2297` |
| `login_flow.py` | the whole module |
| `digest.py` | `:9,101,172,191` (`sent_via` "telegram") |
| `billing.py` | `:7,289-292` (docstring) |
| `booking_flow.py` | `:953` (Google Calendar "via Telegram") |
| `database.py` | `:218` ("New Telegram account added") |
| `ai_responder.py` | `:20,26,448`. These are prompts: your content |

## 7. Config (`tenant_config.py`, `config_store.py`) (d)

- `Safety` `:131-136`: `halt_on_peer_flood`, `known_contacts_only`, `max_flood_wait_seconds`.
- `Outreach` `:139-151`.
- `Booking.provider` `:184-186`: "username, phone or id".
- `Anomaly.new_login_suspend` `:290-291`.
- `Staging.test_chats` `:303-316`.
- `config_store.py:154-166`: the `identity` block (device model, system version, app version, language, time-zone offset); `:100-120`: safety defaults.

## 8. Other modules

- `device_profiles.py`: Telegram app and device profiles (a/d).
- `leasing.py`, `scheduler.py`: leases and running accounts come from `telegram_sessions` (b).
- `health.py:119-135`: `status_of` reads `telegram_sessions.state` (b/d).
- `controls.py:219-270`: hard-off clears the MTProto columns (a/b).
- `context_link.py`: matches username and name across chats (d).
- `database.py:552-581,620-631,688-705,1194`: `telegram_id`, `find_by_telegram_id` (b).
- `media.py`, `vision.py`, `humanlike.py`, `manager.py`: comments only.

## 9. Environment, compose, Docker

- No Telegram environment variables; the API id and hash are stored per account.
- `requirements.txt:4`: `telethon==1.45.0`.
- `.env.example:13-15` and comments in `docker-compose.yml` mention Telegram.
- `README.md`: Telegram text throughout.

## 10. Tests that assume Telegram

- **Telethon fakes:**
  - `test_panel_login.py` (`FakeTelegramClient`)
  - `test_proxies.py`
  - `test_safety_runtime.py` (`FakeEvent` / `FakeClient`, 777000)
  - `test_unanswered.py` (`FakeEvent`), reused by `test_staging.py`
- **Telethon error classes:** `test_chaos.py`, `test_safety.py`.
- **`telegram_state`:** `test_booking_flow.py`.
- **SQL on `telegram_sessions`:**
  - `conftest.py`
  - `test_chaos.py`, `test_digest.py`
  - `test_leasing.py`, `test_manager.py`
  - `test_safety_api.py`, `test_safety_platform.py`
  - `test_scheduler.py`, `test_tenant_isolation.py`, `test_tenants_store.py`
- **`telegram_id` and the "telegram" `customer_ref`:**
  - `test_database.py`, `test_booking_store.py`
  - `test_crypto.py`, `test_tenant_isolation.py`
  - `test_context_link.py`, `test_digest.py`
