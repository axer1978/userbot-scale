# WhatsApp AI Support Assistant — MVP Project Description

A **WhatsApp userbot** that runs on your own WhatsApp account (the one on your
phone), drafts replies to incoming private chats with the DeepSeek API, and
gives you a local web admin panel to supervise everything.

**Nothing is sent without your approval unless you explicitly turn on
auto-send.**

---

## 1. What it is, in one paragraph

You link the app to your WhatsApp account the same way you link WhatsApp Web:
scan a QR code. From then on the app is one of your "linked devices". It sees
every private chat, logs the messages, and for each incoming message writes a
reply in your voice (based on a persona you configure). By default the reply
lands in the admin panel as a **pending draft** with *Approve & Send*, *Edit
then Send* and *Reject*. Turn on auto-send and replies go out on their own,
after a human-looking delay, with a "typing…" indicator and blue ticks, so the
other person experiences a normal conversation with you.

---

## 2. Scope of the MVP

### In the MVP

| Area | Included |
|---|---|
| Login | QR code scan from the panel (WhatsApp → Linked devices). Phone-number pairing code as an alternative. Log out from the panel. Session revocation detected. |
| Listening | Private (1-to-1) chats only. Groups, communities, status/stories, channels (newsletters) and broadcast lists are ignored. |
| Logging | Every message (in and out, including ones you send from your phone) stored in SQLite and pushed live to the panel. |
| Drafting | Persona-driven system prompt + last ~30 messages of the chat → DeepSeek → draft. |
| Approval flow | Pending draft → Approve / Edit-then-Send / Reject. Auto-send switch. |
| Controls | Per-chat pause, global pause, active hours with timezone. |
| Sounding human | Random delay, typing indicator, read receipts (blue ticks), online/offline presence, adaptive style, replies split into several short messages ("bursts"). |
| Manual send | Type and send as yourself from the panel at any time. |
| Settings | Everything editable live in the panel; saved to `config.json`, no restart. |
| Per-contact overrides | Extra rules, style notes, chat samples, message length, custom delays. |
| Safety | Daily send cap, daily distinct-people cap, known-contacts-only, hard halt on ban/logout signals. |
| Outreach | Start conversations with people in your WhatsApp contacts: AI writes a personal message per person, paced and capped per day, approval by default. |
| Context link | Two chats that are the same person (old number / new number) share a summarised brief so the conversation doesn't restart from zero. |
| Deployment | `python main.py` / `start.bat`; Docker Compose; systemd unit; multiple accounts via `--instance NAME`. |

### Deliberately not in the MVP

- Media understanding (images, voice notes, documents): they are logged as
  `[non-text message]` and never replied to automatically.
- Group chats of any kind.
- WhatsApp Business API / Cloud API — this is a **userbot on a personal
  account**, not an official business integration.
- Multi-user panel with authentication (the panel is localhost-only, see §9).
- Message editing/deleting, reactions, polls, stickers.

---

## 3. How WhatsApp is different from Telegram (and what that changes)

This project is a port of an existing Telegram userbot. The product is the
same; the transport is not. Every difference below is reflected in the design.

| Telegram | WhatsApp | Consequence |
|---|---|---|
| Official API for user accounts (`API_ID`/`API_HASH`, phone code login). | **No official userbot API.** The app speaks the WhatsApp Web multi-device protocol as a linked device. | Login = QR scan (or pairing code). No developer credentials to register. |
| Numeric `chat_id`, `access_hash`. | **JIDs**: `34600123456@s.whatsapp.net` (phone-based) or `…@lid` (privacy-preserving ID). Groups end in `@g.us`, channels in `@newsletter`, status is `status@broadcast`. | Chat identity is a string. Filter DMs by JID suffix. Store both JID and phone where available. |
| Bot accounts exist and are flagged. | No bots; **Business accounts** exist instead. | The `BOT` badge becomes a `BUSINESS` badge. |
| Read receipts are per-chat, always available. | **Blue ticks** only appear if *you* have read receipts enabled in WhatsApp Privacy settings. | The feature exists; whether they see it is the account's privacy setting. |
| `FloodWaitError("wait N seconds")` — Telegram tells you when to slow down. | **No rate-limit feedback.** WhatsApp silently scores the account and then temporarily or permanently bans it. Reports by recipients are the main trigger. | Safety limits are the only protection; they are lower and on by default. There is nothing to "sleep through". |
| Session revoked → `AuthKeyUnregisteredError`. | Connection closed with a **`loggedOut` / 401** reason when the phone unlinks the device. | Panel returns to the QR screen with an explanation. |
| Session = ~350-char string. | Session = a small **auth-state store** (keys + identity), kept as a file/SQLite in the data dir. | Nothing to paste; copy the data dir to move the login. |
| The phone can be off. | The phone must have been online at least once every ~14 days for the linked device to stay linked. | Documented as an operating constraint. |

