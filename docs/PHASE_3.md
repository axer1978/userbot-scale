# Phase 3: safety and control

Branch `platform/phase-1` (phase 3 continues on it), local only, not pushed.
Tests: **846 passed** against Postgres 16 (779 after phase 2). See
`ARCHITECTURE.md` → *Safety and control*.

## Done

| Spec item | Where |
|---|---|
| Rate limiting and spend cap per tenant; on cap → soft-off + alert | `daily_message_cap`, the new `hourly_message_cap`, `safety.daily_peer_cap`: messages are held back and you get an alert. `api_spend_cap_eur` / `limits.*`: the client goes **soft-off** with an alert and comes back by itself when the period rolls over or the cap is raised |
| **Soft-off**: stop sending, keep receiving; resume replays nothing | `controls.py`: one *hold* per cause (paused, billing, AI limit, anomaly, Telegram error), each lifted on its own. Checked right before every send, re-read from Postgres, so it works even if the running account never heard about the change. On entering soft-off, waiting drafts and quiet-hours replies are dropped. Reminders that fall due meanwhile are skipped. Messages are stored and never answered later. You can still send by hand from the panel |
| **Hard-off**: revoke the Telethon session | Safety → a client → *Revoke the session* (type the account id to confirm). The running account logs out (Telegram kills the key). If nothing runs it, the panel takes its lease and logs out itself. Then the key is deleted, the account deactivated, audited and alerted |
| **Global stop**: all tenants soft-off, only by me, logged | Safety → All clients (needs a reason), or `docker compose exec panel python controls.py stop "reason"` / `resume` if the panel is down. Admin login only |
| Billing: active → grace (DM to owner, 48h) → suspended; manual override | `billing.py` on the scheduler. The due date is set per client in Safety. The day after it (client's timezone) → grace: the owner gets a message from the client's account (text editable under Safety → Billing notice; retried until sent) and you get an alert. 48 h later → suspended (a soft-off; data kept). *Record payment* or *Override* any time, with a reason |
| Anomaly auto-suspend with alert | `anomaly.py`: **new Telegram login** on the account (checked every 5 min, and at once when Telegram's "new login" message arrives); **send volume** over 5× the account's own average hour (and at least 30); **trip-wire** (a reply with an unknown link, a wallet or an IBAN). Each trigger: soft-off, `anomaly_detected` audit row with the reason, critical alert. A person resumes |
| Health monitor → alert within 5 minutes | `health.py`: the account reports once a minute. The scheduler's watchdog alerts on *not running* / *not connected* (3 min), *logged out*, *rate-limited*, and says "back to normal" on recovery. A dead scheduler shows as a red banner in the panel |
| Escalation keywords → bot pauses that conversation, owner pinged | `escalation_keywords` (word start, any case). The chat is paused with the reason shown (*escalated*); the owner gets a message quoting the customer. If the owner can't be reached, you get an alert |
| Human takeover for `takeover_hours`, resumes automatically | A message typed by hand in a chat (on the phone, or sent from the panel) keeps the bot quiet there for `takeover_hours` (12 by default); the chat header shows until when, with *Hand back to the bot*. The bot's own messages never count |
| Alerts to me | `alerts.py`: stored, listed under **Safety → Alerts** with a count in the top bar. Delivered to `ALERT_EMAIL` (SMTP) and/or `ALERT_WEBHOOK_URL` when set in `.env`. One open alert per cause: a repeat is counted, not re-sent |
| Tests: anomaly triggers | `test_safety_runtime.py` (every trigger through a running account), `test_safety_platform.py` (holds, alerts, billing, watchdog, hard-off, migration), `test_safety_api.py` (panel routes) |

Also fixed, from the known gaps list:
- Deactivating an account (or a hard-off) now stops it within about 10 s, with no manager restart; lease renewal requires `is_active`.
- A session Telegram logged out releases its lease, so the number can be signed in again at once.
- The red dot no longer sticks after resuming a halt (a halt is a hold now, not a session state).
- Messages from Telegram's service account (login codes, "new login" notices) are never answered.

Clicked through in a simulated browser (jsdom) against a real panel and a runtime with Telegram faked, covering:
- every Safety tab, resuming an anomaly, recording a payment, acknowledging alerts, the billing notice;
- the global stop on and off, with the banner and the top-bar chip following;
- Pause all, a takeover badge and *Hand back*, and an escalated chat.

No script errors.

## What changes for the live account when this is deployed

1. **"Pause all" is a soft-off now, and stronger.** Before, it stopped replies only; reminders and owner messages still went out. Now nothing is sent on its own while paused. If the account is paused at deploy time, it stays paused (the migration turns it into a *paused* hold, or a *Telegram error* hold if it was halted).
2. **The spend cap switches the whole client off**, owner messages included, until the month rolls over or you raise it (€10/month by default, as in phase 2).
3. **Human takeover is on (12 h).** When the owner writes in a customer chat on the phone, the bot stays quiet in that chat for 12 hours. Before, it kept answering. `takeover_hours: 0` restores the old behaviour.
4. **Anomalies are on.** The first login check only records the account's current logins; any **later new login, including the owner's own new phone or Telegram Desktop**, switches the client off until you resume it in Safety. `anomaly.new_login_suspend: false` makes it an alert only. A reply with an unknown link or a wallet now also switches the client off (before, it was only held).
5. **Paused and taken-over chats are left alone completely**: no booking detection and no reminders there (before, bookings were still detected in a paused chat).
6. **Billing does nothing until you set a due date** for the client.
7. **Alerts show in the panel only** until you add `ALERT_EMAIL` or `ALERT_WEBHOOK_URL` to `.env`.

## Deploying

The same as before: back up, rehearse the migration on a copy, then deploy. No new service. On the server, in `~/userbot-scale/telegram_admin_bot`:

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > before-phase3-$(date +%F).sql
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" upgrade_check'
docker compose exec -T postgres sh -c 'psql -q -U "$POSTGRES_USER" upgrade_check' < before-phase3-$(date +%F).sql
git fetch && git checkout platform/phase-1 && git pull
docker compose build
docker compose run --rm --no-deps migrate sh -c 'DATABASE_URL="${DATABASE_URL%/*}/upgrade_check" python migrate_entrypoint.py'
docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" upgrade_check'
docker compose up -d --remove-orphans
docker compose logs scheduler | tail -5
```

If phase 1 or 2 is not deployed yet, one rehearsal covers all of them. The migrate job applies every pending migration (0002 to 0004).

Optional, in `.env`, set by you (I don't need the values): `ALERT_EMAIL` (uses the `SMTP_*` settings) and/or `ALERT_WEBHOOK_URL`. Then run `docker compose up -d`.

## Live checklist (your test account, after deploy)

1. Open **Safety**. Both tabs load; the account shows *connected* within a minute or two, and its logins are listed under This client.
2. **Pause all**. The red chip reads "Sending off: paused". Write to the account from another phone: the message shows in the panel, and there is no reply. Resume, and there is still no reply to that message (nothing is replayed). Write again, and it is answered.
3. Set `escalation_keywords` to one test word. Write it from the customer phone: the chat shows *escalated*, and the owner's Telegram gets the message.
4. From the account's own phone, write in a customer chat by hand. The chat header shows "bot quiet until …". The customer writes again and gets no reply. Click *Hand back to the bot*; the next message is answered.
5. **Global stop** with a reason: the banner shows. Lift it.
6. Stop the manager for 5 minutes (`docker compose stop manager`). A *not running* alert appears (and arrives by e-mail or webhook if set). Start it again (`docker compose start manager`); the alert closes and "back to normal" is sent.
7. Optional: set a billing due date of yesterday. Within a minute the client is in grace, and the owner gets the notice. *Record payment* afterwards.

Hard-off is not on the checklist on purpose: it logs the session out for real and needs a fresh login to undo.

## Not tested

- **Real Telegram.** The following were only faked:
  - the login list (`account.getAuthorizations`), including the fact that Telegram reports this server's own session as `current`;
  - `log_out()` for a hard-off;
  - Telegram's "new login" message arriving from 777000.
- **Delivery.** The alert e-mail and webhook were exercised against fakes, not a real SMTP server or a real webhook service.
- **Docker.** The `controls.py` shell command inside the container, and the watchdog's timing in production, have not been run.
- **Short rate limits.** FloodWaits that Telethon sleeps through by itself (under `max_flood_wait_seconds`, 300 s) never reach the code and raise no alert. Only longer ones do, and those also halt the account.
- **The volume thresholds** (5×, at least 30 an hour) are guesses, not measured on real traffic.
- **The panel** was not opened in a real browser (jsdom only).

## Open decisions for you

1. **Alerts on Telegram.** Right now alerts arrive in the panel, by e-mail or by webhook. A Telegram message to you would need an account to send it from (no BotFather bot). Should one of the fleet accounts, or a dedicated one, message you?
2. **Hard-off only logs out this server's session.** It does not end the account's other logins. Should there also be an "end every other login" button? It kicks out a hijacker, but it also logs the owner's phone out.
3. **Does sending from the panel count as a takeover?** It does now, the same as the owner typing on the phone.
4. **Should a new login switch the client off, or only alert?** The owner signing in on a new device will switch the client off too. That is the safe default, but it will happen.
5. Still open from phase 2:
   - taking the owner's proposed time confirms it directly;
   - reminders obey quiet hours.
6. Still open from phase 1:
   - prices;
   - moving a client to a new number;
   - Postgres RLS;
   - hold vs block for policy failures. The trip-wire now also switches the client off.
