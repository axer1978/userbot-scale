"""The one background scheduler: nudges every running account once a
minute to do its timed work, and keeps quiet-hours replies in Postgres.

Timed work is booking reminders, requests nobody answered before their
start, waitlist offers that ran out, owner requests that could not be sent
yet, and replies held back by quiet hours. Each account does its own part
when it receives `scheduler_tick` over the command bus (it holds the live
Telegram client; this process holds none). Every part is idempotent (a
reminder is claimed by a unique row before it goes out, a deferred reply is
deleted when taken), so a missed, doubled or late tick changes nothing.

It also does the platform's own rounds (phase 3), which need no account
running: the health watchdog (health.py: an account that is down, logged
out or rate-limited raises an alert within a few minutes), billing grace
and suspension (billing.py), the owners' weekly digest (digest.py, phase
4), and a heartbeat the panel shows, so a dead scheduler is visible too.

Only one scheduler runs at a time: it holds a Postgres advisory lock for as
long as it lives, and a second copy waits for the lock instead of ticking.

Run it as its own process: `python scheduler.py` (the `scheduler` service
in docker-compose.yml).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncpg

import billing
import commands
import digest
import health
import pg
import review

log = logging.getLogger("scheduler")

TICK_SECONDS = 60
TICK_TIMEOUT_SECONDS = 45
TICK_GRACE_SECONDS = 10
# After losing its database connection (and with it the lock), how long it
# waits before trying again.
RETRY_SECONDS = 5
# Arbitrary but fixed: the advisory lock that makes this a singleton.
LOCK_KEY = 0x5C4ED


# ------------------------------------------------------ deferred replies


async def defer_reply(pool: asyncpg.Pool, tenant_id: int, session_id: str, chat_id: int, due_at: datetime) -> None:
    """Hold this chat's reply until `due_at`. A later message in the same
    chat moves the time; there is only ever one waiting reply per chat."""
    await pool.execute(
        "INSERT INTO deferred_replies (tenant_id, session_id, chat_id, due_at) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (tenant_id, chat_id) DO UPDATE SET due_at = EXCLUDED.due_at, session_id = EXCLUDED.session_id",
        tenant_id, session_id, chat_id, due_at,
    )


async def clear_deferred(pool: asyncpg.Pool, tenant_id: int, chat_id: int) -> None:
    await pool.execute("DELETE FROM deferred_replies WHERE tenant_id = $1 AND chat_id = $2", tenant_id, chat_id)


async def clear_all_deferred(pool: asyncpg.Pool, tenant_id: int) -> list[int]:
    """Drop every held reply of this tenant. Returns the chats they were
    for (soft-off queues them as unanswered)."""
    rows = await pool.fetch("DELETE FROM deferred_replies WHERE tenant_id = $1 RETURNING chat_id", tenant_id)
    return [r["chat_id"] for r in rows]


async def take_due_deferred(pool: asyncpg.Pool, tenant_id: int, now: datetime) -> list[int]:
    """The chats whose held reply is due, removed as they are taken, so two
    ticks can't both start the same reply."""
    rows = await pool.fetch(
        "DELETE FROM deferred_replies WHERE tenant_id = $1 AND due_at <= $2 RETURNING chat_id",
        tenant_id, now,
    )
    return [r["chat_id"] for r in rows]


