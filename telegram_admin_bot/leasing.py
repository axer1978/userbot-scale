"""Lease-fencing: guarantees at most one worker process operates a given
Telegram session at a time.

Two workers racing to run the same session is exactly what caused
AUTH_KEY_UNREGISTERED in earlier testing (Telethon's session state gets
mutated from two places at once). The whole safety property rests on the
`UPDATE ... WHERE` statements in `acquire`/`renew` being atomic — Postgres
takes a row lock on the UPDATE, so a losing concurrent caller re-evaluates
the WHERE clause against the winner's already-committed row and matches
zero rows. No advisory locks, no application-level mutex.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

import asyncpg

log = logging.getLogger(__name__)

LEASE_SECONDS = 30
RENEW_SECONDS = 10
DANGER_SECONDS = 22  # no confirmed renewal for this long -> self-fence, even without proof of loss


@dataclass(frozen=True)
class Lease:
    session_id: str
    worker_id: str
    epoch: int
    expires_at: datetime


class LeaseLost(RuntimeError):
    """Raised on any send/operate path once a session's lease is known (or
    suspected) to no longer be held by this worker."""


_ACQUIRE_SQL = """
UPDATE telegram_sessions
   SET lease_worker_id  = $2,
       lease_expires_at = now() + make_interval(secs => $3),
       lease_epoch      = lease_epoch + 1,
       updated_at       = now()
 WHERE session_id = $1
   AND is_active
   AND (lease_expires_at IS NULL OR lease_expires_at < now())
RETURNING session_id, lease_worker_id, lease_epoch, lease_expires_at
"""

_RENEW_SQL = """
UPDATE telegram_sessions
   SET lease_expires_at = now() + make_interval(secs => $3),
       last_seen_at     = now()
 WHERE session_id = ANY($1) AND lease_worker_id = $2 AND lease_expires_at > now()
RETURNING session_id, lease_worker_id, lease_epoch, lease_expires_at
"""

_RELEASE_SQL = """
UPDATE telegram_sessions
   SET lease_worker_id = NULL, lease_expires_at = NULL, last_seen_at = now()
 WHERE session_id = ANY($1) AND lease_worker_id = $2
