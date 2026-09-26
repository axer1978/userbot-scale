# Telegram AI Support Assistant

A Telethon **userbot** that runs on your own Telegram accounts, drafts replies
to incoming private messages with the DeepSeek API, and gives you one
password-protected web panel to supervise every account. You deploy it on a
server with Docker Compose. You add and run as many accounts as you need from
the panel.

Nothing is sent without your approval unless you turn on auto-send for that
account.

## How it fits together

```
 browser ──SSH tunnel (or optional HTTPS)──▶ panel ──┐
                                                     ├──▶ Postgres  (sessions, messages, config, leases)
                                     manager ────────┤
                               (worker processes     └──▶ Redis     (command bus + live events)
                                holding Telegram
                                clients)
```

| Service | What it does |
|---|---|
| `panel` | The admin UI and API (`panel.py`). Control plane only: it **holds no Telegram connections**. It reads and writes Postgres directly. Anything that needs a live client, like sending a message or approving a draft, goes over Redis to whichever worker runs that account. |
| `manager` | Starts `WORKER_COUNT` worker processes (`manager.py`). Each one runs up to `SESSIONS_PER_WORKER` accounts, one `SessionRuntime` per account with its own live Telethon client. It restarts a worker that dies. Every ~15 s each worker picks up newly activated accounts that nothing is running yet. |
| `postgres` | The source of truth: accounts (with credentials encrypted), conversations, messages, per-account settings, outreach queue, and the leases. |
| `redis` | The command bus (panel → worker) and live-event fan-out (worker → open panel tabs). It stores nothing that outlives a request. |
| `migrate` | A one-shot job that applies database migrations and exits. `panel` and `manager` wait for it. |
| `caddy` | Optional. Serves the panel over public HTTPS. Off unless you enable it. See below. |

**Leasing** makes sure only one worker runs a given account at a time. A worker
has to take a lease on the account's row in Postgres before it connects, and
it renews the lease every 10 s. If a worker dies, its leases expire after 30 s
and another worker can take the account over. Two clients on one account
would answer every chat twice and can get the session revoked. The lease is
what prevents that.

Secrets at rest (Telegram auth key, API hash, DeepSeek key) are encrypted with
AES-GCM under `USERBOT_MASTER_KEY` (`crypto.py`). **If you lose that key, every
stored login becomes unreadable** and each account has to be signed in again.

## Requirements

- A Linux server with Docker and the Docker Compose plugin (`docker compose`, not `docker-compose`)
- For each Telegram account: an **API ID** and **API hash** from https://my.telegram.org → *API development tools*, and access to that account to receive the login code
- A **DeepSeek API key** from https://platform.deepseek.com

## Deploy on a server

```bash
git clone https://github.com/axer1978/userbot.git
cd userbot/telegram_admin_bot
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

`userbot-postgres`, `userbot-redis`, `userbot-panel` and `userbot-manager`
should be `Up` (Postgres and Redis report `healthy`). `userbot-migrate` should
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

With this on, the panel's only protection is the shared admin password.
After 5 wrong passwords from one IP within 15 minutes, that IP is refused
(even with the right password) until the oldest attempt is 15 minutes old;
panel logins also expire after 12 hours. That slows guessing from one
address, not from many, so **use a long admin password**, like the
generated one, not something memorable. The loopback port stays published
either way, so the SSH tunnel keeps working. Everyone coming through the
tunnel shares one rate-limit bucket, so 5 typos there lock the tunnel out
for up to 15 minutes too (restarting the panel clears it).

### Using nginx instead of Caddy

If you'd rather run nginx on the host, use
[`deploy/nginx-panel.conf`](deploy/nginx-panel.conf) and **don't** enable the
`public` profile (both need ports 80 and 443). Open ports 80 and 443 as above,
then, with `panel.example.com` replaced by your hostname:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/nginx-panel.conf /etc/nginx/sites-available/userbot-panel
sudo sed -i 's/panel.example.com/YOUR-HOSTNAME/' /etc/nginx/sites-available/userbot-panel
sudo ln -s /etc/nginx/sites-available/userbot-panel /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d YOUR-HOSTNAME --redirect
```

