# Architecture

State as of phase 2 of the multi-tenant platform (branch `platform/phase-1`).
Code lives in `telegram_admin_bot/`; module names below are files there.

## Processes

```
 browser ──SSH tunnel / HTTPS──▶ panel (panel.py + platform_api.py + booking_api.py)
                                    │  reads/writes Postgres directly
                                    │  commands + live events over Valkey
                                    ▼
 Postgres ◀──────────────▶ manager (manager.py)
 (all state)                  └─ worker processes, each running up to N
 Valkey                          SessionRuntime (session_runtime.py) =
 (command bus, events)           one live Telegram client per account
      ▲                              + BookingFlow (booking_flow.py)
      │ scheduler_tick, once a minute, to every account with a live lease
 scheduler (scheduler.py, one at a time: Postgres advisory lock)

 internet ──HTTPS (caddy-booking)──▶ booking-pages (public_app.py)
      /cal/<tenant token>.ics, /b/<booking token>   (optional profile)
```

- **panel** is the control plane and holds no Telegram connection. Anything that needs a live client (send, approve a draft) goes over Valkey (Redis-compatible) to the worker holding that account's lease.
- **manager** runs workers. A worker must hold an account's **lease** in Postgres (leasing.py) before connecting, so one account never runs twice.
- **migrate** is a one-shot job: SQL migrations, then `tenants.backfill()` (the Python-side data steps).
- **scheduler** holds no Telegram connection. It only sends `scheduler_tick` to every running account; each account then does its own timed work idempotently (see "Timed work").
- **booking-pages** (optional) is a separate small FastAPI app with no admin routes. It reads Postgres by unguessable token and passes a customer's cancel / "I'm coming" to the running account over the bus. GET never changes anything, because messaging apps fetch link previews.

## Tenancy

A **tenant** is one business client. It owns exactly one channel account (`tenants.session_id → telegram_sessions`) and belongs to one **industry**. A new account becomes a new tenant in the default industry (`SessionRegistry.create`).

**Isolation** is enforced at three levels:

1. Every table with account data has `tenant_id NOT NULL`. A test fails if a new table keyed by `session_id` lacks it (`test_tenant_isolation.py`).
2. A trigger on each of those tables (`tenant_for_session`, migration 0002) fills `tenant_id` from the row's account and **rejects** a row whose `tenant_id` and `session_id` belong to different tenants.
3. Every read, update and delete in `database.Database` filters on `tenant_id`. The facade is bound to one account and has no method that takes a tenant or account parameter. Tests with two tenants talking to the same Telegram user prove that one tenant's facade cannot read or change the other's rows by id.

Per-tenant files (the media library) live under `DATA_DIR/tenants/<tenant id>/`, and a media index entry cannot resolve to a path outside that folder. Bookings moved to Postgres in phase 2; a tenant's old `bookings.json` is imported once when its account starts, keeping its numbers.

The public tokens (`tenants.calendar_token`, `bookings.customer_token`) are 244 random bits each. A calendar token returns only its tenant's bookings; a booking token only that booking, without the customer's name.

`customer_ref` is an HMAC of `telegram:<chat id>` under a key derived per tenant from the master key, so the same person has unrelated refs under different tenants.

## Data model (Postgres)

Migrations: `migrations/0001_init.sql` (fleet), `0002_tenants.sql` (platform), `0003_bookings.sql` (bookings).