---

## 4. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  one process, one asyncio loop                                 │
│                                                                │
│  WhatsApp client ──events──▶  handlers ──▶  SQLite (aiosqlite) │
│  (linked device)              │              ▲                 │
│        ▲                      ▼              │                 │
│        │              drafting pipeline ──▶ DeepSeek (httpx)   │
│        │                      │                                │
│  send / typing / read         ▼                                │
│        └──────────── FastAPI + WebSocket ◀──▶ admin panel      │
│                       127.0.0.1:8787          (static HTML/JS) │
└────────────────────────────────────────────────────────────────┘
```

### Stack

- **Python 3.11+**, asyncio.
- **WhatsApp transport:** `neonize` (Python bindings to `whatsmeow`, the Go
  multi-device implementation). Provides QR/pairing-code login, message
  events, sending, chat presence (typing), read receipts, online presence and
  contact listing. *Alternative:* Node.js + Baileys — same capabilities, but
  it would mean rewriting the AI, storage and config layers that are reused
  as-is from the Telegram version.
- **FastAPI + uvicorn** for the admin API and WebSocket.
- **aiosqlite** for storage, **httpx** for DeepSeek, **pytest** for tests.
- Admin panel: one static `index.html`, plain HTML/CSS/JS, no build step.

### Files

```
main.py            entrypoint — WhatsApp client + FastAPI on one loop; handlers, drafting, safety, API
wa_client.py       thin wrapper around the WhatsApp library: connect, QR/pairing events, send, typing, read, presence, contacts
login_flow.py      QR / pairing-code state machine behind the panel's login screen
database.py        SQLite: conversations, messages, outreach, chat links, summaries
ai_responder.py    system prompt from persona, adaptive style, bursts, DeepSeek call with retry
context_link.py    detect + summarise linked chats
config_store.py    load / validate / atomically save config.json
env_file.py        tolerant .env reader/writer
instances.py       --instance NAME: separate data folder + port per account
setup_session.py   terminal login (prints QR in the terminal) for headless servers
static/index.html  the admin panel
config.json        settings (persona blank until filled in)
assistant.db       messages + conversations
session/           WhatsApp auth state (the "login") — treat like a password
```

---

## 5. Login & session

### From the panel (default)

1. `python main.py` (or double-click `start.bat`). The panel opens at
   `http://127.0.0.1:8787` and shows the **sign-in screen**.
2. Enter your **DeepSeek API key** (from platform.deepseek.com).
3. A **QR code** appears. On the phone: WhatsApp → *Linked devices* → *Link a
   device* → scan. The QR refreshes automatically every ~20 s until scanned.
4. Alternative tab: **Pairing code** — enter your phone number in
   international format, the panel shows an 8-character code, type it into
   WhatsApp → *Linked devices* → *Link with phone number instead*.
5. On success the session is saved to `session/` and the DeepSeek key to
   `.env`. The next `python main.py` connects straight away.

Nothing secret is printed to the terminal or sent anywhere except WhatsApp
and DeepSeek.

### Log out / revocation

- **Log out** in the top bar unlinks the device on WhatsApp's side, deletes
  the local session and returns to the sign-in screen.
- If you unlink the device from the phone (*Linked devices → Log out*), the
  client receives a `loggedOut` close reason; the panel notices, wipes the
  dead session and asks you to scan again, with a message saying why.

### Headless / server

