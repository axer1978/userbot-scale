"""Crash tests: what the runtime does when its surroundings fail.

Postgres dropping connections, Valkey (the command bus) going away, a slow
or broken account during the scheduler's round, DeepSeek timing out or
answering garbage, a restart in the middle of a booking, the clocks
changing, odd messages, and memory that must not grow per chat.

Nothing here stops the shared test Postgres: a database outage is a pool
wrapper (`Outage`) that raises the errors asyncpg raises when the server
goes away, and a lost connection is one backend of this test terminated
with pg_terminate_backend. Valkey is fakeredis with its connection switched
off. Telegram and the model are faked as in test_safety_runtime.py.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import asyncpg
import fakeredis
import httpx
import pytest
import pytest_asyncio
from telethon import errors

import ai_responder
import alerts
import audit
import billing
import booking_states as bs
import commands
import controls
import digest
import health
import humanlike
import leasing
import scheduler
import session_runtime
from conftest import FakeHub, PG_TEST_DSN, seed_session
from database import DIR_IN, STATUS_ERROR, STATUS_RECEIVED
from session_runtime import SessionRuntime

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

RIGA = ZoneInfo("Europe/Riga")
OWNER = 999
CUSTOMER = 42
NOW = datetime(2030, 3, 4, 12, 0, tzinfo=RIGA)   # a Monday, midday
REAL_SLEEP = asyncio.sleep
_ids = itertools.count(1)


def dropped() -> Exception:
    return asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation")


class Outage:
    """A pool whose calls fail like a database that went away.

    `fail(n)` makes the next n calls raise; `down()` / `up()` bracket a
    longer outage. Everything else is the real pool underneath."""

    def __init__(self, pool, make_error=dropped):
        self._pool = pool
        self._make_error = make_error
        self.remaining = 0
        self.is_down = False
        self.raised = 0

    def fail(self, n: int, make_error=None) -> None:
        self.remaining = n
        if make_error is not None:
            self._make_error = make_error

    def down(self) -> None:
        self.is_down = True

    def up(self) -> None:
        self.is_down = False
        self.remaining = 0

    def _check(self) -> None:
        if self.is_down or self.remaining > 0:
            if self.remaining > 0:
                self.remaining -= 1
            self.raised += 1
            raise self._make_error()

    def acquire(self, *a, **k):
        self._check()
        return self._pool.acquire(*a, **k)

    async def fetch(self, *a, **k):
        self._check()
        return await self._pool.fetch(*a, **k)

    async def fetchrow(self, *a, **k):
        self._check()
        return await self._pool.fetchrow(*a, **k)

    async def fetchval(self, *a, **k):
        self._check()
        return await self._pool.fetchval(*a, **k)

    async def execute(self, *a, **k):
        self._check()
        return await self._pool.execute(*a, **k)

    async def executemany(self, *a, **k):
        self._check()
        return await self._pool.executemany(*a, **k)

    def __getattr__(self, name):
        return getattr(self._pool, name)


class FakeEvent:
    def __init__(self, chat_id, text="", message_id=None, photo=False, sender=None):
        self.is_private = True
        self.chat_id = chat_id
        self.raw_text = text
        self.message = SimpleNamespace(id=message_id or next(_ids), reply_to_msg_id=None,
                                       photo=object() if photo else None)
        self._sender = sender

    async def get_sender(self):
        if self._sender is not None:
            return self._sender
        return SimpleNamespace(id=self.chat_id, first_name="Anna", last_name=None, username="anna",
                               bot=False, access_hash=1)

    get_chat = get_sender


def live_bus(server=None) -> commands.CommandBus:
    return commands.CommandBus(fakeredis.FakeAsyncRedis(server=server or fakeredis.FakeServer(),
                                                        decode_responses=True))


async def until(predicate, timeout=5.0, step=0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out waiting"
        await REAL_SLEEP(step)


async def quick_sleep(_seconds=0, *a, **k):
    """asyncio.sleep for the runtime under test: no waiting, but it still
    yields, so a loop around it can't starve the test."""
    await REAL_SLEEP(0)


def random_lock_key() -> int:
    # Advisory locks are database-wide: another test (or another run) must
    # not share this scheduler's lock.
    return random.randint(1_000_000, 2_000_000_000)


# ------------------------------------------------------------ a runtime


@pytest_asyncio.fixture
async def rt(pg_pool, tmp_path, monkeypatch):
    """A SessionRuntime over an `Outage` pool, with Telegram and the model
    faked: sends land in `sent`, the model answers `script["reply"]`."""
    await seed_session(pg_pool, "test")
    pool = Outage(pg_pool)
    app = SessionRuntime(pool, "test", data_dir=tmp_path, redis_url="redis://unused")
    app.hub = FakeHub()
    await app.bind_tenant()

    sent: dict[int, list[str]] = {}
    script = {"reply": "Sure, see you then.", "calls": 0, "fail": None}
    clock = {"now": NOW}
    msg_ids = iter(range(5000, 90_000))

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.setdefault(chat_id, []).append(text)
        return SimpleNamespace(id=next(msg_ids))

    async def fake_reply(**kw):
        script["calls"] += 1
        if script["fail"] is not None:
            raise script["fail"]
        return script["reply"]

    async def nothing(*a, **k):
        return None

    async def owner_chat():
        return OWNER

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "utcnow", lambda: clock["now"].astimezone(timezone.utc))
    monkeypatch.setattr(app.flow, "provider_chat_id", owner_chat)
    monkeypatch.setattr(app.flow, "calendar_client", lambda: None)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", quick_sleep)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    app.deepseek_key = "k"
    app.client = SimpleNamespace(is_connected=lambda: False)
    app.config["auto_send"] = True
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    app.config["api_spend_cap_eur"] = 0
    app.telegram_state["connected"] = True
    app.me_info = {"id": 1000, "name": "Salon", "username": "salon"}
    yield SimpleNamespace(app=app, pool=pool, real=pg_pool, sent=sent, script=script, clock=clock)
    await alerts.drain()


