"""Master Process Manager (plan item 6): the real multi-process fleet
runner that `panel.py`'s startup docstring says this file replaces.

Design
------
- The **parent** process owns no `SessionRuntime`s and touches Telegram
  never. Its whole job is bookkeeping: talk to Postgres to find claimable
  sessions, partition them across N OS-level **worker processes**
  (`multiprocessing.Process`, not threads/asyncio tasks — Telethon clients
  and their event loops must not cross a process boundary, and one
  worker's crash must not corrupt or take down any other worker), and
  restart any worker that dies.
- Each **worker process** runs its own asyncio event loop, its own
  `asyncpg` pool (pools cannot be shared/forked across processes), and
  constructs one `SessionRuntime` per session_id it was assigned, calling
  `.start()` on each. A session that raises `NeedsLogin` (hasn't finished
  the login flow yet) or `leasing.LeaseLost` (raced and lost — should not
  normally happen since the parent only hands out sessions from
  `claimable()`, but the DB is the source of truth, not the parent's
  snapshot) is logged and skipped, not fatal to the worker.
- Deliberately **no per-worker `LeaseKeeper` here.** `SessionRuntime.start()`
  / `.stop()` already acquire/release that session's lease and run their
  own internal `LeaseKeeper` (see session_runtime.py's `__init__`, `start`,
  `stop`, `_on_lease_lost`). A manager-level lease keeper would be pure
  duplication and a second source of truth for the same lease — so once a
  worker has started all of its runtimes, it just idles (waits on a stop
  event) and lets each runtime's own background tasks keep it alive.
- If a worker process dies (crash, OOM-kill, etc.), whatever leases it
  held simply age out — nothing releases them explicitly, the TTL
  (`leasing.LEASE_SECONDS`) does it. The parent notices the dead process,
  waits for it to actually exit, and starts a replacement, which re-polls
  `SessionRegistry.claimable()` for a fresh batch. `claimable()` already
  excludes anything another live worker still holds a valid lease on, so
  no extra coordination is needed here.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path

import pg
from database import SessionRegistry
from session_runtime import NeedsLogin, SessionRuntime
import leasing

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
WORKER_COUNT = int(os.getenv("WORKER_COUNT") or 2)
SESSIONS_PER_WORKER = int(os.getenv("SESSIONS_PER_WORKER") or 25)

# How often the parent polls worker liveness / reassigns after a death.
POLL_INTERVAL_SECONDS = 5.0
# Grace period given to a worker after being asked to shut down cleanly
# before the parent gives up waiting and just moves on.
SHUTDOWN_JOIN_SECONDS = 15.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(processName)s] %(name)s: %(message)s",
)
log = logging.getLogger("manager")


# ---------------------------------------------------------------------------
# Worker process entry point
# ---------------------------------------------------------------------------


def _worker_main(worker_index: int, session_ids: list[str], stop_event: "multiprocessing.synchronize.Event") -> None:
    """Runs inside the child process. Owns its own event loop, pool, and
    one `SessionRuntime` per assigned session_id."""
    worker_id = f"worker-{worker_index}:{os.getpid()}"
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s [{worker_id}] %(name)s: %(message)s",
    )
    wlog = logging.getLogger(f"manager.worker.{worker_index}")

    # A plain OS-level signal handler that flips the same multiprocessing
    # Event the parent uses to ask us to stop — lets a worker also react to
    # e.g. its own SIGTERM if the parent's process group forwards one, not
    # just to the parent explicitly calling stop_event.set().
    def _on_signal(signum, _frame) -> None:
        wlog.info("Worker %s received signal %s, stopping.", worker_id, signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    except (ValueError, OSError):
        pass  # not the main thread of this process on some platforms; best-effort

    asyncio.run(_worker_async_main(worker_id, session_ids, stop_event, wlog))


async def _worker_async_main(
    worker_id: str,
    session_ids: list[str],
    stop_event: "multiprocessing.synchronize.Event",
    wlog: logging.Logger,
) -> None:
    pool = await pg.create_pool(DATABASE_URL)
    try:
        await pg.assert_version(pool, pg.latest_version())

        runtimes: dict[str, SessionRuntime] = {}
        for session_id in session_ids:
            runtime = SessionRuntime(
                pool, session_id, data_dir=DATA_DIR, redis_url=REDIS_URL, worker_id=worker_id
            )
            try:
                await runtime.start()
            except NeedsLogin as exc:
                wlog.warning("[%s] Skipping — needs login: %s", session_id, exc)
                continue
            except leasing.LeaseLost as exc:
                wlog.warning("[%s] Skipping — lease already held: %s", session_id, exc)
                continue
            except Exception:
                wlog.exception("[%s] Failed to start", session_id)
                continue
            runtimes[session_id] = runtime
            wlog.info("[%s] Started.", session_id)

        wlog.info(
            "Worker %s running %d/%d assigned session(s).",
            worker_id, len(runtimes), len(session_ids),
        )

        # Each SessionRuntime keeps itself alive (and its own lease fresh)
        # via its own background tasks/LeaseKeeper. All this loop does is
        # wait for a stop request, polling so it stays responsive without
        # needing an asyncio-native cross-process signal.
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            await loop.run_in_executor(None, stop_event.wait, 1.0)

        wlog.info("Worker %s stopping %d session(s)...", worker_id, len(runtimes))
        for session_id, runtime in list(runtimes.items()):
            try:
                await runtime.stop()
            except Exception:
                wlog.exception("[%s] Error while stopping", session_id)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Parent process: partitioning, spawn, monitor, restart
# ---------------------------------------------------------------------------


def _partition(session_ids: list[str], worker_count: int, cap_per_worker: int) -> list[list[str]]:
    """Splits claimable session_ids round-robin across `worker_count`
    buckets, each capped at `cap_per_worker`. Anything beyond total
    capacity is left unclaimed (logged, not fatal)."""
    capacity = worker_count * cap_per_worker
    if len(session_ids) > capacity:
        log.warning(
            "%d claimable session(s) but only %d worker slot(s) available "
            "(WORKER_COUNT=%d x SESSIONS_PER_WORKER=%d) — %d will be left unclaimed for now.",
            len(session_ids), capacity, worker_count, cap_per_worker,
            len(session_ids) - capacity,
        )
        session_ids = session_ids[:capacity]

    buckets: list[list[str]] = [[] for _ in range(worker_count)]
    for i, session_id in enumerate(session_ids):
        bucket = buckets[i % worker_count]
        if len(bucket) < cap_per_worker:
            bucket.append(session_id)
        else:
            # Round-robin already respects the cap in the common case; this
            # is only reached with a skewed leftover after capacity-trimming
            # above, so fall back to first-fit.
            for b in buckets:
                if len(b) < cap_per_worker:
                    b.append(session_id)
                    break
    return buckets


class _WorkerSlot:
    """Parent-side bookkeeping for one worker slot (index 0..N-1). A slot
    outlives any individual `multiprocessing.Process` — when its process
    dies, the slot gets a fresh process with a freshly-claimed batch."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.process: multiprocessing.Process | None = None
        self.stop_event: multiprocessing.Event = multiprocessing.Event()
        self.session_ids: list[str] = []

    def spawn(self, session_ids: list[str]) -> None:
        self.session_ids = session_ids
        self.stop_event = multiprocessing.Event()
        self.process = multiprocessing.Process(
            target=_worker_main,
            args=(self.index, session_ids, self.stop_event),
            name=f"worker-{self.index}",
            daemon=False,
        )
        self.process.start()
        log.info(
            "Spawned worker-%d (pid=%s) with %d session(s): %s",
            self.index, self.process.pid, len(session_ids),
            ", ".join(session_ids) if session_ids else "(none)",
        )

    def is_alive(self) -> bool:
        return self.process is not None and self.process.is_alive()

    def request_stop(self) -> None:
        if self.process is not None and self.process.is_alive():
            self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self.process is not None:
            self.process.join(timeout)