`python setup_session.py` prints the QR in the terminal (ASCII), or
`--pairing +34600123456` prints a pairing code. `--check` validates the saved
session and `.env` without revealing values.

---

## 6. Message flow

### Incoming private message

1. The client emits a message event. The handler **drops** it unless the chat
   JID ends in `@s.whatsapp.net` or `@lid` (private chat). Groups, status,
   channels and broadcasts never reach the rest of the pipeline.
2. The conversation row is **upserted**: JID, phone (if known), push name /
   contact name, `is_business` flag.
3. The message is **recorded** in SQLite (`direction=in`, `status=received`),
   the conversation's preview/unread counter updated, and the row is pushed
   to every open panel over the WebSocket. Non-text messages are stored as
   `[non-text message]`.
4. Gate checks, each logged when it stops the flow:
   - no text → skip
   - global pause on → skip
   - this chat paused → skip
   - outside active hours → skip
5. Otherwise a **draft task** is scheduled for that chat. If one is already
   running for the same chat, it is cancelled and restarted so the reply
   always answers the latest state of the conversation — unless it has
   already committed to sending (see §7).

### Outgoing message you send from your phone

The linked device also receives your own sent messages (`fromMe=true`). They
are mirrored into SQLite as `direction=out, status=sent` so the thread in the
panel stays complete. Messages the app itself sent are de-duplicated by
WhatsApp message ID and by an in-flight text guard.

---

## 7. Drafting pipeline (per chat)

```
delay (random, min–max s)
  → re-check pauses / active hours
  → history = last 30 real messages (in+out, sent/received only; drafts, rejects, errors excluded)
  → quota check (auto-send only)
  → go online (presence "available") after a short random pause
  → mark their message read  (blue ticks)
  → borrowed context from linked chats (if any)
  → DeepSeek  (under a concurrency gate, default 4 in flight across all chats)
  → split into burst parts (max 4)
  → auto-send ON : typing indicator → send each part → record as sent
    auto-send OFF: store as pending_approval → panel shows Approve/Edit/Reject
  → schedule going offline (presence "unavailable") once no chat is mid-exchange
```

### Prompt construction

The system message is assembled, in order, from:

1. **Persona header** — "you are writing as this real person from their
   personal WhatsApp account; never say you are an AI; output only the
   message text."
2. **Persona sections** from Settings: purpose, tone, language (with a
   directive that pins the language even if the other person switches),
   hard rules, sign-off style. If all are blank, a minimal neutral fallback is
   used and the panel shows *"persona not configured"*.
3. **Writing samples** (Settings → Fine-tune) — real examples of how you
   write.
4. **Per-contact overrides** — extra rules, notes about the person, past
   messages to them, message-length rule.
5. **Adaptive style brief** — computed from *their* messages only (never our
   own past replies): average length, emoji use, capitalisation, punctuation,
   language. Needs ≥2 of their messages. If a language is pinned in Settings,
   the brief asks to mirror everything *except* language.
6. **Borrowed context** from linked chats, framed as "things you already
   know", never as a document.
7. **Burst instruction** — put each message on its own line; a `|||`
   separator means the same; max 4 parts.

Then the chat history as alternating `user` / `assistant` turns, with
consecutive messages from the same side merged into one turn.

### Bursts

People text in several short bubbles, not paragraphs. The model's output is
split at line breaks / `|||` into up to 4 parts (overflow folded into the last
one). Each part is a real WhatsApp message: it gets its own typing indicator,
a 0.6–2.2 s gap before the next, its own DB row, and counts against the daily
ceilings. Approving a draft sends it as a burst too.

### Cancellation rules

- A newer incoming message cancels the in-flight draft **unless** the draft
  has already started sending (`sending_chats`); then it finishes, and the new
  message gets its own draft.
- Pausing the chat, pausing globally, or sending a manual message from the
  panel cancels the in-flight draft for that chat.

### Errors

DeepSeek failures (timeout, network, 429, 5xx, malformed JSON) are retried up
to 3 times with exponential backoff honouring `Retry-After`, then surfaced as
a red error row in the conversation and a toast in the panel. 401/403 fail
fast ("check your DeepSeek key"). The bot keeps running.

---

## 8. Sounding human

