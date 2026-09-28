"""The scheduler: one at a time, ticks only accounts that are running, and
keeps quiet-hours replies in Postgres per tenant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

import commands
import scheduler
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

T = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)


def bus():
    return commands.CommandBus(fakeredis.FakeAsyncRedis(decode_responses=True))


async def test_only_accounts_with_a_live_lease_are_ticked(pg_pool):
    for sid in ("running", "stopped", "expired"):
        await seed_session(pg_pool, sid)
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', lease_expires_at = now() + "
                          "interval '1 minute' WHERE session_id = 'running'")
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', lease_expires_at = now() - "
                          "interval '1 minute' WHERE session_id = 'expired'")
    b = bus()
    got, stop = [], asyncio.Event()

    async def worker(action, args):
        got.append(action)
        return {"ok": True}

    serving = asyncio.create_task(b.serve("running", worker, stop))
    await asyncio.sleep(0.05)
    try:
        result = await scheduler.tick(pg_pool, b)
    finally:
        stop.set()
        await serving
    assert result == {"running": "ok"} and got == ["scheduler_tick"]


async def test_a_worker_that_does_not_answer_does_not_stop_the_round(pg_pool, monkeypatch):
    for sid in ("a", "b"):
        await seed_session(pg_pool, sid)
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', lease_expires_at = now() + "
                          "interval '1 minute'")
    monkeypatch.setattr(scheduler, "TICK_TIMEOUT_SECONDS", 0.2)
    b = bus()
    stop = asyncio.Event()

    async def worker(action, args):
        return {"ok": True}

    serving = asyncio.create_task(b.serve("b", worker, stop))
    await asyncio.sleep(0.05)
    try:
        result = await scheduler.tick(pg_pool, b)
    finally:
        stop.set()
        await serving
    assert result["b"] == "ok" and result["a"] != "ok"


async def test_only_one_scheduler_runs(pg_pool, monkeypatch):
    ticks = []

    async def fake_tick(pool, b):
        ticks.append(asyncio.current_task().get_name())
        return {}

    monkeypatch.setattr(scheduler, "tick", fake_tick)
    stop_one, stop_two = asyncio.Event(), asyncio.Event()
    one = asyncio.create_task(scheduler.run(pg_pool, bus(), stop_one, tick_seconds=0.05), name="one")
    await asyncio.sleep(0.1)
    two = asyncio.create_task(scheduler.run(pg_pool, bus(), stop_two, tick_seconds=0.05), name="two")
    await asyncio.sleep(0.3)
    assert set(ticks) == {"one"}
    # When the first stops, the second takes over.
    stop_one.set()
    await one
    await asyncio.sleep(0.3)
    stop_two.set()
    await two
    assert "two" in ticks


async def test_deferred_replies_are_one_per_chat_taken_once_and_per_tenant(pg_pool):
    a = await seed_session(pg_pool, "acc_a")
    b = await seed_session(pg_pool, "acc_b")
    await scheduler.defer_reply(pg_pool, a, "acc_a", 7, T + timedelta(hours=1))
    await scheduler.defer_reply(pg_pool, a, "acc_a", 7, T + timedelta(hours=2))  # a later message moves it
    await scheduler.defer_reply(pg_pool, b, "acc_b", 7, T)
    assert [r["due_at"] for r in await scheduler.deferred_for(pg_pool, a)] == [T + timedelta(hours=2)]

    assert await scheduler.take_due_deferred(pg_pool, a, T + timedelta(hours=1)) == []
    assert await scheduler.take_due_deferred(pg_pool, a, T + timedelta(hours=3)) == [7]
    assert await scheduler.take_due_deferred(pg_pool, a, T + timedelta(hours=3)) == []
    # Tenant b's reply is untouched by tenant a's tick.
    assert [r["chat_id"] for r in await scheduler.deferred_for(pg_pool, b)] == [7]