"""

_HOLDER_SQL = """
SELECT lease_worker_id, lease_expires_at FROM telegram_sessions WHERE session_id = $1
"""


async def acquire(
    pool: asyncpg.Pool, session_id: str, worker_id: str, *, ttl: int = LEASE_SECONDS
) -> Lease | None:
    async with pool.acquire() as con:
        row = await con.fetchrow(_ACQUIRE_SQL, session_id, worker_id, ttl)
    if row is None:
        return None
    return Lease(
        session_id=row["session_id"],
        worker_id=row["lease_worker_id"],
        epoch=row["lease_epoch"],
        expires_at=row["lease_expires_at"],
    )


async def acquire_many(
    pool: asyncpg.Pool, session_ids: list[str], worker_id: str, *, ttl: int = LEASE_SECONDS
) -> dict[str, Lease]:
    out: dict[str, Lease] = {}
    async with pool.acquire() as con:
        async with con.transaction():
            for session_id in session_ids:
                row = await con.fetchrow(_ACQUIRE_SQL, session_id, worker_id, ttl)
                if row is not None:
                    out[session_id] = Lease(
                        session_id=row["session_id"],
                        worker_id=row["lease_worker_id"],
                        epoch=row["lease_epoch"],
                        expires_at=row["lease_expires_at"],
                    )
    return out


async def renew(
    pool: asyncpg.Pool, session_id: str, worker_id: str, *, ttl: int = LEASE_SECONDS
) -> Lease | None:
    result = await renew_many(pool, [session_id], worker_id, ttl=ttl)
    return result.get(session_id)


async def renew_many(
    pool: asyncpg.Pool, session_ids: list[str], worker_id: str, *, ttl: int = LEASE_SECONDS
) -> dict[str, Lease]:
    """Returns the leases that were successfully renewed. Any session_id in
    the input but missing from the return value has lost its lease (either
    taken by someone else, or expired) — the caller must treat it as lost."""
    if not session_ids:
        return {}
    async with pool.acquire() as con:
        rows = await con.fetch(_RENEW_SQL, session_ids, worker_id, ttl)
    return {
        row["session_id"]: Lease(
            session_id=row["session_id"],
            worker_id=row["lease_worker_id"],
            epoch=row["lease_epoch"],
            expires_at=row["lease_expires_at"],
        )
        for row in rows
    }


async def release(pool: asyncpg.Pool, session_id: str, worker_id: str) -> bool:
    n = await release_many(pool, [session_id], worker_id)
    return n > 0


async def release_many(pool: asyncpg.Pool, session_ids: list[str], worker_id: str) -> int:
    if not session_ids:
        return 0
    async with pool.acquire() as con:
        result = await con.execute(_RELEASE_SQL, session_ids, worker_id)
    # asyncpg execute() returns a string like "UPDATE 3"
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0


async def holder(pool: asyncpg.Pool, session_id: str) -> tuple[str | None, datetime | None]:
    async with pool.acquire() as con:
        row = await con.fetchrow(_HOLDER_SQL, session_id)
    if row is None:
        return None, None
    return row["lease_worker_id"], row["lease_expires_at"]


class LeaseKeeper:
    """One per worker process. Renews every session that worker owns with a
    single shared ticker (not one timer per session)."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        worker_id: str,
        *,
        on_lost: Callable[[str], Awaitable[None]],
        interval: float = RENEW_SECONDS,
        danger: float = DANGER_SECONDS,
        ttl: int = LEASE_SECONDS,
    ) -> None:
        self._pool = pool
        self._worker_id = worker_id
        self._on_lost = on_lost
        self._interval = interval
        self._danger = danger
        self._ttl = ttl
        self._leases: dict[str, Lease] = {}
        self._safe: dict[str, bool] = {}
        self._last_ok: dict[str, float] = {}
        self._stop_event = asyncio.Event()

    def track(self, lease: Lease) -> None:
        now = asyncio.get_running_loop().time()
        self._leases[lease.session_id] = lease
        self._safe[lease.session_id] = True
        self._last_ok[lease.session_id] = now

    def untrack(self, session_id: str) -> None:
        self._leases.pop(session_id, None)
        self._safe.pop(session_id, None)
        self._last_ok.pop(session_id, None)

    def is_safe(self, session_id: str) -> bool:
        return self._safe.get(session_id, False)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                break  # stop() was called
            except asyncio.TimeoutError:
                pass
            await self._tick(loop.time())

    async def _tick(self, now: float) -> None:
        tracked = list(self._leases.keys())
        if not tracked:
            return
        renewed: dict[str, Lease] = {}
        # Tracked separately from `renewed` being non-empty: if every tracked
        # session lost its lease in the same tick, a successful call legally
        # returns an empty dict too, and that must still fence immediately
        # rather than being mistaken for the call itself having failed.
        call_ok = False
        try:
            renewed = await renew_many(self._pool, tracked, self._worker_id, ttl=self._ttl)
            call_ok = True
        except Exception:
            log.exception("lease renewal batch failed for worker %s", self._worker_id)

        for session_id in tracked:
            if session_id in renewed:
                self._leases[session_id] = renewed[session_id]
                self._safe[session_id] = True
                self._last_ok[session_id] = now
                continue
            # Missing from RETURNING: either the renewal call itself failed
            # (Postgres unreachable) or this specific session's lease was
            # confirmed lost. A confirmed loss fences immediately; a failed
            # call only fences once we're past the danger window.
            last_ok = self._last_ok.get(session_id, now)
            if call_ok or (now - last_ok) > self._danger:
                await self._fence(session_id)
            # else: the whole batch errored and we're still inside the danger
            # window — leave it tracked, try again next tick.

    async def _fence(self, session_id: str) -> None:
        if session_id not in self._leases:
            return
        self.untrack(session_id)
        try:
            await self._on_lost(session_id)
        except Exception:
            log.exception("on_lost callback failed for session %s", session_id)

    async def stop(self) -> None:
        """Signals `run()`'s loop to exit on its next wake. The caller owns
        the task `run()` executes in and is responsible for awaiting it."""
        self._stop_event.set()
