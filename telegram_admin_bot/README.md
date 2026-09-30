# Telegram and WhatsApp AI Support Assistant

A **userbot** that runs on your own Telegram accounts (through Telethon) and
WhatsApp numbers (as a linked device, through Baileys), drafts replies to
incoming private messages with the DeepSeek API, and gives you one
password-protected web panel to supervise every account. You deploy it on a
server with Docker Compose. You add and run as many accounts as you need from
the panel.

Nothing is sent without your approval unless you turn on auto-send for that
account.

## How it fits together

```
 browser ──SSH tunnel (or optional HTTPS)──▶ panel ──┐
                                                     ├──▶ Postgres  (sessions, messages, config, leases,
                                     manager ────────┤              WhatsApp login state, wa_inbox)
                               (worker processes     └──▶ Valkey    (command bus + live events)
                                holding Telegram           ▲
                                clients; for WhatsApp,     │ commands / events
                                a handle on the socket)    │
                                                     wa-gateway ◀──▶ WhatsApp
                                                     (one socket per WhatsApp number)
```

| Service | What it does |
|---|---|
| `panel` | The admin UI and API (`panel.py`). Control plane only: it **holds no Telegram or WhatsApp connections**. It reads and writes Postgres directly. Anything that needs a live client, like sending a message or approving a draft, goes over Valkey to whichever worker runs that account. |
| `manager` | Starts `WORKER_COUNT` worker processes (`manager.py`). Each one runs up to `SESSIONS_PER_WORKER` accounts, one `SessionRuntime` per account: a live Telethon client for a Telegram account, or, for a WhatsApp account, the same runtime driving that number's socket in `wa-gateway`. It restarts a worker that dies. Every ~15 s each worker picks up newly activated accounts that nothing is running yet. |
| `postgres` | The source of truth: accounts (with credentials and WhatsApp login state encrypted), conversations, messages, per-account settings, outreach queue, and the leases. |
| `valkey` | Valkey (Redis-compatible): the command bus (panel → worker) and live-event fan-out (worker → open panel tabs). It stores nothing that outlives a request. |
| `migrate` | A one-shot job that applies database migrations and exits. `panel` and `manager` wait for it. |
| `scheduler` | Once a minute, tells every running account to do its timed work: booking reminders, requests nobody answered in time, waitlist offers, and replies that quiet hours held back (`scheduler.py`). Only one runs at a time. |
| `wa-gateway` | The WhatsApp transport (`wa_gateway/`, Node + Baileys). Holds the WhatsApp sockets the way a worker holds Telethon clients, takes its orders from Python over Valkey and keeps the WhatsApp login state encrypted in Postgres. Transport only: it never decides to send anything. Exactly one runs (a Postgres lock); a second copy exits. See [WhatsApp accounts](#whatsapp-accounts) and `wa_gateway/README.md`. |
| `caddy` | Optional. Serves the panel over public HTTPS. Off unless you enable it. See below. |
| `booking-pages`, `caddy-booking` | Optional (`--profile booking-pages`). The public calendar feed per client and the read-only page per booking, on their own domain. See "Bookings". |

**Leasing** makes sure only one worker runs a given account at a time. A worker
has to take a lease on the account's row in Postgres before it connects, and
it renews the lease every 10 s. If a worker dies, its leases expire after 30 s
and another worker can take the account over. Two clients on one account
would answer every chat twice and can get the session revoked. The lease is
what prevents that. For WhatsApp the lease also carries an **epoch** that goes
up every time a worker takes it; `wa-gateway` checks it on every command, so a
worker that lost the lease can't drive a socket another worker now owns (two
sockets on one WhatsApp number get it logged out, and look like a bot).

Secrets at rest (Telegram auth key, API hash, DeepSeek key, the WhatsApp
linked-device keys) are encrypted with AES-GCM under `USERBOT_MASTER_KEY`
(`crypto.py`; `wa-gateway` uses the same key and format). **If you lose that
key, every stored login becomes unreadable** and each account has to be signed
in (or paired) again.

## Requirements