async def settle(app):
    for _ in range(20):
        tasks = [t for t in [*app.flow.scan_tasks.values(), *app.draft_tasks.values(),
                             *getattr(app, "_background", ())]
                 if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def customer(rt, text, chat=CUSTOMER, **event):
    await rt.app.on_incoming(FakeEvent(chat, text, **event))
    await settle(rt.app)


async def stored(rt, chat=CUSTOMER):
    return [m["text"] for m in await rt.app.db.get_messages(chat) if m["direction"] == DIR_IN]


# =====================================================================
# 1. Postgres unreachable, mid-flight
# =====================================================================


async def test_scheduler_keeps_ticking_through_a_database_outage(pg_pool, monkeypatch):
    monkeypatch.setattr(scheduler, "LOCK_KEY", random_lock_key())
    await seed_session(pg_pool, "acct")
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', "
                          "lease_expires_at = now() + interval '1 hour'")
    pool = Outage(pg_pool)
    b, stop, serve_stop = live_bus(), asyncio.Event(), asyncio.Event()
    ticks: list[str] = []

    async def worker(action, args):
        ticks.append(action)
        return {"ok": True}

    serving = asyncio.create_task(b.serve("acct", worker, serve_stop))
    running = asyncio.create_task(scheduler.run(pool, b, stop, tick_seconds=0.05))
    try:
        await until(lambda: ticks)
        pool.down()
        await REAL_SLEEP(0.3)                          # every round fails meanwhile
        assert not running.done()
        before = len(ticks)
        pool.up()
        await until(lambda: len(ticks) >= before + 2)  # and it carries on by itself
    finally:
        stop.set()
        serve_stop.set()
        await asyncio.wait_for(running, 5)
        await serving


async def test_the_platform_round_still_runs_when_the_heartbeat_write_fails(pg_pool, monkeypatch):
    pool = Outage(pg_pool)
    ran = []

    async def fake(name):
        ran.append(name)
        return []

    monkeypatch.setattr(health, "check_all", lambda p, now=None: fake("health"))
    monkeypatch.setattr(billing, "tick", lambda p, b, now=None: fake("billing"))
    monkeypatch.setattr(digest, "tick", lambda p, b, now=None: fake("digest"))
    pool.fail(1)                                        # the heartbeat's write
    await scheduler.platform_tick(pool, live_bus())
    assert ran == ["health", "billing", "digest"]


async def test_a_scheduler_that_loses_its_lock_connection_never_ticks_alongside_another(pg_pool, monkeypatch):
    key = random_lock_key()
    monkeypatch.setattr(scheduler, "LOCK_KEY", key)
    monkeypatch.setattr(scheduler, "RETRY_SECONDS", 0.05)
    ticks: list[str] = []

    async def fake_tick(pool, b):
        ticks.append(asyncio.current_task().get_name())
        return {}

    async def no_platform(pool, b):
        return None

    monkeypatch.setattr(scheduler, "tick", fake_tick)
    monkeypatch.setattr(scheduler, "platform_tick", no_platform)
    stop_one, stop_two = asyncio.Event(), asyncio.Event()
    one = asyncio.create_task(scheduler.run(pg_pool, live_bus(), stop_one, tick_seconds=0.05), name="one")
    await until(lambda: "one" in ticks)
    two = asyncio.create_task(scheduler.run(pg_pool, live_bus(), stop_two, tick_seconds=0.05), name="two")
    await REAL_SLEEP(0.2)
    assert "two" not in ticks

    # The first scheduler's database connection dies, and the lock with it.
    pid = await pg_pool.fetchval(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objid::text::bigint = $1 "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())", key)
    assert pid is not None
    await pg_pool.fetchval("SELECT pg_terminate_backend($1)", pid)

    await REAL_SLEEP(0.4)
    ticks.clear()
    await REAL_SLEEP(0.6)
    # Whichever holds the lock now ticks; never both.
    assert len(set(ticks)) == 1, ticks
    stop_one.set()
    stop_two.set()
    await asyncio.wait_for(asyncio.gather(one, two), 5)


async def test_lease_keeper_fences_before_the_lease_expires_when_renewal_hangs(monkeypatch):
    loop = asyncio.get_running_loop()
    started = loop.time()
    lost: list[float] = []

    async def on_lost(session_id):
        lost.append(loop.time() - started)

    async def hang(*a, **k):
        await asyncio.Event().wait()                   # Postgres unreachable: no answer at all

    monkeypatch.setattr(leasing, "renew_many", hang)
    keeper = leasing.LeaseKeeper(object(), "w1", on_lost=on_lost, interval=0.2, danger=0.5, ttl=1)
    keeper.track(leasing.Lease("acct01", "w1", 1, datetime.now(timezone.utc)))
    task = asyncio.create_task(keeper.run())
    try:
        await REAL_SLEEP(1.2)
        # Fenced at the danger mark, well before the (1s) lease ran out.
        assert lost and lost[0] < 0.8, lost
    finally:
        await keeper.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_lease_keeper_fences_at_the_danger_mark_when_postgres_refuses(pg_pool):
    """Renewal every 0.4s, danger at 0.5s: the fence must come at 0.5s, not
    at the next regular renewal (0.8s, past a 0.6s lease)."""
    await seed_session(pg_pool, "acct01")
    pool = Outage(pg_pool)
    loop = asyncio.get_running_loop()
    lost: list[float] = []
    keeper = leasing.LeaseKeeper(pool, "w1", on_lost=lambda s: _append(lost, loop.time() - started),
                                 interval=0.4, danger=0.5)
    keeper.track(await leasing.acquire(pg_pool, "acct01", "w1"))
    started = loop.time()
    pool.down()
    task = asyncio.create_task(keeper.run())
    try:
        await REAL_SLEEP(1.0)
        assert lost and 0.45 <= lost[0] < 0.7, lost
        assert not task.done()                          # the keeper itself carries on
    finally:
        await keeper.stop()
        await asyncio.wait_for(task, 2)


async def _append(target, value):
    target.append(value)


