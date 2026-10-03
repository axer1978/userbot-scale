"""Migration 0006 (WhatsApp): the new tables and columns, the channel kept in
step between an account and its tenant, the upgrade of a database that
already holds Telegram data, and SessionRegistry.create(channel=...)."""

from __future__ import annotations

import json
import shutil

import asyncpg
import pytest

import audit
import controls
import pg as pg_module
from conftest import seed_session
from database import SessionRegistry

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

BEFORE_0006 = (
    "0001_init.sql", "0002_tenants.sql", "0003_bookings.sql", "0004_safety.sql", "0005_client_facing.sql",
)


async def seed_whatsapp(pool, session_id: str) -> int:
    """seed_session for a WhatsApp account. Returns the tenant id."""
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, channel) VALUES ($1, 'whatsapp')", session_id
        )
        return await con.fetchval(
            "INSERT INTO tenants (name, industry_id, session_id) VALUES ($1, 1, $1) RETURNING id", session_id
        )


async def channels(pool, session_id: str) -> tuple:
    row = await pool.fetchrow(
        "SELECT s.channel AS account, t.channel AS tenant FROM telegram_sessions s "
        "JOIN tenants t ON t.session_id = s.session_id WHERE s.session_id = $1",
        session_id,
    )
    return row["account"], row["tenant"]


# ----------------------------------------------------------------- schema


async def test_a_fresh_database_has_every_object(pg_pool):
    assert pg_module.latest_version() >= 6
    async with pg_pool.acquire() as con:
        assert await con.fetchval("SELECT max(version) FROM schema_migrations") == pg_module.latest_version()
        columns = {
            (r["table_name"], r["column_name"]): (r["data_type"], r["is_nullable"])
            for r in await con.fetch(
                "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
        }
        assert columns[("telegram_sessions", "channel")] == ("text", "NO")
        assert columns[("messages", "wa_message_id")] == ("text", "YES")
        assert columns[("bookings", "provider_wa_message_id")] == ("text", "YES")
        for table, cols in {
            "wa_peers": ("tenant_id", "session_id", "chat_id", "jid", "phone_jid", "lid", "push_name",
                         "created_at", "updated_at"),
            "wa_auth_state": ("tenant_id", "session_id", "kind", "key_id", "value_enc", "updated_at"),
            "wa_inbox": ("id", "tenant_id", "session_id", "wa_message_id", "payload", "created_at"),
        }.items():
            assert {c for t, c in columns if t == table} == set(cols), table
            assert columns[(table, "tenant_id")] == ("integer", "NO")
        assert columns[("wa_auth_state", "value_enc")][0] == "bytea"
        assert columns[("wa_inbox", "payload")][0] == "jsonb"

        indexes = {
            r["indexname"]: r["indexdef"]
            for r in await con.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")
        }
        assert "WHERE (wa_message_id IS NOT NULL)" in indexes["idx_messages_wa"]
        assert "WHERE (phone_jid IS NOT NULL)" in indexes["idx_wa_peers_phone"]
        assert "WHERE (lid IS NOT NULL)" in indexes["idx_wa_peers_lid"]
        assert "(session_id, id)" in indexes["idx_wa_inbox_session"]

        assert await con.fetchval(
            "SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = 'wa_chat_id_seq' "
            "AND relnamespace = current_schema()::regnamespace"
        ) == 1
        triggers = {
            r["tgname"]
            for r in await con.fetch(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = current_schema()::regnamespace AND NOT t.tgisinternal"
            )
        }
        assert {"trg_wa_peers_tenant", "trg_wa_auth_state_tenant", "trg_wa_inbox_tenant",
                "trg_tenants_channel", "trg_telegram_sessions_channel"} <= triggers
        holds_check = await con.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'tenant_holds_kind_check' AND connamespace = current_schema()::regnamespace"
        )
        assert "'whatsapp'" in holds_check and "'telegram'" in holds_check


