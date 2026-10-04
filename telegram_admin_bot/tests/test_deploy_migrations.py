"""The migration path a deployment actually takes: every migration on an
empty database and then again (a no-op, nothing changes), the real
`migrate_entrypoint.py` run twice against its own database (the second run
reports "already at latest" and the backfill does nothing), and a database
left at 0005 by the previous release, with Telegram data in every table the
WhatsApp migration touches, upgraded to the current version.

The entrypoint test needs CREATEDB on PG_TEST_DSN's role (it makes and drops a
database of its own, since the entrypoint takes a DSN, not a schema)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

import pg as pg_module
from conftest import PG_TEST_DSN, TEST_MASTER_KEY

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

APP_DIR = Path(__file__).resolve().parent.parent
BEFORE_0006 = ("0001_init.sql", "0002_tenants.sql", "0003_bookings.sql", "0004_safety.sql", "0005_client_facing.sql")


@pytest_asyncio.fixture
async def empty_pool():
    """Like conftest.pg_pool, but nothing applied yet: the tests here run
    the migrations themselves."""
    if not PG_TEST_DSN:
        pytest.skip("PG_TEST_DSN not set; skipping Postgres-backed tests")
    schema = f"t_{uuid.uuid4().hex[:16]}"
    admin = await asyncpg.connect(PG_TEST_DSN)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()
    pool = await asyncpg.create_pool(PG_TEST_DSN, min_size=1, max_size=4, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        admin = await asyncpg.connect(PG_TEST_DSN)
        try:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await admin.close()


async def catalog(pool) -> dict:
    """Everything that defines the schema, independent of its name."""
    async with pool.acquire() as con:
        return {
            "columns": [tuple(r) for r in await con.fetch(
                "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() ORDER BY 1, 2")],
            "constraints": [tuple(r) for r in await con.fetch(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = current_schema()::regnamespace ORDER BY 1, 2")],
            "indexes": [tuple(r) for r in await con.fetch(
                "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() ORDER BY 1, 2")],
            "triggers": [tuple(r) for r in await con.fetch(
                "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = current_schema()::regnamespace AND NOT t.tgisinternal ORDER BY 1, 2")],
            "functions": [tuple(r) for r in await con.fetch(
                "SELECT proname, pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace = current_schema()::regnamespace "
                "ORDER BY 1")],
            "sequences": [r["sequence_name"] for r in await con.fetch(
                "SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = current_schema() ORDER BY 1")],
            "migrations": [tuple(r) for r in await con.fetch("SELECT version, name FROM schema_migrations ORDER BY 1")],
        }


async def table_rows(pool, tables) -> dict[str, list[dict]]:
    async with pool.acquire() as con:
        return {t: [dict(r) for r in await con.fetch(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables}


# ------------------------------------------------------------- idempotency


async def test_migrations_on_an_empty_database_then_again_change_nothing(empty_pool):
    latest = pg_module.latest_version()
    assert await pg_module.current_version(empty_pool) == 0
    assert await pg_module.apply_migrations(empty_pool) == list(range(1, latest + 1))
    first = await catalog(empty_pool)
    assert [v for v, _ in first["migrations"]] == list(range(1, latest + 1))
    assert len({n for _, n in first["migrations"]}) == latest  # every version recorded with its file name

    # Second run: nothing to apply, nothing re-applied, the catalog untouched.
    assert await pg_module.apply_migrations(empty_pool) == []
    assert await pg_module.current_version(empty_pool) == latest
    assert await catalog(empty_pool) == first
    await pg_module.assert_version(empty_pool, latest)  # what panel/manager check at boot


def _run_entrypoint(dsn: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=dsn, USERBOT_MASTER_KEY=TEST_MASTER_KEY)
    env.pop("USERBOT_MASTER_KEY_FILE", None)
    return subprocess.run(
        [sys.executable, "migrate_entrypoint.py"], cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=120,
    )


async def test_migrate_entrypoint_runs_twice_on_the_same_database(empty_pool):
    """The compose `migrate` service as it runs on every `up`: the real
    script, a real database. Each run must exit 0, the second must find
    nothing to do (migrations or backfill), and a third run after new data
    arrived backfills exactly that data."""
    dbname = f"deploy_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(PG_TEST_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    except asyncpg.InsufficientPrivilegeError:
        pytest.skip("PG_TEST_DSN's role cannot CREATE DATABASE")
    finally:
        await admin.close()
    dsn = PG_TEST_DSN.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        first = _run_entrypoint(dsn)
        assert first.returncode == 0, first.stdout + first.stderr
        assert f"applied migrations {list(range(1, pg_module.latest_version() + 1))}" in first.stdout

        second = _run_entrypoint(dsn)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "already at latest schema version" in second.stdout
        assert "customer_ref" not in second.stdout and "imported settings" not in second.stdout

        con = await asyncpg.connect(dsn)
        try:
            assert await con.fetchval("SELECT max(version) FROM schema_migrations") == pg_module.latest_version()
            # A pre-platform account whose tenant was never imported, with a
            # conversation that predates customer_ref: the backfill's work.
            await con.execute("INSERT INTO telegram_sessions (session_id, label) VALUES ('tg1', 'Salon')")
            await con.execute("INSERT INTO tenants (name, industry_id, session_id) VALUES ('Salon', 1, 'tg1')")
            await con.execute("INSERT INTO conversations (session_id, chat_id, display_name) VALUES ('tg1', 7, 'Ann')")
            await con.execute("UPDATE conversations SET customer_ref = NULL")
        finally:
            await con.close()

        third = _run_entrypoint(dsn)
        assert third.returncode == 0, third.stdout + third.stderr
        assert "already at latest schema version" in third.stdout
        assert "set customer_ref on 1 conversation(s)" in third.stdout
        assert "tenant 1: imported settings" in third.stdout

        fourth = _run_entrypoint(dsn)  # and now there is nothing left again
        assert fourth.returncode == 0, fourth.stdout + fourth.stderr
        assert "customer_ref" not in fourth.stdout and "imported settings" not in fourth.stdout
        con = await asyncpg.connect(dsn)
        try:
            assert await con.fetchval("SELECT count(*) FROM conversations WHERE customer_ref IS NULL") == 0
            assert await con.fetchval("SELECT count(*) FROM tenants WHERE legacy_imported_at IS NULL") == 0
        finally:
            await con.close()
    finally:
        admin = await asyncpg.connect(PG_TEST_DSN)
        try:
            await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()


# --------------------------------------------------------- populated upgrade


async def test_upgrading_a_populated_0005_database_keeps_the_data_and_adds_the_rules(empty_pool, tmp_path):
    """The database of a server on the previous release (0005), with the
    phase-4 tables in use: two tenants (one whose account was deleted),
    conversations, messages, bookings, a 'telegram' hold, sessions_health,
    alerts. Upgrade, then: every row still there, byte for byte apart from
    the new nullable columns; the channel set on both sides; the new
    constraints and triggers in force."""
    pool = empty_pool
    old = tmp_path / "m"
    old.mkdir()
    for name in BEFORE_0006:
        shutil.copy(pg_module.MIGRATIONS_DIR / name, old)
    assert await pg_module.apply_migrations(pool, old) == [1, 2, 3, 4, 5]

    async with pool.acquire() as con:
        await con.execute("INSERT INTO telegram_sessions (session_id, label, is_active, state) "
                          "VALUES ('tg1', 'Salon', true, 'running'), ('tg2', 'Garage', false, 'stopped')")
        salon = await con.fetchval("INSERT INTO tenants (name, industry_id, session_id) VALUES ('Salon', 1, 'tg1') RETURNING id")
        garage = await con.fetchval("INSERT INTO tenants (name, industry_id, session_id) VALUES ('Garage', 1, 'tg2') RETURNING id")
        orphan = await con.fetchval("INSERT INTO tenants (name, industry_id, session_id) VALUES ('Closed', 1, NULL) RETURNING id")
        await con.execute("INSERT INTO conversations (session_id, chat_id, display_name, customer_ref, unread) "
                          "VALUES ('tg1', 7, 'Ann', 'ref-7', 2), ('tg2', 9, 'Bob', NULL, 0)")
        await con.execute("INSERT INTO messages (session_id, chat_id, telegram_id, direction, status, text) VALUES "
                          "('tg1', 7, 100, 'in', 'received', 'hi'), ('tg1', 7, 101, 'out', 'sent', 'hello'), "
                          "('tg2', 9, 5, 'in', 'received', 'brakes?')")
        await con.execute(
            "INSERT INTO bookings (session_id, number, chat_id, customer_ref, starts_at, ends_at, blocked_until, "
            "tz, state, provider_chat_id, provider_message_id) VALUES "
            "('tg1', 1, 7, 'ref-7', '2026-10-01 10:00+00', '2026-10-01 11:00+00', '2026-10-01 11:00+00', "
            "'Europe/Riga', 'confirmed', 55, 900), "
            "('tg1', 2, 7, 'ref-7', '2026-10-02 10:00+00', '2026-10-02 11:00+00', '2026-10-02 11:00+00', "
            "'Europe/Riga', 'cancelled', 55, 901)"
        )
        await con.execute("INSERT INTO tenant_holds (tenant_id, kind, reason, created_by) VALUES "
                          "($1, 'telegram', 'FloodWait', 'system'), ($2, 'manual', 'unpaid', 'admin'), "
                          "($3, 'billing', '', 'system')", salon, garage, orphan)
        await con.execute("INSERT INTO sessions_health (tenant_id, session_id, status, status_reason, last_seen_at, "
                          "last_error, known_session_ids_json) VALUES "
                          "($1, 'tg1', 'connected', '', '2026-09-30 12:00+00', '', '[{\"hash\": 1}]'::jsonb), "
                          "($2, 'tg2', 'stopped', 'by admin', NULL, 'AuthKeyUnregistered', NULL)", salon, garage)
        await con.execute("INSERT INTO alerts (tenant_id, kind, severity, message) VALUES ($1, 'telegram', 'warning', "
                          "'FloodWait 30 s')", salon)
        tables = ("tenants", "telegram_sessions", "conversations", "messages", "bookings", "tenant_holds",
                  "sessions_health", "alerts")
        before = await table_rows(pool, tables)
        assert before["tenant_holds"] and before["sessions_health"] and before["alerts"]

    assert await pg_module.apply_migrations(pool) == list(range(6, pg_module.latest_version() + 1))
    assert await pg_module.apply_migrations(pool) == []

    after = await table_rows(pool, tables)
    # tenants.channel has been there since 0002; 0006 only has to leave it alone.
    new_cols = {"telegram_sessions": {"channel"}, "messages": {"wa_message_id"},
                "bookings": {"provider_wa_message_id",
                             # 0011: arrival cleanup
                             "instructions_message_ids", "instructions_cleanup_at", "instructions_cleaned_at"}}
    for table, rows in before.items():
        added = new_cols.get(table, set())
        assert [{k: v for k, v in r.items() if k not in added} for r in after[table]] == rows, table
    assert all(r["wa_message_id"] is None for r in after["messages"])
    assert all(r["provider_wa_message_id"] is None for r in after["bookings"])
    assert {r["session_id"]: r["channel"] for r in after["telegram_sessions"]} == {"tg1": "telegram", "tg2": "telegram"}
    assert {r["name"]: r["channel"] for r in after["tenants"]} == {"Salon": "telegram", "Garage": "telegram", "Closed": "telegram"}
    assert await pg_module.current_version(pool) == pg_module.latest_version()

    async with pool.acquire() as con:
        # The upgraded database enforces the same rules as a fresh one.
        with pytest.raises(asyncpg.CheckViolationError):
            await con.execute("INSERT INTO tenant_holds (tenant_id, kind, created_by) VALUES ($1, 'bogus', 'x')", salon)
        await con.execute("INSERT INTO tenant_holds (tenant_id, kind, created_by) VALUES ($1, 'whatsapp', 'x')", salon)
        with pytest.raises(asyncpg.CheckViolationError):
            await con.execute("UPDATE telegram_sessions SET channel = 'signal' WHERE session_id = 'tg1'")
        await con.execute("INSERT INTO telegram_sessions (session_id, channel) VALUES ('wa1', 'whatsapp')")
        wa = await con.fetchval("INSERT INTO tenants (name, industry_id, session_id, channel) "
                                "VALUES ('WA', 1, 'wa1', 'telegram') RETURNING id")
        assert await con.fetchval("SELECT channel FROM tenants WHERE id = $1", wa) == "whatsapp"
        await con.execute("INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES ('wa1', 'M1', '{}')")
        assert await con.fetchval("SELECT tenant_id FROM wa_inbox") == wa
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute("INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES ('wa1', 'M1', '{}')")
        with pytest.raises(asyncpg.RaiseError, match="does not own session"):
            await con.execute("INSERT INTO wa_auth_state (session_id, tenant_id, kind, key_id, value_enc) "
                              "VALUES ('wa1', $1, 'creds', '', '\\x00'::bytea)", salon)
        # Telegram's own rules still hold too.
        assert await con.fetchval("SELECT status FROM sessions_health WHERE tenant_id = $1", salon) == "connected"
        with pytest.raises(asyncpg.UniqueViolationError):
            await con.execute("INSERT INTO tenant_holds (tenant_id, kind, created_by) VALUES ($1, 'telegram', 'x')", salon)
