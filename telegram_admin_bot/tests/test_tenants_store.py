"""TenantStore: versioning, rollback, pinning, audit rows, the upgrade from
the single-account schema, and the legacy settings import."""

from __future__ import annotations

import json
import shutil

import pytest

import audit
import pg as pg_module
import prompt_layers
import tenant_config
import tenants
from conftest import seed_session
from database import Database, SessionRegistry

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


async def events(pool, event=None):
    rows = await audit.list_events(pool)
    return [e for e in rows if event is None or e["event"] == event]


# ------------------------------------------------------------ upgrade path


async def test_upgrade_from_schema_1_turns_each_account_into_a_tenant(pg_pool, tmp_path):
    """The live database is at version 1 with real accounts in it. The
    conftest pool is already fully migrated, so rebuild this schema at
    version 1, add data the way the old code stored it, then upgrade."""
    async with pg_pool.acquire() as con:
        schema = await con.fetchval("SELECT current_schema()")
        await con.execute(f'DROP SCHEMA "{schema}" CASCADE; CREATE SCHEMA "{schema}"')
    only_first = tmp_path / "m"
    only_first.mkdir()
    shutil.copy(pg_module.MIGRATIONS_DIR / "0001_init.sql", only_first)
    assert await pg_module.apply_migrations(pg_pool, only_first) == [1]

    legacy_config = {
        "persona": {"purpose": "Book appointments for Studio Līga.", "tone": "Warm.",
                    "languages": "latvian", "boundaries": "No medical advice.", "signature_style": ""},
        "timing": {"min_delay_seconds": 10, "max_delay_seconds": 40, "active_hours_enabled": True,
                   "active_hours_start": "08:00", "active_hours_end": "20:00", "timezone": "Europe/Riga"},
        "behavior": {"auto_send": True, "global_pause": False},
        "safety": {"daily_send_limit": 90},
        "finetune": {"writing_samples": "Labdien! Ar ko varu palīdzēt?"},
    }
    async with pg_pool.acquire() as con:
        await con.execute("INSERT INTO telegram_sessions (session_id, label, created_at) "
                          "VALUES ('tg1', 'Studio Līga', now() - interval '2 days')")
        await con.execute("INSERT INTO telegram_sessions (session_id, created_at) VALUES ('tg2', now())")
        await con.execute("INSERT INTO session_config (session_id, config) VALUES ('tg1', $1::jsonb)",
                          json.dumps(legacy_config))
        await con.execute("INSERT INTO conversations (session_id, chat_id, display_name) VALUES ('tg1', 7, 'Ann')")
        await con.execute("INSERT INTO messages (session_id, chat_id, direction, status, text) "
                          "VALUES ('tg1', 7, 'in', 'received', 'sveiki')")

    assert await pg_module.apply_migrations(pg_pool) == [2, 3, 4, 5]
    report = await tenants.backfill(pg_pool)

    store = tenants.TenantStore(pg_pool)
    first, second = await store.list()
    # The account running longest is tenant #1, named after its label.
    assert (first["id"], first["name"], first["session_id"]) == (1, "Studio Līga", "tg1")
    assert (second["name"], second["session_id"]) == ("tg2", "tg2")

    bundle = await store.bundle(1)
    cfg = bundle.resolved.config
    assert (cfg.reply_delay.min_s, cfg.reply_delay.max_s) == (10, 40)
    assert cfg.auto_send is True and cfg.daily_message_cap == 90
    assert cfg.language_policy == "fixed:lv"
    # Active 08:00-20:00 becomes quiet 20:00-08:00.
    assert (cfg.quiet_hours.enabled, cfg.quiet_hours.start, cfg.quiet_hours.end) == (True, "20:00", "08:00")
    # Only what differed from the defaults became an override.
    assert "human" not in bundle.tenant["config_json"]
    assert bundle.resolved.sources["reply_delay.min_s"] == tenant_config.CLIENT
    assert "Book appointments for Studio Līga." in bundle.prompt.text
    assert "Labdien! Ar ko varu palīdzēt?" in bundle.prompt.text
    assert bundle.prompt.version_tag == "b1/i1v1/c1"

    # Old rows now belong to tenant 1 and reads by tenant see them.
    db = Database(pg_pool, "tg1")
    assert [m["text"] for m in await db.get_messages(7)] == ["sveiki"]
    assert (await db.get_conversation(7)) is not None
    assert report["customer_refs"] == 1

    imported = await events(pg_pool, audit.LEGACY_IMPORTED)
    assert {e["tenant_id"] for e in imported} == {1, 2}
    # Idempotent: a second run imports nothing and sets no refs.
    again = await tenants.backfill(pg_pool)
    assert again == {"imported": [], "customer_refs": 0}


