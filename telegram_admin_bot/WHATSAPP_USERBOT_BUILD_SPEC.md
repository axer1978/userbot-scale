# BUILD SPEC — WhatsApp AI Support Assistant (userbot + local admin panel)

> **How to use this file with Claude Code**
> 1. Create an empty folder next to the Telegram project, e.g. `Downloads/telegram_admin_bot/whatsapp_userbot/`.
> 2. Copy this file into it. Open Claude Code in that folder.
> 3. Prompt: *"Build the complete project described in `WHATSAPP_USERBOT_BUILD_SPEC.md`. The reference Telegram implementation is in `../telegram_admin_bot`. Follow the spec's build order, run the tests after each phase, and don't stop until the acceptance checklist passes."*
>
> Everything below is written for the agent that will build it. Library APIs in §4 were verified against the installed package (`neonize 0.4.3.post0`, Python 3.13, Windows) — use them exactly as written, do not guess alternatives.

---

## 0. Goal

Port the existing **Telegram AI Support Assistant** (`../telegram_admin_bot`) to **WhatsApp**, keeping the product identical:

- A userbot on the user's **own personal WhatsApp account** (linked-device protocol, like WhatsApp Web).
- Every incoming **private** message is logged and answered with a DeepSeek-drafted reply in the user's voice.
- **Default: approval mode** — drafts wait in a local web panel for *Approve / Edit / Reject*. Auto-send is opt-in.
- Human-looking behaviour: random delay, online presence, read receipts, typing indicator, replies split into short bursts, style adapted to the other person.
- Local admin panel (FastAPI + WebSocket + one static HTML file), localhost only, no login.
- Safety caps so the number doesn't get banned. Outreach to contacts. Cross-chat context links. Multi-account via `--instance`.

The reference project is ~7.5k lines and works. **Reuse as much of it as possible** — §6 says file by file what to copy verbatim, what to adapt, and what to write new.

---

## 1. Stack (pin these)

```
python >= 3.11
neonize>=0.4.3,<0.5        # WhatsApp multi-device client (Python bindings to whatsmeow, Go). Ships wheels for win/linux/mac.
fastapi>=0.115
uvicorn[standard]>=0.30
httpx>=0.27
aiosqlite>=0.20
python-dotenv>=1.0
segno>=1.6                  # already a neonize dependency; used to render the login QR as SVG for the panel
tzdata>=2024.1
pytest>=8.0 ; pytest-asyncio>=0.24     # tests only
```

No Node, no browser automation, no build step for the panel.

---

## 2. Non-negotiable behaviours (acceptance criteria)

1. `python main.py` starts, opens `http://127.0.0.1:8787`, shows a sign-in screen with a **live QR code** (auto-refreshing as the library rotates it) and a **pairing-code** tab. Scanning the QR logs the account in; the panel switches to the conversations view without a restart.
2. Session persists in `DATA_DIR/session.db`. Next start connects without a QR.
3. A private text message from another number appears in the panel within ~1 s, is stored in SQLite, and (unless paused / outside hours) produces a **pending draft** after the configured random delay.
4. **Approve & Send** sends the draft as one or more WhatsApp messages (burst), the rows flip to `sent`, the other phone receives them. **Edit then Send** sends the edited text. **Reject** marks it rejected.
5. With **auto-send on**: after the delay the account goes online → marks their message read → shows "typing…" for a plausible time → sends → goes offline later. All visible on the other phone.
6. Group, status, channel/newsletter and broadcast messages are **never** logged or answered.
7. Messages sent from the phone itself appear in the panel thread as outgoing. Nothing is ever duplicated (dedupe by WhatsApp message ID).
8. Unlinking the device from the phone (`Linked devices → Log out`) sends the panel back to the QR screen with an explanatory notice, and wipes the dead session.
9. **Log out** in the panel unlinks on WhatsApp's side, deletes `session.db`, returns to sign-in.
10. Daily send cap / distinct-people cap block *every* send path with a clear message. A `TemporaryBanEv` or `LoggedOutEv` flips global pause and cancels everything.
11. Settings edited in the panel save to `config.json` and apply immediately.
12. `python -m pytest` passes; the WhatsApp client is mocked in tests.
13. `python main.py --instance work` runs a second account with its own data folder and port.

