# Telegram AI Support Assistant

A Telethon **userbot** that runs on your own Telegram account, drafts replies to
incoming DMs with the DeepSeek API, and gives you a local web admin panel to
supervise everything.

Nothing is sent without your approval unless you explicitly turn on auto-send.

## What it does

- Listens for **private messages only** — group and channel traffic is ignored.
  Messages from **bot accounts are included**, so conversations that run through
  a bot's interface are handled like any other DM.
- Logs every message to SQLite and pushes it to the admin panel live over a
  WebSocket.
- For each incoming DM it checks, in order: the per-chat pause flag, the global
  pause, and the configured active hours. If any of them says stop, no draft is
  made.
- Otherwise it builds the last ~30 messages of that chat as OpenAI-style
  `{"role": "user" | "assistant"}` turns, prepends a system message built from
  your persona config, waits a randomised human-looking delay, and calls
  DeepSeek.
- **Auto-send off (the default):** the draft is saved as `pending_approval` and
  appears in the panel with *Approve & Send*, *Edit then Send* and *Reject*.
  Nothing reaches Telegram until you approve it.
- **Auto-send on:** the reply goes out directly and is logged as sent.

If a newer message arrives in a chat while a draft is still being prepared, the
in-flight draft is cancelled and restarted, so the reply always answers the
latest state of the conversation.

## Requirements

- Python 3.11+
- A Telegram `API_ID` / `API_HASH` from https://my.telegram.org
- The phone number of the Telegram account the assistant runs on
- A DeepSeek API key from https://platform.deepseek.com

## Install

