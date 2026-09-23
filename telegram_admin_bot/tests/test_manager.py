"""Workers pick up sessions that become runnable after they started.

An account signed in from the panel is marked active while the manager is
already running; without this it would sit idle until someone restarted
the manager. SessionRuntime is replaced by a stand-in that does the one
thing that matters here — take the session's lease, as the real start()
does — so the workers' race for a new session is decided by real Postgres.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import asyncpg
import pytest

import leasing
from conftest import PG_TEST_DSN
from session_runtime import NeedsLogin


class FakeRuntime:
    started: list[tuple[str, str]] = []
    attempts: list[str] = []
    needs_login: set[str] = set()

    def __init__(self, pool, session_id, *, data_dir, redis_url, worker_id):
        self.pool, self.session_id, self.worker_id = pool, session_id, worker_id

    async def start(self):
        FakeRuntime.attempts.append(self.session_id)
        if await leasing.acquire(self.pool, self.session_id, self.worker_id) is None:
            raise leasing.LeaseLost(self.session_id)
        if self.session_id in FakeRuntime.needs_login:
            await leasing.release(self.pool, self.session_id, self.worker_id)
            raise NeedsLogin(self.session_id)
        FakeRuntime.started.append((self.worker_id, self.session_id))

    async def stop(self):
        await leasing.release(self.pool, self.session_id, self.worker_id)


@pytest.fixture
def manager(pg_pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    import manager as manager_module

    async def own_pool(_dsn, **_kw):
        # Each worker opens (and closes) its own pool, bound to this test's schema.
        async with pg_pool.acquire() as con:
            schema = await con.fetchval("SHOW search_path")
        return await asyncpg.create_pool(PG_TEST_DSN, min_size=1, max_size=2,
                                         server_settings={"search_path": schema})

    FakeRuntime.started, FakeRuntime.attempts, FakeRuntime.needs_login = [], [], set()
    monkeypatch.setattr(manager_module.pg, "create_pool", own_pool)
    monkeypatch.setattr(manager_module, "SessionRuntime", FakeRuntime)
    monkeypatch.setattr(manager_module, "ADOPT_INTERVAL_SECONDS", 0.1)
    return manager_module


async def seed(pg_pool, session_id, *, active):
    async with pg_pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, is_active) VALUES ($1, $2)", session_id, active
        )


async def activate(pg_pool, session_id):
    async with pg_pool.acquire() as con:
        await con.execute("UPDATE telegram_sessions SET is_active = TRUE WHERE session_id = $1", session_id)


async def until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def run_workers(manager, n):
    stop = threading.Event()
    tasks = [
        asyncio.create_task(
            manager._worker_async_main(f"worker-{i}", [], stop, logging.getLogger(f"test.w{i}"))
        )
        for i in range(n)
    ]
    return stop, tasks


@pytest.mark.asyncio
async def test_a_session_activated_after_startup_gets_picked_up(manager, pg_pool):
    await seed(pg_pool, "acct01", active=False)
    stop, tasks = run_workers(manager, 1)
    try:
        await asyncio.sleep(1.5)
        assert FakeRuntime.attempts == []  # inactive: left alone

        await activate(pg_pool, "acct01")
        await until(lambda: FakeRuntime.started)
        assert FakeRuntime.started == [("worker-0", "acct01")]
    finally:
        stop.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_two_workers_racing_for_a_new_session_start_it_once(manager, pg_pool):
    await seed(pg_pool, "acct01", active=False)
    stop, tasks = run_workers(manager, 2)
    try:
        await asyncio.sleep(0.5)
        await activate(pg_pool, "acct01")
        await until(lambda: FakeRuntime.started)
        await asyncio.sleep(2.5)  # both workers have polled again since
        assert [sid for _, sid in FakeRuntime.started] == ["acct01"]
    finally:
        stop.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_a_session_that_fails_to_start_is_not_retried_every_poll(manager, pg_pool):
    await seed(pg_pool, "broken", active=True)
    FakeRuntime.needs_login.add("broken")
    stop, tasks = run_workers(manager, 1)
    try:
        await until(lambda: FakeRuntime.attempts)
        await asyncio.sleep(2.5)
        assert FakeRuntime.attempts == ["broken"]
        assert FakeRuntime.started == []
    finally:
        stop.set()
        await asyncio.gather(*tasks)