---

## 3. Key differences from the Telegram version (design decisions already made)

| Topic | Decision |
|---|---|
| Identity of a chat | A **string JID** `"<user>@<server>"`, e.g. `34600123456@s.whatsapp.net` or `123456789@lid`. Replaces the integer `chat_id` everywhere (DB primary key, API paths, config `contacts` keys, WS payloads). No `access_hash`. |
| Private chat filter | `Info.MessageSource.IsGroup == False` **and** `Chat.Server in ("s.whatsapp.net", "lid")`. This excludes `@g.us`, `status@broadcast`, `@newsletter`, `@broadcast`. |
| "Bot" flag | Replaced by `is_business` (from `Info.VerifiedName` being set, or contact `BusinessName` non-empty). Panel badge says `BUSINESS`. |
| Credentials | No `API_ID`/`API_HASH`/`SESSION`. `.env` holds only `DEEPSEEK_API_KEY`. The login is the neonize session database file. |
| Login flow | QR (`client.qr` callback → SVG → WebSocket) or pairing code (`await client.PairPhone(phone, True)` → 8-char code shown in panel). Success = `PairStatusEv` then `ConnectedEv`. |
| Rate limits | WhatsApp gives none. Remove all FloodWait logic. Keep `SendBlocked` quotas (lower defaults) + halt on `TemporaryBanEv`/`LoggedOutEv`/`ConnectFailureEv`. |
| Reconnect | whatsmeow reconnects by itself. Do **not** write a reconnect loop; track `ConnectedEv`/`DisconnectedEv` and show state. Only re-create the client after logout. |
| Presence | `send_presence(Presence.AVAILABLE/UNAVAILABLE)`; typing = `send_chat_presence(jid, COMPOSING, MEDIA_TEXT)`, stop = `PAUSED`. |
| Read receipts | `mark_read(*ids, chat=jid, sender=jid, receipt=ReceiptType.READ)`. Blue ticks only show if the account's privacy setting allows — document, don't try to work around. |
| One client per process | neonize keeps a module-global event loop reference; **never** create two `NewAClient`s in one process. `--instance` = separate process (already how the reference works). |

---

## 4. Verified neonize API cheat-sheet (use exactly this)

