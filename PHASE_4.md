# Phase 4: client-facing, public HTTPS, phones and tablets

Branch `platform/phase-1`. Tests: **924 passed** against Postgres 16 (846 after phase 3), plus a jsdom click-through of every new screen and of the client dashboard (19 checks, no script errors). To deploy on the new server, follow [DEPLOY_TODAY.md](DEPLOY_TODAY.md).

## Done

| Item | Where |
|---|---|
| **Client master logins** (one login, one or more businesses) | `owner_auth.py`, `owner_api.py`, `owner_admin_api.py`; admin screen **☰ → Client logins**. <br>Passwords are hashed with scrypt. Sessions are stored server-side as hashes only, in a Secure, HttpOnly, SameSite=Strict cookie. <br>The first login forces a password change. Failed logins are limited per address and per username. The client can turn on an authenticator code. <br>A client only ever sees the businesses linked to their login; tests prove A can't reach B, and that client and admin logins never open each other's routes |
| **Client dashboard** at `/owner/` | Mobile-first. <br>- today's bookings and the next 7 days; <br>- this week: bookings, no-show rate, messages received and answered by the bot or by hand; <br>- an 8-week chart; <br>- the unanswered list, where the client can mark items reviewed; <br>- a switcher and an "all my businesses" summary for clients with several bots; <br>- a flagged-customers slot, which stays empty until phase 5. <br>Read-only apart from marking reviewed |
| **Unanswered queue** | `unanswered.py`, `unanswered_api.py`, **☰ → Unanswered**. <br>Filled in code whenever a customer message gets no reply: <br>- skipped by a reply limit or your no-reply instruction; <br>- an AI error; <br>- soft-off; <br>- a paused or taken-over chat; <br>- escalated; <br>- held by policy; <br>- staging; <br>- or the reply contains one of your `unanswered.fallback_phrases`. <br>Bare "ok"/"thanks" are not queued. You can mark items reviewed, reopen them, or **promote** one: you type the answer, and it is added to the industry template's FAQ as a new, audited version |
| **Weekly digest** | `digest.py` on the scheduler. <br>It is sent on `digest.weekday` at `digest.hour` in the client's time (default Monday 09:00): last week's numbers, to the owner's Telegram and to e-mail when SMTP is set. At most once per week (`digest_log`); an alert if neither channel works |
| **Staging mode** | `staging.enabled` + `staging.test_chats`: only the test chats are answered. Everyone else is stored, noted in the chat and put in the unanswered queue. Escalation still works |
| **Onboarding wizard** | **☰ → New client**, steps: <br>1. sign in the Telegram account; <br>2. business name and industry; <br>3. key settings; <br>4. staging with test chats; <br>5. check the prompt, read only; <br>6. **Go live**. <br>It can be resumed: it shows which steps are done |
| **Review batches** | `review_api.py`, **☰ → Review**. <br>A batch covers a client and a date range: every reply the bot sent, with up to 20 earlier messages of context. Approve, reject or edit each one, with big buttons for a tablet. Export as JSONL (approved and edited only, the edited text wins) |
| **Phones and tablets** | Admin panel: <br>- the top bar folds into **☰ Menu**; <br>- the chat list and the chat take turns, with **‹ Chats** to go back; <br>- overlays and sheets go full screen; <br>- buttons are finger-sized; <br>- 16 px inputs, so iOS doesn't zoom. <br>The client dashboard is built for phones |
| **Public HTTPS and protection layers** | See the table at the end of DEPLOY_TODAY.md. <br>- One Caddy serves the panel and, when set, the booking pages: TLS 1.2/1.3 and HSTS for 2 years. <br>- The panel sends a strict CSP (no inline scripts), no framing, no referrer, and no-store on the API. <br>- It **refuses to go public** without the admin authenticator code and a password of 14+ characters. <br>- The server is firewalled to 22/80/443, SSH is key-only, fail2ban and automatic updates are on. <br>- Backups are encrypted with `age` to your own key |
| **Deploy scripts** | `deploy/bootstrap_ubuntu24.sh` and `deploy/backup.sh`, with DEPLOY_TODAY.md |

## What changes for the live account

1. It moves to the new server as a **fresh sign-in**. Stop the manager and scheduler on AWS first (DEPLOY_TODAY.md, step 7), or both servers will answer.
2. **The weekly digest is on by default.** The owner gets a Telegram message every Monday at 09:00, client's time. Set `digest.enabled: false` to stop it.
3. The unanswered queue starts filling. Nothing is sent because of it.
4. Staging is off by default. The wizard turns it on for a new client until you press Go live.

## Not tested

- **The real server.** Nothing has been deployed yet:
  - Let's Encrypt;
  - Caddy with the new config, including `tls` and the combined panel + booking file;
  - the bootstrap script on Ubuntu 24.04 (it was only syntax-checked);
  - the backup script.
- **Real phones.** Layout was checked with jsdom (functional clicks) and, for the client page, in headless Edge at 360 and 430 px. The admin panel has not been seen on a real phone.
- **Real Telegram** for the digest and staging.
- **Authenticator codes after a restart.** Used codes and a 2FA setup in progress are kept in memory. After a panel restart, a code already used could work once more within about 90 seconds, and a half-finished 2FA setup has to start again.

## Open decisions for you

1. **Weekly digest on by default** for every client. Keep it, or off until you turn it on per client?
2. **Clients can only view** and mark messages reviewed. Should they also be able to pause their bot, or answer booking requests from the dashboard?
3. **Unanswered "promote" goes into the industry template**, so every client in that industry gets it. Should it go into the client's own FAQ instead?
4. Still open from phase 3: alerts on Telegram, hard-off scope, whether panel messages count as a takeover, whether a new login suspends.