async def test_lease_keeper_rides_out_a_blip_shorter_than_the_danger_window(pg_pool):
    await seed_session(pg_pool, "acct01")
    pool = Outage(pg_pool)
    lost: list[str] = []
    keeper = leasing.LeaseKeeper(pool, "w1", on_lost=lambda s: _append(lost, s), interval=0.1, danger=0.5)
    keeper.track(await leasing.acquire(pg_pool, "acct01", "w1"))
    pool.fail(2)
    task = asyncio.create_task(keeper.run())
    await REAL_SLEEP(0.6)
    await keeper.stop()
    await asyncio.wait_for(task, 2)
    assert lost == [] and keeper.is_safe("acct01")


async def test_the_health_loop_keeps_going_when_reporting_fails(rt, monkeypatch):
    seen: list[str] = []
    fails = {"left": 2}

    async def flaky_seen(pool, tenant_id, session_id):
        if fails["left"]:
            fails["left"] -= 1
            raise dropped()
        seen.append(session_id)

    monkeypatch.setattr(health, "seen", flaky_seen)
    rt.app.REMINDER_TICK_SECONDS = 0
    loop_task = asyncio.create_task(rt.app.reminder_loop())
    try:
        await until(lambda: seen)
        assert not loop_task.done()
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_a_reply_interrupted_by_the_database_is_written_again_and_sent_once(rt):
    await rt.app.on_incoming(FakeEvent(CUSTOMER, "Are you open tomorrow?"))
    # The message is stored; the database drops out as the reply is drafted.
    rt.pool.fail(2)
    await settle(rt.app)
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]
    assert rt.app.draft_tasks == {}


async def test_a_reply_is_never_retried_once_it_may_have_gone_out(rt, monkeypatch):
    """The database fails right after Telegram took the message: retrying
    could send it twice, so it is reported instead."""
    real_record = rt.app.db.record_message

    async def record(chat_id, direction, status, text, **kw):
        if direction == "out" and status == "sent":
            raise dropped()
        return await real_record(chat_id, direction, status, text, **kw)

    await rt.app.on_incoming(FakeEvent(CUSTOMER, "Hello"))
    monkeypatch.setattr(rt.app.db, "record_message", record)
    await settle(rt.app)
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


# =====================================================================
# 2. Valkey (the command bus) down
# =====================================================================


async def test_dispatch_says_the_bus_is_down_as_a_command_error():
    server = fakeredis.FakeServer()
    b = live_bus(server)
    server.connected = False
    with pytest.raises(commands.CommandError) as info:
        await b.dispatch("acct", "scheduler_tick", {}, timeout=1)
    assert isinstance(info.value, commands.BusUnavailable)


async def test_the_scheduler_round_survives_the_bus_being_down(pg_pool):
    for sid in ("a", "b"):
        await seed_session(pg_pool, sid)
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', "
                          "lease_expires_at = now() + interval '1 hour'")
    server = fakeredis.FakeServer()
    b = live_bus(server)
    server.connected = False
    result = await scheduler.tick(pg_pool, b)
    assert set(result) == {"a", "b"} and all(v != "ok" for v in result.values())


async def test_reload_controls_is_best_effort_when_the_bus_is_down(pg_pool):
    await seed_session(pg_pool, "a")
    await pg_pool.execute("UPDATE telegram_sessions SET lease_expires_at = now() + interval '1 hour'")
    server = fakeredis.FakeServer()
    b = live_bus(server)
    server.connected = False
    await controls.reload_controls(pg_pool, b)          # no exception