```python
from neonize.aioze.client import NewAClient
from neonize.aioze.events import (
    ConnectedEv, DisconnectedEv, LoggedOutEv, PairStatusEv, MessageEv,
    TemporaryBanEv, ConnectFailureEv, StreamReplacedEv, ClientOutdatedEv,
)
from neonize.utils.enum import ChatPresence, ChatPresenceMedia, Presence, ReceiptType
from neonize.utils.jid import build_jid, Jid2String
from neonize.proto.Neonize_pb2 import JID
import segno

client = NewAClient(str(DATA_DIR / "session.db"))   # `name` IS the sqlite path of the session store

# ---- registration (decorators; must be done BEFORE connect) ----
@client.event(MessageEv)
async def on_message(c: NewAClient, ev: MessageEv): ...
@client.event(ConnectedEv)        async def on_connected(c, ev): ...
@client.event(DisconnectedEv)     async def on_disconnected(c, ev): ...
@client.event(LoggedOutEv)        async def on_logged_out(c, ev): ...   # ev.Reason (enum), ev.OnConnect (bool)
@client.event(PairStatusEv)       async def on_paired(c, ev): ...       # ev.ID: JID, ev.BusinessName, ev.Platform, ev.Status, ev.Error
@client.event(TemporaryBanEv)     async def on_ban(c, ev): ...          # ev.Code (enum), ev.Expire (seconds)
@client.event(ConnectFailureEv)   async def on_connect_failure(c, ev): ... # ev.Reason, ev.Message

# QR: set a coroutine callback; it receives the raw QR payload (bytes) every time it rotates (~20s)
@client.qr
async def on_qr(c: NewAClient, data: bytes):
    svg_uri = segno.make_qr(data).svg_data_uri(scale=6)   # -> "data:image/svg+xml;..." for <img src>
    ...
# If you don't set client.qr, the default prints the QR to the terminal (wanted in setup_session.py).

# ---- lifecycle ----
task = await client.connect()          # returns an asyncio.Task; the Go side runs in a thread until disconnect/logout
client.is_connected()                  # sync -> bool
client.is_logged_in()                  # sync -> bool
await client.disconnect()
await client.logout()                  # unlinks on WhatsApp's side; afterwards delete session.db and build a NEW client
me = await client.get_me()             # Device: me.JID (JID), me.LID, me.PushName, me.BussinessName (sic), me.Platform
code: str = await client.PairPhone("34600123456", True)   # only while connected & not logged in; returns pairing code

# ---- messages ----
resp = await client.send_message(jid, "text")            # SendResponse: resp.ID (str), resp.Timestamp (int)
await client.send_chat_presence(jid, ChatPresence.CHAT_PRESENCE_COMPOSING, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
await client.send_chat_presence(jid, ChatPresence.CHAT_PRESENCE_PAUSED,    ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
await client.send_presence(Presence.AVAILABLE)           # or Presence.UNAVAILABLE
await client.mark_read(msg_id, chat=jid, sender=jid, receipt=ReceiptType.READ)   # *message_ids varargs first

# ---- contacts ----
contacts = await client.contact.get_all_contacts()       # iterable of Contact: .JID (JID), .Info.FullName/.FirstName/.PushName/.BusinessName/.Found
info     = await client.contact.get_contact(jid)         # ContactInfo

# ---- JID helpers ----
jid = build_jid("34600123456")                 # -> JID(User="34600123456", Server="s.whatsapp.net")
jid = build_jid("123456789", "lid")
key = f"{jid.User}@{jid.Server}"               # the string form used as chat key everywhere in this project
def parse_key(key: str) -> JID:  user, server = key.split("@", 1); return build_jid(user, server)

# ---- MessageEv shape ----
src  = ev.Info.MessageSource      # .Chat (JID), .Sender (JID), .IsFromMe (bool), .IsGroup (bool), .SenderAlt/.RecipientAlt (JID)
mid  = ev.Info.ID                 # str — WhatsApp message id, unique per chat; use for dedupe
name = ev.Info.Pushname           # str — sender's display name as they set it
ts   = ev.Info.Timestamp          # int unix seconds
m    = ev.Message                 # waE2E Message
text = (m.conversation
        or m.extendedTextMessage.text
        or m.ephemeralMessage.message.conversation
        or m.ephemeralMessage.message.extendedTextMessage.text
        or "")
# skip entirely: m.HasField("protocolMessage") (revokes, key distribution, history sync), m.HasField("reactionMessage")
is_business = ev.Info.HasField("VerifiedName")
```

Pitfalls the agent must respect:
- Register `@client.event(...)` and `client.qr` **before** `await client.connect()`; the handler table is serialised at connect time.
- `connect()` must be awaited inside the running asyncio loop that the rest of the app (FastAPI) uses — same loop.
- Handlers are scheduled onto the loop with `run_coroutine_threadsafe`; exceptions inside them vanish silently → wrap every handler body in `try/except Exception: log.exception(...)`.
- After `logout()` the session store is invalid: `disconnect()`, delete `session.db` (+ `-wal`, `-shm`), construct a fresh `NewAClient`, re-register handlers, `connect()` → QR flows again.
- Windows: run Python with `PYTHONIOENCODING=utf-8` or avoid printing emoji; neonize logs contain emoji.

---

## 5. Repository layout (target)

