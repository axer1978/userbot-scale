# Phase 1: multi-tenant skeleton and prompt inheritance

Branch `platform/phase-1`, local only (not pushed). Tests: **352 passed**
against Postgres 16 (233 before). See `ARCHITECTURE.md` for the data model
and message flow.

## Done

| Spec item | Where |
|---|---|
| Tenants; the existing bot becomes tenant #1 | Migration `0002_tenants.sql`: one tenant per account, oldest first. `tenants.backfill()` (run by the migrate job) turns each account's old persona and timing settings into its client overrides, once |
| `tenant_id` on every client table, every query scoped by it | Column + consistency trigger on all 12 account tables; `database.Database` filters every read, update and delete on `tenant_id`. `test_tenant_isolation.py` proves cross-tenant reads and writes fail, including two tenants talking to the same person |
| Prompt inheritance with precedence | `prompt_layers.py`: base, then industry, then client override/append and addendum, then the precedence line. `test_prompt_layers.py` includes a client contradicting a base rule every way it can; base stays first, verbatim, and the client cannot target it |
| Tenant config schema (pydantic) | `tenant_config.py`: all the listed fields plus the existing sections. Three layers, strict validation (reject, never clamp), per-field source |
| Config from natural language | `config_assist.py` + **Ask AI** tab: LLM proposes → schema validates → diff shown → admin presses Apply → normal audited save. The propose endpoint writes nothing (tested) |
| Admin panel: industry folders → clients, inherited greyed, overridden highlighted, template editing, versions, pin/rollback | **Clients** view (`static/js/platform.js`, `platform_api.py`); panel split into `index.html` + `css/` + one script per feature |
| Per-tenant Telethon sessions | Kept as it was (encrypted auth keys in Postgres + leases), as agreed; account ↔ tenant 1:1 |
| Humanlike send from config | `humanlike.py`: `reply_delay` (uniform/lognormal), `burst` (max messages, gap), `quiet_hours` (replies wait instead of being dropped; DST-correct), `daily_message_cap`; typing indicator and read receipts as before |
| Policy layer (rule 4) | `policy.py`, run on every AI-written reply: disallowed links, wallets, IBANs, unshared phones/e-mails, prices under `price_floors`, `banned_topics`, promises not in the business's own text. A failure keeps the reply as a draft with the reasons shown |
| Audit (rule 5) | `audit_log`, append-only in Postgres. Every send (actor + reason), config change (with diff), prompt version, rollback, pin, tenant change, pause/resume/halt, policy hold and config proposal |
| Spend metering | `llm_usage.py`: cache-hit, cache-miss and output tokens priced separately, as you asked; unknown models priced at the highest listed rate. Enforcement is phase 3 |

## Where I deviated from the spec

- **Postgres, not SQLite**, as agreed.
- `tenants.prompt_addendum` and `industries.template_prompt` are not columns. Both live in the versioned `prompt_versions` content, so there is one source of truth with history.
- `burst.gap_ms` is `{min, max}` instead of one number, because the code already used a random range.
- `log_all_messages` is gone. Every message is always stored, which the audit rule needs.
- Context-link and outreach default **off** for tenants. Outreach stays on for an imported account only if it had actually used it.

## What changes for the live account when this is deployed

Please read this list before deploying.

1. **Its prompt changes.** The old header ("write as that person … never say you're an AI") is replaced by the platform rules. Rule 1 has the bot say it is the business's automated assistant if someone sincerely asks. The old persona fields become its client sections (purpose → About, tone, boundaries, sign-off, writing samples). Check **Clients → the account → Rendered prompt** right after deploying.
1. **Quiet hours instead of active hours.** The old window becomes its inverse. A message that arrives at night is now answered in the morning; before, it was skipped.
2. **Policy holds.** With auto-send on, a reply containing a link, phone number, e-mail or price the config doesn't allow now waits as a draft. Fill `allowed_link_domains`, `shareable_contacts` and `price_floors` for the account.
3. **Context-link is off.**
4. **The Settings sheet is replaced by the Clients view.** Writing samples and the media rules moved there.
6. **Files move.** On first start, the account's folder moves from `data/<account-id>/` to `data/tenants/<id>/`.
7. **Valkey replaces Redis** (`valkey/valkey:8-alpine`, service `valkey`). It only carries commands and live events, so nothing needs copying; `--remove-orphans` below removes the old `userbot-redis` container.

## Deploying: rehearse on a copy first

Migration 0002 alters every table. It runs in one transaction, so it either
completes or changes nothing. Still, run it against a copy of the live data
before the real thing. On the server, in `~/userbot-scale/telegram_admin_bot`:

```bash
# 1. Back up, and make a scratch copy to rehearse on
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > before-phase1-$(date +%F).sql
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" upgrade_check'
docker compose exec -T postgres sh -c 'psql -q -U "$POSTGRES_USER" upgrade_check' < before-phase1-$(date +%F).sql

# 2. Build the new code and migrate the copy only
git fetch && git checkout platform/phase-1
docker compose build
docker compose run --rm --no-deps migrate sh -c 'DATABASE_URL="${DATABASE_URL%/*}/upgrade_check" python migrate_entrypoint.py'
#    It prints, per tenant, which settings and persona sections it imported
#    and anything it dropped. Send me that output if anything looks off.

# 3. Throw the copy away, then deploy for real
docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" upgrade_check'
docker compose up -d --remove-orphans
```

To use **Ask AI**, add `DEEPSEEK_PLATFORM_KEY=` to `.env` yourself, then run
`docker compose up -d --force-recreate panel`.

## Not tested

- **Nothing ran against real Telegram or real DeepSeek.** The runtime tests use a scripted model and a fake send.
- **The policy patterns** are tested against sentences I wrote, not real replies. False holds are possible (e.g. an order number read as a phone number), and so are misses for unusual formats.
- **The data migration** was tested against a synthetic version-1 database, not your data. Hence the rehearsal above.
- **The panel UI** was driven in jsdom (every tab, save, validation error, proposal/apply, versions). Nobody has looked at it in a real browser, so layout and CSS are unverified.
- **Quiet-hours waits are in memory.** If the manager restarts overnight, a reply that was waiting is lost, and that customer gets no answer until they write again. A persistent scheduler is phase 2/3 work.
- **Several manager replicas** were not tested; there is still only one.

## Open decisions for you

Answered: the platform rules, industries and anything about what the bot
says are configured by you in the panel or the JSON; the code doesn't ship
opinions about them.

Booking numbers: a plain count per business, each starting at 1. Phase 2 keeps
that when bookings move into Postgres (numbered per tenant).

2. **Prices.** The `llm_prices` values (DeepSeek USD per 1M tokens, `usd_to_eur` 0.86) are what I knew when writing the migration. Verify them before phase 3 enforces caps.
3. **Changing a client's phone number** isn't supported: a new number is a new account and a new tenant. Should a tenant be able to move to a new account and keep its history?
4. **Postgres row-level security** as a fourth isolation layer. It would mean setting the tenant on every connection; I held off because it adds per-query overhead and complexity. Want it?
5. **Policy holds vs blocks.** A failing reply is held for you, never dropped. Keep that, or block some categories outright (wallets, for instance)?