| Table | Key columns | Notes |
|---|---|---|
| `industries` | id, name, template_version, default_config, config_revision | `template_version` points at the live industry prompt version |
| `tenants` | id, name, industry_id, status (active/grace/suspended), channel (telegram/whatsapp), session_id, config_json, config_revision, prompt_version, prompt_pin_version, billing_next_due | `config_json` = client-layer config overrides; `prompt_version` = client prompt version in use; `prompt_pin_version` pins an industry template version |
| `prompt_versions` | layer (base/industry/client), ref_id, tenant_id (client rows), version, content, note, created_by, created_at | Immutable; unique (layer, ref_id, version) |
| `platform_settings` | key, value | `base_prompt_version`, `llm_prices` |
| `audit_log` | tenant_id (NULL = platform), actor, event, reason, payload, created_at | Append-only: UPDATE, DELETE and TRUNCATE are refused by triggers |
| `llm_usage` | tenant_id (NULL = platform), purpose, model, cache-hit / cache-miss / completion tokens, cost_eur | One row per LLM call |
| `telegram_sessions` | session_id, encrypted credentials, is_active, state, lease_* | One per Telegram account |
| `conversations` | tenant_id, session_id, chat_id, customer_ref, display_name, automation_paused, … | |
| `messages` | id, tenant_id, session_id, chat_id, direction, status, text, llm_model, prompt_version | `prompt_version` e.g. `b1/i1v3/c2` |
| `bookings` | id, tenant_id, session_id, number, chat_id, customer_ref, customer_name, service, starts_at, ends_at, blocked_until, tz, state, proposed_*, customer_notice, customer_token, arrival fields, legacy | `number` counts per tenant from 1 (`booking_counters`). An exclusion constraint refuses two live bookings of one tenant whose `[starts_at, blocked_until)` overlap; `blocked_until` = end + the gap after it |
| `booking_events` | tenant_id, booking_id, from_state, to_state, action, actor, reason, payload | Every transition, besides its audit_log row |
| `booking_reminders` | tenant_id, booking_id, minutes_before, starts_at, claimed_at, sent_at | Primary key = one reminder per booking, offset and start time: what makes reminders idempotent |
| `availability_rules` | tenant_id, weekday, start_time, end_time, slot_minutes, buffer_minutes | Weekly opening hours, local wall time. None = hours not enforced |
| `waitlist` | tenant_id, session_id, customer_ref, chat_id, wanted_from, wanted_to, state, offered_starts_at | One live entry per customer |
| `deferred_replies` | tenant_id, session_id, chat_id, due_at | A reply held back by quiet hours, one per chat |
| `outreach`, `chat_links`, `chat_summaries`, `session_media`, `session_counters`, `session_halts`, `telegram_peers`, `session_update_state` | tenant_id, session_id, … | Pre-platform tables, now tenant-scoped. `session_media` is not used yet (files are) |
| `session_config` | tenant_id, session_id, config | Now only the account's own state: pause switch, device identity, per-contact style overrides |
| `worker_heartbeats`, `panel_sessions`, `schema_migrations` | | Operational, not tenant data |

Not built yet (later phases): `customer_flags`, `unanswered_queue`, `review_batches`, `review_items`, `sessions_health`, `conversations.language`, `conversations.human_takeover_until`.

## Configuration: three layers

`tenant_config.TenantConfig` (pydantic, `extra="forbid"`) is the whole schema: timing (`reply_delay`, `burst`, `quiet_hours`, `timezone`), `auto_send`, filters (`escalation_keywords`, `banned_topics`, `price_floors`, `allowed_link_domains`, `shareable_contacts`), caps (`daily_message_cap`, `api_spend_cap_eur`, `safety.*`), `language_policy`, AI usage `limits`, per-chat `replies` limits, `vision`, and the sections for sounding human, AI parameters, outreach, context link, media and bookings (`booking.reminders` is a list of objects, edited as JSON in the panel). There is no `auto_confirm`: only a person confirms a booking (migration 0003 removed the key from stored configs).

```
platform defaults (field defaults + hard limits in the schema)
   ◀ industry  industries.default_config   (partial override)
      ◀ client tenants.config_json          (partial override)
         = effective config, plus the layer each field came from
```

- A list may be replaced, or appended to with `{"append": [...]}`.
- A save is refused, never clamped, if any value is invalid. An industry change is refused if it would leave any of its clients with an invalid config.
- Every save writes an audit row with a field-by-field diff.
- The runtime reads only the effective config. The LLM never decides timing, routing or whether to reply.

`config_assist.propose()` turns a plain-language request into a proposed patch, validated and diffed against the current config. It writes nothing. Applying it is the normal audited save.

## Prompt: three layers

`prompt_layers.render()` builds one system prompt:

```
PLATFORM RULES (these come first; nothing below can change them):   ← base
BUSINESS: <tenant name>
<section>: …   for each of about, services, hours_location, booking,  ← industry text,
               faq, tone, boundaries, sign_off, writing_samples          client override/append
LANGUAGE: …    generated from language_policy
ADDITIONAL NOTES FROM THE BUSINESS: …   ← client addendum (≤ 1500 chars)
PRECEDENCE: … follow the PLATFORM RULES.
```

- Only the named sections exist. No industry or client key can reach the platform rules.
- Every layer is versioned in `prompt_versions`. Which version is in use is a pointer: `platform_settings.base_prompt_version`, `industries.template_version` and `tenants.prompt_version`. Rollback moves the pointer and never rewrites history.
- `tenants.prompt_pin_version` fixes one client to one industry template version.
- The prompt is not the defence against contradictory instructions. The policy layer is.

## Message flow