class Manager:
    def __init__(self, dsn: str, worker_count: int, sessions_per_worker: int) -> None:
        self.dsn = dsn
        self.worker_count = worker_count
        self.sessions_per_worker = sessions_per_worker
        self.slots = [_WorkerSlot(i) for i in range(worker_count)]
        self._shutting_down = False

    async def _claimable(self) -> list[str]:
        pool = await pg.create_pool(self.dsn, min_size=1, max_size=2)
        try:
            await pg.assert_version(pool, pg.latest_version())
            registry = SessionRegistry(pool)
            return await registry.claimable()
        finally:
            await pool.close()

    def _spawn_all(self, session_ids: list[str]) -> None:
        buckets = _partition(session_ids, self.worker_count, self.sessions_per_worker)
        for slot, bucket in zip(self.slots, buckets):
            slot.spawn(bucket)

    def _request_shutdown(self) -> None:
        self._shutting_down = True
        log.info("Shutdown requested: signalling all workers to stop cleanly.")
        for slot in self.slots:
            slot.request_stop()
        deadline = time.monotonic() + SHUTDOWN_JOIN_SECONDS
        for slot in self.slots:
            remaining = max(0.0, deadline - time.monotonic())
            slot.join(remaining)
        for slot in self.slots:
            if slot.is_alive():
                log.warning("worker-%d did not stop in time; terminating.", slot.index)
                slot.process.terminate()
                slot.join(5.0)

    def run(self) -> None:
        log.info(
            "Starting manager: WORKER_COUNT=%d SESSIONS_PER_WORKER=%d DATA_DIR=%s",
            self.worker_count, self.sessions_per_worker, DATA_DIR,
        )

        claimable = asyncio.run(self._claimable())
        log.info("Found %d claimable session(s) at startup.", len(claimable))
        self._spawn_all(claimable)

        def _handle_signal(signum, _frame):
            log.info("Manager received signal %s.", signum)
            self._request_shutdown()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        try:
            while not self._shutting_down:
                time.sleep(POLL_INTERVAL_SECONDS)
                if self._shutting_down:
                    break
                self._check_and_restart_dead_workers()
        finally:
            if not self._shutting_down:
                self._request_shutdown()
            log.info("Manager exiting.")

    def _check_and_restart_dead_workers(self) -> None:
        dead = [slot for slot in self.slots if slot.process is not None and not slot.is_alive()]
        if not dead:
            return
        for slot in dead:
            exitcode = slot.process.exitcode
            log.warning(
                "worker-%d (pid=%s) exited unexpectedly (exitcode=%s) while running %d "
                "session(s): %s. Restarting with a fresh claimable batch.",
                slot.index, slot.process.pid, exitcode,
                len(slot.session_ids), ", ".join(slot.session_ids) or "(none)",
            )
        try:
            fresh = asyncio.run(self._claimable())
        except Exception:
            log.exception(
                "Failed to fetch claimable sessions while restarting dead worker(s); "
                "will retry next poll."
            )
            return

        # Only re-partition across the slots that actually died, so still-
        # healthy workers' assignments are left untouched.
        buckets = _partition(fresh, len(dead), self.sessions_per_worker)
        for slot, bucket in zip(dead, buckets):
            slot.spawn(bucket)


def main() -> None:
    # The manager is also the process responsible for bringing the schema
    # up to date — workers/panel only ever assert_version (see pg.py).
    async def _migrate() -> None:
        pool = await pg.create_pool(DATABASE_URL, min_size=1, max_size=2)
        try:
            applied = await pg.apply_migrations(pool)
            if applied:
                log.info("Applied migration(s): %s", applied)
            else:
                log.info("Schema already up to date (version %d).", pg.latest_version())
        finally:
            await pool.close()

    asyncio.run(_migrate())

    manager = Manager(DATABASE_URL, WORKER_COUNT, SESSIONS_PER_WORKER)
    manager.run()


if __name__ == "__main__":
    # Required for multiprocessing on Windows (and harmless elsewhere) —
    # child processes re-import this module, so the spawn entry point must
    # be guarded.
    multiprocessing.freeze_support()
    main()