async def test_upgrading_a_database_with_telegram_data_keeps_it_all(pg_pool, tmp_path):
    """The live database is at 0005 with a Telegram account at work; 0006
    marks it Telegram on both sides and touches nothing else."""
    async with pg_pool.acquire() as con:
        schema = await con.fetchval("SELECT current_schema()")
        await con.execute(f'DROP SCHEMA "{schema}" CASCADE; CREATE SCHEMA "{schema}"')
    first = tmp_path / "m"
    first.mkdir()
    for name in BEFORE_0006:
        shutil.copy(pg_module.MIGRATIONS_DIR / name, first)
    assert await pg_module.apply_migrations(pg_pool, first) == [1, 2, 3, 4, 5]

    async with pg_pool.acquire() as con:
        await con.execute("INSERT INTO telegram_sessions (session_id, label, is_active, state) "
                          "VALUES ('tg1', 'Salon', true, 'running')")
        tid = await con.fetchval("INSERT INTO tenants (name, industry_id, session_id) "
                                 "VALUES ('Salon', 1, 'tg1') RETURNING id")
        await con.execute("INSERT INTO conversations (session_id, chat_id, display_name) VALUES ('tg1', 7, 'Ann')")
        await con.execute("INSERT INTO messages (session_id, chat_id, telegram_id, direction, status, text) VALUES "
                          "('tg1', 7, 100, 'in', 'received', 'hi'), ('tg1', 7, 101, 'out', 'sent', 'hello')")
        await con.execute(
            "INSERT INTO bookings (session_id, number, chat_id, customer_ref, starts_at, ends_at, blocked_until, "
            "tz, state, provider_chat_id, provider_message_id) VALUES ('tg1', 1, 7, 'ref', "
            "'2026-10-01 10:00+00', '2026-10-01 11:00+00', '2026-10-01 11:00+00', 'Europe/Riga', 'confirmed', "
            "55, 900)"
        )
        await con.execute("INSERT INTO tenant_holds (tenant_id, kind, created_by) VALUES ($1, 'telegram', 'x')", tid)
        before = {
            t: [dict(r) for r in await con.fetch(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("conversations", "messages", "bookings", "tenant_holds")
        }

    assert await pg_module.apply_migrations(pg_pool) == list(range(6, pg_module.latest_version() + 1))

    assert await channels(pg_pool, "tg1") == ("telegram", "telegram")
    async with pg_pool.acquire() as con:
        for table, rows in before.items():
            after = [dict(r) for r in await con.fetch(f"SELECT * FROM {table} ORDER BY 1, 2")]
            new_cols = {"wa_message_id", "provider_wa_message_id"}
            # Added by later migrations (0011), with their own defaults.
            later = {"instructions_message_ids", "instructions_cleanup_at", "instructions_cleaned_at"}
            assert [{k: v for k, v in r.items() if k not in new_cols | later} for r in after] == rows, table
            assert all(r.get(c) is None for r in after for c in new_cols)
        # The account still runs as it did.
        row = await con.fetchrow("SELECT label, is_active, state FROM telegram_sessions WHERE session_id = 'tg1'")
        assert tuple(row) == ("Salon", True, "running")


# ------------------------------------------------------------- tenant_id


async def test_the_new_tables_take_the_tenant_from_the_account(pg_pool):
    wa1 = await seed_whatsapp(pg_pool, "wa1")
    wa2 = await seed_whatsapp(pg_pool, "wa2")
    inserts = {
        "wa_peers": ("INSERT INTO wa_peers (session_id, tenant_id, jid, phone_jid) "
                     "VALUES ('wa1', {tid}, 'x{n}@s.whatsapp.net', 'x{n}@s.whatsapp.net')"),
        "wa_auth_state": ("INSERT INTO wa_auth_state (session_id, tenant_id, kind, key_id, value_enc) "
                          "VALUES ('wa1', {tid}, 'pre-key', '{n}', '\\x00'::bytea)"),
        "wa_inbox": ("INSERT INTO wa_inbox (session_id, tenant_id, wa_message_id, payload) "
                     "VALUES ('wa1', {tid}, 'M{n}', '{{}}'::jsonb)"),
    }
    async with pg_pool.acquire() as con:
        for table, sql in inserts.items():
            await con.execute(sql.format(tid="NULL", n=1))
            assert await con.fetchval(f"SELECT tenant_id FROM {table} WHERE session_id = 'wa1'") == wa1
            with pytest.raises(asyncpg.RaiseError, match="does not own session"):
                await con.execute(sql.format(tid=wa2, n=2))
            with pytest.raises(asyncpg.RaiseError, match="does not own session"):
                await con.execute(f"UPDATE {table} SET tenant_id = $1 WHERE session_id = 'wa1'", wa2)
        await con.execute("INSERT INTO telegram_sessions (session_id, channel) VALUES ('orphan', 'whatsapp')")
        with pytest.raises(asyncpg.RaiseError, match="no tenant owns session"):
            await con.execute("INSERT INTO wa_inbox (session_id, wa_message_id, payload) "
                              "VALUES ('orphan', 'M1', '{}'::jsonb)")


async def test_deleting_the_account_deletes_its_whatsapp_rows(pg_pool):
    await seed_whatsapp(pg_pool, "wa1")
    async with pg_pool.acquire() as con:
        await con.execute("INSERT INTO wa_peers (session_id, jid, lid) VALUES ('wa1', '1@lid', '1@lid')")
        await con.execute("INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc) "
                          "VALUES ('wa1', 'creds', '', '\\x01'::bytea)")
        await con.execute("INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES ('wa1', 'M1', '{}')")
        await con.execute("DELETE FROM telegram_sessions WHERE session_id = 'wa1'")
        for table in ("wa_peers", "wa_auth_state", "wa_inbox"):
            assert await con.fetchval(f"SELECT count(*) FROM {table}") == 0, table


# ------------------------------------------------------------- uniqueness


async def test_wa_peers_holds_one_row_per_phone_jid_and_per_lid(pg_pool):
    await seed_whatsapp(pg_pool, "wa1")
    await seed_whatsapp(pg_pool, "wa2")
    phone, lid = "34600123456@s.whatsapp.net", "123456789@lid"
    async with pg_pool.acquire() as con:
        a = await con.fetchval("INSERT INTO wa_peers (session_id, jid, phone_jid) VALUES ('wa1', $1, $1) "
                               "RETURNING chat_id", phone)
        b = await con.fetchval("INSERT INTO wa_peers (session_id, jid, lid) VALUES ('wa1', $1, $1) "
                               "RETURNING chat_id", lid)
        assert a != b
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute("INSERT INTO wa_peers (session_id, jid, phone_jid) VALUES ('wa1', $1, $1)", phone)
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute("INSERT INTO wa_peers (session_id, jid, lid) VALUES ('wa1', $1, $1)", lid)
        # The same person under another account is another peer.
        await con.execute("INSERT INTO wa_peers (session_id, jid, phone_jid, lid) VALUES ('wa2', $1, $1, $2)",
                          phone, lid)
        # Rows with only one of the two do not collide on the missing one.
        await con.execute("INSERT INTO wa_peers (session_id, jid, lid) VALUES ('wa1', '9@lid', '9@lid')")
        await con.execute("INSERT INTO wa_peers (session_id, jid, phone_jid) VALUES ('wa1', '9@s.whatsapp.net', "
                          "'9@s.whatsapp.net')")
        with pytest.raises(asyncpg.CheckViolationError):
            await con.execute("INSERT INTO wa_peers (session_id, jid) VALUES ('wa1', 'nobody')")
        ids = [r["chat_id"] for r in await con.fetch("SELECT chat_id FROM wa_peers")]
        assert len(ids) == len(set(ids))


async def test_a_whatsapp_message_is_stored_once_per_chat(pg_pool):
    await seed_whatsapp(pg_pool, "wa1")
    insert = ("INSERT INTO messages (session_id, chat_id, wa_message_id, direction, status, text) "
              "VALUES ('wa1', $1, $2, 'in', 'received', 'x')")
    async with pg_pool.acquire() as con:
        await con.execute(insert, 1, "3EB0ABC")
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute(insert, 1, "3EB0ABC")
        await con.execute(insert, 2, "3EB0ABC")
        await con.execute(insert, 1, None)
        await con.execute(insert, 1, None)
        assert await con.fetchval("SELECT count(*) FROM messages") == 4


async def test_wa_inbox_and_auth_state_keys(pg_pool):
    await seed_whatsapp(pg_pool, "wa1")
    async with pg_pool.acquire() as con:
        await con.execute("INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES ('wa1', 'M1', $1)",
                          json.dumps({"text": "hi"}))
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute("INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES ('wa1', 'M1', '{}')")
        await con.execute("INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc) "
                          "VALUES ('wa1', 'creds', '', '\\x01'::bytea)")
        await con.execute("INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc) "
                          "VALUES ('wa1', 'pre-key', '', '\\x01'::bytea)")
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute("INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc) "
                              "VALUES ('wa1', 'creds', '', '\\x02'::bytea)")


# ---------------------------------------------------------------- channel


async def test_an_account_is_on_telegram_or_whatsapp_only(pg_pool):
    await seed_session(pg_pool, "tg1")
    assert await channels(pg_pool, "tg1") == ("telegram", "telegram")
    async with pg_pool.acquire() as con:
        with pytest.raises(asyncpg.CheckViolationError):
            await con.execute("INSERT INTO telegram_sessions (session_id, channel) VALUES ('x1', 'signal')")
        with pytest.raises(asyncpg.CheckViolationError):
            await con.execute("UPDATE telegram_sessions SET channel = 'signal' WHERE session_id = 'tg1'")


async def test_the_tenant_follows_its_accounts_channel(pg_pool):
    await seed_whatsapp(pg_pool, "wa1")
    assert await channels(pg_pool, "wa1") == ("whatsapp", "whatsapp")
    async with pg_pool.acquire() as con:
        # Whatever an insert says, the tenant takes its account's channel.
        await con.execute("INSERT INTO telegram_sessions (session_id, channel) VALUES ('wa2', 'whatsapp')")
        await con.execute("INSERT INTO tenants (name, industry_id, session_id, channel) "
                          "VALUES ('B', 1, 'wa2', 'telegram')")
        assert await channels(pg_pool, "wa2") == ("whatsapp", "whatsapp")
        # ... and so does a direct update.
        await con.execute("UPDATE tenants SET channel = 'telegram' WHERE session_id = 'wa2'")
        assert await channels(pg_pool, "wa2") == ("whatsapp", "whatsapp")

        # A change on the account reaches the tenant.
        await con.execute("UPDATE telegram_sessions SET channel = 'telegram' WHERE session_id = 'wa1'")
        assert await channels(pg_pool, "wa1") == ("telegram", "telegram")
        await con.execute("UPDATE telegram_sessions SET channel = 'whatsapp' WHERE session_id = 'wa1'")
        assert await channels(pg_pool, "wa1") == ("whatsapp", "whatsapp")

        # A tenant moved to another account takes that account's channel.
        await con.execute("INSERT INTO telegram_sessions (session_id) VALUES ('tg1')")
        tid = await con.fetchval("SELECT id FROM tenants WHERE session_id = 'wa1'")
        await con.execute("UPDATE tenants SET session_id = 'tg1' WHERE id = $1", tid)
        assert await channels(pg_pool, "tg1") == ("telegram", "telegram")

        # A tenant whose account is deleted keeps the channel it had.
        await con.execute("DELETE FROM telegram_sessions WHERE session_id = 'wa2'")
        row = await con.fetchrow("SELECT session_id, channel FROM tenants WHERE name = 'B'")
        assert tuple(row) == (None, "whatsapp")


async def test_a_tenant_can_be_held_for_whatsapp(pg_pool):
    tid = await seed_whatsapp(pg_pool, "wa1")
    assert controls.WHATSAPP in controls.KINDS
    assert await controls.add_hold(pg_pool, tid, controls.WHATSAPP, "Logged out", actor=audit.SYSTEM)
    assert [h["kind"] for h in await controls.holds(pg_pool, tid)] == ["whatsapp"]
    with pytest.raises(asyncpg.CheckViolationError):
        await pg_pool.execute("INSERT INTO tenant_holds (tenant_id, kind, created_by) VALUES ($1, 'bogus', 'x')", tid)


# --------------------------------------------------------------- registry


async def test_the_registry_creates_a_whatsapp_account_and_tenant(pg_pool):
    registry = SessionRegistry(pg_pool)
    row = await registry.create("wa1", label="Salon WA", channel="whatsapp")
    assert row["channel"] == "whatsapp"
    assert await channels(pg_pool, "wa1") == ("whatsapp", "whatsapp")
    created = [e for e in await audit.list_events(pg_pool) if e["event"] == audit.TENANT_CREATED]
    assert [e["reason"] for e in created] == ["New WhatsApp account added"]

    # Adding it again on the same channel refreshes it; no second tenant.
    assert (await registry.create("wa1", label="Salon WA 2", channel="whatsapp"))["label"] == "Salon WA 2"
    assert await pg_pool.fetchval("SELECT count(*) FROM tenants") == 1

    tg = await registry.create("tg1", label="Salon")
    assert tg["channel"] == "telegram"
    assert {r["session_id"]: r["channel"] for r in await registry.list()} == {"tg1": "telegram", "wa1": "whatsapp"}
    created = [e for e in await audit.list_events(pg_pool) if e["event"] == audit.TENANT_CREATED]
    assert sorted(e["reason"] for e in created) == ["New Telegram account added", "New WhatsApp account added"]


async def test_the_registry_never_switches_an_accounts_channel(pg_pool):
    registry = SessionRegistry(pg_pool)
    await registry.create("tg1", label="Salon", api_id=1, channel="telegram")
    await registry.create("wa1", label="WA", channel="whatsapp")
    with pytest.raises(ValueError, match="telegram account"):
        await registry.create("tg1", label="Changed", channel="whatsapp")
    with pytest.raises(ValueError, match="whatsapp account"):
        await registry.create("wa1", label="Changed", channel="telegram")
    with pytest.raises(ValueError, match="channel must be one of"):
        await registry.create("x1", channel="signal")
    assert await pg_pool.fetchval("SELECT count(*) FROM telegram_sessions WHERE session_id = 'x1'") == 0

    tg, wa = await registry.get("tg1"), await registry.get("wa1")
    assert (tg["channel"], tg["label"], tg["api_id"]) == ("telegram", "Salon", 1)
    assert (wa["channel"], wa["label"]) == ("whatsapp", "WA")
    assert await channels(pg_pool, "tg1") == ("telegram", "telegram")
    assert await channels(pg_pool, "wa1") == ("whatsapp", "whatsapp")