certbot adds HTTPS and the http→https redirect to that file and renews the
certificate automatically. If you edit the config, keep
`proxy_set_header X-Forwarded-For $remote_addr;` as it is: the panel treats
the first address in that header as the client, so nginx's usual
`$proxy_add_x_forwarded_for` (which appends to whatever the visitor sent)
would let anyone fake their IP and dodge the login rate limit.

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

**2. nginx rate limits and scanner catch-all.** `deploy/nginx-panel.conf`
already limits logins to 10 a minute per IP (on top of the panel's own
5-wrong-in-15-minutes lockout), caps connections, cuts off slow clients and
sends security headers. Add the catch-all so requests by bare IP, not by the
panel's name, get dropped without a response:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo cp deploy/nginx-default-deny.conf /etc/nginx/sites-available/default-deny
sudo ln -sf /etc/nginx/sites-available/default-deny /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**3. fail2ban.** Bans an address at the firewall for an hour after 10 failed
or refused logins within 10 minutes:

```bash
sudo apt install -y fail2ban
sudo cp deploy/fail2ban/userbot-panel.conf /etc/fail2ban/filter.d/
sudo cp deploy/fail2ban/userbot-panel.local /etc/fail2ban/jail.d/
sudo systemctl restart fail2ban
sudo fail2ban-client status userbot-panel
```

Banned yourself: `sudo fail2ban-client set userbot-panel unbanip <your-ip>`.

**4. The server itself.**
- In the cloud firewall (e.g. AWS security group), allow SSH (22) only from
  your own IP, and nothing inbound besides 22, 80 and 443.
- Keep security updates automatic:
  `sudo apt install -y unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades`.
- Log in to the server with keys only (the AWS default); don't enable
  password SSH.
- Back up `.env`. Everyone who can read it can decrypt every stored login.

## Add a Telegram account

Open the account picker at the top left and choose **+ Add account**. With no
accounts yet, the dialog opens by itself. Fill in:

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

## Settings that matter first

Select the account in the picker and open **Settings**. Every account has its
own settings. They are stored in Postgres and take effect immediately, with no
restart.

### Persona: fill it in before anything else

**Every persona field starts blank, and a blank persona produces generic
replies.** Until at least one field is filled in, the account uses a minimal
neutral system prompt and the top bar shows *"persona not configured"*.

| Field | What to put there |
|---|---|
| Purpose | What this account is for and what you want the assistant to do |
| Tone & style | How it should sound |
| Languages | e.g. "always reply in the language the person wrote in" |
| Boundaries | Hard rules: what it must never say, promise, or do |
| Sign-off behaviour | Whether and how to sign off |

### The rest

| Setting | Default | Meaning |
|---|---|---|
| Auto-send AI replies | **off** | Off: every draft waits in the panel for *Approve & Send*, *Edit then Send* or *Reject*. On: replies go out by themselves. |
| Min / max delay | 20 / 90 s | Random wait before a reply is drafted, so replies don't look instant |
| Active hours | **off**, 09:00–21:00 UTC | When on, drafting only happens inside the window (it may cross midnight), in the IANA timezone you set |
| Log all messages | on | Store messages in the database. Turned off, messages still show live but no history is kept, so the AI also gets less context. |
| Model / max tokens / temperature | `deepseek-chat` / 400 / 1.0 | DeepSeek request parameters |
| Parallel replies | 4 | How many chats can be drafted at once for this account |

**Account safety** protects the number itself. Telegram does not publish its
thresholds, so the defaults are deliberately low:

| Setting | Default | Meaning |
|---|---|---|
| Messages per day | 150 | All messages sent by this account per day, replies included |
| People per day | 30 | Distinct people written to per day |
| Max wait | 300 s | If Telegram asks for a longer pause (FloodWait), automation halts instead of waiting it out |
| Stop everything if Telegram flags the account as spammy | on | Halt on `PeerFloodError` |
| Only message people in contacts or who wrote first | on | Applies to messages the bot **starts** (outreach). The bot never opens a conversation with a stranger. Replies to people who wrote to you are not affected. |

### Pausing and "Automation halted"

- **Pause / Resume** on a conversation stops AI drafting for that chat, for when you take it over by hand. Any draft in progress is cancelled.
- **Pause all** in the top bar pauses every chat on the selected account. The button then reads **Automation paused**. Click it again to resume.
- **Automation halted** means the account paused itself. This happens when Telegram returned `PeerFloodError`, asked for a wait longer than *Max wait*, or rejected the session (banned or revoked). The reason appears as an error in the panel and in `docker compose logs manager` (`HALTING ALL AUTOMATION: ...`). Queued outreach is cancelled. **Resuming is manual on purpose:** find out why before you click *Automation paused* to resume. Sending straight through a flood warning is how numbers get banned.