All under **Settings → Sounding human**, on by default.

| Feature | How it works on WhatsApp |
|---|---|
| **Random delay** | 20–90 s (configurable) before the draft is even started, so replies are never instant. |
| **Presence** | Account goes *online* 2–8 s before opening the chat, stays online while any chat is mid-exchange, goes *offline* 15–90 s after the last one finishes. Kept independent of typing so "online" never coincides exactly with a message landing. |
| **Read receipts** | Their message is marked read *after* the delay and *before* typing — the order a person produces: pause, open chat, read, type, send. Visible to them as blue ticks only if your WhatsApp privacy setting allows it. |
| **Typing indicator** | `composing` presence for `len(text) / typing_speed_cps` seconds (default 12 cps, capped at 25 s, ±15 % jitter). The message is sent while the indicator is still up. Manual and approved-by-hand sends skip it. |
| **Adaptive style** | See §7. |
| **Bursts** | See §7. |

None of these can block a message: if WhatsApp refuses the presence/read
call, it is logged and the message still goes out.

---

## 9. Admin panel

Single-page app served from the same process at `http://127.0.0.1:8787`.
Live updates over a WebSocket (`/ws`); the first frame carries conversations,
config, status and auth state.

- **Top bar** — connection state (connected / reconnecting / signed out),
  account name and number, instance name, *Pause all*, *Outreach*,
  *Settings*, *Log out*.
- **Sign-in screen** — DeepSeek key field, QR code image (auto-refreshing),
  pairing-code tab, progress messages.
- **Left sidebar** — conversations sorted by last activity: name, last
  message preview, timestamp, unread count, `BUSINESS` badge, per-chat
  *Pause / Resume*.
- **Thread view** — colour-coded rows: incoming, outgoing, **pending draft**
  (with *Approve & Send*, *Edit then Send*, *Reject*), rejected draft, error.
  A "drafting in N s…" indicator while a draft task is waiting.
- **Message box** — send as yourself at any time, regardless of automation
  state. Also cancels a pending draft for that chat.
- **Contact drawer** — per-contact overrides (extra rules, notes, samples,
  length, delays) and **Linked chats** (current links, suggested matches,
  link / unlink).
- **Settings** — every `config.json` field, saved live.
- **Outreach panel** — pick contacts, state the goal, queue; queue status list
  with *Cancel queued*.

**Security model:** the server binds to `127.0.0.1` only and has **no
login** — which is exactly why it must stay on localhost. Reach it on a
server through an SSH tunnel (`ssh -N -L 8787:127.0.0.1:8787 user@server`).
Binding `ADMIN_HOST` to anything else logs a loud warning.

### API surface

| Method & path | Purpose |
|---|---|
| `GET /api/status` | connection, account, pause flags, persona configured? |
| `GET/PUT /api/config` | read / save settings |
| `GET /api/conversations` | sidebar list |
| `GET /api/conversations/{jid}/messages` | thread + links |
| `POST /api/conversations/{jid}/read` | clear unread |
| `POST /api/conversations/{jid}/pause` | `{paused: bool}` |
| `POST /api/conversations/{jid}/send` | `{text}` — send as me |
| `GET/POST/DELETE /api/conversations/{jid}/links[/{source}]` | context links |
| `POST /api/global-pause` | `{global_pause: bool}` |
| `POST /api/drafts/{id}/approve` | `{text?}` — optional edited text |
| `POST /api/drafts/{id}/reject` | |
| `GET /api/contacts` | WhatsApp contacts (outreach targets) |
| `GET/POST /api/outreach`, `POST /api/outreach/cancel` | queue |
| `GET /api/auth`, `POST /api/auth/start|pairing|cancel|logout` | login flow |
| `WS /ws` | live events: `hello`, `message`, `conversation`, `drafting`, `config`, `status`, `auth`, `qr`, `error`, `halted`, `outreach`, `chat_link` |

---

## 10. Configuration (`config.json`)

Saved atomically (temp file + rename), validated and clamped on load and
save, defaults filled in for anything missing. Editable live from Settings.

