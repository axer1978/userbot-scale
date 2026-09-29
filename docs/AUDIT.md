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
