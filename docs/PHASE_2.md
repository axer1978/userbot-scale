# Phase 2: bookings

Branch `platform/phase-1` (phase 2 continues on it), local only, not pushed.
Tests: **779 passed** against Postgres 16 (352 after phase 1). See
`ARCHITECTURE.md` → *Bookings*, *Timed work* and *Limits before every reply*.

## Done

| Item | Where |
|---|---|
| Bookings in Postgres, numbered per client from 1 | Migration `0003_bookings.sql`, `booking_store.py`. Two live bookings of one client can't overlap, including the gap after each; the database refuses it (`EXCLUDE` constraint). Five customers racing for one slot get one booking (tested). Each account's old `bookings.json` is imported once on start, keeping its numbers |
| State machine, illegal transitions raise | `booking_states.py`: requested → pending → confirmed → completed / no_show, and cancelled. Every transition is checked for state, actor and time. Tested for every transition × state × actor |
| **No auto-confirm** | `auto_confirm` removed from the schema, and from stored configs by the migration. Only the owner (by text) or an admin (in the panel) confirms |
| Owner answers by text, no BotFather bot | `YES 7`, `NO 7`, `7 15:30`, `7 04.10 15:30`, `7 tomorrow 15:30`, `CANCEL 7`, `DONE 7`, `NOSHOW 7`, `LIST`, in en/lv/ru. A bare `yes` works as a reply to the request, or when only one request is open |
| Propose a time | By the owner (`7 15:30`) or in the panel; the customer is asked, and taking it confirms. The customer can also ask to move a confirmed booking, and the owner answers `YES 7` / `NO 7` |
| Availability engine | `availability.py`: weekly hours (several windows per day), slot length, gap after each booking, closed dates, minimum notice, how far ahead. Stored in UTC, shown in the client's zone. Correct across the clock change (tested on both 2026 DST days). A taken or closed time is refused in code, never put to the owner, and the reply offers the nearest free times |
| Cancel | By the customer in chat or on their page, by the owner (`CANCEL 7`), or in the panel. It frees the slot, tells both sides, and offers the slot to the waitlist |
| Reschedule as its own action | Same booking, same number and history. A moved booking gets its reminders again |
| Reminders, configurable | `booking.reminders`: a list of `{minutes_before, instruction}`, 24 h and 2 h by default. The instruction says what that reminder should say. The customer replies `1` or `2`, which is handled in code with no model call. Sent at most once: the reminder is claimed in the database before it goes out |
| One background scheduler, idempotent | `scheduler.py`, new compose service `scheduler`. A Postgres advisory lock makes it a singleton (tested with two running). It ticks every running account once a minute |
| Quiet-hours replies survive a restart | They are stored in `deferred_replies` and sent by the scheduler's tick (this fixes a phase 1 gap) |
| ICS feed + owner calendar + customer page | `ics.py`, `public_app.py` (optional `booking-pages` profile with its own Caddy site), and the **Bookings** view in the panel: calendar, waiting, waitlist, opening hours, calendar link, AI usage |
| Waitlist | When a time is taken, the waitlist is offered. When a slot frees up, the first person whose wish covers it gets an offer. An offer not taken in time moves on to the next person, and taking one still needs the owner's YES |
| E-mail record | `mailer.py`: SMTP from `.env`, skipped while empty. Sent on confirm, move, cancel, decline and lapse, with an `.ics` attached |
| Photos (optional) | `vision.py`: any OpenAI-compatible vision model (`VISION_API_URL` / `VISION_API_KEY`; DeepSeek can't see images). A customer's photo gets a short description, and it is told never to describe the person. An **arrival photo check** compares it with the media items marked *Entrance*. The owner can send the entrance photo captioned "door". With `arrival_requires_photo`, the door code waits for a matching photo |
| AI token / spend limits per client | `ai_limits.py`, `limits.*` plus `api_spend_cap_eur` (now enforced): tokens and EUR per day and per month. At a limit no model call is made, and the panel and audit log are told once |
| Bot doesn't over-answer | `replies.*`: bot messages per chat per hour/day, a least gap, skipping bare "ok"/"thanks", and your own `no_reply_instruction` (the model may answer `[NO_REPLY]`). Every skip is noted and audited. Booking news is never held back |
| Panel | New **Bookings** view. Config editor: reminders edited as JSON, long texts in a box. **Clients → Platform rules → AI prices** editor. Media: *Entrance* checkbox |

Clicked through in a simulated browser (jsdom) against a real panel and a runtime with Telegram faked, covering every Bookings tab, confirm, propose, hours, the waitlist, reminders JSON, prices and the entrance mark. No script errors.

## What changes for the live account when this is deployed

1. **Bookings move into Postgres.** On first start, `data/tenants/<id>/bookings.json` is imported and renamed `bookings.json.imported`. Numbers are kept, and new ones continue after the highest.
2. **Reminders.** The old single check-in (`reminder_minutes_before`, 120 by default) becomes a one-item `booking.reminders` list if the account had set it. If it hadn't, the new default applies: **24 h and 2 h before**, so one more message than before. Reminders also obey quiet hours, so one due at 07:00 goes out when quiet hours end.
3. **The spend cap is enforced.** `api_spend_cap_eur` defaults to €10 per month. If the account uses more, replies stop until the month ends. Check **Bookings → Calendar link & AI usage** right after deploying, and raise or zero the cap in Config if needed.
4. **Times are checked before the owner sees them.** With no opening hours set, which is the live account's case, only overlaps with other bookings are refused.
5. **Unanswered requests lapse** at their start time, and both sides are told.
6. **A customer changing the time keeps the booking number.** Before, it made a new number marked "replaces #n".
7. **More owner commands.** `CANCEL 7` now cancels (before, "cancel" meant no). `LIST`, `DONE`, `NOSHOW` and proposing a time are new.
8. **New `scheduler` service.** `docker compose up -d` starts it.
9. Photos are stored as `[photo]` instead of `[non-text message]`, and are only read when `vision` is on.
10. The legacy single-account `main.py`, which is unused, no longer runs: the old booking file store it imported is gone.

## Deploying

The same as phase 1: back up, rehearse the migration on a copy, then deploy. On the server, in `~/userbot-scale/telegram_admin_bot`:

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > before-phase2-$(date +%F).sql
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" upgrade_check'
docker compose exec -T postgres sh -c 'psql -q -U "$POSTGRES_USER" upgrade_check' < before-phase2-$(date +%F).sql
git fetch && git checkout platform/phase-1 && git pull
docker compose build
docker compose run --rm --no-deps migrate sh -c 'DATABASE_URL="${DATABASE_URL%/*}/upgrade_check" python migrate_entrypoint.py'
docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" upgrade_check'
docker compose up -d --remove-orphans
docker compose ps        # scheduler should be Up
docker compose logs scheduler | tail -5    # "Scheduler running (tick every 60s)."
```

If phase 1 is not deployed yet, one rehearsal covers both. The migrate job applies 0002 and 0003.

Optional, and only in `.env`, set by you (I don't need the values):

- **E-mail record:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, and `booking.owner_email` in the client's Config.
- **Photos:** `VISION_API_URL`, `VISION_API_KEY`, then `vision.enabled` and `vision.model` in Config, and the model's price under AI prices.
- **Public pages:** `BOOKING_DOMAIN` and `PUBLIC_BASE_URL`, then `docker compose --profile booking-pages up -d`. They use ports 80/443, so they can't run next to the `public` panel profile as they are.

## Live checklist (your test account, after deploy)

1. As a customer, ask for a time tomorrow. The owner's Telegram gets *Booking request #1*.
2. As the owner, reply `1 16:00`. The customer is asked about 16:00; they say yes, and it is confirmed.
3. As the customer, write "can we do Thursday 10 instead?" The owner gets *asks to move*; `YES 1` moves it.
4. In Config set `booking.min_notice_minutes` to 0 and `booking.reminders` to `[{"minutes_before": 10, "instruction": ""}]`. Book and confirm a time 20 minutes ahead. About 10 minutes before it, the customer gets the reminder; reply `1`.
5. Cancel as the customer. The owner is told.
6. Send "ok" with `replies.skip_acknowledgements` on. There should be no reply, and a note in the chat.
7. Optional, with vision on: mark a photo *Entrance*, then send photos from the customer side during the arrival window.

## Not tested

- **Real Telegram, real DeepSeek, a real vision provider, a real SMTP server.** Everything runs against fakes. The extraction prompt changed (it now returns an intent: book / cancel / accept / …), and its accuracy on real chats is unknown until the checklist above.
- **Google Calendar mirror.** The code moved into `booking_flow.py`, but the tests switch it off, so create/confirm/delete against Google was not exercised.
- **Docker.** `docker-compose.yml` was checked as YAML only, since there is no Docker here. The `scheduler`, `booking-pages` and `caddy-booking` services have not been started for real, and the booking pages were only tested in-process.
- **Owner messages** are in English only; their wording is fixed in code (`bookings.py`).
- **Vision** photo comparison quality depends on the model you pick. The threshold `arrival_photo_min_confidence` defaults to 0.7.
- **The panel** in a real browser (jsdom only, as in phase 1).

## Open decisions for you

1. **When the customer takes the owner's proposed time, it is confirmed right away**, because the owner wrote that time. Should the owner instead confirm again with `YES n`?
2. **Reminders obey quiet hours.** A 2 h reminder for a 09:00 booking falls at 07:00 and waits until quiet hours end, which may be too late. Should reminders ignore quiet hours?
3. **The default 24 h + 2 h reminders apply to the live account** unless you change `booking.reminders` (see "What changes" 2).
4. Still open from phase 1: verify prices (now editable under AI prices), moving a client to a new number, Postgres RLS, and hold vs block for policy failures.
