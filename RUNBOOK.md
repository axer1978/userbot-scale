# Runbook: when something goes wrong

Every command runs on the server, in `~/userbot-scale/telegram_admin_bot`, unless it says otherwise. Nothing here needs a secret. If you send me output, these commands are safe to paste: they never print passwords or keys.

## First look, always

```bash
docker compose ps -a                          # what is up, what exited
docker compose logs --tail 100 panel manager scheduler caddy
sudo ss -tlnp | grep -E ':(80|443|8787) '     # who listens
df -h / && free -m                            # disk and memory
```

In the panel, **☰ Menu → Safety** shows every client's account health, the holds that switch it off, open alerts and whether the scheduler is alive.

## The panel

| Symptom | Cause | Fix |
|---|---|---|
| Browser says the site can't be reached | Caddy is down, the firewall blocks 80/443, or DNS/IP is wrong | `docker compose ps caddy`; `sudo ufw status`; `curl -sI http://127.0.0.1:80` from the server. For sslip.io, the name must be the server's public IP with dashes |
| Certificate error / "not secure" | Let's Encrypt hasn't issued yet, or hit its rate limit (sslip.io names share it) | `docker compose logs caddy \| grep -iE "obtain\|rate\|error" \| tail`. Wait, then `docker compose restart caddy`. If the rate limit is named, wait an hour; it clears by itself |
| Panel container keeps restarting | It refuses to start publicly without `ADMIN_TOTP_SECRET` or with a short password | `docker compose logs panel \| tail -20` says which. Fix `.env`, then `docker compose up -d panel` |
| "Wrong password or code" although both are right | The server's clock is off (codes are time-based), or the code was already used | `timedatectl` should say *synchronized: yes*; `sudo timedatectl set-ntp true`. Wait 30 s for a new code |
| "Too many wrong passwords" | Five failures from your address in 15 min | Wait 15 min, or `docker compose restart panel` (clears the counters and logs everyone out) |
| Locked out of the admin: lost the authenticator | Only the secret in `.env` can make codes | `grep ADMIN_TOTP_SECRET .env` and enter that key again in a new authenticator entry. To replace it: `docker compose exec panel python totp.py`, put the new value in `.env`, `docker compose up -d panel` |
| Lost the admin password | It is only in `.env` | `grep ADMIN_PASSWORD .env`. To change: edit `.env`, `docker compose up -d panel` |
| Panel loads but shows "connecting…" forever | The websocket isn't reaching the panel (proxy), or Valkey is down | `docker compose ps valkey`; `docker compose logs --tail 20 valkey panel`. `docker compose restart valkey panel` |
| Client can't log in to `/owner/` | Disabled login, wrong username, or 5 failures | ☰ → Client logins: enable, or *Reset password…* (kills their sessions, forces a new password) |
| Client lost their authenticator | | ☰ → Client logins → *Remove 2FA* |

## The Telegram account

| Symptom | Cause | Fix |
|---|---|---|
| Red dot / Safety says **not running** | The manager is down or all worker slots are full | `docker compose ps manager`; `docker compose logs --tail 50 manager`; `docker compose restart manager` |
| **not connected** | Telegram unreachable from the server, or the account is mid-reconnect | Manager log shows the reason; it reconnects by itself with backoff. If it persists for 10 min, `docker compose restart manager` |
| **logged out** / "Sign in again" | The session was ended from the phone (Settings → Devices) or by Telegram | ☰ → New client, or **+ Add account**: sign the number in again. Nothing else is lost |
| **rate-limited** | Telegram asked for a long wait | Leave it. Reduce `daily_message_cap` / delays in the client's Config if it repeats |
| Bot answers nothing (red chip "Sending off: …") | A hold: paused, billing, AI limit, anomaly, Telegram error, or the global stop | ☰ → Safety → the client → read the reason → **Resume** that hold (AI limit lifts itself when the period rolls over or the cap is raised) |
| Bot answers nothing, no chip | Chat paused / escalated / taken over, quiet hours, staging on, no DeepSeek balance | Look at the chat's badges and the notes in the thread; `docker compose logs manager \| grep -i deepseek \| tail`; check `staging.enabled` in Config |
| "Stopped after a Telegram error: PeerFloodError" | Telegram thinks the account spams | **Do not resume at once.** Lower `daily_message_cap`, `safety.daily_peer_cap`, raise delays; wait several hours; then Resume in Safety |
| Account switched off by "a new Telegram login" | Someone (maybe the owner) signed in on a new device | Confirm with the owner. If it was them: Safety → Resume the anomaly hold. If not: **Hard-off** (Safety → Revoke the session) and have the owner end the other device under Telegram → Settings → Devices, then sign in again |
| Both the old AWS server and the new one answer customers | Both run the same number | On AWS: `docker compose stop manager scheduler`. Telegram may also have logged one of them out; sign in again on the new server |
| Owner's YES/NO to bookings is ignored | `booking.provider` not set or not resolvable, or the owner's chat is paused | Config → `booking.provider` = the owner's @username or numeric id; the account must have chatted with them once |