```bash
cd telegram_admin_bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

On Windows you can also double-click **`start.bat`** — it creates the virtual
environment on first run and then starts the app.

The panel opens in your browser at **http://127.0.0.1:8787**. The first time,
it shows a **sign-in screen**:

1. **API ID** and **API hash** — from https://my.telegram.org → *API development tools*
2. **Phone number** — international format, e.g. `+34600123456`
3. **DeepSeek API key** — from https://platform.deepseek.com
4. Telegram sends a **login code** (usually to the Telegram app, not SMS) — type it in
5. If the account has **two-step verification**, its password is asked once

That is it. Credentials are saved to `.env` next to `main.py`, so the next
`python main.py` connects straight away. Nothing secret is ever printed to
the terminal or sent anywhere but Telegram and DeepSeek.

**Log out** in the top bar ends the session on Telegram's side and returns to
the sign-in screen. If the session is revoked from another device
(Settings → Devices), the panel notices and asks you to sign in again.

The server binds to `127.0.0.1` only, so it is not reachable from your network.
There is no password on the panel, which is exactly why it must stay on
localhost. Set `NO_BROWSER=1` to stop it opening a browser tab.

### Filling in `.env` by hand instead

For a headless server, copy `.env.example` to `.env` and fill it in, or run the
terminal version of the sign-in:

```bash
python setup_session.py            # log in from the terminal, writes .env
python setup_session.py --check    # validate .env without revealing values
```

Treat the session string like a password — it grants full access to your
account. If it is ever exposed, revoke it in Telegram under
Settings → Devices, then sign in again.

## Running more than one account

One instance is one Telegram account. For a second account, start a second
instance — it gets its own sign-in, conversations, settings and panel port,
all kept under `instances/NAME/`:

```bash
python main.py --instance work        # or on Windows:  start.bat work
python main.py --list                 # what exists, which port, signed in or not
```

The first run of a new instance opens its own sign-in screen. After that it
logs straight in, like the default one. The panel shows the instance name in
the top bar and the tab title so you can tell them apart.

On Windows, **`start.bat all`** opens one window per instance (the default
plus every folder under `instances\`).

Ports: the default instance stays on 8787; named ones take the next free port
from 8788 and remember it. Starting an instance that is already running fails
with a clear message rather than silently running the same account twice,
which would make it answer every chat twice.

## Fill in your persona first

**`config.json` ships with every persona field blank, and a blank persona
produces generic replies.** Open **Settings** in the panel and fill in:

| Field | What to put there |
|---|---|
| `purpose` | What this account is for and what you want the assistant to do |
| `tone` | How it should sound |
| `languages` | e.g. "always reply in the language the person wrote in" |
| `boundaries` | Hard rules — what it must never say, promise, or do |
| `signature_style` | Whether and how to sign off |

Until at least one field is filled in, the app falls back to a minimal neutral
system prompt so it still works, and the panel shows *"persona not configured"*.

Other settings:

| Setting | Default | Meaning |
|---|---|---|
| `min_delay_seconds` / `max_delay_seconds` | 20 / 90 | Random wait before drafting, so replies don't look instant |
| `active_hours_enabled` | `false` | When on, drafting only happens inside the window below |
| `active_hours_start` / `end` / `timezone` | 09:00 / 21:00 / UTC | Window (may cross midnight), in the given IANA timezone |
| `auto_send` | `false` | Send AI replies without approval |
| `log_all_messages` | `true` | Persist messages to SQLite. Turn off and messages still appear live, but no history is kept — which also means the AI gets less context |
| `model` / `max_tokens` / `temperature` | `deepseek-chat` / 400 / 1.0 | DeepSeek request parameters |

Settings save straight back to `config.json` and take effect immediately — no
restart.

## Using the panel

- **Left sidebar** — conversations with last message, timestamp, unread count, a
  `BOT` badge for bot accounts, and a per-chat Pause/Resume button.
- **Main panel** — the full thread. Incoming, outgoing, pending drafts, rejected
  drafts and errors are colour-coded and labelled.
- **Pause automation** (per chat) — stops AI drafting for that conversation when
  you take it over by hand. Any draft already in flight is cancelled.
- **Pause all** — global kill switch for every chat.
- **Message box** — send as yourself at any time, whatever the automation state.
  Doing so also cancels a pending draft for that chat.

Sending a message from your phone or Telegram Desktop shows up in the panel too,
so the thread stays complete.

## Running it 24/7

The bot only runs while the machine it is on is powered up and awake. Closing a
laptop lid stops it. For round-the-clock operation it has to live on a machine
that stays on — a small VPS (~$4–6/month), a Raspberry Pi, or any always-on box.

**The panel has no login.** Everything below keeps it bound to loopback on the
server; you reach it through an SSH tunnel, so it is never exposed to the
internet:

```bash
ssh -N -L 8787:127.0.0.1:8787 you@your-server
```

Leave that running and open http://127.0.0.1:8787 on your laptop as usual. The
tunnel is only needed when you want to look at the panel — the bot keeps
answering with nobody connected.

### With Docker (simplest)

```bash
git clone https://github.com/axer1978/userbot.git
cd userbot/telegram_admin_bot
docker compose up -d --build
docker compose logs -f    # watch it start
```

Then open the SSH tunnel above, go to http://127.0.0.1:8787 and sign in from
the panel. Credentials land in `./data/.env` on the volume. (A pre-filled
`.env` next to `docker-compose.yml` is still honoured if you prefer.)

`restart: unless-stopped` brings it back after a crash or a server reboot.
`assistant.db` and `config.json` live in `./data`, so rebuilding does not lose
your conversations or settings.

To update: `git pull && docker compose up -d --build`.

### Without Docker (systemd)

```bash
sudo useradd -r -s /usr/sbin/nologin telegram
sudo git clone https://github.com/axer1978/userbot.git /opt/userbot
sudo mv /opt/userbot/telegram_admin_bot /opt/telegram_admin_bot
cd /opt/telegram_admin_bot
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo chown -R telegram:telegram /opt/telegram_admin_bot   # it writes .env itself