```
whatsapp_userbot/
├── main.py                 entrypoint: WhatsApp client + FastAPI on one loop; handlers, drafting, safety, API, runners
├── wa_client.py            thin async wrapper over NewAClient (connect, qr/pair, send, typing, read, presence, contacts, logout) — the ONLY file that imports neonize
├── login_flow.py           login state machine for the panel: idle → qr | pairing → linked; holds latest QR svg / pairing code / notice
├── ai_responder.py         COPY from reference, wording "Telegram" → "WhatsApp"
├── context_link.py         COPY from reference, int ids → str jids
├── config_store.py         COPY + edits (§6)
├── database.py             COPY + edits (§6)
├── env_file.py             COPY verbatim
├── instances.py            COPY verbatim (folder/port logic is transport-agnostic)
├── setup_session.py        terminal login: prints QR (neonize default) or `--pairing +NUMBER`; `--check`
├── static/index.html       COPY + edits (§6)
├── config.example.json, config.json
├── .env.example            only DEEPSEEK_API_KEY (+ DATA_DIR/ADMIN_HOST/ADMIN_PORT/NO_BROWSER docs)
├── requirements.txt, pytest.ini, start.bat, Dockerfile, docker-compose.yml, .dockerignore, .gitignore
├── deploy/whatsapp-assistant.service
├── tests/  conftest.py test_ai_responder.py test_burst.py test_concurrency.py test_config_store.py
│           test_context_link.py test_database.py test_safety.py test_wa_client.py test_login_flow.py
└── README.md               rewritten for WhatsApp (see the reference README for tone/structure)
```

Runtime files (git-ignored, under `DATA_DIR`, default = project dir): `session.db`, `assistant.db`, `config.json`, `.env`.

---

## 6. File-by-file instructions

### 6.1 Copy verbatim
`env_file.py`, `instances.py`, `pytest.ini`, `.dockerignore`, `.gitignore` (add `session.db*`), `tests/test_config_store.py`, `tests/test_ai_responder.py`, `tests/test_burst.py`.

### 6.2 `ai_responder.py` — copy, then:
- Replace "Telegram" with "WhatsApp" in `FALLBACK_SYSTEM_PROMPT`, `PERSONA_HEADER`, `SUMMARY_SYSTEM_PROMPT`, comments.
- Nothing else changes. `generate_reply`, `generate_opener`, `summarize_conversation`, `split_burst`, `describe_style` are transport-agnostic.

### 6.3 `config_store.py` — copy, then:
- Delete `safety.halt_on_peer_flood`, `safety.max_flood_wait_seconds`. Add `safety.halt_on_ban: True` (bool).
- New defaults: `safety.daily_send_limit=120`, `safety.daily_peer_limit=25`, `outreach.min_gap_seconds=120`, `outreach.max_gap_seconds=420`, `outreach.daily_limit=15`.
- `_normalize_contacts`: keys are JID strings — accept any non-empty string containing `@`, drop everything else (was `str(int(key))`).
- Update `config.example.json` to match.

### 6.4 `database.py` — copy, then:
- All `chat_id INTEGER` → `jid TEXT`, `source_id INTEGER` → `source_jid TEXT`. Rename Python params `chat_id`→`jid`, `source_id`→`source_jid` throughout (keep function names).
- `conversations`: drop `access_hash`, `username`; rename `is_bot`→`is_business`; add `phone TEXT` (the `User` part when server is `s.whatsapp.net`, else NULL).
- `messages.telegram_id INTEGER` → `wa_id TEXT`; unique index `(jid, wa_id) WHERE wa_id IS NOT NULL`. Rename `find_by_telegram_id`→`find_by_wa_id`, `update_message(telegram_id=)`→`wa_id=`.
- Remove `get_access_hash`. Keep everything else (outreach, chat_links, chat_summaries, quotas `sent_since`, `distinct_peers_since`).
- Row dicts: `chat_id`→`jid`, `is_bot`→`is_business`, add `phone`.

### 6.5 `context_link.py` — copy, then:
- ints → str jids. Detection signals become: identical `display_name` (full push/contact name, case-insensitive, ≥2 words) = auto-link confidence; same **phone digits suffix** (last 9 digits equal) between an `@s.whatsapp.net` and an `@lid` chat = auto-link (this is the common "same person, two JIDs" case on WhatsApp); same first name only = suggestion. Remove the `@username` signal.

### 6.6 `wa_client.py` — NEW. Wrap neonize so `main.py` and tests never touch it directly.