```jsonc
{
  "persona":  { "purpose": "", "tone": "", "languages": "", "boundaries": "", "signature_style": "" },
  "timing":   { "min_delay_seconds": 20, "max_delay_seconds": 90,
                "active_hours_enabled": false, "active_hours_start": "09:00",
                "active_hours_end": "21:00", "timezone": "UTC" },
  "behavior": { "auto_send": false, "log_all_messages": true, "global_pause": false },
  "ai":       { "model": "deepseek-chat", "max_tokens": 400, "temperature": 1.0,
                "max_concurrent_requests": 4 },
  "human":    { "adaptive_style": true, "typing_indicator": true,
                "typing_speed_cps": 12, "typing_max_seconds": 25, "mark_read": true },
  "presence": { "enabled": true, "go_online_delay_min": 2, "go_online_delay_max": 8,
                "offline_delay_min": 15, "offline_delay_max": 90 },
  "finetune": { "writing_samples": "" },
  "context_link": { "enabled": true, "auto_detect": true, "history_limit": 60,
                    "max_sources": 2, "refresh_after_messages": 5 },
  "outreach": { "min_gap_seconds": 120, "max_gap_seconds": 420, "daily_limit": 15,
                "auto_send": false },
  "safety":   { "daily_send_limit": 120, "daily_peer_limit": 25,
                "known_contacts_only": true, "halt_on_logout": true },
  "contacts": { "<jid>": { "persona_extra": "", "style_notes": "", "chat_samples": "",
                           "message_length": "auto", "min_delay_seconds": null, ... } }
}
```

Persona fields **ship blank on purpose**; a blank persona produces generic
replies and the panel says so until at least one field is filled in.

`.env` holds only `DEEPSEEK_API_KEY` (plus optional `DATA_DIR`, `ADMIN_HOST`,
`ADMIN_PORT`, `NO_BROWSER`). The WhatsApp login lives in `session/`, not in
`.env`.

---

## 11. Account safety

WhatsApp gives **no rate-limit feedback**: it scores behaviour (outbound
volume, number of *different* people contacted, how often recipients tap
*Report* or *Block*) and then applies a temporary or permanent ban. So the
approach is to send less, not to look different while sending the same
amount. Limits are therefore lower than the Telegram version's.

| Guard | Default | What it does |
|---|---|---|
| `daily_send_limit` | 120 | Total messages per day, replies included. Reaching it blocks every send path — auto-send, approvals, manual — with a clear message in the panel. |
| `daily_peer_limit` | 25 | Distinct people written to per day. Breadth is a far stronger spam signal than depth. |
| `known_contacts_only` | on | Never start a conversation with someone who is not in your contacts and never wrote first. |
| `halt_on_logout` | on | A `loggedOut`/401 close, or a ban notice, flips **global pause**, cancels drafts and outreach, and tells every panel why. Recovery is manual. |
| Blocked recipient | — | A send that fails because the person blocked you pauses that conversation and says so. |

Quotas are checked *before* spending a DeepSeek call, so no drafts are made
for messages that could not be sent.

---

## 12. Outreach — messaging your contacts first

The **Outreach** panel starts conversations rather than replying to them.

- Recipients come **only from your WhatsApp contacts** (phone address book
  synced to WhatsApp); the server re-checks membership when you queue.
- You state a **goal** ("let them know I'm away next week"). Each person gets
  their **own** message written for them — always a **single** message (a
  first contact arriving as three bubbles reads as a bot).
- **Approval by default:** each opener appears as a pending draft in that
  conversation. *"Send without asking me"* skips approval.
- **Paced:** one at a time, random gap 120–420 s, daily cap 15, on top of the
  global safety caps. Hitting the cap leaves the rest queued for tomorrow.
- **One per person:** someone with a queued or unapproved opener is skipped.
- **Stoppable:** *Cancel queued* drops everything not yet acted on; the global
  pause holds outreach too.

This is for people who already know you and expect to hear from you.
Unsolicited messages to strangers are what gets a WhatsApp number banned, and
that risk falls on your number.

---

## 13. Context link — the same person on two chats

When someone writes from a new number, or you run two instances (two of your
own numbers) and the same person talks to both, the second chat can borrow
what the first one established.