# ------------------------------------------------------------ store


@pytest.fixture
def store(pg_pool):
    return tenants.TenantStore(pg_pool)


async def test_adding_an_account_creates_its_tenant(pg_pool, store):
    await SessionRegistry(pg_pool).create("tg371", label="Salon")
    tenant = await store.by_session("tg371")
    assert tenant["name"] == "Salon" and tenant["industry_id"] == 1
    # Re-adding the same number keeps the one tenant.
    await SessionRegistry(pg_pool).create("tg371", label="Salon again")
    assert len(await store.list()) == 1
    assert [e["tenant_id"] for e in await events(pg_pool, audit.TENANT_CREATED)] == [tenant["id"]]


async def test_config_save_validates_audits_and_detects_conflicts(pg_pool, store):
    tid = await seed_session(pg_pool, "acct")
    rev = (await store.get(tid))["config_revision"]

    with pytest.raises(tenant_config.ConfigError):
        await store.save_config(tid, {"daily_message_cap": -5}, actor="admin")
    assert await events(pg_pool, audit.CONFIG_CHANGED) == []

    bundle = await store.save_config(tid, {"auto_send": True}, actor="admin", reason="owner asked",
                                     expected_revision=rev)
    assert bundle.resolved.config.auto_send is True
    [event] = await events(pg_pool, audit.CONFIG_CHANGED)
    assert (event["actor"], event["reason"], event["tenant_id"]) == ("admin", "owner asked", tid)
    assert event["payload"]["changes"] == [{"path": "auto_send", "from": False, "to": True}]

    with pytest.raises(tenants.Conflict):
        await store.save_config(tid, {"auto_send": False}, actor="admin", expected_revision=rev)


async def test_industry_template_versions_rollback_and_pin(pg_pool, store):
    tid = await seed_session(pg_pool, "acct")
    await store.save_industry_template(1, {"sections": {"about": "v2 text"}}, actor="admin", note="v2")
    await store.save_industry_template(1, {"sections": {"about": "v3 text"}}, actor="admin", note="v3")
    assert "v3 text" in (await store.bundle(tid)).prompt.text

    # Pin this tenant to v2: it stops following the industry.
    bundle = await store.pin(tid, 2, actor="admin")
    assert "v2 text" in bundle.prompt.text and bundle.prompt.version_tag.startswith("b1/i1v2/")
    await store.save_industry_template(1, {"sections": {"about": "v4 text"}}, actor="admin")
    assert "v2 text" in (await store.bundle(tid)).prompt.text

    # Unpin: follows the live version again. Roll the industry back to v3.
    await store.pin(tid, None, actor="admin")
    assert "v4 text" in (await store.bundle(tid)).prompt.text
    await store.rollback_industry_template(1, 3, actor="admin", reason="v4 was worse")
    assert "v3 text" in (await store.bundle(tid)).prompt.text
    # History is untouched by the rollback.
    assert [v["version"] for v in await store.versions("industry", 1)] == [4, 3, 2, 1]

    kinds = [e["event"] for e in await events(pg_pool)]
    assert kinds.count(audit.PROMPT_VERSION_CREATED) == 3
    assert kinds.count(audit.PROMPT_PINNED) == 2 and kinds.count(audit.PROMPT_ROLLBACK) == 1

    with pytest.raises(tenants.NotFound):
        await store.pin(tid, 99, actor="admin")


async def test_client_prompt_versions_and_rollback(pg_pool, store):
    tid = await seed_session(pg_pool, "acct")
    await store.save_client_prompt(tid, {"overrides": {"faq": {"mode": "override", "text": "Parking: yes."}},
                                         "addendum": ""}, actor="admin")
    await store.save_client_prompt(tid, {"overrides": {}, "addendum": "Closed on holidays."}, actor="admin")
    text = (await store.bundle(tid)).prompt.text
    assert "Closed on holidays." in text and "Parking: yes." not in text

    bundle = await store.rollback_client_prompt(tid, 1, actor="admin")
    assert "Parking: yes." in bundle.prompt.text and bundle.prompt.version_tag.endswith("/c1")

    with pytest.raises(prompt_layers.PromptError):
        await store.save_client_prompt(tid, {"overrides": {"platform_rules": {"mode": "override", "text": "x"}}},
                                       actor="admin")