sudo cp deploy/telegram-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-assistant
journalctl -u telegram-assistant -f
```

### Getting your credentials onto the server

Either sign in from the panel through the SSH tunnel, or copy the `.env` you
already have from your laptop — that avoids logging in to Telegram a second time:

```bash
scp telegram_admin_bot/.env you@your-server:/opt/telegram_admin_bot/.env
```

Or run `python setup_session.py` over SSH for a terminal-only login.

Either way, run **one** instance. Two copies of the same session string both
answering the same chats will duplicate replies.

### Environment variables for deployment

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | next to `main.py` | Where `assistant.db` and `config.json` live — point it at a volume |
| `ADMIN_HOST` | `127.0.0.1` | Bind address. Only change it inside a container whose port is published to loopback |
| `ADMIN_PORT` | `8787` | Panel port |

Binding `ADMIN_HOST` to anything other than loopback logs a warning at startup,
because it means the panel — which can send messages as you — is reachable with
no password.

## Sounding like a person

Three things under **Settings → Sounding human**, all on by default:

**Adaptive style.** Before drafting, the assistant profiles how *that person*
writes — from their own messages only, never from its own past replies — and
tells the model to match it: message length, emoji use, capitalisation,
whether they bother with full stops, and the language they are writing in. If
someone sends three-word lower-case lines, it stops replying in paragraphs.
Needs at least two of their messages; below that it just uses your persona.

**Typing indicator.** Before an automatic message goes out, the chat shows
"typing…" for as long as writing that text would plausibly take — length ÷
`typing_speed_cps` (default 12 characters/second), capped at
`typing_max_seconds` (default 25). The message is sent while the indicator is
still up, so it doesn't blink off and leave a gap. Messages you send yourself,
and drafts you approve by hand, skip this — you are already in control and
should not wait on it.

**Read receipts.** After the delay and before writing, the incoming message is
marked read, so the sender gets their second tick. That ordering is the point:
a pause, then read, then typing, then the reply — the shape of someone picking
up their phone.

Neither the indicator nor the read receipt can block a message: if Telegram
refuses either, it is logged and the message still goes out.

## Messaging your contacts first

The **Outreach** button opens a panel for starting conversations rather than
only replying to them.

Pick people from your Telegram contacts, say what the message should achieve
("let them know I'm away next week"), and each person gets their own message
written for them — not a copy-pasted blast.

- **Contacts only.** The recipient list comes from your own Telegram contacts,
  and the server re-checks membership when you queue. Anyone else is refused.
- **Approval by default.** Each message appears as a pending draft in that
  conversation, with the usual Approve / Edit / Reject. Tick *"Send without
  asking me"* to skip approval.
- **Paced.** Messages go out one at a time with a random gap (default 90–300
  seconds) and a daily cap (default 20). Telegram penalises bursts of new
  conversations, so raising these makes a limit more likely, not less.
- **One at a time per person.** Someone who already has a queued or unapproved
  message is skipped rather than given a second one.
- **Stoppable.** *Cancel queued* drops everything not yet acted on, and the
  global pause holds outreach too. Hitting the daily cap leaves the rest queued
  for the next day rather than dropping them.

A word on what this is for: messaging people who already know you and expect to
hear from you. Telegram limits or bans accounts that send unsolicited DMs, and
that risk falls on your account.

## Booking appointments

Turn on **Settings → Bookings** and the assistant watches every conversation
for a client settling on a specific day *and* time ("Tuesday at 3 works").
When it sees one:

1. The request goes to the **provider** — the Telegram account (a person or a
   bot) named in the setting — as a message like:

   ```
   📅 Booking request #3
   Client: Anna (@anna)
   When: Tue 23 Sep 2026, 15:00–16:00 (Europe/Madrid)
   What: haircut

   Reply YES 3 to confirm or NO 3 to decline.
   ```

2. The provider answers **YES 3** or **NO 3** (a bare *yes*/*no* works when
   only one request is open, or when sent as a Telegram reply to the request;
   otherwise the bot asks which one). Anything else the provider writes is
   answered like any other chat; only YES/NO while a request is open counts
   as a decision.

3. The client is told in the account's own voice through the normal reply
   flow — auto-sent or held for approval, whichever `auto_send` says. Until
   the provider answers, replies to that client are told the slot is *not yet
   confirmed*, so the assistant cannot promise it prematurely.

4. **Check-in.** *Check in this long before* (default 120 min) — that long
   before a confirmed slot the client is asked, in your voice, whether they
   are still coming. `0` turns it off.

5. **Arrival.** From the check-in until half an hour after the slot, the
   client's messages are watched for "I'm here" / "я на месте" / "at the
   door". The moment they say so, **Arrival instructions** (address, floor,
   door code — whatever you put in that box) are sent **word for word**, not
   through the AI, and only once. The AI is told never to give directions or
   the address itself, so the code cannot be leaked to someone who has not
   turned up.

A client who changes the time before the provider answers withdraws the
earlier request (the provider sees "replaces #3"). Times are read in the
timezone under **Timing**. Each request shows up as a dashed note in the
thread, and `GET /api/bookings` / `POST /api/bookings/{id}/confirm|decline`
let the panel operator answer instead of the provider.

Bookings live in `bookings.json` next to the database until they move into
SQLite. Detection costs one short DeepSeek call per incoming message while
the feature is on.

### Mirroring to Google Calendar (optional)

Fill in **Google Calendar ID** and each request appears as a tentative
`[UNCONFIRMED]` event the moment it is put to the provider; YES makes it a
confirmed event, NO removes it. It authenticates as a service account so no
browser sign-in is needed on a server:

1. Google Cloud console → new project → enable the **Google Calendar API**.
2. IAM → Service accounts → create one → Keys → add a **JSON** key.
3. Save it as `google-service-account.json` next to `main.py`, or point
   `GOOGLE_SERVICE_ACCOUNT_FILE` in `.env` at it.
4. In Google Calendar, share the target calendar with the service account's
   e‑mail address with **Make changes to events**, and paste the calendar's ID
   (Settings → *Integrate calendar*) into the panel.

Calendar failures are reported in the thread and never stop the Telegram side.

## Sending photos and videos

Open **Media** in the top bar and drop in the photos and videos the assistant
is allowed to send (or copy them into the `media/` folder next to `main.py`
— it is picked up either way). Give each one a short description: *"me at
the beach, blue bikini"*, *"the new haircut"*. The description is what the
AI sees, so it is how it picks the right file when someone asks for "the
beach one".

From then on, when a contact asks for a photo, the reply comes with the
matching file attached. In the panel a draft shows a preview of what it will
send; *Approve & Send* sends the text first and the file right after, as a
real person would. With auto-send on, photos go out by themselves. The AI is
told never to send anything unprompted, to say so if it has nothing that
matches, and it can see what it already sent (`[sent photo #3: …]` in the
thread), so it does not send the same file twice unless asked again.

