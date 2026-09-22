"""Shared fixtures.

Storage moved from one SQLite file / one config.json per process to a
session-scoped facade over a shared Postgres pool (see database.py,
config_store.py). Tests that need real Postgres semantics (test_database.py,
test_leasing.py, ...) request the `pg_pool` fixture, which spins up a
throwaway schema per test against `PG_TEST_DSN` and is skipped — loudly, via
`pytest.skip`, not silently — when that env var isn't set.

`main.py` (and therefore the `app` fixture below, and every test that
requests it — test_safety.py / test_concurrency.py / test_burst.py /
test_ai_responder.py / test_bookings.py / test_context_link.py /
test_media.py) is mid-rewrite: it still expects the old synchronous,
file-per-instance `Database`/`config_store` API and will only work again
once main.py's logic is ported into session_runtime.py (Task 6) and this
fixture is rebuilt around a `SessionRuntime`, per the design in the fleet
rewrite. Until then those tests are expected to fail at fixture setup, not
because of a bug introduced here.
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

    async def _init(conn: "asyncpg.Connection") -> None:
        await conn.execute(f'SET search_path TO "{schema}"')

    pool = await asyncpg.create_pool(PG_TEST_DSN, min_size=1, max_size=4, init=_init)
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
async def app(monkeypatch, db, tmp_path):
    """`main` with its globals pointed at throwaway state.

    BROKEN until Task 6 (session_runtime.py) lands: main.py still builds its
    own `Database(DB_PATH)` / `config_store.load()` at import time using the
    old file-based signatures, which no longer exist. Left in place,
    unmodified, so the diff for this rewrite stays visible; do not patch
    main.py's globals here to paper over it — that is exactly the kind of
    shim the rewrite is meant to remove (see design decision D9).
    """
    import main

    hub = FakeHub()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(main, "config", config_store.normalize({}))
    # Never write the real config.json from a test.
    monkeypatch.setattr(
        main.config_store, "save", lambda cfg, path=None: config_store.normalize(cfg)
    )
    # Presence and draft state are module-level; start every test from clean.
    monkeypatch.setattr(main, "active_chats", set())
    monkeypatch.setattr(main, "sending_chats", set())
    monkeypatch.setattr(main, "draft_tasks", {})
    monkeypatch.setattr(main, "in_flight_sends", {})
    monkeypatch.setattr(main, "presence_online", False)
    monkeypatch.setattr(main, "offline_timer", None)
    monkeypatch.setattr(main, "_ai_gate", None)
    monkeypatch.setattr(main, "_ai_gate_size", 0)

    main.hub = hub  # handle_send_failure reaches for it by attribute
    yield main
