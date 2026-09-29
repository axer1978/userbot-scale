"""A first deploy, minus Docker: an empty database, the `migrate` service's
command run exactly as docker-compose.yml runs it (twice — the second run
must be a no-op), then every long-running service's module imported and its
boot-time schema check (`pg.assert_version`) run against that database.

The per-test schemas the rest of the suite uses live in a database that
already has the extensions; these tests create their own database (and, for
the non-superuser case, their own role) from nothing and drop it afterwards.
Needs PG_TEST_DSN, like every Postgres-backed test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio

import pg

APP_DIR = Path(__file__).resolve().parent.parent
PG_TEST_DSN = os.environ.get("PG_TEST_DSN")
MASTER_KEY = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI="

pytestmark = pytest.mark.requires_pg


def _with_db(dsn: str, dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((parts.scheme, netloc, "/" + dbname, parts.query, parts.fragment))


def _clean_env(**extra: str) -> dict[str, str]:
    """The process environment minus anything the stack reads, plus `extra`:
    what a container gets is only what compose passes it."""
    stack_vars = {
        "DATABASE_URL", "REDIS_URL", "USERBOT_MASTER_KEY", "USERBOT_MASTER_KEY_FILE",
        "ADMIN_PASSWORD", "ADMIN_TOTP_SECRET", "PANEL_DOMAIN", "ADMIN_HOST", "DATA_DIR",
        "PUBLIC_BASE_URL", "BOOKING_DOMAIN",
    }
    env = {k: v for k, v in os.environ.items() if k not in stack_vars}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def _run_migrate(dsn: str) -> subprocess.CompletedProcess:
    # docker-compose.yml: command: ["python", "migrate_entrypoint.py"], with
    # DATABASE_URL and USERBOT_MASTER_KEY from the shared app environment.
    return subprocess.run(
        [sys.executable, "migrate_entrypoint.py"],
        cwd=APP_DIR, env=_clean_env(DATABASE_URL=dsn, USERBOT_MASTER_KEY=MASTER_KEY),
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


@pytest_asyncio.fixture
async def fresh_db():
    """(dsn, dbname) of a brand-new, empty database; dropped afterwards."""
    if not PG_TEST_DSN:
        pytest.skip("PG_TEST_DSN not set; skipping Postgres-backed tests")
    import asyncpg

    name = f"fresh_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_with_db(PG_TEST_DSN, "postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield _with_db(PG_TEST_DSN, name), name
    finally:
        admin = await asyncpg.connect(_with_db(PG_TEST_DSN, "postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_fresh_database_migrates_then_every_service_accepts_it(fresh_db):
    import asyncpg

    dsn, _ = fresh_db
    latest = pg.latest_version()
    assert latest >= 1

    # Nothing but plpgsql in a brand-new database: 0003's btree_gist must be
    # created by the migration itself.
    con = await asyncpg.connect(dsn)
    try:
        assert {r["extname"] for r in await con.fetch("SELECT extname FROM pg_extension")} == {"plpgsql"}
    finally:
        await con.close()

    first = _run_migrate(dsn)
    assert first.returncode == 0, first.stdout + first.stderr
    assert f"applied migrations {list(range(1, latest + 1))}" in first.stdout

    second = _run_migrate(dsn)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already at latest schema version" in second.stdout

    con = await asyncpg.connect(dsn)
    try:
        versions = [r["version"] for r in await con.fetch("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, latest + 1))
        assert await con.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'") == 1
    finally:
        await con.close()

    # Each long-running service's module, imported with the environment
    # compose gives it (Valkey is not needed to import), then its boot-time
    # schema check. A separate interpreter: panel.py reads its settings at
    # import time and must not leak them into the rest of the suite.
    probe = (
        "import asyncio, panel, manager, scheduler, public_app, pg\n"
        "assert panel.check_public_setup() == [], panel.check_public_setup()\n"
        "async def main():\n"
        "    pool = await pg.create_pool(panel.DATABASE_URL, min_size=1, max_size=2)\n"
        "    try:\n"
        "        await pg.assert_version(pool, pg.latest_version())\n"
        "    finally:\n"
        "        await pool.close()\n"
        "asyncio.run(main())\n"
        "print('services-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=APP_DIR,
        env=_clean_env(
            DATABASE_URL=dsn,
            REDIS_URL="redis://valkey:6379/0",
            USERBOT_MASTER_KEY=MASTER_KEY,
            ADMIN_PASSWORD="x" * 32,
            ADMIN_TOTP_SECRET="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            PANEL_DOMAIN="1-2-3-4.sslip.io",
            ADMIN_HOST="0.0.0.0",
            DATA_DIR=str(APP_DIR / "data"),
        ),
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "services-ok" in result.stdout


@pytest.mark.asyncio
async def test_fresh_database_owned_by_a_non_superuser_migrates():
    """The compose Postgres user is a superuser, so btree_gist is never a
    problem there. On a managed Postgres the app role usually isn't: it
    only owns its database. btree_gist is a trusted extension (PG 13+), so
    that is enough."""
    if not PG_TEST_DSN:
        pytest.skip("PG_TEST_DSN not set; skipping Postgres-backed tests")
    import asyncpg

    suffix = uuid.uuid4().hex[:12]
    name, role, password = f"fresh_{suffix}", f"fresh_owner_{suffix}", uuid.uuid4().hex
    admin = await asyncpg.connect(_with_db(PG_TEST_DSN, "postgres"))
    try:
        await admin.execute(f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'")
        await admin.execute(f'CREATE DATABASE "{name}" OWNER "{role}"')
        try:
            result = _run_migrate(_with_db(PG_TEST_DSN, name, user=role, password=password))
            assert result.returncode == 0, result.stdout + result.stderr
            assert "applied migrations" in result.stdout
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
    finally:
        await admin.close()