## The scheduler (reminders, digests, billing, health alerts)

| Symptom | Fix |
|---|---|
| Red banner "The scheduler is not running" | `docker compose ps scheduler`; `docker compose logs --tail 30 scheduler`; `docker compose restart scheduler`. Only one runs at a time (a Postgres lock); a second copy just waits |
| Reminders not sent | Scheduler up? Client soft-off? Chat paused/taken over? Quiet hours (reminders wait for them)? The note in the chat says which |
| Digest never arrives | `digest.enabled`, `digest.weekday/hour` in the client's timezone; `booking.provider` reachable; e-mail needs `SMTP_*` + `digest.email` or `booking.owner_email` |

## Postgres and Valkey

| Symptom | Fix |
|---|---|
| Everything is down, `postgres` unhealthy | `docker compose logs --tail 50 postgres`. Disk full is the usual cause: `df -h`; delete old backups in `backups/`, `docker system prune -f`. Then `docker compose up -d` |
| "password authentication failed" after editing `.env` | `POSTGRES_PASSWORD` is fixed when the volume was created. Put the old value back. (Changing it for real: `docker compose exec postgres psql -U userbot -c "ALTER USER userbot PASSWORD '<new>'"` then update `.env`) |
| Valkey down | Nothing is lost (it only carries live commands/events). `docker compose restart valkey`, then `docker compose restart panel manager scheduler` so they reconnect |
| Migration failed on start (`migrate` exited non-zero) | `docker compose logs migrate`. Send me that output. Nothing is half-applied: each migration is one transaction |

## Restore from a backup

Backups are `backups/userbot-<date>.tar.age`, encrypted to your own age key (`deploy/backup.sh`).

```bash
docker compose down                       # keeps the volumes
age -d -i my-backup-key.txt backups/userbot-<date>.tar.age | tar x -C /tmp
docker compose up -d postgres
gunzip -c /tmp/database.sql.gz | docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
tar -xf /tmp/files.tar                    # restores data/ and .env into the current folder
docker compose up -d
```

For a brand-new server: do DEPLOY_TODAY.md steps 2–3, restore `.env` from the backup **before** the first `docker compose up`, then the rest above.

## Roll back a bad update

```bash
git log --oneline -5                      # find the previous commit
git checkout <previous commit>
docker compose up -d --build
```

Migrations are not rolled back; the code before an update tolerates newer tables, but if `pg.assert_version` complains, come back to the newer commit and send me the log.

## Emergency: stop everything from sending

- Panel: **☰ → Safety → Stop everything…**
- Server, if the panel is unreachable: `docker compose exec panel python controls.py stop "reason"`, later `... resume`
- Nuclear: `docker compose stop manager` (accounts go offline; messages still arrive on the phones)

## Emergency: a Telegram account is hijacked

1. **Safety → the client → Revoke the session** (hard-off). This logs out this server's session and deletes its key.
2. Have the owner open Telegram → Settings → Devices → *Terminate all other sessions* and change their 2-step password.
3. Sign the account in again from the panel when it's clean.

## Emergency: the server itself is compromised

1. Snapshot it at the provider (evidence), then destroy it.
2. New server: DEPLOY_TODAY.md with a **new** `.env` (new master key and passwords).
3. Sign every Telegram account in again; revoke the old sessions from the phones (Settings → Devices).
4. Rotate the DeepSeek keys (platform.deepseek.com) and any SMTP/vision keys; re-enter them.
5. Old backups still open with your age key; restore only the database and `data/`, never the old `.env`.

## Capacity

| Symptom | Fix |
|---|---|
| Accounts beyond `WORKER_COUNT × SESSIONS_PER_WORKER` (2×25) stay idle | Raise them in `.env`, `docker compose up -d manager` |
| Server slow, OOM kills in `dmesg` | 2 GB swap is added by the bootstrap; upgrade the VPS RAM past 2 GB for more than a few accounts |
| Disk filling | Logs are capped (10 MB × 3 per container). Check `backups/` and `docker system df` |