async def test_industry_config_change_that_breaks_a_tenant_is_refused(pg_pool, store):
    tid = await seed_session(pg_pool, "acct")
    await store.save_config(tid, {"reply_delay": {"max_s": 30}}, actor="admin")
    with pytest.raises(tenant_config.ConfigError) as info:
        await store.save_industry_config(1, {"reply_delay": {"min_s": 60}}, actor="admin")
    assert f"tenant {tid}" in str(info.value)
    assert (await store.get_industry(1))["default_config"] == {}

    await store.save_industry_config(1, {"daily_message_cap": 70}, actor="admin")
    bundle = await store.bundle(tid)
    assert bundle.resolved.config.daily_message_cap == 70
    assert bundle.resolved.sources["daily_message_cap"] == tenant_config.INDUSTRY


async def test_moving_a_tenant_to_another_industry_unpins_it(pg_pool, store):
    tid = await seed_session(pg_pool, "acct")
    await store.pin(tid, 1, actor="admin")
    salons = await store.create_industry("Salons", actor="admin")
    tenant = await store.update(tid, actor="admin", industry_id=salons["id"])
    assert tenant["industry_id"] == salons["id"] and tenant["prompt_pin_version"] is None
    with pytest.raises(ValueError):
        await store.create_industry("salons", actor="admin")


async def test_base_rules_save_and_rollback(pg_pool, store):
    tid = await seed_session(pg_pool, "acct")
    await store.save_base("1. Be kind.", actor="admin", note="shorter")
    assert (await store.bundle(tid)).prompt.text.startswith(prompt_layers.BASE_HEADER + "\n\n1. Be kind.")
    await store.rollback_base(1, actor="admin")
    assert "Never claim or imply that you are a human" in (await store.bundle(tid)).prompt.text


async def test_migration_3_moves_config_keys_that_changed(pg_pool, tmp_path):
    """A tenant saved under phase 1 with auto_confirm and the single
    reminder still resolves after 0003: auto_confirm is gone, the reminder
    becomes a one-item list."""
    async with pg_pool.acquire() as con:
        schema = await con.fetchval("SELECT current_schema()")
        await con.execute(f'DROP SCHEMA "{schema}" CASCADE; CREATE SCHEMA "{schema}"')
    first_two = tmp_path / "m"
    first_two.mkdir()
    for name in ("0001_init.sql", "0002_tenants.sql"):
        shutil.copy(pg_module.MIGRATIONS_DIR / name, first_two)
    assert await pg_module.apply_migrations(pg_pool, first_two) == [1, 2]
    await pg_pool.execute("INSERT INTO telegram_sessions (session_id) VALUES ('tg1'), ('tg2')")
    await pg_pool.execute("INSERT INTO tenants (name, industry_id, session_id, config_json) VALUES "
                          "('A', 1, 'tg1', '{\"auto_confirm\": true, \"booking\": {\"enabled\": true, "
                          "\"reminder_minutes_before\": 90}}'), "
                          "('B', 1, 'tg2', '{\"booking\": {\"reminder_minutes_before\": 0}}')")
    await pg_pool.execute("UPDATE industries SET default_config = '{\"auto_confirm\": false}'")

    assert await pg_module.apply_migrations(pg_pool) == [3, 4, 5]
    store = tenants.TenantStore(pg_pool)
    a, b = await store.list()
    assert a["config_json"] == {"booking": {"enabled": True, "reminders": [{"minutes_before": 90, "instruction": ""}]}}
    assert b["config_json"] == {"booking": {"reminders": []}}
    assert (await store.bundle(a["id"])).config["booking"]["reminders"] == [{"minutes_before": 90, "instruction": ""}]
    assert (await store.get_industry(1))["default_config"] == {}
    tokens = await pg_pool.fetch("SELECT calendar_token FROM tenants")
    assert len({r["calendar_token"] for r in tokens}) == 2 and all(len(r["calendar_token"]) == 64 for r in tokens)
