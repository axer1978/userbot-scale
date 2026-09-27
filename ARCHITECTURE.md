# Architecture

State as of phase 1 of the multi-tenant platform (branch `platform/phase-1`).
Code lives in `telegram_admin_bot/`; module names below are files there.

## Processes

```
 browser ──SSH tunnel / HTTPS──▶ panel (panel.py + platform_api.py)
                                    │  reads/writes Postgres directly
                                    │  commands + live events over Valkey
                                    ▼
 Postgres ◀──────────────▶ manager (manager.py)
 (all state)                  └─ worker processes, each running up to N
 Valkey                          SessionRuntime (session_runtime.py) =
 (command bus, events)           one live Telegram client per account
```

- **panel** is the control plane and holds no Telegram connection. Anything that needs a live client (send, approve a draft) goes over Valkey (Redis-compatible) to the worker holding that account's lease.
- **manager** runs workers. A worker must hold an account's **lease** in Postgres (leasing.py) before connecting, so one account never runs twice.
- **migrate** is a one-shot job: SQL migrations, then `tenants.backfill()` (the Python-side data steps).

## Tenancy

A **tenant** is one business client. It owns exactly one channel account (`tenants.session_id → telegram_sessions`) and belongs to one **industry**. A new account becomes a new tenant in the default industry (`SessionRegistry.create`).

**Isolation** is enforced at three levels:

1. Every table with account data has `tenant_id NOT NULL`. A test fails if a new table keyed by `session_id` lacks it (`test_tenant_isolation.py`).
2. A trigger on each of those tables (`tenant_for_session`, migration 0002) fills `tenant_id` from the row's account and **rejects** a row whose `tenant_id` and `session_id` belong to different tenants.
3. Every read, update and delete in `database.Database` filters on `tenant_id`. The facade is bound to one account and has no method that takes a tenant or account parameter. Tests with two tenants talking to the same Telegram user prove that one tenant's facade cannot read or change the other's rows by id.

Per-tenant files (media library, `bookings.json`) live under `DATA_DIR/tenants/<tenant id>/`, and a media index entry cannot resolve to a path outside that folder.

`customer_ref` is an HMAC of `telegram:<chat id>` under a key derived per tenant from the master key, so the same person has unrelated refs under different tenants.

## Data model (Postgres)

Migrations: `migrations/0001_init.sql` (fleet), `0002_tenants.sql` (platform).

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
| `outreach`, `chat_links`, `chat_summaries`, `bookings`, `session_media`, `session_counters`, `session_halts`, `telegram_peers`, `session_update_state` | tenant_id, session_id, … | Pre-platform tables, now tenant-scoped. `bookings` and `session_media` are not used yet (files are); phase 2 moves bookings into Postgres |
| `session_config` | tenant_id, session_id, config | Now only the account's own state: pause switch, device identity, per-contact style overrides |
| `worker_heartbeats`, `panel_sessions`, `schema_migrations` | | Operational, not tenant data |

Not built yet (later phases): `availability_rules`, `waitlist`, `customer_flags`, `unanswered_queue`, `review_batches`, `review_items`, `sessions_health`, `conversations.language`, `conversations.human_takeover_until`.

## Configuration: three layers

`tenant_config.TenantConfig` (pydantic, `extra="forbid"`) is the whole schema: timing (`reply_delay`, `burst`, `quiet_hours`, `timezone`), `auto_send`, `auto_confirm`, filters (`escalation_keywords`, `banned_topics`, `price_floors`, `allowed_link_domains`, `shareable_contacts`), caps (`daily_message_cap`, `api_spend_cap_eur`, `safety.*`), `language_policy`, and the sections for sounding human, AI parameters, outreach, context link, media and bookings.

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
   account paused / chat paused? ── yes ─▶ stop
   schedule draft (a newer message cancels and restarts it)
        │
        ▼ draft_worker
   wait reply_delay (per-contact override, else uniform/lognormal from config)
   + if that lands in quiet hours: wait until they end, plus a fresh delay
   (re-checked after waking, in case quiet hours changed meanwhile)
   history (last 30) ─▶ ai_responder.generate_reply(system_prompt = rendered layers,
                                                    burst_max, language lock, …)
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

## Where changes reach a running account

A panel save writes Postgres, then sends `reload_config` to every affected account that has a live lease, all at once. Affected means one tenant, every tenant in an industry, or every tenant for the base rules. The worker calls `bind_tenant()`: it reloads the effective config and the rendered prompt. As a backstop, each runtime rebinds on its own every 5 minutes (`REBIND_SECONDS`).