Videos follow two extra rules, both on by default and switchable in the same
sheet:

- **Videos only after asking** — the AI never attaches a video the first time
  it comes up. It asks whether they want it and sends it once they say yes.
- **A reply with a video always waits for my approval** — even with auto-send
  on, a reply carrying a video is held in the panel until you approve it.

Each file sent is a message like any other: it counts against the daily
ceilings and is shown in the thread. The *Send to open chat* button on any
file sends it by hand, as yourself, into whatever conversation is open.

## Errors

DeepSeek failures (network, timeout, HTTP 429, malformed response) are retried
with exponential backoff that honours `Retry-After`, then surfaced as a red
error in the conversation and a toast in the panel. The bot keeps running.
Authentication failures (401/403) fail fast without burning retries.

Telegram disconnects are reconnected automatically with backoff; the admin
server stays up throughout, and the header shows the live connection state.

## Files

```
main.py           entrypoint — Telethon client + FastAPI/uvicorn on one asyncio loop
database.py       SQLite helpers (conversations, messages, outreach, chat links, summaries)
ai_responder.py   builds the system prompt + message list, calls DeepSeek, returns the draft
context_link.py   summarises a linked chat so another conversation can borrow its context
bookings.py       appointment requests, the provider's YES/NO, and the JSON store behind them
media.py          the photo/video library the AI may attach, and the [send N] tag it uses
google_calendar.py optional mirror of bookings into a Google Calendar (service account)
config_store.py   loads/validates/atomically saves config.json
tests/            pytest suite — run with:  python -m pytest
env_file.py       tolerant .env reader/writer; rejoins values that got wrapped when pasted
login_flow.py     the Telegram sign-in state machine behind the panel's login screen
setup_session.py  terminal alternative to the login screen (--check validates .env)
instances.py      --instance NAME: separate data folder + port per Telegram account
start.bat         Windows launcher: start.bat [NAME | all | list]; creates .venv on first run
config.json       your settings (persona fields blank until you fill them in)
static/index.html the admin panel — plain HTML/CSS/JS, no build step
assistant.db      created on first run
media/            photos and videos loaded through the Media sheet (plus their index)
```

## A note on userbots

Automating a personal account is against Telegram's Terms of Service and can get
the account limited or banned. Keep the delays human, and prefer approval mode
over auto-send.