- **Detection** (`auto_detect`): after a message, conversations with the same
  full push/contact name are linked automatically; same first name only is
  offered as a *suggestion* in the contact drawer for you to confirm.
- **Manual** link / unlink in the panel. An unlink is remembered as *blocked*
  so detection never re-links that pair.
- **Summaries:** the source chat's last 60 messages are condensed by DeepSeek
  (low temperature, ≤120 words) into a brief: who they are, facts they stated,
  what was asked/promised, open questions, where it was left. Cached and
  rebuilt only after 5 new messages. A chat with nothing worth carrying gives
  `NONE` → no context.
- Up to 2 sources feed one reply, injected as "things you already know" —
  the model is told never to mention the other chat.
- A failure here never costs the reply; the draft just goes without context.

---

## 14. Data model (SQLite)

```
conversations  jid PK, phone, display_name, is_business, automation_paused,
               unread, last_message_at, last_message_preview, created_at
messages       id PK, jid, wa_id (WhatsApp message key, unique per chat),
               direction in|out|system, status received|sent|pending_approval|rejected|error,
               text, created_at
outreach       id, jid, display_name, goal, status queued|drafted|sent|failed|cancelled,
               message, error, draft_id, created_at, sent_at
chat_links     jid, source_jid, origin auto|manual|blocked, reason, confidence, created_at
chat_summaries jid PK, summary, last_message_id, updated_at
```

`log_all_messages=false` still shows messages live in the panel but keeps no
history — which also means the AI gets no context.

---

## 15. Running more than one account

One instance = one WhatsApp number. `python main.py --instance work` (or
`start.bat work`) keeps its own `session/`, `assistant.db`, `config.json` and
panel port under `instances/work/`. The default stays on 8787; named ones take
the next free port from 8788 and remember it. Starting an instance that is
already running fails loudly rather than answering every chat twice.
`start.bat all` opens one window per instance; `--list` shows what exists and
whether it is linked.

---

## 16. Deployment

The bot only works while the machine is on and awake. For 24/7 use it lives
on a small VPS, a Raspberry Pi or any always-on box. The panel stays on
loopback; you reach it through an SSH tunnel.

- **Docker:** `docker compose up -d --build`; `session/`, `assistant.db`,
  `config.json` and `.env` live under `./data` on a volume so a rebuild does
  not unlink the account. `restart: unless-stopped`.
- **systemd:** `deploy/whatsapp-assistant.service`, dedicated unprivileged
  user, `DATA_DIR` pointed at a writable directory.
- **Moving the login:** copy the whole data dir (`session/` included) instead
  of scanning the QR again. Run **one** copy of a session — two linked-device
  clients sharing one session will conflict and get the device unlinked.
- **Phone requirement:** WhatsApp unlinks companion devices if the phone has
  not been online for about 14 days.

Environment: `DATA_DIR`, `ADMIN_HOST` (default `127.0.0.1`), `ADMIN_PORT`
(default `8787`), `NO_BROWSER`.

---

## 17. Reconnection & resilience

- WhatsApp disconnects (network, phone offline, server restart) are
  reconnected with exponential backoff (5 s → 120 s) while the admin server
  stays up; the header shows the live state.
- Draft tasks are per chat and isolated: one failing chat never takes the
  process down.
- All writes to `config.json` are atomic; SQLite runs in WAL mode.
- Startup with a missing or corrupt session goes straight to the sign-in
  screen with a reason, rather than crashing.

---

## 18. Testing

`python -m pytest`. The suite covers: prompt building and burst splitting,
adaptive-style profiling, DeepSeek retry/backoff/redaction, config
normalisation, database invariants (dedupe by message key, history filter),
draft cancellation and concurrency gate, safety quotas and halt behaviour,
context-link detection and summary caching. The WhatsApp client is mocked;
nothing in the tests touches WhatsApp or DeepSeek.

---

## 19. A note on userbots

Running an unofficial client on a personal WhatsApp account is against
WhatsApp's Terms of Service and can get the number **temporarily or
permanently banned**, with no appeal path worth counting on. Keep the delays
human, keep the daily caps low, prefer approval mode over auto-send, and never
message people who did not expect to hear from you.
