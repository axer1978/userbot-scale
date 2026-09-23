"""Shared fixtures.

Storage moved from one SQLite file / one config.json per process to a
session-scoped facade over a shared Postgres pool (see database.py,
config_store.py). Tests that need real Postgres semantics (test_database.py,
test_leasing.py, ...) request the `pg_pool` fixture, which spins up a
throwaway schema per test against `PG_TEST_DSN` and is skipped — loudly, via
`pytest.skip`, not silently — when that env var isn't set.

Task 6 (session_runtime.py) has landed, so the `app` fixture below builds a
real `SessionRuntime` instead of monkeypatching `main`'s old module-level
globals — `main.py` itself stays untouched and superseded, per design
decision D9; it is not patched back into working order.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

# The app modules import each other by bare name (`import config_store`), so
# the package directory has to be importable as-is.
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import config_store  # noqa: E402
import crypto  # noqa: E402
import pg as pg_module  # noqa: E402
from database import Database  # noqa: E402
from session_runtime import SessionRuntime  # noqa: E402

PG_TEST_DSN = os.environ.get("PG_TEST_DSN")
TEST_MASTER_KEY = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI="  # base64(32 bytes), fixed, test-only


class FakeHub:
    """Stands in for the websocket fan-out; keeps what was broadcast."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def broadcast(self, payload: dict) -> None:
        self.events.append(payload)

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]


@pytest.fixture(autouse=True)
def crypto_key(monkeypatch):
    """Every test gets a fixed, known AES-GCM key so crypto.py never touches
    a real secret and round-trips are deterministic."""
    monkeypatch.setenv("USERBOT_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.delenv("USERBOT_MASTER_KEY_FILE", raising=False)
    crypto.reset_cache()
    yield
    crypto.reset_cache()


@pytest_asyncio.fixture
async def pg_pool():
    """A pool bound to a throwaway schema, migrated to the latest version,
    dropped afterwards. Skips (doesn't fail) when PG_TEST_DSN isn't set."""
    if not PG_TEST_DSN:
        pytest.skip("PG_TEST_DSN not set; skipping Postgres-backed tests")
    import asyncpg

    schema = f"t_{uuid.uuid4().hex[:16]}"
    admin = await asyncpg.connect(PG_TEST_DSN)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()

    # A startup parameter, not `SET` in an init hook: the pool runs
    # `RESET ALL` on every release, which would put search_path back to
    # public after the first query and point every test at shared tables.
    pool = await asyncpg.create_pool(
        PG_TEST_DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        await pg_module.apply_migrations(pool)
        yield pool
    finally:
        await pool.close()
        admin = await asyncpg.connect(PG_TEST_DSN)
        try:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await admin.close()


@pytest_asyncio.fixture
async def db(pg_pool):
    """A `Database` bound to session_id "test", with its telegram_sessions
    row already seeded so foreign keys resolve."""
    async with pg_pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, is_active) VALUES ($1, TRUE)",
            "test",
        )
    database = Database(pg_pool, "test")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def app(pg_pool, db, tmp_path):
    """A `SessionRuntime` for session "test", built but never started.

    Config-only tests (settings routes, halt_everything, global pause) only
    touch `runtime.config` / `save_config()`, neither of which needs a
    lease, Redis, or a Telegram connection — `start()` would need all
    three and this session has none of them. Skipping `start()` means
    there is nothing to `stop()` in teardown either.
    """
    runtime = SessionRuntime(
        pg_pool, db.session_id, data_dir=tmp_path, redis_url="redis://unused"
    )
    runtime.hub = FakeHub()  # handle_send_failure and friends reach for it by attribute
    yield runtime


TEST_ADMIN_PASSWORD = "test-admin"


@pytest_asyncio.fixture
async def panel_client(pg_pool, tmp_path, monkeypatch):
    """An httpx client for panel.py's FastAPI app, already past the admin
    password, with the module's pool/registry/login flow bound to this
    test's schema and its command bus on fakeredis. The app's startup hook
    is not run (it would connect to the real DATABASE_URL/REDIS_URL).
    """
    import fakeredis
    import httpx

    # panel reads these at import time.
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    import commands
    import panel
    from database import SessionRegistry
    from login_flow import LoginFlow

    bus = commands.CommandBus(fakeredis.FakeAsyncRedis(decode_responses=True))
    monkeypatch.setattr(panel, "ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setattr(panel, "pool", pg_pool)
    monkeypatch.setattr(panel, "registry", SessionRegistry(pg_pool))
    monkeypatch.setattr(panel, "login_flow", LoginFlow(pg_pool))
    monkeypatch.setattr(panel, "bus", bus)
    monkeypatch.setattr(panel, "DATA_DIR", tmp_path)
    monkeypatch.setattr(panel, "_valid_tokens", {})
    monkeypatch.setattr(panel, "_login_failures", {})
    monkeypatch.setattr(panel, "_pending_deepseek_key", "")

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app), base_url="http://test")
    try:
        (await client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})).raise_for_status()
        yield client
    finally:
        await client.aclose()
        await panel.login_flow.reset()
        await bus.close()