async def deferred_for(pool: asyncpg.Pool, tenant_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT chat_id, due_at FROM deferred_replies WHERE tenant_id = $1 ORDER BY due_at", tenant_id,
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------- the loop


async def live_sessions(pool: asyncpg.Pool) -> list[str]:
    """Accounts some worker is running right now (a valid lease)."""
    rows = await pool.fetch(
        "SELECT session_id FROM telegram_sessions WHERE lease_expires_at > now() ORDER BY session_id"
    )
    return [r["session_id"] for r in rows]


async def tick(pool: asyncpg.Pool, bus: commands.CommandBus) -> dict[str, str]:
    """One round: every running account gets `scheduler_tick`, all at once.
    Returns session -> "ok" or the error, for the log and tests."""
    sessions = await live_sessions(pool)

    async def one(session_id: str) -> tuple[str, str]:
        # Bounded here as well as inside dispatch: one account (or a bus
        # that stopped answering) can't hold the round up past the timeout,
        # and whatever goes wrong for one account is its own result only.
        try:
            await asyncio.wait_for(
                bus.dispatch(session_id, "scheduler_tick", {}, timeout=TICK_TIMEOUT_SECONDS),
                TICK_TIMEOUT_SECONDS + TICK_GRACE_SECONDS,
            )
            return session_id, "ok"
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("scheduler_tick for %s did not finish in time", session_id)
            return session_id, "timed out"
        except Exception as exc:
            log.warning("scheduler_tick for %s failed: %s", session_id, exc)
            return session_id, str(exc) or type(exc).__name__

    return dict(await asyncio.gather(*(one(s) for s in sessions)))


async def platform_tick(pool: asyncpg.Pool, bus: commands.CommandBus) -> None:
    """The rounds that don't go through an account. Each part is guarded
    on its own, so one failing does not skip the others."""

    async def heartbeat() -> None:
        await pool.execute(
            "INSERT INTO platform_settings (key, value, updated_by) VALUES ('scheduler_heartbeat', to_jsonb(now()), "
            "'scheduler') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
        )

    data_root = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent / "data")
    for name, step in (("heartbeat", heartbeat), ("health", lambda: health.check_all(pool)),
                       ("billing", lambda: billing.tick(pool, bus)), ("digest", lambda: digest.tick(pool, bus)),
                       # Verification holds and the retention of review files (review.py).
                       ("review", lambda: review.tick(pool, bus, data_root))):
        try:
            await step()
        except Exception:
            log.exception("Scheduler %s round failed", name)


async def run(pool: asyncpg.Pool, bus: commands.CommandBus, stop: asyncio.Event,
              tick_seconds: float = TICK_SECONDS) -> None:
    """Tick until `stop` is set, while holding the lock. Postgres going away
    does not end it: the lock went with the connection, so it waits a
    little, takes the lock again (or waits for whoever has it now) and goes
    on."""
    while not stop.is_set():
        try:
            if await _run_locked(pool, bus, stop, tick_seconds):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler lost its database connection; trying again in %ss.", RETRY_SECONDS)
        if await _wait(stop, min(tick_seconds, RETRY_SECONDS)):
            return


async def _run_locked(pool: asyncpg.Pool, bus: commands.CommandBus, stop: asyncio.Event,
                      tick_seconds: float) -> bool:
    """One stretch of holding the lock. True when stopped; raises when the
    lock's connection is lost (the lock is gone with it)."""
    async with pool.acquire() as lock_con:
        while not await lock_con.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            log.info("Another scheduler holds the lock; waiting.")
            if await _wait(stop, tick_seconds):
                return True
        log.info("Scheduler running (tick every %ss).", tick_seconds)
        try:
            while not stop.is_set():
                # The lock lives as long as this connection. If it died, a
                # second scheduler may already hold the lock: stop ticking.
                await lock_con.fetchval("SELECT 1")
                try:
                    await tick(pool, bus)
                except Exception:
                    log.exception("Scheduler tick failed")
                try:
                    await platform_tick(pool, bus)
                except Exception:
                    log.exception("Scheduler platform round failed")
                if await _wait(stop, tick_seconds):
                    return True
            return True
        finally:
            with suppress(Exception):
                await lock_con.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep, waking early on stop. True when stopping."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pool = await pg.create_pool(os.environ["DATABASE_URL"])
    await pg.assert_version(pool, pg.latest_version())
    bus = await commands.CommandBus.connect(os.environ["REDIS_URL"])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    try:
        await run(pool, bus, stop)
    finally:
        await bus.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