```python
class WAClient:
    def __init__(self, session_path: Path, on_event: Callable[[str, dict], Awaitable[None]]): ...
    # on_event(kind, payload) receives: "qr" {svg}, "paired" {jid, name}, "connected", "disconnected",
    #   "logged_out" {reason}, "temporary_ban" {code, expire}, "connect_failure" {reason, message},
    #   "message" {jid, wa_id, text, has_text, from_me, pushname, is_business, timestamp}
    async def start(self) -> None            # builds NewAClient, registers handlers + qr cb, awaits connect()
    async def stop(self) -> None             # disconnect, swallow errors
    async def logout_and_wipe(self) -> None  # logout (ignore errors) → stop → delete session.db* → ready for start()
    def connected(self) -> bool; def logged_in(self) -> bool
    async def me(self) -> dict               # {"jid": "...", "phone": "...", "name": "..."}
    async def request_pairing_code(self, phone: str) -> str
    async def send_text(self, jid: str, text: str) -> str          # returns wa_id
    async def typing(self, jid: str, on: bool) -> None
    async def presence(self, online: bool) -> None
    async def mark_read(self, jid: str, *wa_ids: str) -> None
    async def contacts(self) -> list[dict]   # [{"jid","phone","display_name","is_business"}], self excluded, unnamed excluded
```
- `_extract(ev) -> dict | None`: returns None for groups/status/newsletter/broadcast/protocol/reaction; otherwise the "message" payload. **Unit-test this with hand-built `Neonize_pb2.Message` protos** (`tests/test_wa_client.py`): group → None; status@broadcast → None; conversation text; extendedTextMessage; ephemeral; image → `has_text=False`; `IsFromMe=True` → `from_me=True`.
- Display name: `Pushname` if set, else contact FullName, else the phone with `+`, else the jid user.

### 6.7 `login_flow.py` — NEW (replace the Telethon one).
State: `step ∈ {"idle","qr","pairing","linked"}`, `qr_svg: str|None`, `pairing_code: str|None`, `phone`, `notice`, `error`. Methods: `show_qr(svg)`, `set_pairing(code, phone)`, `linked()`, `reset(notice=None)`, `state() -> dict`. Pure Python, no I/O; test it.

### 6.8 `main.py` — adapt from the reference (keep its structure, section order, logging style and comments). Concretely:

**Remove:** everything Telethon (`resolve_peer`, `describe_sender`, `handle_send_failure`'s Flood/Peer branches, `flood_sleep_threshold`, `run_telegram` reconnect loop, `REQUIRED_ENV` beyond `DEEPSEEK_API_KEY`, `build_env` session validation, `list_contacts` via `GetContactsRequest`).

**Keep unchanged in logic:** `Hub`, `ai_gate`, `SendBlocked`, `halt_everything`, `check_daily_quota`, `may_message`, `within_active_hours`, `push_message`, `push_error`, `contact_overrides`, `borrowed_context`, `detect_links`, `typing_seconds`, presence timers (`set_presence` → `wa.presence`), `deliver` (typing via `wa.typing(jid, True)` … send … `wa.typing(jid, False)` in `finally`), `mark_read`, `send_as_me`, `send_burst`, `schedule_draft/cancel_draft/draft_worker`, outreach worker, `settle_outreach_draft`, all `/api/*` routes (paths take `{jid}` as `str`), `/ws`, `run_web`, `open_browser`.

**Event dispatch** (`async def on_wa_event(kind, payload)`):
- `"qr"` → `login_flow.show_qr(svg)`; `broadcast_auth()`.
- `"paired"` → log; `login_flow.linked()`.
- `"connected"` → `wa_state["connected"]=True`; `me_info = await wa.me()`; `login_flow.linked()`; if presence enabled → `set_presence(False)`; broadcast status+auth.
- `"disconnected"` → `connected=False`; broadcast status. (No reconnect code.)
- `"logged_out"` → `await drop_session(notice="This device was unlinked from WhatsApp (Linked devices → Log out). Scan the QR to link again.")`.
- `"temporary_ban"` → `halt_everything(f"WhatsApp issued a temporary ban (code {code}, expires in {expire}s). Everything is paused. Do not resume until you know why.")` when `safety.halt_on_ban`.
- `"connect_failure"` → `wa_state["error"] = ...`; broadcast status.
- `"message"` → `from_me` ? `on_outgoing(p)` : `on_incoming(p)` — same bodies as the reference, minus Telegram lookups.

**Send failures** (`handle_send_failure`): only two cases remain — client not connected → `push_error("WhatsApp is not connected")`; any other exception → generic error row. A send to someone who blocked you does not raise on WhatsApp (it just never delivers) — don't try to detect it.

**Auth routes:** `GET /api/auth` → `{logged_in, deepseek_saved, **login_flow.state()}`; `POST /api/auth/start {deepseek_api_key}` → validate key (non-empty, not placeholder), save to `.env`, `await wa.start()` if not started, `step="qr"`; `POST /api/auth/pairing {phone}` → `code = await wa.request_pairing_code(phone)`, `login_flow.set_pairing(code, phone)`; `POST /api/auth/cancel` → `login_flow.reset()`; `POST /api/auth/logout` → `wa.logout_and_wipe()`, `drop_session(None)`, `wa.start()` again so a QR is ready.

**Startup (`main()`):** `db.connect()`; read `.env`; `wa = WAClient(DATA_DIR/"session.db", on_wa_event)`; `await wa.start()` **always** (if no session, neonize emits QR events, which the panel shows; if a session exists, it connects). Then `run_web()`. On shutdown: `wa.stop()`, close http client and DB.

**Status payload** (`/api/status` and WS `status`): `{"instance", "whatsapp_connected", "whatsapp_error", "me": {"jid","phone","name"}, "global_pause", "auto_send", "persona_configured"}`.

### 6.9 `static/index.html` — copy, then:
- Sign-in screen: replace API ID / hash / phone / code / password with: DeepSeek key field + **Continue** → then two tabs: **Scan QR** (`<img id="qr">` bound to `auth.qr_svg`, caption "WhatsApp → Linked devices → Link a device"; show "waiting for QR…" until first frame) and **Pairing code** (phone input → shows `auth.pairing_code` in large monospace, caption "Linked devices → Link with phone number instead").
- `chat_id` → `jid` everywhere; `telegram_id` → `wa_id`; `BOT` badge → `BUSINESS` from `is_business`; show `phone` under the name when present.
- Status text: "WhatsApp connected as {name} ({phone})" / "reconnecting…" / "signed out".
- Remove any "resend code" UI. Everything else (sidebar, thread, drafts, message box, settings, outreach, links, toasts) stays.
- Handle new WS event `{"type":"auth"}` carrying the full auth state (QR svg included) — re-render the sign-in screen on every frame.

### 6.10 `setup_session.py` — NEW, small.
`python setup_session.py` → asks for DeepSeek key if missing, builds `WAClient` with default terminal QR (don't set `client.qr`), connects, waits for `connected`, prints "Linked as {name}", exits. `--pairing +34600123456` prints the code instead. `--check` prints whether `session.db` exists and `.env` has a key, without values.

### 6.11 Tests — adapt from reference
`conftest.py`: replace the Telethon fake with a `FakeWA` implementing the `WAClient` surface (records sent texts, typing/presence/read calls; `connected()` toggle). Keep the DB-in-tmpdir fixture. Update `test_database.py` (str jids, `wa_id`), `test_safety.py` (ban → halt; quota blocks; no flood cases), `test_concurrency.py`, `test_context_link.py` (new signals). Add `test_wa_client.py` (`_extract`) and `test_login_flow.py`.

### 6.12 Deployment files
`Dockerfile`: `python:3.12-slim`, install requirements, `ENV DATA_DIR=/data ADMIN_HOST=0.0.0.0 NO_BROWSER=1`, `VOLUME /data`, `CMD ["python","main.py"]`. `docker-compose.yml`: publish `127.0.0.1:8787:8787`, `./data:/data`, `restart: unless-stopped`. `deploy/whatsapp-assistant.service`: as the reference, renamed. `start.bat`: copy, rename.

---

## 7. Behaviour reference (so nothing is lost in the port)

Everything in this section already exists in the reference `main.py`/`ai_responder.py` — this is the checklist of what must still be true afterwards.

**Incoming pipeline:** filter private → upsert conversation → record `in/received` (or `[non-text message]`) → push → gates (no text / global pause / chat paused / active hours; each logged) → `schedule_draft(jid)` (cancels an older draft for the same chat unless it is already in `sending_chats`).

**`draft_worker`:** random delay in `[min,max]` (per-contact override) → broadcast `drafting` → re-check gates → `history = get_history_for_ai(jid, 30)` → if auto-send: `check_daily_quota()` **before** the API call → `go_online_for(jid)` → `mark_read(jid, last incoming wa_id)` → `borrowed_context(jid)` → `async with ai_gate(): generate_reply(...)` → `split_burst` (empty → error) → auto-send: `sending_chats.add`, `send_burst(typing=True)`; else record `out/pending_approval` → `schedule_go_offline(jid)`. Errors: `SendBlocked` → info + error row; `AIResponderError` → error row; anything else → `handle_send_failure` or generic.

**`deliver`:** if typing enabled → `typing(jid, True)`, sleep `typing_seconds(text)`, send, `typing(jid, False)` in `finally`; the indicator must never block the send.

**Bursts:** 0.6–2.2 s gap between parts; first part settles the draft row, others are new rows; each part quota-checked.

**Presence:** `active_chats` set; online only when first chat becomes active (2–8 s pause); offline timer 15–90 s after the last chat finishes; never go offline while `active_chats` is non-empty.

**Manual send (`POST /send`):** cancels in-flight draft; no typing indicator; still quota-guarded.

**Approve:** must be `pending_approval` else 409; optional edited text; sent as burst; settles outreach row if it was an opener.

**Outreach worker:** one queued item at a time; global pause stops it; daily outreach cap; `may_message` (contacts only) + quota before drafting; `generate_opener` (single message); approval by default; random gap between items.

**Context link:** `autolink` after each incoming message; `build_background` under `ai_gate`; summary cached until `refresh_after_messages` new messages; failures swallowed.

**Halt:** sets `global_pause=True` in config, cancels all drafts, cancels queued outreach, broadcasts `config` + `halted` + error.

---

## 8. WebSocket events (unchanged names, new payload keys)

`hello {conversations, config, status, auth}` · `message {message, conversation}` · `conversation {conversation}` · `conversation_paused {jid}` · `drafting {jid, delay_seconds}` · `config {config}` · `status {status}` · `auth {auth}` (includes `qr_svg`, `pairing_code`, `step`, `notice`) · `error {jid|null, text, message|null}` · `halted {reason}` · `outreach {items}` · `outreach_paused {reason}` · `chat_link {link}` · `chat_unlink {jid, source_jid}`.

---

## 9. Build order (do it in this sequence; run tests after each step)

1. Scaffold folder, `requirements.txt`, copy verbatim files (§6.1). `pip install -r requirements.txt`.
2. `database.py` + `test_database.py` (str jids). Green.
3. `config_store.py` + `test_config_store.py`. Green.
4. `ai_responder.py` (wording only) + its tests. Green.
5. `wa_client.py` with `_extract` + `test_wa_client.py`. Green. (No network needed.)
6. `login_flow.py` + test.
7. `main.py` core: hub, state, event dispatch, incoming/outgoing, drafting, deliver, send_as_me/burst, quotas/halt, conversations/drafts/config/auth routes, `/ws`, runners. `conftest.py` FakeWA; `test_safety.py`, `test_concurrency.py`. Green.
8. `static/index.html` edits. Manually: start app, confirm QR renders and rotates (visually), sign in with a real phone, send a test DM, approve a draft.
9. `context_link.py` + outreach paths + their tests.
10. `setup_session.py`, `instances` wiring, `start.bat`, Docker, systemd, `.env.example`, `config.example.json`, `README.md`.
11. Walk the acceptance list in §2 and fix anything that fails.

---

## 10. README must include
Install/run, the QR + pairing login, `.env` (only the DeepSeek key), "fill in your persona first" table, settings table, panel usage, sounding-human explanation (note that blue ticks depend on the account's privacy setting), outreach warning, safety section explaining WhatsApp bans silently, multi-instance, 24/7 deployment (SSH tunnel, Docker, systemd, copy `session.db` to move a login, phone must come online at least every ~14 days), and the ToS note: automating a personal WhatsApp account can get the number permanently banned.