## What it does with a message

- It handles **private messages only**. Group and channel traffic is ignored. Messages from **bot accounts are included**, so conversations that run through a bot's interface are handled like any other DM.
- Every message is stored in Postgres and pushed live to any open panel tab.
- For each incoming text message it checks the global pause, the per-chat pause and the active hours, in that order. If any says stop, no draft is made.
- Otherwise it waits the random delay, builds the last ~30 messages of the chat into the prompt under your persona, and asks DeepSeek for a reply. With auto-send off, the draft is saved as pending and appears in the panel. With auto-send on, it is sent.
- **If a newer message arrives while a draft is still being prepared, that draft is cancelled and restarted**, so the reply always answers the latest state of the conversation. Sending a message yourself from the panel also cancels any draft in progress for that chat.
- Messages you send from your phone or Telegram Desktop also appear in the panel, so the thread stays complete.
- DeepSeek failures (network, timeout, 429, malformed response) are retried with backoff that honours `Retry-After`, then shown as a red error in the conversation. A bad key (401/403) fails at once. Telegram disconnects are reconnected automatically with backoff.

## Other features

All of these are per account, under the buttons in the top bar.

**Sounding human** (Settings, all on by default). *Adaptive style* profiles
how each person writes (length, emoji, capitalisation, language) from their
own messages and tells the model to match it. It needs at least two of their
messages. *Mark read* marks their message read after the delay. The *typing
indicator* shows "typing…" for as long as the text would plausibly take:
length ÷ 12 characters/second, capped at 25 s. Replies you send or approve by
hand skip the typing wait. *Presence* keeps the account offline between
conversations and online only around replying.

**Outreach** starts conversations with people in the account's Telegram
contacts. You say what each message should achieve, and each person gets one
written for them. The server re-checks that every recipient is a contact.
Messages wait for approval by default and go out one at a time, 90–300 s
apart, with at most 20 per day. Someone who already has a queued message is
skipped. *Cancel queued* and the global pause both stop it. Use it only for
people who expect to hear from you: Telegram limits or bans accounts that send
unsolicited DMs.

**Bookings** (Settings → Bookings, off by default). When a client settles on a
day and time, a request goes to a *provider* account (a person or a bot), who
replies `YES <n>` or `NO <n>`. The client is then told through the normal
reply flow. You can set a check-in reminder before the slot (default 120 min).
The *arrival instructions* (address, door code) are sent word for word, once,
when the client says they have arrived. Detection costs one short DeepSeek
call per incoming message while it is on. Bookings are stored in
`./data/<account-id>/bookings.json`.

*Google Calendar mirror (optional):* create a Google Cloud service account
with the Calendar API enabled and download its JSON key. Put the key at
`./data/<account-id>/google-service-account.json`, or put it anywhere under
`./data` and set `GOOGLE_SERVICE_ACCOUNT_FILE=/app/data/<file>.json` in `.env`.
That path is the one inside the container. Then recreate with
`docker compose up -d`. Share the calendar with the service account's email
address with *Make changes to events*, and paste the calendar ID into
Settings. Calendar failures are reported in the thread and never stop the
Telegram side.

**Media.** Upload photos and videos the assistant may send, or copy them into
`./data/<account-id>/media/`, and give each one a short description. The AI
uses the description to pick the right file when someone asks. By default a
video is only offered first and sent once the person says yes, and a reply
carrying a video always waits for approval, even with auto-send on.

**Style** holds writing samples and per-contact overrides: extra persona
notes, delays and typing speed for one person.

## Operations

```bash
docker compose logs -f manager          # what the accounts are doing
docker compose logs -f panel            # panel / API errors
docker compose restart manager          # restart all accounts; leases are released, or expire within 30 s
git pull && docker compose up -d --build  # update; migrate runs before panel/manager start
```

**Capacity.** One manager runs `WORKER_COUNT × SESSIONS_PER_WORKER` accounts
(2 × 25 = 50 by default). Accounts beyond that stay idle until a slot frees
up. Change both in `.env` and run `docker compose up -d`.

