# Runbook: when something goes wrong

Every command runs on the server, in `~/userbot-scale/telegram_admin_bot`, unless it says otherwise. Nothing here needs a secret. If you send me output, these commands are safe to paste: they never print passwords or keys.

## First look, always

```bash
docker compose ps -a                          # what is up, what exited
docker compose logs --tail 100 panel manager scheduler caddy
docker compose logs --tail 100 wa-gateway     # only if you run WhatsApp accounts
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
| Client says "waiting for approval" | They signed up themselves; nobody approved them yet | ☰ → Client logins → *Waiting*: approve (and tick their business) or reject with a reason. A manager can approve too, but only you can link a business |
| Client is stuck on the terms screen | A new terms version that requires acceptance was published | Expected: they must tick and accept it. ☰ → Terms shows how many have not accepted yet |
| Nobody can sign up / no "Create an account" link | Sign-up is closed (the default), or no terms are published | ☰ → Terms: publish the terms (every `[[FILL IN` must be written first), then *Open sign-up* |
| Sign-up says "try again later" | 50 sign-ups already wait for approval (spam guard) | Approve or reject the waiting ones |
| Manager can't log in to `/manager/` | Disabled, wrong password, 5 failures, or lost the authenticator | ☰ → Managers: enable, *Reset password…* or *Remove 2FA* (they set up a new app at the next sign-in) |
| A manager did something wrong | | Every manager action is in the audit log as `manager:<username>` (☰ → Clients → a client → Audit). ☰ → Managers → *Disable* ends their sessions at once |

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

## A WhatsApp account

Background, and what each reason means: `telegram_admin_bot/README.md`, section *WhatsApp accounts*. When a number drops, work through [WhatsApp session dropped](#whatsapp-session-dropped) below.

| Symptom | Cause | Fix |
|---|---|---|
| Pairing says *"The WhatsApp gateway is not running"*; manager log says `The WhatsApp gateway is not answering; retrying every 15s.`; every WhatsApp account is red | `wa-gateway` is down, restarting, or can't reach Valkey | `docker compose ps -a wa-gateway`; `docker compose logs --tail 50 wa-gateway`; `docker compose up -d wa-gateway`. Accounts reconnect within ~15 s, no re-pair. Exit code 2 / "refusing to boot without a master key": `USERBOT_MASTER_KEY` is missing. Exit code 3 / "another wa-gateway holds the singleton advisory lock": a second gateway uses the same Postgres. Find it (`docker ps -a \| grep wa-gateway`, e.g. a leftover `docker compose run wa-gateway …`) and remove it |
| Pairing refused: *"already running … Stop it before pairing it again"* | The number has a live lease and is healthy: it runs | Take it out of rotation (the `UPDATE telegram_sessions SET is_active = false …` in the README, *Operations*), wait 10 s for the grey dot, pair again |
| Pairing refused: *"lost its WhatsApp session and is being stopped; try again in half a minute"* | The account was halted by a session loss; pairing again deactivated it, but its runtime had not let go of the lease yet | Wait half a minute and pair again. If it keeps saying so, `docker compose logs manager` for that account |
| Pairing ends *failed* or *expired* | The QR was not scanned in time, the phone was offline, or the pairing code was asked for another number | Start again. For a pairing code, the number must be the phone's own, with country code. `docker compose logs --tail 30 wa-gateway \| grep -i pair` shows the reason |
| **Session lost**: `loggedOut`, `badSession`, `multideviceMismatch` | The linked device is gone: removed on the phone, corrupt, or out of step | [WhatsApp session dropped](#whatsapp-session-dropped), then pair again and resume the `whatsapp` hold |
| **Session lost: `forbidden`** (state `revoked`) | WhatsApp refused the account: **likely banned** | **Stop. Do not re-pair.** Check WhatsApp on the phone for a ban notice. Keep the account out of rotation until you know what triggered it |
| **Session lost: `connectionReplaced`** | Another WhatsApp Web session took this login: a second copy of the stack (old server, restored backup) or the manual-test CLI | Find and stop the other copy **first**, or the two keep knocking each other off (which also looks like a bot). Then pair again |
| Halted: *"WhatsApp returned a rate limit (rate-overlimit)"*; manager log `did not deliver message … (rate_limited, code 463)` | 463: WhatsApp has **restricted the account** (no new chats; existing chats still work). Without code 463 it is a plain rate limit | **Do not resume at once.** Check WhatsApp on the phone for a restriction notice. Lower `daily_message_cap`, `hourly_message_cap`, `safety.daily_peer_cap`; wait until the restriction is gone; then Safety → Resume the `whatsapp` hold |
| A chat paused with *"Cannot message this person (blocked / not on WhatsApp)"* | The person blocked the number, or the number isn't on WhatsApp | Nothing to fix on the account. Unpause the chat only if you know it was a mistake |
| A message red in the thread, *"WhatsApp did not deliver a message (other, code …)"* | WhatsApp refused it after it went out; the code is in the manager log | Send it again by hand if it matters. If many chats show it, *Soft-off* the client and check the phone before anything else goes out |
| wa-gateway log: `inbox backlog: postgres has been refusing inserts; messages are held in memory, none dropped` | Postgres is down or full; 5000 or more received messages for one account wait in the gateway's memory | Fix Postgres (see *Postgres and Valkey*: usually the disk). **Do not restart `wa-gateway` meanwhile**: those messages exist only in its memory. Once Postgres answers they are written and the accounts pick them up |
| wa-gateway log: `watchdog: postgres unreachable`, then every WhatsApp account not connected | Without Postgres no lease can be proven, so after 22 s the gateway closes every socket | Fix Postgres. The accounts reopen within ~15 s after; messages sent to them meanwhile are delivered on reconnect |
| The old server and the new one both have the WhatsApp number | Both hold the same linked-device keys (`connectionReplaced` on both) | On the old one: `docker compose stop manager wa-gateway`. If the session was lost already, pair again on the new one |

## WhatsApp session dropped

A WhatsApp account went red, Safety says *stopped after a WhatsApp error*, or an alert says *WhatsApp session lost*. Don't pair again until you have been through this.

**1. Read the logs.**

```bash
docker compose logs --since 24h manager | grep -E "HALTING ALL AUTOMATION|WhatsApp" | tail -20
docker compose logs --since 24h wa-gateway | grep -E "SESSION LOST|HARD-OFF|connection closed" | tail -20
```

- `SESSION LOST wa34600123456: forbidden (403)` (wa-gateway): WhatsApp ended the linked device for good. The word after the colon is the reason. The gateway never reconnects it.
- `[wa34600123456] HALTING ALL AUTOMATION: WhatsApp session lost (forbidden): …` (manager): the account reacted. It has a `whatsapp` hold, an alert is open, the stored login is **deleted**, and the state is `needs_login` (`revoked` for `forbidden`). Nothing reconnects it by itself.
- `HALTING ALL AUTOMATION: WhatsApp returned a rate limit …` is not a session loss: it is a rate limit or a 463 restriction (see the table above). The session still works; don't re-pair.
- `connection closed; reconnecting` on its own is a normal short drop; the gateway reconnects with backoff. Only a `SESSION LOST` line means the device is gone.

**2. Look at the phone.** WhatsApp → Settings (Android: ⋮) → **Linked devices**.

- Is the server's device (a desktop browser, e.g. *Chrome (Mac OS)*) still listed? After a loss it normally isn't.
- Is anything there nobody recognises? Log it out.
- Does WhatsApp show a ban or restriction notice?
- Did the owner remove the device, or log out of all linked devices?

**3. Decide by reason.**

| Reason | Pair again? |
|---|---|
| `forbidden` | **No, not now.** The number is likely banned. Leave the account out of rotation, look at what it sent (volume, new chats, complaints), and only consider pairing again once WhatsApp works normally on the phone and you know what caused it |
| `connectionReplaced` | **Not until the other copy is stopped.** Look for an old server or a restored backup still running this stack, and for a leftover gateway container: `docker ps -a \| grep wa-gateway`. Pairing again with the other copy alive just repeats the loss |
| `loggedOut` | Only if nobody removed the device on purpose. If the owner did, ask why first |
| `badSession`, `multideviceMismatch` | Yes |
| The number is lost again within hours of a re-pair | **No.** Stop and find the cause; linking again and again is itself a ban signal |

**4. Pair again.** Panel: **+ Add account → WhatsApp**, the same number, DeepSeek key blank (the stored one is kept). History and settings stay. The halted runtime still holds the lease (that is why the dot is red): pairing again deactivates the account and waits up to about 25 s for that runtime to let go before it shows the QR or code. If it says *"try again in half a minute"*, do that. Once linked, the account becomes active and a worker picks it up within about 15 s: `docker compose logs -f manager` shows `Started (picked up while running)` and `WhatsApp connected as …`, and the dot turns green.

**5. Resume.** It still sends nothing on its own: **Safety → the client → Resume** the `whatsapp` hold, once you are happy with the cause.

## WhatsApp: messages and replies

Replace `wa34600123456` with the account id from the picker. The gateway logs one JSON line per event; `grep` works on the message text.

| Symptom | Cause | Fix |
|---|---|---|
| A customer writes, and nothing shows in the panel | Either the gateway doesn't receive the message, or the account doesn't take it out of the inbox | Look for the message in the gateway log: `docker compose logs --since 1h wa-gateway \| grep -E "inbound message\|connection (open\|closed)\|wa_inbox insert failed" \| tail -20`. <br>**No `inbound message` line:** the socket isn't connected (see the dot and Safety), or the phone has been offline too long (WhatsApp unlinks devices after about 14 days without the phone online; that shows as `loggedOut`). <br>**An `inbound message` line but nothing in the panel:** check the inbox (next row) |
| Messages pile up in the inbox | The account's runtime isn't running, or can't store them | `docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT session_id, count(*), min(created_at) FROM wa_inbox GROUP BY 1"'`. A count that stays above 0 for more than a minute means the account isn't draining: `docker compose logs --tail 50 manager \| grep wa34600123456`. The runtime re-reads the inbox every 15 s, so a lost Valkey nudge delays a message by at most that |
| **Duplicate replies** to one message | Two copies are running the same number. The lease makes that impossible inside one stack, so it is almost always a second server, a restored copy, or a leftover `docker compose run` | Stop sending first: **Safety → Stop everything…**. Then look for the other copy: `docker ps -a \| grep -E "manager\|wa-gateway"` here, and on the old server. Both copies usually also log `SESSION LOST … connectionReplaced`. Keep one, then pair again on that one ([WhatsApp session dropped](#whatsapp-session-dropped)). Each inbound message is stored once (its WhatsApp id is unique), so duplicates mean a second stack, not a retry here |
| Replies are written but never sent | Approval mode (the default for a new WhatsApp number), a hold, or the gateway not answering | Drafts waiting in the thread: approve them, or turn on `auto_send` in Config. A red chip: read the hold in Safety. A red *"The WhatsApp gateway is not answering"* line in the thread: see the first row of *A WhatsApp account* |
| A sent message turns red | The gateway or WhatsApp refused it. Nothing is retried by itself, so nothing is sent twice | Read the red line: *not on WhatsApp / blocked* pauses that chat; a code (`463`, `rate-overlimit`) is a restriction (next row). Send it again by hand only if it matters |
| **The number may be restricted or banned** | WhatsApp is pushing back on the number | Signs, in order of severity: <br>- many chats paused as *blocked / not on WhatsApp* in one day; <br>- `463` or `rate-overlimit` in the manager log (the account halts itself); <br>- *Session lost: `forbidden`*. <br>Do this: **Soft-off** the client, check the phone for a WhatsApp notice, lower `daily_message_cap`, `hourly_message_cap` and `safety.daily_peer_cap`, and keep `auto_send` off. Don't pair again or resume until WhatsApp works normally on the phone |
| `wa-gateway` keeps restarting (`docker compose ps` shows *Restarting*) | It exits on a fatal start error | `docker compose logs --tail 40 wa-gateway`. <br>- *"refusing to boot without a master key"*: `USERBOT_MASTER_KEY` missing from `.env`. <br>- *"another wa-gateway holds the singleton advisory lock"*: a second gateway uses this Postgres; remove it. <br>- *"wa-gateway failed to start"* with a connection error: Postgres or Valkey is down (fix those first). <br>- Anything else: send me the last 40 lines. <br>WhatsApp numbers stay linked meanwhile; they reconnect without a re-pair once it starts |
| `docker compose ps` shows `wa-gateway` *unhealthy* | The healthcheck asks Valkey whether the gateway is listening on its command channel. It isn't: it hasn't finished starting (master key, singleton lock, Valkey), or it lost Valkey | `docker compose logs --tail 40 wa-gateway`, and the crash-loop row above. A gateway that is *healthy* but answers nothing (a frozen process) is not caught by this check: `docker compose restart wa-gateway`, no re-pair needed |
| wa-gateway log: `MESSAGE LOST wa34600123456` | Postgres refused one received message five times for a reason that isn't an outage (e.g. the account row is gone). It is dropped so the messages after it still arrive. The log line has the message id and the sender, never the text | Rare. Tell the customer you may have missed a message if the sender matters. If it repeats, send me the lines around it: `docker compose logs --since 1h wa-gateway \| grep -B3 "MESSAGE LOST"` |
| `manager` is down, `wa-gateway` is up | Nothing renews the leases | After about 30 s the gateway closes those numbers' sockets by itself (its watchdog), so they never run unsupervised. `docker compose up -d manager`; they reopen within ~15 s, and messages sent meanwhile arrive on reconnect |
| Valkey is down | No commands or events: nothing can be sent, pairing fails, the panel shows no live updates. Received messages still reach Postgres | `docker compose restart valkey`, then `docker compose restart panel manager scheduler wa-gateway` |

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

**WhatsApp numbers after a restore.** Their linked-device keys are in the database, so a restore puts back the keys as they were at backup time. Those keys may be out of date by now.

- **Never run the restored stack while the old one is still up.** Both would use one linked device, both get `connectionReplaced`, and customers may get two replies. Stop the old one first: `docker compose stop manager wa-gateway`.
- A number that comes back as `badSession` or `loggedOut`, or whose gateway log shows decryption errors for most messages: pair it again (*WhatsApp session dropped*, step 4). History and settings are kept.
- Telegram accounts are unaffected.

## Roll back a bad update

```bash
git log --oneline -5                      # find the previous commit
git checkout <previous commit>
docker compose up -d --build
```

Migrations are not rolled back; the code before an update tolerates newer tables, but if `pg.assert_version` complains, come back to the newer commit and send me the log.

**Rolling back to a version without WhatsApp.**
1. Run `docker compose stop wa-gateway` first. The older compose file doesn't know the service, so it would keep running on its own.
2. WhatsApp accounts stay in the database. The older manager skips them as *needs login*, so they are offline while you are rolled back.
3. Rolling forward again brings them back without a re-pair, as long as the gateway wasn't stopped for more than about 14 days.

## Emergency: stop everything from sending

- Panel: **☰ → Safety → Stop everything…**
- Server, if the panel is unreachable: `docker compose exec panel python controls.py stop "reason"`, later `... resume`
- Nuclear: `docker compose stop manager` (accounts go offline; messages still arrive on the phones). `docker compose stop wa-gateway` also closes every WhatsApp socket at once; `docker compose start wa-gateway` brings them back without a re-pair

## Emergency: a Telegram account is hijacked

1. **Safety → the client → Revoke the session** (hard-off). This logs out this server's session and deletes its key.
2. Have the owner open Telegram → Settings → Devices → *Terminate all other sessions* and change their 2-step password.
3. Sign the account in again from the panel when it's clean.

For a WhatsApp number: Hard-off unlinks this server's device. Have the owner open WhatsApp → Settings → **Linked devices** and log out every device they don't recognise, then pair again from the panel.

## Emergency: the server itself is compromised

1. Snapshot it at the provider (evidence), then destroy it.
2. New server: DEPLOY_TODAY.md with a **new** `.env` (new master key and passwords).
3. Sign every Telegram account in again; revoke the old sessions from the phones (Settings → Devices). For every WhatsApp number: remove the old server's device under Linked devices on the phone, then pair again.
4. Rotate the DeepSeek keys (platform.deepseek.com) and any SMTP/vision keys; re-enter them.
5. Old backups still open with your age key; restore only the database and `data/`, never the old `.env`.

## Capacity

| Symptom | Fix |
|---|---|
| Accounts beyond `WORKER_COUNT × SESSIONS_PER_WORKER` (2×25) stay idle | Raise them in `.env`, `docker compose up -d manager` |
| Server slow, OOM kills in `dmesg` | 2 GB swap is added by the bootstrap; upgrade the VPS RAM past 2 GB for more than a few accounts |
| Disk filling | Logs are capped (10 MB × 3 per container). Check `backups/` and `docker system df` |