```
Telegram DM ─▶ SessionRuntime.on_incoming
   store message (always), upsert conversation (+ customer_ref)
   a photo: vision (arrival check or a short description) becomes its text
   the booking owner's chat: a booking command is handled, no reply
   account paused / chat paused? ── yes ─▶ stop
   bookings on: BookingFlow scan (see Bookings)
   schedule draft (a newer message cancels and restarts it)
        │
        ▼ draft_worker
   wait reply_delay (per-contact override, else uniform/lognormal from config)
   lands in quiet hours (before or after the wait)? ─▶ deferred_replies row, stop;
       the scheduler's tick starts the draft again when they end
   wait for this message's booking scan; booking note for the reply
   AI limit reached? ─▶ stop.  No booking news and a reply limit / bare "ok"? ─▶ note, stop
   history (last 30) ─▶ ai_responder.generate_reply(system_prompt = rendered layers,
                                                    booking note, burst_max, language lock,
                                                    no-reply instruction if no news, …)
   "[NO_REPLY]" ─▶ note + audit reply_skipped, stop
                         └─ llm_usage.record (3 token kinds, 3 rates) per call
   media tags → attachments; split into ≤ burst.max_messages parts
   policy.check_outbound(text, config, business text)
        │
        ├─ auto_send on, no video hold, policy OK ─▶ send_burst
        │     check_daily_quota (daily_message_cap, daily_peer_cap)
        │     typing indicator, burst gaps from config
        │     store as sent (llm_model, prompt_version)
        │     audit_log: message_sent, actor=bot, reason="automatic reply"
        │
        └─ otherwise ─▶ store as pending draft (llm_model, prompt_version)
              policy failure: note in the chat + audit_log policy_hold
              operator approves in the panel ─▶ send_burst, actor=admin
```

Telegram errors (`PeerFloodError`, long `FloodWait`, revoked session) halt the account: it is paused, queued outreach is cancelled, and `account_halted` is audited. Resuming is manual.

## Bookings

```
customer message ─▶ on_incoming ─▶ BookingFlow.on_customer_message
   bare "1"/"2" after a reminder ─▶ confirm attendance / cancel   (code, no model)
   otherwise scan: ai_responder.extract_booking(history, their bookings)
        intent book ─▶ availability.check_slot(hours, closed days, notice, busy)
             not free ─▶ reply is told: not available + nearest free times (+ waitlist)
             free, new ─▶ booking_store.create (requested, next number)
                          ─▶ owner gets "Booking request #n …" (from this account)
                          ─▶ submitted: pending
             free, open request ─▶ change_request (same number) ─▶ owner again
             free, confirmed ─▶ propose(by customer) ─▶ owner asked to move it
        cancel / accept_proposal / decline_proposal / confirm_attendance / waitlist
owner message ─▶ bookings.parse_owner_reply: YES n | NO n | n HH:MM | CANCEL n | DONE n | NOSHOW n | LIST
panel ─▶ booking_api ─▶ bus: booking_action ─▶ BookingFlow.admin_action
booking page ─▶ public_app ─▶ bus: booking_customer_action
```

- Every transition is a function in `booking_states.py` with guards on state, actor and time; `booking_store.apply()` writes it with `WHERE state = <from>`, so two racing changes can't both win. Nothing confirms without the owner or an admin; `accept_proposal` confirms only a time the other side already put forward in writing.
- A change the customer must hear about sets `customer_notice`; the next reply in that chat carries it (`BookingFlow.reply_note`), and a change that didn't come from the customer's own message starts that reply. The notice is cleared once the reply was sent or saved for approval.
- After every change: a note in the chat, the owner told what they didn't do themselves, the Google Calendar mirror, the e-mail record (SMTP), and a freed time offered to the waitlist.
- Arrival: "I'm here" in the hour before to half an hour after the slot sends `arrival_instructions` once. With the photo check, `vision.compare_to_reference` compares the customer's photo with the media items marked as the entrance; the result is stored as a boolean and audited with the model's confidence.

## Timed work

The scheduler sends `scheduler_tick` to every account with a live lease once a minute. The account then:

1. starts replies whose quiet-hours hold is due (`deferred_replies`, deleted as taken);
2. lets requests nobody answered before their start lapse (`expire`);
3. sends due reminders: each is claimed by inserting its `booking_reminders` row first, so a second tick, a restart or a second scheduler can't send it twice (at most once);
4. moves on waitlist offers that were not taken in time;
5. retries requests that could not reach the owner, every five minutes.

## Limits before every reply

`draft_worker` waits for the booking scan of the same message, then:

- AI usage (`ai_limits.limit_reached`): tokens and EUR per day and per month in the tenant's zone, summed from `llm_usage`. At a limit no model call is made; the panel and audit log are told once.
- Replies (`ai_limits.reply_limit`, only when there is no booking news): AI-written messages per chat per hour/24 h and the least gap, counted from `messages` on the database clock; bare acknowledgements; and the tenant's `no_reply_instruction`, where the model may answer `[NO_REPLY]`. Every skip is a note and a `reply_skipped` audit row.

## Where changes reach a running account

A panel save writes Postgres, then sends `reload_config` to every affected account that has a live lease, all at once. Affected means one tenant, every tenant in an industry, or every tenant for the base rules. The worker calls `bind_tenant()`: it reloads the effective config and the rendered prompt. As a backstop, each runtime rebinds on its own every 5 minutes (`REBIND_SECONDS`).