**Backups.** Everything that matters is in three places: the Postgres volume,
`./data` (media, bookings, the last halt reason for each account), and `.env`.
To dump the database:

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > userbot-$(date +%F).sql
```

**Taking an account out of rotation.** The panel has no stop button yet. Mark
the account inactive in the database, then restart the manager:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
#   UPDATE telegram_sessions SET is_active = false WHERE session_id = 'tg34600123456';
#   \q
docker compose restart manager
```

To bring the account back, set `is_active = true` again. A worker picks it up
within ~15 s. Signing the number in again from the panel also re-activates
it.

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
passed) and the rest still run. Redis is replaced by `fakeredis`, so no Redis
is needed.

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

- `Skipping — needs login`: the account has no usable Telegram login or no DeepSeek key. Sign it in again.
- `Failed to start` followed by a traceback: the error is in the traceback. The worker retries that account after 5 minutes. `docker compose restart manager` retries it now.
- Nothing about it at all: all worker slots may be full (see *Capacity*).

**Account dot is red**: a worker holds the account but it isn't connected,
halted, or needs a new login. The reason is in the manager logs. If the
session was ended from Telegram (Settings → Devices), the log says so. Run
`docker compose restart manager` so the worker releases the account, then sign
the number in again with **+ Add account**. After a halt, the dot can stay red
even after you resume. `docker compose restart manager` clears it.

**Drafts appear but nothing is sent**: auto-send is off, which is the
default. Approve drafts by hand, or turn on *Auto-send* in Settings.

**No reply at all**: first check that the account isn't paused (top bar and
conversation), that you are inside active hours if they are on, and that the
message was private and had text. Then check `docker compose logs manager` for
`DeepSeek` errors (bad key, no balance, rate limit) or other errors for that
account. DeepSeek errors also show up red in the conversation.

**"Automation halted"**: a Telegram flood limit tripped (`PeerFloodError`, or
a FloodWait longer than *Max wait*) or the session was rejected. Read the
reason in the panel or the manager logs, wait and work out what caused it,
then resume by clicking *Automation paused*.

## Legacy files

`main.py`, `start.bat`, `setup_session.py`, `instances.py` and `env_file.py`
are the old single-account mode: one process per account, SQLite, and
credentials in `.env`. The fleet stack superseded them (design decision D9),
and nothing in it imports or runs them. The same goes for
`deploy/telegram-assistant.service` (a systemd unit that runs `main.py`) and
`config.example.json` (settings now live in Postgres). They are kept for
reference only. Don't use them to deploy.

## Files

```
docker-compose.yml     the stack: postgres, redis, migrate, panel, manager, optional caddy
Dockerfile             one image for panel, manager and migrate
Caddyfile              optional public HTTPS front door (profile "public")
panel.py               admin panel + API; control plane, no Telegram connections
manager.py             spawns worker processes, restarts dead ones, adopts new accounts
session_runtime.py     one Telegram account: client, drafting, sending, safety, outreach, bookings
login_flow.py          phone -> code -> 2FA sign-in behind "Add account"
leasing.py             one-worker-per-account leases in Postgres
commands.py            Redis command bus (panel -> worker) and live events (worker -> panel)
database.py            Postgres access: SessionRegistry (accounts) and per-account Database
config_store.py        per-account settings, defaults and validation
crypto.py              AES-GCM encryption of stored secrets under USERBOT_MASTER_KEY
device_profiles.py     stable per-account device identity
pg.py                  connection pool and migration runner
migrate_entrypoint.py  the one-shot `migrate` service
migrations/            numbered SQL migrations
ai_responder.py        builds the prompt, calls DeepSeek
context_link.py        borrows context from a linked chat of the same person
bookings.py            appointment requests and the provider's YES/NO
media.py               the photo/video library the AI may attach
google_calendar.py     optional Google Calendar mirror for bookings
static/index.html      the panel UI: plain HTML/CSS/JS, no build step
tests/                 pytest suite (see "Running the tests")
data/                  created at runtime: per-account media/ and bookings.json
```

## A note on userbots

Automating a personal account is against Telegram's Terms of Service and can
get the account limited or banned. Keep the delays human, keep the safety
limits low, and prefer approval mode over auto-send.