- A Linux server with Docker and the Docker Compose plugin (`docker compose`, not `docker-compose`)
- For each Telegram account: an **API ID** and **API hash** from https://my.telegram.org → *API development tools*, and access to that account to receive the login code
- For each WhatsApp number: the phone that has WhatsApp on it, to link the server as a device (start with a secondary number; see [WhatsApp accounts](#whatsapp-accounts))
- A **DeepSeek API key** from https://platform.deepseek.com

## Deploy on a server

```bash
git clone https://github.com/axer1978/userbot-scale.git
cd userbot-scale/telegram_admin_bot
```

Create `.env` with freshly generated secrets. The one-liner is safe to paste,
unlike a heredoc. `umask 077` makes the file readable only by you:

```bash
umask 077
python3 -c "import secrets,base64;print('USERBOT_MASTER_KEY='+base64.b64encode(secrets.token_bytes(32)).decode());print('ADMIN_PASSWORD='+secrets.token_urlsafe(18));print('POSTGRES_PASSWORD='+secrets.token_urlsafe(24))" > .env
```

Before going further:

- **Back up `.env`**, and the master key above all, somewhere off the server. Without it the stored logins cannot be decrypted.
- **Do not run the one-liner again** on a deployed server. It overwrites `.env`, which replaces the master key and the database password.
- **`POSTGRES_PASSWORD` is fixed when the database is first created.** Changing it in `.env` later does not change it inside Postgres, and the app then can't connect. To change it you need the old value (to run `ALTER USER` in psql), or you run `docker compose down -v`, which **deletes all data**.

`.env.example` lists every variable the stack reads, including the optional ones.

Start everything:

```bash
docker compose up -d --build
docker compose ps -a
```

`userbot-postgres`, `userbot-valkey`, `userbot-panel`, `userbot-manager`,
`userbot-scheduler` and `userbot-wa-gateway` should be `Up` (Postgres and
Valkey report `healthy`). `userbot-migrate` should
show `Exited (0)`, because it is a one-shot job. It only appears with `-a`.
Anything else means a failed migration: check `docker compose logs migrate`.

`restart: unless-stopped` brings the stack back after a crash or a reboot.

## Open the panel

The panel is published on the server's loopback only (`127.0.0.1:8787`), so
it is not reachable from the internet. Reach it with an SSH tunnel from your
own machine:

```bash
ssh -i <key>.pem -L 8787:127.0.0.1:8787 <user>@<server-ip>
```

Leave that open and browse to **http://localhost:8787**. You only need the
tunnel while you look at the panel. The accounts keep running without it.

The admin password is the `ADMIN_PASSWORD` from `.env`. To see the value the
panel is actually using:

```bash
docker compose exec panel printenv ADMIN_PASSWORD
```

To change it, edit `ADMIN_PASSWORD` in `.env` and recreate the panel:

```bash
docker compose up -d --force-recreate panel
```

Admin logins are held in the panel's memory, so restarting the panel signs
everyone out. **Log out** in the top bar signs you out of the panel only. It
does not touch any Telegram session.

## Optional: public HTTPS

The SSH tunnel is the default and needs no extra setup. If you want the panel
at a public `https://` address instead, the `caddy` service does it. It is
opt-in through the compose profile `public`. Caddy reverse-proxies to the
panel and gets and renews a Let's Encrypt certificate automatically.

1. Pick a hostname that resolves to the server: a domain with an **A record**
   pointing at the server's IP, or a free sslip.io name built from the IP
   (for server IP `1.2.3.4`, use `1-2-3-4.sslip.io`).
2. Add it to `.env`:
   ```
   PANEL_DOMAIN=1-2-3-4.sslip.io
   ```
3. Open **ports 80 and 443** in the server firewall and in the cloud provider's
   security group. Let's Encrypt needs port 80 to issue the certificate.
4. Start with the profile:
   ```bash
   docker compose --profile public up -d
   ```
   Watch `docker compose logs -f caddy` until the certificate is obtained,
   then open `https://<PANEL_DOMAIN>`.

Once you use the profile, include `--profile public` in every `docker compose
up`, or add `COMPOSE_PROFILES=public` to `.env` so plain commands include it.

With this on, the panel is protected by the admin password (plus an
authenticator code if you turn on two-factor login — see "Hardening a public
panel" below). After 5 wrong passwords from one IP within 15 minutes, that IP is refused
(even with the right password) until the oldest attempt is 15 minutes old;
panel logins also expire after 12 hours. That slows guessing from one
address, not from many, so **use a long admin password**, like the
generated one, not something memorable. The loopback port stays published
either way, so the SSH tunnel keeps working. Everyone coming through the
tunnel shares one rate-limit bucket, so 5 typos there lock the tunnel out
for up to 15 minutes too (restarting the panel clears it).

### Hardening a public panel

Do these once the panel has a public address. In order of how much they
protect:

**1. Two-factor login.** With `ADMIN_TOTP_SECRET` set, signing in takes the
password *and* the current 6-digit code from an authenticator app (Google
Authenticator, Authy, 1Password, ...). A leaked or guessed password alone
gets nowhere, and each code works only once.

```bash
umask 077; docker compose exec -T panel python totp.py > /tmp/totp.txt   # secret + app link, only you can read it
head -1 /tmp/totp.txt >> .env                                   # ADMIN_TOTP_SECRET=...
sudo apt install -y qrencode && tail -1 /tmp/totp.txt | qrencode -t ansiutf8   # scan with the app
rm /tmp/totp.txt
docker compose up -d --force-recreate panel
```

No QR scanner? Add the account in the app by typing the `ADMIN_TOTP_SECRET`
value (`grep ADMIN_TOTP_SECRET .env`) as a time-based key. Sign in once in a
private window before closing your current session, to confirm the code is
accepted. Lost the phone? Remove the line from `.env` and recreate the
panel; then set up a new secret.

**2. The server itself.**
- In the cloud firewall (e.g. AWS security group), allow SSH (22) only from
  your own IP, and nothing inbound besides 22, 80 and 443.
- Keep security updates automatic:
  `sudo apt install -y unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades`.
- Log in to the server with keys only (the AWS default); don't enable
  password SSH.
- Back up `.env`. Everyone who can read it can decrypt every stored login.

## Add a Telegram account

Open the account picker at the top left and choose **+ Add account**, then
**Telegram**. With no accounts yet, the dialog opens by itself. Fill in:

1. **Name** (optional). This is how the account appears in the picker. It defaults to the phone number.
2. **API ID** and **API hash** from https://my.telegram.org.
3. **Phone number** in international format, e.g. `+34600123456`.
4. **DeepSeek API key.**

Press **Send code**. Telegram usually sends the code **inside the Telegram
app** (the "Telegram" chat with the blue checkmark on a device where the
account is logged in), not by SMS. The dialog says where it went. Enter the
code. If the account has two-step verification, enter its cloud password next.
The password is used once and never stored.

How accounts are identified:

- **One account per phone number.** The account's id is `tg` plus the digits of the number, e.g. `tg34600123456`.
- **Signing the same number in again updates that account** and keeps its history and settings. You can leave the DeepSeek key blank to keep the stored one.
- **A number that is currently running is refused.** Take it out of rotation first (see [Operations](#operations)).

After sign-in, the account is marked active, and a manager worker picks it up
within about 15 s. To watch it happen:

```bash
docker compose logs -f manager
```

Look for `[tg34600123456] Started (picked up while running).` Accounts that
exist when the manager starts log `Started.` instead. The dot next to the
account in the picker turns green once it is connected.

Each account signs in and runs with its own **stable device identity**
(`device_profiles.py`): a real phone or desktop model with a matching OS and
Telegram app version, picked from the account id and stored in its settings.
Without this, every account on the server would report Telethon's
`PC 64bit`. Locale and timezone offset default to Latvian (`lv`,
Europe/Riga).

## WhatsApp accounts

A WhatsApp account runs as an **inbound receptionist on a linked device**:
the server is linked to the number the way WhatsApp Web is (Settings → Linked
devices on the phone), speaking WhatsApp Web's protocol through the Baileys
library in the `wa-gateway` service. Customers write to the number; the
assistant drafts replies exactly as it does on Telegram, with the same
approval, caps, quiet hours, holds and audit log. The phone keeps working
normally, and what you type on it shows up in the panel.

**This is not an official WhatsApp API, and WhatsApp bans numbers that behave
like bots**, much sooner than Telegram limits them. So:

- **Soft-launch on a secondary number** you can afford to lose, not the
  business's main line, and watch it for a couple of weeks before moving a
  real number over.
- **Keep approval on** (`auto_send` off, the default for WhatsApp). Turn on
  auto-send only once the drafts are consistently right, and keep the caps low.
- Keep the phone itself online now and then: WhatsApp unlinks the devices of
  a phone that stays offline for about two weeks.

### Linking a number

1. Account picker → **+ Add account** → **WhatsApp**.
2. Fill in **Name** (optional; defaults to the phone number), **Phone
   number** in international format (`+34600123456`), and the **DeepSeek API
   key**. Choose how to link: **Scan a QR code** or **Type a pairing code**
   (for when the phone can't scan the screen, e.g. you are on the phone
   itself). Press **Link WhatsApp**.
3. On the phone: WhatsApp → **Settings** (on Android: the **⋮** menu) →
   **Linked devices** → **Link a device**, then
   - QR: point the camera at the code in the panel. **It changes about every
     20 s**; the panel redraws it, just scan the current one.
   - Pairing code: tap **Link with phone number instead** and type the
     8-character code the panel shows.
4. The panel says the number is linked. It stores the DeepSeek key, marks the
   account active, and a manager worker picks it up within about 15 s
   (`[wa34600123456] Started (picked up while running).` in
   `docker compose logs -f manager`, then `WhatsApp connected as …`).

On the phone the server appears under Linked devices as a desktop browser,
e.g. *Chrome (Mac OS)*. Each account gets its own stable browser identity
(`wa_device_profiles.py`), picked once from the account id and reused on every
re-pair. A pairing nobody finishes gives up after a few minutes (start
again); at most 5 numbers can be pairing at once.

How WhatsApp accounts are identified:

- **One account per number.** The id is `wa` plus the digits, e.g.
  `wa34600123456`. The same number can also have a Telegram account
  (`tg34600123456`); the two are separate clients.
- **Pairing the same number again keeps its history and settings.** Leave the
  DeepSeek key (and the name) blank to keep the stored ones. A re-pair is a
  new linked device: the old device's keys are wiped first.
- **A number that is running is refused** ("already running … Stop it before
  pairing it again"). Take it out of rotation first (see
  [Operations](#operations)); this is also the case after a session loss,
  see below.

### Safety defaults for a new WhatsApp account

A brand-new WhatsApp client starts with lower limits than a Telegram one.
They are written into the client's own config layer when the number is first
paired (audited as *WhatsApp safety defaults*), so they show as highlighted
rows in **Settings → Config**: ordinary client settings you can change like
any other, every change audited. A re-paired number keeps whatever its config
says by then.

| Setting | WhatsApp default | Platform default (Telegram) |
|---|---|---|
| `auto_send` | off | off |
| `daily_message_cap` | 60 | 150 |
| `hourly_message_cap` | 15 | 0 (none) |
| `safety.daily_peer_cap` | 15 | 30 |
| `reply_delay` | 45–180 s, `lognormal` | 20–90 s, uniform |
| `burst` | up to 3 messages, 1.2–3.5 s apart | up to 4, 0.6–2.2 s apart |
| `quiet_hours` | **on**, 21:00–09:00 | off, 21:00–09:00 |
| `outreach.enabled` | off (and outreach is not available on WhatsApp anyway) | off |

Raise them slowly, if at all. Volume, breadth (many different people a day)
and instant round-the-clock answers are what gets a WhatsApp number banned.

### The status dot

The same rules as for Telegram:

- **grey**: no worker holds the account (not active, no stored login, or no
  free slot);
- **green**: a worker runs it and it is connected;
- **red**: a worker holds it but it is not connected, or it was halted after
  a session loss. After a session loss the runtime deliberately keeps the
  lease, so the dot stays red until you deal with it (see below).

The Safety view and the manager log say which.

### What works and what doesn't

Works as on Telegram: drafting and approval, auto-send, the policy checks,
caps and quiet hours, holds and the global stop, escalation keywords, human
takeover (a message typed on the phone counts), per-chat reply limits,
"typing…", read receipts (blue ticks, if the number's own privacy setting
sends them), presence, the unanswered queue, the health watchdog and alerts,
hard-off, and **bookings** (below). Private chats only: groups, status
updates, broadcast lists and channels are ignored.

Not supported on WhatsApp:

- **Outreach.** The panel hides it and the API refuses it.
- **Sending media**, so the media library isn't used in replies.
- **Reading photos** (vision). A photo is stored as its caption, or as
  `[photo]` without one; it is not described, so `vision.*` and the arrival
  photo check do nothing.
- **The new-login anomaly check** (it reads Telegram's list of logins). Volume
  spikes and the trip-wire still apply.
- **A proxy.** Every WhatsApp socket connects from the server's own address.

### Bookings

They work as on Telegram, with one difference: **`booking.provider` is the
owner's phone number** in international format (`+34600555666`), not a
username. Booking requests go to the owner from the account's own number; the
owner answers `YES 7` / `NO 7` (and the other commands), or **replies to the
request message** (WhatsApp's reply/quote) with a bare `yes` or `no`.

### When a send fails

- A message WhatsApp refuses **stays in the thread, red, with its text**. That
  includes a refusal that arrives after the message seemed to go out (WhatsApp
  answers some sends only later).
- A **rate limit**, or code **463** (WhatsApp has restricted the account: no
  new chats), **halts the account**: a `whatsapp` hold, an alert, `HALTING ALL
  AUTOMATION` in the manager log. Don't resume straight away: lower the caps,
  wait hours, then resume the hold in Safety.
- **Blocked**, or **not on WhatsApp**: that chat is paused, with a note in it.
  Nothing else stops.

### Session lost

WhatsApp ends a linked device for good in five ways. The gateway reports each
once and never reconnects it. In every case the account **halts** with a
`whatsapp` hold (Safety shows *stopped after a WhatsApp error*), an alert is
raised, the stored login is **deleted**, and the account's state becomes
`needs_login` (`revoked` for `forbidden`).

| Reason (code) | What it means | What to do |
|---|---|---|
| `loggedOut` (401) | The device was removed: from the phone's Linked devices, by a logout, or by WhatsApp | Ask whoever has the phone. If it was deliberate, leave it. Otherwise pair again |
| `forbidden` (403) | WhatsApp refused the account: **likely banned** | **Stop. Don't re-pair straight away.** Open WhatsApp on the phone: a ban notice says so. Work out what triggered it (volume, reports) before this number goes back on |
| `badSession` (500) | The stored session is corrupt | Pair again |
| `connectionReplaced` (440) | Another WhatsApp Web session took over this login: usually a second copy of this stack (an old server, a restored backup) or the test CLI running for the same number | Find and stop the other copy first, or they will keep knocking each other off. Then pair again |
| `multideviceMismatch` (411) | WhatsApp's multi-device state no longer matches this login | Pair again |

Recovery, every time: **find out why** (the checklist *WhatsApp session
dropped* in [`RUNBOOK.md`](../RUNBOOK.md#whatsapp-session-dropped)), **take
the account out of rotation** (the SQL under [Operations](#operations); the
runtime still holds the lease, so pairing is refused until then), **pair
again** from **+ Add account → WhatsApp** with the same number, then **resume
the `whatsapp` hold** in Safety. Pairing again brings the account back, but it
sends nothing on its own until the hold is lifted.

### Restarts and recovery

- `docker compose restart manager` or `docker compose restart wa-gateway`
  needs **no re-pair**: the login is in Postgres. The runtime asks the gateway
  to `open` the socket every 15 s, so accounts come back within about 15 s of
  the gateway being up again.
- A worker that dies lets its leases expire (30 s); another worker takes the
  account with a higher epoch, and the gateway closes the old socket before
  opening the new one. The gateway also closes a socket whose lease has been
  gone for 30 s, and every socket when it can't reach Postgres for 22 s.
- Messages that arrive while the gateway (or the whole server) is down are
  delivered by WhatsApp when it reconnects, and **stored once** (by WhatsApp's
  message id).
- The gateway is a **singleton**: a second copy finds the lock taken, exits
  and is restarted by compose. Don't `--scale` it.
- Inbound messages go from the gateway to the runtime through Postgres
  (`wa_inbox`). **During a Postgres outage, messages not yet written wait in
  the gateway's memory only**, retried until Postgres answers; restarting the
  gateway then loses them (it logs how many). Fix Postgres first, and don't
  restart `wa-gateway` while it is down.

### Known limits

- **No stop-account button** in the panel. Use the SQL under
  [Operations](#operations), the same as for Telegram (the id is
  `wa<digits>`).
- **The Baileys version is a release candidate (7.0.0-rc14), pinned
  exactly** in `wa_gateway/package.json`. WhatsApp changes its protocol from
  time to time; upgrading Baileys is a deliberate change with its own testing,
  never a routine `npm update`.
- `wa_gateway/README.md` also documents a manual-test CLI (`node dist/cli.js`).
  It opens a socket **without** a lease: never run it for a number the stack
  is running.

## Clients, industries and settings

Every account, Telegram or WhatsApp, belongs to a **client** (a tenant: one business), and
every client belongs to an **industry**. A new account becomes a new client in
the *General* industry. Open **Clients** in the top bar to see them all, or
**Settings** to open the client of the account you are looking at. Changes are
stored in Postgres, logged in the audit log, and reach a running account within
seconds (at most five minutes if the reload message is missed).

How a client's bot behaves comes from three layers, lowest first:

| Layer | Where | What it holds |
|---|---|---|
| Platform | the code's defaults, and **Clients → Platform rules** | Default values and hard limits for every setting; the platform rules at the top of every prompt |
| Industry | **Clients → the industry folder** | A prompt template (one text per section) and default settings for every client in it |
| Client | **Clients → the client** | Only what differs for this business: its own settings, and per prompt section *override* or *append* |

In a client's **Config** tab, greyed rows are inherited and highlighted rows are
set for this client. **Override** sets a value here, **Inherit** removes it.
Every value is checked when you save; nothing is silently clamped, and an
error shows next to its field.

### Prompt: fill it in before anything else

**The business sections start empty, and an empty prompt produces generic
replies.** Until a section says something, the top bar shows *"no business
details in the prompt yet"*. Fill the industry template once (**Template**
tab of the industry), then each client's own details (**Prompt** tab of the
client): about the business, services and prices, opening hours and
location, how booking works, frequent questions, tone, what not to do,
sign-off, and examples of how you write.

The **platform rules** always come first and are restated as taking
precedence at the end; no industry or client text can change them. See the
**Rendered prompt** tab for exactly what the model is given. Every save of any
layer is a new version: the **Versions** tabs roll back, and a client can be
pinned to one industry template version so industry edits don't reach it.

**Ask AI** (on a client) turns a plain request ("don't answer between 10 pm
and 8 am") into a proposed config change with a field-by-field diff. Nothing
changes until you press **Apply**. It uses `DEEPSEEK_PLATFORM_KEY` from `.env`.

### Settings that matter first

| Setting | Default | Meaning |
|---|---|---|
| `auto_send` | **off** | Off: every draft waits in the panel for *Approve & Send*, *Edit then Send* or *Reject*. On: replies that pass the policy checks go out by themselves. |
| `reply_delay` | 20–90 s, uniform | Wait before a reply is written. `lognormal` clusters most replies early with a few slow ones. |
| `quiet_hours` | **off**, 21:00–09:00 | In the client's `timezone` (default Europe/Riga). A reply due inside the window waits until it ends; it is not dropped. |
| `burst` | up to 4 messages, 0.6–2.2 s apart | A reply may go out as several short messages. |
| `language_policy` | `mirror` | Or `fixed:lv` / `fixed:ru` / `fixed:en`. |
| `daily_message_cap` | 150 | All messages the account sends per day, replies included. |
| `hourly_message_cap` | 0 (none) | The same per hour. At either cap messages are held back and you get an alert. |
| `safety.daily_peer_cap` | 30 | Distinct people written to per day. |
| `price_floors` | none | Service → lowest price (EUR) a reply may quote. |
| `allowed_link_domains`, `shareable_contacts` | none | Links, phone numbers and e-mail addresses a reply may contain. |
| `banned_topics` | none | A reply mentioning one is held for approval. |
| `api_spend_cap_eur` | 10 | AI spend per calendar month, in EUR. At the limit the client is **soft-off** with an alert (messages are still received) until the month rolls over or the cap is raised; then it resumes by itself. 0 = no limit. |
| `limits.*` | no limits | Tokens per day and per month, and EUR per day, for the same purpose. |
| `replies.*` | no limits | Bot messages per chat per hour/day, least gap between them, not answering bare "ok"/"thanks", and your own *when not to reply* instruction. |
| `escalation_keywords` | none | A customer message containing one pauses that chat and pings the owner (`booking.provider`). |
| `takeover_hours` | 12 | After someone writes in a chat by hand (phone or panel), the bot keeps quiet there this long. 0 = off. |
| `anomaly.*` | all on | Automatic soft-off with an alert on a new Telegram login, a send-volume spike (`volume_multiplier` × the usual hour, at least `volume_min_messages`), or a trip-wire match in a reply. |
| `ai.*` | `deepseek-chat`, 400 tokens, 1.0 | DeepSeek request parameters |

**Account safety** (`safety.*`) protects the number itself. Telegram does not
publish its thresholds, so the defaults are deliberately low: halt on
`PeerFloodError`, halt if Telegram asks for a wait longer than
`max_flood_wait_seconds` (300 s), and never start a conversation with someone
who is not a contact and has not written first.

### Policy checks on every AI-written reply

Before a reply goes out on its own, it is checked in code: links to domains not
allowed, crypto wallet addresses or IBANs the business has not written itself,
phone numbers or e-mail addresses not in `shareable_contacts`, prices below
`price_floors`, `banned_topics`, and discounts, refunds or guarantees the
business has not offered in its own prompt text. A reply that fails is kept as
a draft with the reasons shown in the chat, and the hold is audited. Customers
will try to talk the model out of its rules; these checks don't listen.

### Safety: switching a client off, and alerts

Open **Safety** in the top bar. The number next to it counts open alerts.

- **Soft-off** stops a client's account from sending anything on its own:
  replies, reminders, owner messages and outreach. Messages keep arriving and
  are stored and shown. **Resuming replays nothing**: messages that arrived
  meanwhile get no automatic answer, and reminders that fell due are skipped.
  You can still send by hand from the panel.
  A client can be off for several reasons at once, and each is lifted on its own:
  - **paused**: *Pause all* in the top bar, or *Soft-off* in Safety.
  - **suspended (billing)**: see Billing below.
  - **AI limit reached**: see `api_spend_cap_eur` and `limits.*`. This one lifts by itself.
  - **anomaly**: see below. A person resumes it.
  - **stopped after a Telegram error**: `PeerFloodError`, a FloodWait longer
    than `max_flood_wait_seconds`, or a banned or revoked session. Find out why
    before resuming; sending straight through a flood warning is how numbers
    get banned.
  - **stopped after a WhatsApp error**: a rate limit, code 463 (account
    restricted) or a lost session. See [WhatsApp accounts](#whatsapp-accounts).

  The red chip in the top bar says why the selected client is off.
- **Global stop** (Safety → All clients) switches every client off at once. It
  needs a reason and is audited. If the panel is unreachable, run it from the
  server: `docker compose exec panel python controls.py stop "reason"`, and
  `... resume` to lift it.
- **Hard-off** (Safety → a client) is for a hijacked or leaked session. It
  logs this server's Telegram session out (for WhatsApp: unlinks this
  server's linked device), deletes its key and deactivates the account. You
  confirm by typing the account id. It does not touch the owner's phone or
  their other logins or devices. Signing the number in (or pairing it) again
  is a new login. If the network could not be told, the message says so:
  end the session on the phone (Telegram: Settings → Devices; WhatsApp:
  Linked devices).
- **Anomalies** switch a client off by themselves (`anomaly.*`):
  - a Telegram login appears on the account that wasn't there before
    (checked every 5 minutes, and at once when Telegram's own "new login"
    message arrives; Telegram accounts only);
  - the account sends far more in an hour than it usually does (every
    outgoing message counts, including ones typed on a phone);
  - a reply the bot wrote links to a domain nobody allowed, or contains a
    wallet address or an IBAN.
- **Billing**: set a client's next due date in Safety. The day after it, if no
  payment was recorded, the client goes into **grace**. The owner gets a
  message from the client's own account (the text is under Safety → Billing
  notice) and you get an alert. After 48 hours it is **suspended**, which is a
  soft-off; nothing is deleted. *Record payment* makes it active again. You can
  also set the status by hand at any time, with a reason.
- **Health**: the scheduler checks every account once a minute. You get an
  alert within about 4 minutes when an account is not running, not connected
  to Telegram or WhatsApp, logged out or rate-limited, and a "back to normal" when it
  recovers. A red banner shows if the scheduler itself stops.
- **Alerts** are listed under Safety → Alerts. With `ALERT_EMAIL` (plus the
  `SMTP_*` settings) or `ALERT_WEBHOOK_URL` in `.env`, each new one is also sent
  there. A repeat of an open alert is counted, not re-sent.

**In a single chat:**

- **Pause / Resume** on a conversation stops the bot in that chat until you
  resume it.
- **Escalation keywords** (`escalation_keywords`): a customer message
  containing one pauses the chat (the badge reads *escalated*) and the owner
  gets a message quoting it.
- **Human takeover**: when someone writes in a chat by hand, on the phone or
  from the panel, the bot keeps quiet in that chat for `takeover_hours` and
  then carries on by itself. The chat header shows until when, with *Hand back
  to the bot* to end it sooner.

## What it does with a message

- It handles **private messages only**. Group and channel traffic (on WhatsApp also status updates and broadcast lists) is ignored. On Telegram, messages from **bot accounts are included**, so conversations that run through a bot's interface are handled like any other DM.
- Every message is stored in Postgres and pushed live to any open panel tab.
- For each incoming text message it checks, in order: an escalation keyword (pauses the chat and pings the owner), whether the client is soft-off, and whether the chat is paused or taken over by a person. If any says stop, no draft is made. Messages from Telegram's own service account (login codes) are never answered.
- Otherwise it waits the reply delay (and, if the reply would land in quiet hours, until they end), builds the last ~30 messages of the chat into the prompt under the client's rendered prompt, and asks DeepSeek for a reply. The reply is checked by the policy layer. With auto-send off, or if a check fails, it is saved as a draft for the panel. With auto-send on and all checks passed, it is sent.
- Every message the account sends is recorded in the audit log with who caused it (the bot, or you from the panel) and why. AI-written messages also record the model and the prompt versions used (e.g. `b1/i1v3/c2`). Every DeepSeek call is metered per client.
- **If a newer message arrives while a draft is still being prepared, that draft is cancelled and restarted**, so the reply always answers the latest state of the conversation. Sending a message yourself from the panel also cancels any draft in progress for that chat.
- Messages you send from your phone (or Telegram Desktop, or another WhatsApp linked device) also appear in the panel, so the thread stays complete. They also start a human takeover of that chat (see *Safety*).
- Before writing a reply it checks the client's AI limits (`limits.*`, `api_spend_cap_eur`) and the per-chat reply limits (`replies.*`). A reply not written for either reason is noted in the chat and the audit log. Booking news (a confirmation, a reminder) is never held back by the reply limits.
- A reply held back by quiet hours is stored in Postgres and sent by the scheduler when they end, so a restart during the night does not lose it.
- DeepSeek failures (network, timeout, 429, malformed response) are retried with backoff that honours `Retry-After`, then shown as a red error in the conversation. A bad key (401/403) fails at once. Telegram and WhatsApp disconnects are reconnected automatically with backoff (a lost WhatsApp session is not: see [WhatsApp accounts](#whatsapp-accounts)).

## Other features

**Sounding human** (`human.*` and `presence.*`, all on by default). *Adaptive
style* profiles how each person writes (length, emoji, capitalisation) from
their own messages and tells the model to match it. It needs at least two of
their messages. *Mark read* marks their message read after the delay. The
*typing indicator* shows "typing…" for as long as the text would plausibly
take: length ÷ 12 characters/second, capped at 25 s. Replies you send or
approve by hand skip the typing wait. *Presence* keeps the account offline
between conversations and online only around replying.

**Outreach** (off for clients unless `outreach.enabled` is on) starts
conversations with people in the account's Telegram contacts. You say what
each message should achieve, and each person gets one written for them. The
server re-checks that every recipient is a contact. Messages wait for approval
by default and go out one at a time, 90–300 s apart, with at most 20 per day.
Use it only for people who expect to hear from you: Telegram limits or bans
accounts that send unsolicited DMs.

**Linked-chat context** (`context_link.*`) is **off for clients**: it stores
written summaries about people, which the platform does not keep.

**Bookings** (`booking.*`, off by default). Open **Bookings** in the top bar
for the selected account's calendar, what waits for an answer, the waitlist,
the opening hours, the calendar link and AI usage.

- When a customer asks for a day and time, the code checks it against the
  opening hours, closed days, notice, how far ahead, and every other booking
  (plus the gap after each). A taken or closed time is never put to the
  owner; the reply offers the nearest free times and, if it was taken, the
  waitlist.
- A free time goes to the owner (`booking.provider`) from this same account:
  *Booking request #7 … Reply YES 7 or NO 7, or a new time like 7 15:30.*
  Numbers count per client from 1. **Nothing is confirmed until a person
  says yes**: the owner by text, or you in the panel.
- The owner can answer `YES 7`, `NO 7`, a new time (`7 15:30`,
  `7 04.10 15:30`, `7 tomorrow 15:30`), `CANCEL 7`, `DONE 7`, `NOSHOW 7`, or
  `LIST`. A bare `yes` works as a reply to the request message, or when only
  one request is open. When the owner proposes a time, the customer is asked;
  if they take it, it is confirmed (the owner wrote that time).
- The customer can change the time before the owner answers (same number),
  ask to move a confirmed booking (the owner answers `YES 7` / `NO 7`), or
  cancel. Every change is in the booking's history and the audit log.
- **Reminders** (`booking.reminders`): a list of `{minutes_before,
  instruction}`, by default 24 h and 2 h before. The instruction says what
  that reminder should say. The customer can reply `1` (coming) or `2`
  (cancel). If the scheduler was down, only the latest due reminder goes out.
  Quiet hours apply to reminders too.
- A request nobody answered before its start time lapses, and both sides are
  told. A cancelled or moved booking frees its time for the first person on
  the waitlist whose wish covers it; they have `waitlist_offer_hours` to take
  it before the next one is asked.
- **Arrival**: when the customer says they are there, `arrival_instructions`
  (address, door code) are sent word for word, once. With
  `arrival_photo_check` (needs `vision`), a photo they send is compared with
  the media items marked *Entrance*; with `arrival_requires_photo` the
  instructions wait for a matching photo. The owner can send the entrance
  photo to this account captioned "door".
- **E-mail record**: with `SMTP_*` in `.env` and `booking.owner_email` set,
  every confirmation, move and cancellation is e-mailed with an `.ics`
  attached.
- **Public pages** (optional): set `BOOKING_DOMAIN` and `PUBLIC_BASE_URL` in
  `.env` and run `docker compose --profile booking-pages up -d`. Each client
  then has a secret calendar feed (Bookings → Calendar link) and each booking
  a read-only page that reminders link to, where the customer can say they
  are coming or cancel. This uses ports 80/443, like the `public` profile for
  the panel: run one of the two, or add the booking site to `Caddyfile`.

Opening hours live in Postgres and are edited in **Bookings → Opening
hours**; with no rows, any time is accepted as long as it doesn't overlap
another booking. Detection costs one short DeepSeek call per customer
message while bookings are on.

**Photos** (`vision.*`, off by default). DeepSeek cannot see images, so photos
go to a separate OpenAI-compatible model: set `VISION_API_URL` and
`VISION_API_KEY` in `.env`, `vision.enabled` and `vision.model` in the
client's config, and add the model's price under **Clients → Platform rules
→ AI prices**. A customer's photo is then described in one or two sentences
(never the person's appearance) so the reply can take it into account.

*Google Calendar mirror (optional):* create a Google Cloud service account
with the Calendar API enabled and download its JSON key. Put the key at
`./data/tenants/<client id>/google-service-account.json`, or put it anywhere
under `./data` and set `GOOGLE_SERVICE_ACCOUNT_FILE=/app/data/<file>.json` in
`.env`. That path is the one inside the container. Then recreate with
`docker compose up -d`. Share the calendar with the service account's email
address with *Make changes to events*, and put the calendar ID in
`booking.google_calendar_id`. Calendar failures are reported in the thread and
never stop the Telegram side.

**Media.** Upload photos and videos the assistant may send, or copy them into
`./data/tenants/<client id>/media/`, and give each one a short description.
The AI uses the description to pick the right file when someone asks. By
default (`media.*`) a video is only offered first and sent once the person
says yes, and a reply carrying a video always waits for approval, even with
auto-send on.

**Style** holds per-contact overrides for one person: extra notes, message
length, delays and typing speed. Writing samples for everyone are the
client's *Examples of how we write* prompt section.

## Operations

```bash
docker compose logs -f manager          # what the accounts are doing
docker compose logs -f panel            # panel / API errors
docker compose logs -f wa-gateway       # the WhatsApp sockets (JSON lines; never message text at info)
docker compose restart manager          # restart all accounts; leases are released, or expire within 30 s
docker compose restart wa-gateway       # reconnect every WhatsApp number; no re-pair, back within ~15 s
git pull && docker compose up -d --build  # update; migrate runs before panel/manager/wa-gateway start
```

**Capacity.** One manager runs `WORKER_COUNT × SESSIONS_PER_WORKER` accounts
(2 × 25 = 50 by default). Accounts beyond that stay idle until a slot frees
up. Change both in `.env` and run `docker compose up -d`.

**Backups.** Everything that matters is in three places: the Postgres volume,
`./data` (per client under `./data/tenants/<client id>`: media), and `.env`. Bookings are in Postgres,
and so are the WhatsApp logins (encrypted): a restored backup running next to
the original stack puts two sockets on each WhatsApp number (`connectionReplaced`).
Never run both.
To dump the database:

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > userbot-$(date +%F).sql
```

**Taking an account out of rotation.** To stop it sending, use *Soft-off*
(Safety). To disconnect it altogether, mark it inactive in the database; its
worker lets go within about 10 seconds, with no restart needed:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
#   UPDATE telegram_sessions SET is_active = false WHERE session_id = 'tg34600123456';
#   \q
```

The table is called `telegram_sessions` for historical reasons; it holds the
WhatsApp accounts too (`WHERE session_id = 'wa34600123456'`). For WhatsApp,
the worker then tells `wa-gateway` to close the socket (the gateway's own
watchdog closes it anyway once the lease is gone). The linked device stays
linked, so setting the account active again needs no re-pair.

To bring the account back, set `is_active = true` again. A worker picks it up
within ~15 s. Signing the number in (or pairing it) again from the panel also
re-activates it.

## Running the tests

**On the server, inside the stack.** Create the test database once:

```bash
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" userbot_test'
```

Then run the suite in a throwaway manager container. It uses the stack's
Postgres, pointed at the `userbot_test` database:

```bash
docker compose run --rm --no-deps manager sh -c 'PG_TEST_DSN="${DATABASE_URL%/*}/userbot_test" python -m pytest -q'
```

**Locally**, with Python 3.11+ and any Postgres you can reach:

```bash
cd telegram_admin_bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
PG_TEST_DSN=postgresql://user:password@localhost:5432/userbot_test python -m pytest -q
```

Each Postgres-backed test creates its own throwaway schema, migrates it and
drops it afterwards, so the target database is left as it was. Without
`PG_TEST_DSN`, the Postgres tests are skipped (reported as skipped, not
passed) and the rest still run. Valkey is replaced by `fakeredis`, so no Valkey
is needed.

`tests/test_wa_crypto_compat.py` checks that `crypto.py` and the gateway's
`crypto.ts` read each other's blobs. Its golden-vector part always runs; its
live Python↔Node round trip needs `node` on the PATH (or `NODE_BIN`) and a
built `wa_gateway/dist/` (`npm run build` there), and is skipped otherwise,
e.g. in the manager container, which has no Node.

**The gateway's Node tests** (`wa_gateway/test/`) run on **Node 24**: it runs
the TypeScript tests directly. The `wa-gateway` image can't run them: it is
built with production dependencies only and without `test/`. Locally:

```bash
cd telegram_admin_bot/wa_gateway
npm ci
npm test
npm run typecheck   # optional: tsc over src and tests
```

On a server without Node, the same in a throwaway `node:24` container, on a
copy so nothing is written into the checkout:

```bash
docker run --rm -v "$PWD/wa_gateway:/src:ro" node:24-slim \
  sh -c 'cp -r /src /tmp/gw && cd /tmp/gw && rm -rf node_modules dist && npm ci && npm test'
```

`test/authstate.test.ts` also needs a Postgres (`PG_TEST_DSN`, same idea as
above: it makes and drops its own schema) and skips when there is none.
Nothing in the tests ever connects to WhatsApp.

## Troubleshooting

**`Bind for 127.0.0.1:8787 failed: port is already allocated`**: an older
container still holds the port, usually the legacy single-account
`telegram-assistant`. Find it with `docker ps`, remove it, and bring the stack
up again:

```bash
docker rm -f <name>
docker compose up -d --remove-orphans
```

**Panel log shows `Temporary failure in name resolution` after a clash like
that**: the panel came up on a broken network attachment. Recreate it:
`docker compose up -d --force-recreate panel`.

**"Wrong password" at the admin sign-in**: compare
`docker compose exec panel printenv ADMIN_PASSWORD` with the value in `.env`.
If they differ, the container is still running with an old value: run
`docker compose up -d --force-recreate panel`. A variable exported in your
shell also overrides `.env`. After repeated wrong passwords your IP is locked
out for a while ("Too many wrong passwords"). Wait and try again.

**Account dot stays grey**: no worker is running the account. Check
`docker compose logs manager` for that account id:

- `Skipping — needs login`: the account has no usable Telegram login (for WhatsApp: no stored linked device) or no DeepSeek key. Sign it in (or pair it) again.
- `Failed to start` followed by a traceback: the error is in the traceback. The worker retries that account after 5 minutes. `docker compose restart manager` retries it now.
- Nothing about it at all: all worker slots may be full (see *Capacity*).

**Account dot is red**: a worker holds the account but it isn't connected,
or it needs a new login. Safety shows the reason, and so do the manager logs.
If the session was ended from Telegram (Settings → Devices), the worker lets
go of it by itself; sign the number in again with **+ Add account**. A
WhatsApp account whose session was lost keeps its lease on purpose and stays
red: see [Session lost](#session-lost). A WhatsApp account that is red with
*"The WhatsApp gateway is not answering"* in the manager log needs
`docker compose ps wa-gateway` and `docker compose logs --tail 50 wa-gateway`.

**Drafts appear but nothing is sent**: auto-send is off, which is the
default. Approve drafts by hand, or turn on `auto_send` in the client's
Config. With auto-send on, a draft with a *"Held for approval"* note above it
failed a policy check; the note says which.

**No reply at all**: first check the red chip in the top bar (the client is
soft-off), the chat's badges (paused, escalated, "you're handling"), whether it
is inside the client's quiet hours (the reply then
waits until they end), and that the message was private and had text. Then check `docker compose logs manager` for
`DeepSeek` errors (bad key, no balance, rate limit) or other errors for that
account. DeepSeek errors also show up red in the conversation.

**"Stopped after a Telegram error"**: a Telegram flood limit tripped
(`PeerFloodError`, or a FloodWait longer than `max_flood_wait_seconds`) or the
session was rejected. Read the reason in Safety or the manager logs, wait and
work out what caused it, then resume that hold in Safety.

**"Stopped after a WhatsApp error"**: a rate limit, a 463 restriction, or a
lost session. See [When a send fails](#when-a-send-fails) and
[Session lost](#session-lost), and the checklist in
[`RUNBOOK.md`](../RUNBOOK.md#whatsapp-session-dropped).

## Files

```
docker-compose.yml     the stack: postgres, valkey, migrate, panel, manager, scheduler, wa-gateway, optional caddy and booking pages
Dockerfile             one image for panel, manager, scheduler, migrate and the booking pages
wa_gateway/            the wa-gateway service (Node 24, TypeScript, Baileys): WhatsApp sockets, pairing,
                       auth state in Postgres, wa_inbox writer; its own Dockerfile, tests and README.md
Caddyfile              optional public HTTPS front door for the panel (profile "public")
Caddyfile.booking      optional public HTTPS for the booking pages (profile "booking-pages")
Caddyfile.both         the panel and the booking pages on one Caddy
deploy/                bootstrap_ubuntu24.sh (prepares a new server) and backup.sh (encrypted backups)
owner_auth.py          client logins: passwords, sessions, optional 2FA
owner_api.py           API behind the client dashboard (/owner/)
proxies.py             per-account Telegram proxy (socks5/http)
scheduler.py           the one background scheduler; replies held back by quiet hours; watchdog, billing
controls.py            kill switches: soft-off holds, the global stop (also a shell command), hard-off
alerts.py              alerts for the operator: stored, shown in Safety, e-mailed / webhooked
health.py              what each account reports, and the watchdog that turns it into alerts
anomaly.py             new Telegram logins and send-volume spikes
billing.py             active -> grace -> suspended, the owner's notice, payments
safety_api.py          admin API behind the Safety view
public_app.py          the public booking pages: calendar feed and read-only page per booking
panel.py               admin panel + API; control plane, no Telegram or WhatsApp connections
manager.py             spawns worker processes, restarts dead ones, adopts new accounts
session_runtime.py     one account, either network: drafting, sending, safety, outreach, bookings
transport.py           the seam between an account's runtime and its network (Telegram or WhatsApp)
telegram_transport.py  the Telegram transport: the Telethon client
whatsapp_transport.py  the WhatsApp transport: drives the socket in wa-gateway over the bus, drains wa_inbox
wa_store.py            WhatsApp in Postgres: chat identity (wa_peers), the wa_inbox handoff, the stored login
wa_pairing.py          linking a WhatsApp number from the panel (QR or pairing code) via wa-gateway
wa_device_profiles.py  stable per-account linked-device browser identity
login_flow.py          phone -> code -> 2FA sign-in behind "Add account" (Telegram)
leasing.py             one-worker-per-account leases in Postgres
commands.py            Valkey command bus (panel -> worker) and live events (worker -> panel)
database.py            Postgres access: SessionRegistry (accounts) and the tenant-scoped Database
tenants.py             tenants, industries, prompt versions; one-off import of pre-platform settings
tenant_config.py       the per-client config schema and its platform <- industry <- client layers
prompt_layers.py       renders platform rules + industry template + client overrides into one prompt
policy.py              in-code checks on every AI-written reply before it is sent automatically
humanlike.py           reply delay, burst gaps and quiet hours from the config
audit.py               the append-only audit log
llm_usage.py           per-client LLM token and cost metering
config_assist.py       plain-language request -> proposed config change (never applied by itself)
platform_api.py        admin API behind the Clients view
booking_api.py         admin API behind the Bookings view
booking_states.py      the booking state machine: which change is allowed, from where, by whom
booking_store.py       bookings, opening hours, waitlist, reminders in Postgres, per client
booking_flow.py        bookings on a running account: requests, owner answers, reminders, arrival
availability.py        is a time bookable, and what is free nearby
ai_limits.py           AI usage limits per client and reply limits per chat
vision.py              photos through an OpenAI-compatible vision model
mailer.py              the booking e-mail record over SMTP
ics.py                 calendar files (the feed and e-mail attachments)
config_store.py        per-account state: device identity, per-contact styles
crypto.py              AES-GCM encryption of stored secrets under USERBOT_MASTER_KEY
device_profiles.py     stable per-account device identity
pg.py                  connection pool and migration runner
migrate_entrypoint.py  the one-shot `migrate` service
migrations/            numbered SQL migrations
ai_responder.py        builds the prompt, calls DeepSeek
context_link.py        borrows context from a linked chat of the same person
bookings.py            the words around bookings: owner messages and commands, prompt lines
media.py               the photo/video library the AI may attach
google_calendar.py     optional Google Calendar mirror for bookings
static/                the panel UI: index.html, css/, js/ (plain HTML/CSS/JS, no build step)
tests/                 pytest suite (see "Running the tests")
data/                  created at runtime: tenants/<client id>/media/
```

## A note on userbots

Automating a personal account is against Telegram's and WhatsApp's Terms of
Service and can get the account limited or banned; WhatsApp bans sooner. Keep
the delays human, keep the safety limits low, and prefer approval mode over
auto-send.

See [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the data model and how a
message flows through the system.