async def test_billing_still_suspends_and_retries_the_notice_when_the_bus_is_down(pg_pool, monkeypatch):
    for name in ("SMTP_HOST", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    notice_tid = await seed_session(pg_pool, "notice")
    late_tid = await seed_session(pg_pool, "late")
    now = datetime(2030, 3, 4, 12, 0, tzinfo=timezone.utc)
    await pg_pool.execute("UPDATE tenants SET status = 'grace', grace_until = $2 WHERE id = $1",
                          notice_tid, now + timedelta(hours=10))
    await pg_pool.execute("UPDATE tenants SET status = 'grace', grace_until = $2, billing_notice_sent_at = now() "
                          "WHERE id = $1", late_tid, now - timedelta(minutes=1))
    await pg_pool.execute("UPDATE tenants SET billing_next_due = '2030-03-01'")
    server = fakeredis.FakeServer()
    b = live_bus(server)
    server.connected = False

    changed = await billing.tick(pg_pool, b, now)
    assert changed == [f"{late_tid}:suspended"]
    assert "billing" in await controls.off_reason(pg_pool, late_tid)
    assert await pg_pool.fetchval("SELECT billing_notice_sent_at FROM tenants WHERE id = $1", notice_tid) is None
    kinds = {(a["tenant_id"], a["kind"]) for a in await alerts.list_alerts(pg_pool, open_only=True)}
    assert (notice_tid, "billing_notice") in kinds

    # The bus is back: the next tick tells the owner.
    class Answering:
        async def dispatch(self, session_id, action, args=None, *, timeout=30):
            return {"sent": True, "error": ""}

    assert await billing.tick(pg_pool, Answering(), now + timedelta(minutes=1)) == [f"{notice_tid}:notified"]
    assert await pg_pool.fetchval("SELECT billing_notice_sent_at FROM tenants WHERE id = $1", notice_tid)
    await alerts.drain()


async def test_the_digest_records_what_happened_when_the_bus_is_down(pg_pool, monkeypatch):
    for name in ("SMTP_HOST", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    tid = await seed_session(pg_pool, "acct", name="Salon Anna")
    await pg_pool.execute("UPDATE telegram_sessions SET lease_expires_at = now() + interval '1 hour'")
    server = fakeredis.FakeServer()
    b = live_bus(server)
    server.connected = False
    monday = datetime(2030, 3, 4, 10, 0, tzinfo=RIGA)
    [via] = (await digest.tick(pg_pool, b, monday)).values()
    assert via.startswith("none: telegram: the account did not answer (BusUnavailable)")
    assert await pg_pool.fetchval("SELECT sent_via FROM digest_log WHERE tenant_id = $1", tid) == via
    assert [a["kind"] for a in await alerts.list_alerts(pg_pool, open_only=True)] == ["digest"]
    await alerts.drain()


async def test_live_events_failing_never_break_message_handling(rt):
    server = fakeredis.FakeServer()
    rt.app.bus = live_bus(server)
    rt.app.hub = session_runtime.Hub(rt.app)
    server.connected = False
    await customer(rt, "Hi, still there?")
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


async def test_a_hanging_bus_never_holds_a_message_up(rt, monkeypatch):
    class Hanging:
        async def publish(self, *a, **k):
            await asyncio.Event().wait()

    monkeypatch.setattr(commands, "PUBLISH_TIMEOUT_SECONDS", 0.05)
    rt.app.bus = commands.CommandBus(Hanging())
    rt.app.hub = session_runtime.Hub(rt.app)
    await asyncio.wait_for(customer(rt, "Hello?"), 10)
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


async def test_the_command_server_reconnects_after_the_bus_comes_back(monkeypatch):
    monkeypatch.setattr(commands, "SERVE_BACKOFF_SECONDS", 0.05)
    monkeypatch.setattr(commands, "SERVE_BACKOFF_MAX_SECONDS", 0.2)
    server = fakeredis.FakeServer()
    worker_bus, panel_bus = live_bus(server), live_bus(server)
    stop = asyncio.Event()

    async def handler(action, args):
        return {"pong": action}

    serving = asyncio.create_task(worker_bus.serve("acct", handler, stop))
    await REAL_SLEEP(0.05)                              # subscribed
    try:
        assert await panel_bus.dispatch("acct", "ping", timeout=2) == {"pong": "ping"}
        server.connected = False
        await REAL_SLEEP(0.4)
        assert not serving.done(), serving.exception() if serving.done() else None
        server.connected = True
        # Commands reach the worker again, with no restart.
        await until(lambda: False if serving.done() else True)
        result = None
        for _ in range(20):
            try:
                result = await panel_bus.dispatch("acct", "ping", timeout=0.5)
                break
            except commands.CommandTimeout:
                continue
        assert result == {"pong": "ping"}
    finally:
        stop.set()
        await asyncio.wait_for(serving, 5)


async def test_the_runtime_starts_a_command_server_that_died_again(rt):
    server = fakeredis.FakeServer()
    rt.app.bus = live_bus(server)

    async def died():
        raise RuntimeError("serve crashed")

    dead = asyncio.create_task(died())
    await asyncio.gather(dead, return_exceptions=True)
    rt.app._command_serve_task = dead
    rt.app.ensure_command_server()
    try:
        assert rt.app._command_serve_task is not dead and not rt.app._command_serve_task.done()
        await REAL_SLEEP(0.05)
        reply = await live_bus(server).dispatch("test", "reload_controls", timeout=3)
        assert reply == {"off": ""}
    finally:
        rt.app._command_stop_event.set()
        await asyncio.wait_for(rt.app._command_serve_task, 5)


# =====================================================================
# 3. The scheduler's round
# =====================================================================


async def test_one_accounts_broken_tick_does_not_stop_the_others(pg_pool):
    for sid in ("a", "b", "c"):
        await seed_session(pg_pool, sid)
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', "
                          "lease_expires_at = now() + interval '1 hour'")
    real = live_bus()
    stop = asyncio.Event()

    async def ok(action, args):
        return {"ok": True}

    async def broken(action, args):
        raise RuntimeError("bad booking row")

    serving = [asyncio.create_task(real.serve("b", broken, stop)), asyncio.create_task(real.serve("c", ok, stop))]
    await REAL_SLEEP(0.05)

    class Bus:
        """Account a: the bus call itself blows up in an unexpected way."""

        async def dispatch(self, session_id, action, args=None, *, timeout=30):
            if session_id == "a":
                raise ValueError("unexpected")
            return await real.dispatch(session_id, action, args, timeout=timeout)

    try:
        result = await scheduler.tick(pg_pool, Bus())
    finally:
        stop.set()
        await asyncio.gather(*serving)
    assert result["c"] == "ok"
    assert "unexpected" in result["a"] and "bad booking row" in result["b"]


async def test_a_hanging_account_does_not_hold_up_the_round(pg_pool, monkeypatch):
    for sid in ("slow", "fast"):
        await seed_session(pg_pool, sid)
    await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = 'w', "
                          "lease_expires_at = now() + interval '1 hour'")
    monkeypatch.setattr(scheduler, "TICK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(scheduler, "TICK_GRACE_SECONDS", 0.1)

    class Bus:
        async def dispatch(self, session_id, action, args=None, *, timeout=30):
            if session_id == "slow":
                await asyncio.Event().wait()           # ignores its own timeout
            return {"ok": True}

    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(scheduler.tick(pg_pool, Bus()), 5)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert result == {"fast": "ok", "slow": "timed out"}


async def test_a_scheduler_stopped_mid_round_lets_the_next_one_take_over(pg_pool, monkeypatch):
    monkeypatch.setattr(scheduler, "LOCK_KEY", random_lock_key())
    in_round = asyncio.Event()
    ticks: list[str] = []

    async def slow_tick(pool, b):
        ticks.append(asyncio.current_task().get_name())
        if asyncio.current_task().get_name() == "first":
            in_round.set()
            await asyncio.Event().wait()                # killed in the middle of this
        return {}

    async def no_platform(pool, b):
        return None

    monkeypatch.setattr(scheduler, "tick", slow_tick)
    monkeypatch.setattr(scheduler, "platform_tick", no_platform)
    first = asyncio.create_task(scheduler.run(pg_pool, live_bus(), asyncio.Event(), tick_seconds=0.05),
                                name="first")
    await asyncio.wait_for(in_round.wait(), 5)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    stop = asyncio.Event()
    second = asyncio.create_task(scheduler.run(pg_pool, live_bus(), stop, tick_seconds=0.05), name="second")
    await until(lambda: "second" in ticks, timeout=3)
    stop.set()
    await asyncio.wait_for(second, 5)


# =====================================================================
# 4. Worker and runtime
# =====================================================================


class FakeWorkerRuntime:
    instances: list["FakeWorkerRuntime"] = []

    def __init__(self, pool, session_id, *, data_dir, redis_url, worker_id):
        self.pool, self.session_id, self.worker_id = pool, session_id, worker_id
        self.finished = False
        self.stopped = False
        FakeWorkerRuntime.instances.append(self)

    async def start(self):
        if await leasing.acquire(self.pool, self.session_id, self.worker_id) is None:
            raise leasing.LeaseLost(self.session_id)

    async def stop(self):
        self.stopped = True
        await leasing.release(self.pool, self.session_id, self.worker_id)
        raise RuntimeError("stop failed half way")      # the worker must drop it all the same


async def test_the_worker_drops_a_finished_runtime_and_picks_the_account_up_again(pg_pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    import manager as manager_module

    async def own_pool(_dsn, **_kw):
        async with pg_pool.acquire() as con:
            schema = await con.fetchval("SHOW search_path")
        return await asyncpg.create_pool(PG_TEST_DSN, min_size=1, max_size=2,
                                         server_settings={"search_path": schema})

    FakeWorkerRuntime.instances = []
    monkeypatch.setattr(manager_module.pg, "create_pool", own_pool)
    monkeypatch.setattr(manager_module, "SessionRuntime", FakeWorkerRuntime)
    monkeypatch.setattr(manager_module, "ADOPT_INTERVAL_SECONDS", 0.1)
    await pg_pool.execute("INSERT INTO telegram_sessions (session_id, is_active) VALUES ('acct01', TRUE)")

    stop = threading.Event()
    worker = asyncio.create_task(manager_module._worker_async_main(
        "worker-0", ["acct01"], stop, logging.getLogger("test.worker")))
    try:
        await until(lambda: FakeWorkerRuntime.instances)
        first = FakeWorkerRuntime.instances[0]
        first.finished = True                           # fenced / logged out / hard-off
        await until(lambda: first.stopped, timeout=5)
        await until(lambda: len(FakeWorkerRuntime.instances) >= 2, timeout=5)
        assert FakeWorkerRuntime.instances[1] is not first
    finally:
        stop.set()
        await asyncio.gather(worker, return_exceptions=True)


async def test_stopping_tears_everything_down_even_when_the_database_is_gone(rt, monkeypatch):
    app = rt.app
    server = fakeredis.FakeServer()
    app.bus = live_bus(server)
    app._command_serve_task = asyncio.create_task(app.bus.serve(app.session_id, app.handle_command,
                                                                 app._command_stop_event))
    app.http_client = httpx.AsyncClient()

    async def refused(*a, **k):
        raise dropped()

    monkeypatch.setattr(leasing, "release", refused)
    await app.stop()                                    # no exception
    assert app._command_serve_task.done()
    assert app.http_client.is_closed
    assert app.finished


async def test_a_message_arriving_during_a_database_blip_is_kept_and_answered(rt):
    rt.pool.fail(2)                                     # upserting the conversation fails twice
    await customer(rt, "Can I book for Friday?")
    assert await stored(rt) == ["Can I book for Friday?"]
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


async def test_a_message_the_database_cannot_take_never_reaches_telethon_and_the_next_one_works(rt, monkeypatch):
    real_upsert = rt.app.db.upsert_conversation
    calls = {"n": 0}

    async def once_broken(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("a row the database refuses")
        return await real_upsert(*a, **k)

    monkeypatch.setattr(rt.app.db, "upsert_conversation", once_broken)
    await customer(rt, "first")                         # logged, not raised to Telethon
    await customer(rt, "second")
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


async def test_a_failing_model_call_is_reported_and_the_task_ends_cleanly(rt):
    rt.script["fail"] = RuntimeError("model client exploded")
    await customer(rt, "Hello")
    assert CUSTOMER not in rt.sent
    errors_ = [m for m in await rt.app.db.get_messages(CUSTOMER) if m["status"] == STATUS_ERROR]
    assert errors_ and "Drafting failed" in errors_[-1]["text"]
    queued = await rt.real.fetchval("SELECT count(*) FROM unanswered_queue WHERE tenant_id = $1", rt.app.tenant_id)
    assert queued == 1
    assert rt.app.draft_tasks == {}
    rt.script["fail"] = None
    await customer(rt, "Hello again")
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


def _transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_deepseek_rate_limits_and_server_errors_are_retried_within_a_bounded_time(monkeypatch):
    sleeps: list[float] = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ai_responder.asyncio, "sleep", record_sleep)
    answers = iter([httpx.Response(429, headers={"retry-after": "60"}, text="slow down"),
                    httpx.Response(500, text="oops"),
                    httpx.Response(200, json={"choices": [{"message": {"content": "Hi!"}}]})])
    async with _transport(lambda request: next(answers)) as client:
        text = await ai_responder._complete(api_key="k", messages=[{"role": "user", "content": "x"}],
                                            ai_config={}, client=client)
    assert text == "Hi!" and sum(sleeps) <= ai_responder.TOTAL_DEADLINE_SECONDS

    sleeps.clear()
    async with _transport(lambda request: httpx.Response(429, headers={"retry-after": "60"})) as client:
        with pytest.raises(ai_responder.AIResponderError, match="rate limit"):
            await ai_responder._complete(api_key="k", messages=[{"role": "user", "content": "x"}],
                                         ai_config={}, client=client)
    assert len(sleeps) <= ai_responder.MAX_ATTEMPTS - 1


async def test_a_deepseek_call_that_never_answers_gives_up_in_bounded_time(monkeypatch):
    monkeypatch.setattr(ai_responder, "REQUEST_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(ai_responder, "TOTAL_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(ai_responder, "BASE_BACKOFF_SECONDS", 0.01)

    async def never(request):
        await asyncio.Event().wait()                    # a server trickling nothing back

    started = asyncio.get_running_loop().time()
    async with _transport(never) as client:
        with pytest.raises(ai_responder.AIResponderError, match="timed out"):
            await asyncio.wait_for(ai_responder._complete(
                api_key="k", messages=[{"role": "user", "content": "x"}], ai_config={}, client=client), 5)
    assert asyncio.get_running_loop().time() - started < 1.5


@pytest.mark.parametrize("body", [
    "not json at all",
    json.dumps({"choices": []}),
    json.dumps({"choices": [{"message": {"content": ""}}]}),
    json.dumps({"choices": [{"message": {"content": None}}]}),
])
async def test_garbage_from_deepseek_is_an_ai_error_not_a_crash(body):
    async with _transport(lambda request: httpx.Response(200, text=body)) as client:
        with pytest.raises(ai_responder.AIResponderError):
            await ai_responder._complete(api_key="k", messages=[{"role": "user", "content": "x"}],
                                         ai_config={}, client=client)


@pytest.mark.parametrize("extracted", [
    "Sure! Here you go: {not json}",
    "```json\n[1, 2, 3]\n```",
    '{"intent": "book"}',
    '{"intent": "book", "start": "tomorrow afternoon"}',
    '{"intent": "book", "start": "2030-13-45T25:99"}',
    '{"intent": "book", "start": "2030-03-05T15:00", "duration_minutes": 1e400}',
    '{"intent": "teleport", "start": "2030-03-05T15:00"}',
    '{"intent": "waitlist", "waitlist_from": 5, "waitlist_to": null}',
    "",
])
async def test_garbage_from_the_extraction_model_books_nothing_and_breaks_nothing(rt, monkeypatch, extracted):
    async def complete(**kw):
        return extracted

    monkeypatch.setattr(ai_responder, "_complete", complete)
    rt.app.config["booking"].update(enabled=True, provider="@owner", min_notice_minutes=0)
    await customer(rt, "tomorrow at 3pm please")
    assert await rt.real.fetchval("SELECT count(*) FROM bookings") == 0
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]       # the reply still goes out


# =====================================================================
# 5. A restart in the middle of things
# =====================================================================


def fresh_flow(rt):
    """What a restarted process has: the same database, no memory."""
    rt.app.flow = session_runtime.booking_flow.BookingFlow(rt.app)

    async def owner_chat():
        return OWNER

    rt.app.flow.provider_chat_id = owner_chat
    rt.app.flow.calendar_client = lambda: None


async def confirmed(rt, start):
    b = await rt.app.booking_store.create(
        chat_id=CUSTOMER, customer_name="Anna", customer_username="anna", starts_at=start,
        ends_at=start + timedelta(hours=1), buffer_minutes=0, tz="Europe/Riga")
    await rt.real.execute("UPDATE bookings SET state = 'confirmed' WHERE id = $1", b["id"])
    return b


async def test_a_reminder_claimed_just_before_a_crash_is_never_sent_twice(rt):
    rt.app.config["booking"].update(enabled=True, provider="@owner")
    await rt.app.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    await rt.app.db.record_message(CUSTOMER, DIR_IN, STATUS_RECEIVED, "see you tomorrow")
    start = datetime(2030, 3, 5, 15, 0, tzinfo=RIGA)
    await confirmed(rt, start)
    rt.clock["now"] = start - timedelta(minutes=90)

    # Tick: the reminder is claimed and its reply about to start; the
    # process dies before the reply runs.
    started: list[int] = []
    rt.app.schedule_draft = started.append
    await rt.app.flow.tick()
    assert started == [CUSTOMER]
    del rt.app.schedule_draft                           # the restarted process drafts normally

    fresh_flow(rt)
    for _ in range(3):
        await rt.app.flow.tick()
        await settle(rt.app)
    assert CUSTOMER not in rt.sent                      # at most once: lost, never doubled
    rows = await rt.real.fetch("SELECT sent_at FROM booking_reminders")
    assert rows and all(r["sent_at"] is None for r in rows)


async def test_a_reminder_sent_just_before_a_crash_is_not_sent_again(rt):
    rt.app.config["booking"].update(enabled=True, provider="@owner")
    await rt.app.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    start = datetime(2030, 3, 5, 15, 0, tzinfo=RIGA)
    await confirmed(rt, start)
    await rt.app.db.record_message(CUSTOMER, DIR_IN, STATUS_RECEIVED, "see you tomorrow")
    rt.clock["now"] = start - timedelta(minutes=90)
    await rt.app.flow.tick()
    await settle(rt.app)
    assert len(rt.sent[CUSTOMER]) == 1
    fresh_flow(rt)
    for _ in range(3):
        await rt.app.flow.tick()
        await settle(rt.app)
    assert len(rt.sent[CUSTOMER]) == 1


async def test_a_booking_created_just_before_a_crash_still_reaches_the_owner(rt):
    rt.app.config["booking"].update(enabled=True, provider="@owner")
    await rt.app.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    await rt.app.db.upsert_conversation(OWNER, "Owner", "owner", False, 1)
    start = datetime(2030, 3, 6, 15, 0, tzinfo=RIGA)
    # Created, then the process died before the owner was asked.
    await rt.app.booking_store.create(
        chat_id=CUSTOMER, customer_name="Anna", customer_username="anna", starts_at=start,
        ends_at=start + timedelta(hours=1), buffer_minutes=0, tz="Europe/Riga")
    fresh_flow(rt)
    await rt.app.flow.tick()
    await settle(rt.app)
    [request] = rt.sent[OWNER]
    assert "Booking request #1" in request
    assert await rt.real.fetchval("SELECT state FROM bookings") == bs.PENDING
    # And it is not asked twice.
    await rt.app.flow.tick()
    assert len(rt.sent[OWNER]) == 1


async def test_one_broken_booking_does_not_keep_the_others_from_their_tick(rt, monkeypatch):
    rt.app.config["booking"].update(enabled=True, provider="@owner")
    await rt.app.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    await rt.app.db.upsert_conversation(OWNER, "Owner", "owner", False, 1)
    start = datetime(2030, 3, 5, 15, 0, tzinfo=RIGA)
    lapsing = await rt.app.booking_store.create(
        chat_id=CUSTOMER, customer_name="Anna", customer_username="anna",
        starts_at=datetime(2030, 3, 4, 11, 0, tzinfo=RIGA), ends_at=datetime(2030, 3, 4, 11, 30, tzinfo=RIGA),
        buffer_minutes=0, tz="Europe/Riga")
    await confirmed(rt, start)
    await rt.app.db.record_message(CUSTOMER, DIR_IN, STATUS_RECEIVED, "see you tomorrow")
    rt.clock["now"] = start - timedelta(minutes=90)
    real_change = rt.app.flow.change

    async def change(booking, change_, **kw):
        if booking["id"] == lapsing["id"]:
            raise RuntimeError("this one is broken")
        return await real_change(booking, change_, **kw)

    monkeypatch.setattr(rt.app.flow, "change", change)
    result = await rt.app.handle_command("scheduler_tick", {})
    await settle(rt.app)
    assert result["ok"] is True                         # the flow's own guard caught it
    assert rt.sent[CUSTOMER]                            # the other booking's reminder still went out


async def test_a_suspension_is_never_left_without_its_hold(pg_pool, monkeypatch):
    """The process dies (here: the call fails) right after a tenant was moved
    to suspended: it must not be left suspended but still sending."""
    tid = await seed_session(pg_pool, "acct")
    now = datetime(2030, 3, 4, 12, 0, tzinfo=timezone.utc)
    await pg_pool.execute("UPDATE tenants SET status = 'grace', grace_until = $2, billing_notice_sent_at = now(), "
                          "billing_next_due = '2030-03-01' WHERE id = $1", tid, now - timedelta(minutes=1))

    async def crash(*a, **k):
        raise dropped()

    monkeypatch.setattr(controls, "add_hold", crash)
    monkeypatch.setattr(alerts, "raise_alert", crash)
    await billing.tick(pg_pool, None, now)
    assert await pg_pool.fetchval("SELECT status FROM tenants WHERE id = $1", tid) == "suspended"
    assert "billing" in await controls.off_reason(pg_pool, tid)


async def test_two_digest_rounds_at_once_send_one_digest(pg_pool, monkeypatch):
    for name in ("SMTP_HOST", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    await seed_session(pg_pool, "acct", name="Salon Anna")
    await pg_pool.execute("UPDATE telegram_sessions SET lease_expires_at = now() + interval '1 hour'")
    sent = []

    class Bus:
        async def dispatch(self, session_id, action, args=None, *, timeout=30):
            sent.append(args["text"])
            await REAL_SLEEP(0.05)
            return {"sent": True, "error": ""}

    monday = datetime(2030, 3, 4, 10, 0, tzinfo=RIGA)
    await asyncio.gather(digest.tick(pg_pool, Bus(), monday), digest.tick(pg_pool, Bus(), monday),
                         digest.tick(pg_pool, Bus(), monday + timedelta(minutes=1)))
    assert len(sent) == 1


# =====================================================================
# 6. Clocks and time zones
# =====================================================================


@pytest.mark.parametrize("monday", [date(2030, 4, 1), date(2030, 10, 28)])   # the Mondays after DST changes
async def test_the_digest_goes_at_nine_local_on_the_monday_after_a_clock_change(pg_pool, monkeypatch, monday):
    for name in ("SMTP_HOST", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    tid = await seed_session(pg_pool, "acct", name="Salon Anna")
    await pg_pool.execute("UPDATE telegram_sessions SET lease_expires_at = now() + interval '1 hour'")
    await pg_pool.execute("UPDATE tenants SET config_json = $1::jsonb",
                          json.dumps({"timezone": "Europe/Riga", "digest": {"weekday": 0, "hour": 9}}))
    sent = []

    class Bus:
        async def dispatch(self, session_id, action, args=None, *, timeout=30):
            sent.append(args["text"])
            return {"sent": True, "error": ""}

    nine = datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=RIGA)
    assert await digest.tick(pg_pool, Bus(), (nine - timedelta(minutes=1)).astimezone(timezone.utc)) == {}
    assert await digest.tick(pg_pool, Bus(), nine.astimezone(timezone.utc)) == {tid: "telegram"}
    assert await digest.tick(pg_pool, Bus(), (nine + timedelta(hours=5)).astimezone(timezone.utc)) == {}
    assert len(sent) == 1
    assert await pg_pool.fetchval("SELECT week_start FROM digest_log") == monday - timedelta(days=7)


async def test_billing_counts_the_due_date_in_local_days_across_a_clock_change(pg_pool):
    tid = await seed_session(pg_pool, "acct")
    await pg_pool.execute("UPDATE tenants SET config_json = '{\"timezone\": \"Europe/Riga\"}', "
                          "billing_next_due = '2030-10-26' WHERE id = $1", tid)   # the Saturday before
    # 23:59 on the due day, local: not yet.
    assert await billing.tick(pg_pool, None, datetime(2030, 10, 26, 23, 59, tzinfo=RIGA)) == []
    # 00:30 on the Sunday the clocks go back: grace.
    assert await billing.tick(pg_pool, None, datetime(2030, 10, 27, 0, 30, tzinfo=RIGA)) == [f"{tid}:grace"]
    await alerts.drain()


@pytest.mark.parametrize("night", [date(2030, 3, 31), date(2030, 10, 27)])
@pytest.mark.parametrize("window", [("22:00", "03:30"), ("23:00", "08:00"), ("02:30", "03:15")])
async def test_quiet_hours_never_come_out_negative_or_endless_on_a_clock_change_night(night, window):
    quiet = {"enabled": True, "start": window[0], "end": window[1]}
    start = datetime(night.year, night.month, night.day, tzinfo=timezone.utc) - timedelta(hours=6)
    for step in range(0, 18 * 60, 5):
        now_local = (start + timedelta(minutes=step)).astimezone(RIGA)
        left = humanlike.seconds_until_quiet_ends(now_local, quiet)
        assert 0 <= left <= 24 * 3600
        if left:
            after = humanlike.later(now_local, left + 1)
            assert not humanlike.in_quiet_hours(after, quiet)


async def test_a_hand_edited_invalid_timezone_keeps_the_last_good_config(rt):
    before = dict(rt.app.config)
    await rt.real.execute("UPDATE tenants SET config_json = '{\"timezone\": \"Mars/Olympus_Mons\"}'")
    await rt.app.bind_tenant()                          # no exception
    assert rt.app.config == before
    await rt.real.execute("UPDATE tenants SET config_json = '[1, 2, 3]'")
    await rt.app.bind_tenant()
    assert rt.app.config == before
    # And the account still answers.
    await customer(rt, "Hi")
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


async def test_billing_and_the_digest_survive_a_tenant_config_edited_into_nonsense(pg_pool, monkeypatch):
    for name in ("SMTP_HOST", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    bad = await seed_session(pg_pool, "bad")
    good = await seed_session(pg_pool, "good")
    await pg_pool.execute("UPDATE tenants SET config_json = '[1, 2, 3]' WHERE id = $1", bad)
    await pg_pool.execute("UPDATE tenants SET billing_next_due = '2030-03-01'")
    assert await billing._timezone(pg_pool, bad) == "UTC"
    changed = await billing.tick(pg_pool, None, datetime(2030, 3, 4, 12, 0, tzinfo=timezone.utc))
    assert sorted(changed) == sorted([f"{bad}:grace", f"{good}:grace"])
    assert await digest.effective_config(pg_pool, bad) is None
    await digest.tick(pg_pool, None, datetime(2030, 3, 4, 10, 0, tzinfo=RIGA))    # no exception
    await alerts.drain()


# =====================================================================
# 7. Odd messages
# =====================================================================


async def test_a_very_long_message_with_emoji_and_rtl_text_is_stored_intact_and_answered(rt):
    text = ("שלום! مرحبا 👋🏽 👨‍👩‍👧‍👦 " * 400)[:4096].strip()
    await customer(rt, text)
    assert await stored(rt) == [text]
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


async def test_a_nul_character_does_not_cost_the_message(rt):
    sender = SimpleNamespace(id=CUSTOMER, first_name="An\x00na", last_name=None, username="anna", bot=False,
                             access_hash=1)
    await customer(rt, "hello\x00 there", sender=sender)
    assert await stored(rt) == ["hello there"]
    assert rt.sent[CUSTOMER] == ["Sure, see you then."]


@pytest.mark.parametrize("photo, expected", [(False, "[non-text message]"), (True, "[photo]")])
async def test_a_sticker_or_a_photo_alone_is_stored_and_not_answered(rt, photo, expected):
    await customer(rt, "", photo=photo)
    assert await stored(rt) == [expected]
    assert CUSTOMER not in rt.sent and rt.script["calls"] == 0


async def test_a_deleted_account_is_stored_under_a_placeholder_and_the_chat_paused_on_send(rt, monkeypatch):
    ghost = SimpleNamespace(id=CUSTOMER, first_name=None, last_name=None, username=None, bot=False,
                            access_hash=None, deleted=True)

    async def deactivated(peer, chat_id, text, typing):
        raise errors.InputUserDeactivatedError(request=None)

    monkeypatch.setattr(rt.app, "deliver", deactivated)
    await customer(rt, "hi", sender=ghost)
    conversation = await rt.app.db.get_conversation(CUSTOMER)
    assert conversation["display_name"] == f"Chat {CUSTOMER}"
    assert conversation["automation_paused"]
    await customer(rt, "hi again", sender=ghost)        # still no crash; paused chat, no reply
    assert rt.script["calls"] == 1


async def test_telegrams_service_account_is_never_answered(rt, monkeypatch):
    checked = []

    async def check_logins():
        checked.append(True)
        return []

    monkeypatch.setattr(rt.app, "check_logins", check_logins)
    await customer(rt, "Login code: 12345", chat=session_runtime.TELEGRAM_SERVICE_ID)
    assert rt.sent == {} and checked == [True]


async def test_the_owner_writing_in_saved_messages_is_no_takeover(rt):
    me = rt.app.me_info["id"]
    await rt.app.on_outgoing(FakeEvent(me, "note to self"))
    conversation = await rt.app.db.get_conversation(me)
    assert conversation is not None and conversation["human_takeover_until"] is None
    assert rt.sent == {}


# =====================================================================
# 8. Memory that must not grow per chat
# =====================================================================


async def test_drafts_and_scans_leave_nothing_behind(rt):
    for chat in range(100, 160):
        await rt.app.on_incoming(FakeEvent(chat, "hi"))
    await settle(rt.app)
    assert rt.app.draft_tasks == {} and rt.app.flow.scan_tasks == {}
    assert rt.app.in_flight_sends == {} and rt.app.in_flight_media == {}


async def test_per_chat_throttles_and_hints_are_pruned(rt):
    import time

    old = time.monotonic() - 2 * session_runtime.STAGING_NOTE_SECONDS
    rt.app._staging_noted.update({chat: old for chat in range(10_000)})
    rt.app._staging_noted[1] = time.monotonic()
    long_ago = rt.app.utcnow() - timedelta(days=3)
    rt.app.flow.last_unavailable.update({chat: (long_ago, long_ago) for chat in range(5000)})
    loop_now = asyncio.get_running_loop().time()
    rt.app.flow._submit_attempts.update({n: loop_now - 10_000 for n in range(5000)})
    rt.app.prune_memory()
    assert list(rt.app._staging_noted) == [1]
    assert rt.app.flow.last_unavailable == {} and rt.app.flow._submit_attempts == {}


async def test_the_command_server_keeps_no_finished_handler_tasks():
    b = live_bus()
    stop = asyncio.Event()

    async def handler(action, args):
        return {}

    serving = asyncio.create_task(b.serve("acct", handler, stop))
    await REAL_SLEEP(0.05)
    for _ in range(20):
        await b.dispatch("acct", "x", timeout=2)
    await until(lambda: not b._tasks)
    stop.set()
    await serving
