"""Lease-fencing: the property that stops two workers from ever operating
the same Telegram session at once (the AUTH_KEY_UNREGISTERED bug).

Needs real Postgres — the whole point is testing the atomicity of a
concurrent `UPDATE ... WHERE`, which no fake can stand in for.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import leasing

pytestmark = pytest.mark.requires_pg


async def _seed_session(pg_pool, session_id: str = "acct01", *, active: bool = True) -> None:
    async with pg_pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, is_active) VALUES ($1, $2)",
            session_id,
            active,
        )


async def _backdate_lease(pg_pool, session_id: str, worker_id: str, *, seconds_ago: float) -> None:
    async with pg_pool.acquire() as con:
        await con.execute(
            "UPDATE telegram_sessions SET lease_worker_id=$2, "
            "lease_expires_at = now() - make_interval(secs => $3) WHERE session_id=$1",
            session_id,
            worker_id,
            seconds_ago,
        )


@pytest.mark.asyncio
async def test_acquire_marks_the_session_and_returns_a_lease(pg_pool):
    await _seed_session(pg_pool)
    lease = await leasing.acquire(pg_pool, "acct01", "w1")
    assert lease is not None
    assert lease.session_id == "acct01"
    assert lease.worker_id == "w1"
    holder, expires_at = await leasing.holder(pg_pool, "acct01")
    assert holder == "w1"
    assert expires_at is not None


@pytest.mark.asyncio
async def test_two_concurrent_acquires_only_one_wins(pg_pool):
    await _seed_session(pg_pool)
    results = await asyncio.gather(
        leasing.acquire(pg_pool, "acct01", "w1"),
        leasing.acquire(pg_pool, "acct01", "w2"),
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    holder, _ = await leasing.holder(pg_pool, "acct01")
    assert holder == winners[0].worker_id


@pytest.mark.asyncio
async def test_fifty_concurrent_acquires_only_one_wins(pg_pool):
    await _seed_session(pg_pool)
    results = await asyncio.gather(
        *[leasing.acquire(pg_pool, "acct01", f"w{i}") for i in range(50)]
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


@pytest.mark.asyncio
async def test_a_held_lease_cannot_be_acquired_by_another_worker(pg_pool):
    await _seed_session(pg_pool)
    first = await leasing.acquire(pg_pool, "acct01", "w1")
    assert first is not None
    second = await leasing.acquire(pg_pool, "acct01", "w2")
    assert second is None


@pytest.mark.asyncio
async def test_an_expired_lease_can_be_acquired_by_a_new_worker(pg_pool):
    await _seed_session(pg_pool)
    await _backdate_lease(pg_pool, "acct01", "w1", seconds_ago=5)
    lease = await leasing.acquire(pg_pool, "acct01", "w2")
    assert lease is not None
    assert lease.worker_id == "w2"


@pytest.mark.asyncio
async def test_renew_extends_the_expiry(pg_pool):
    await _seed_session(pg_pool)
    first = await leasing.acquire(pg_pool, "acct01", "w1")
    renewed = await leasing.renew(pg_pool, "acct01", "w1")
    assert renewed is not None
    assert renewed.expires_at >= first.expires_at


@pytest.mark.asyncio
async def test_renew_fails_for_a_worker_that_lost_the_lease(pg_pool):
    await _seed_session(pg_pool)
    await leasing.acquire(pg_pool, "acct01", "w1")
    await _backdate_lease(pg_pool, "acct01", "w1", seconds_ago=1)
    stolen = await leasing.acquire(pg_pool, "acct01", "w2")
    assert stolen is not None
    renewed = await leasing.renew(pg_pool, "acct01", "w1")
    assert renewed is None


@pytest.mark.asyncio
async def test_renew_many_reports_exactly_the_lost_sessions(pg_pool):
    for i in range(25):
        await _seed_session(pg_pool, f"acct{i:02d}")
    for i in range(25):
        await leasing.acquire(pg_pool, f"acct{i:02d}", "w1")
    # Lose exactly one of them.
    await _backdate_lease(pg_pool, "acct07", "w1", seconds_ago=1)
    await leasing.acquire(pg_pool, "acct07", "w2")

    ids = [f"acct{i:02d}" for i in range(25)]
    renewed = await leasing.renew_many(pg_pool, ids, "w1")
    assert set(renewed.keys()) == set(ids) - {"acct07"}


@pytest.mark.asyncio
async def test_release_frees_it_immediately(pg_pool):
    await _seed_session(pg_pool)
    await leasing.acquire(pg_pool, "acct01", "w1")
    ok = await leasing.release(pg_pool, "acct01", "w1")
    assert ok is True
    lease = await leasing.acquire(pg_pool, "acct01", "w2")
    assert lease is not None


@pytest.mark.asyncio
async def test_release_by_a_non_holder_is_a_no_op(pg_pool):
    await _seed_session(pg_pool)
    await leasing.acquire(pg_pool, "acct01", "w1")
    ok = await leasing.release(pg_pool, "acct01", "w2")
    assert ok is False
    holder, _ = await leasing.holder(pg_pool, "acct01")
    assert holder == "w1"


@pytest.mark.asyncio
async def test_acquire_skips_inactive_sessions(pg_pool):
    await _seed_session(pg_pool, active=False)
    lease = await leasing.acquire(pg_pool, "acct01", "w1")
    assert lease is None


@pytest.mark.asyncio
async def test_epoch_increments_on_every_acquire(pg_pool):
    await _seed_session(pg_pool)
    first = await leasing.acquire(pg_pool, "acct01", "w1")
    await leasing.release(pg_pool, "acct01", "w1")
    second = await leasing.acquire(pg_pool, "acct01", "w2")
    assert second.epoch == first.epoch + 1


@pytest.mark.asyncio
async def test_keeper_fences_a_session_when_renewal_is_refused(pg_pool):
    await _seed_session(pg_pool, "acct01")
    await _seed_session(pg_pool, "acct02")
    lost: list[str] = []

    async def on_lost(session_id: str) -> None:
        lost.append(session_id)

    keeper = leasing.LeaseKeeper(pg_pool, "w1", on_lost=on_lost, interval=0.05, danger=0.2)
    lease1 = await leasing.acquire(pg_pool, "acct01", "w1")
    lease2 = await leasing.acquire(pg_pool, "acct02", "w1")
    keeper.track(lease1)
    keeper.track(lease2)

    # Someone else takes acct01's lease out from under w1.
    await _backdate_lease(pg_pool, "acct01", "w1", seconds_ago=1)
    await leasing.acquire(pg_pool, "acct01", "w2")

    await keeper._tick(asyncio.get_event_loop().time())

    assert lost == ["acct01"]
    assert keeper.is_safe("acct02") is True
    assert keeper.is_safe("acct01") is False


@pytest.mark.asyncio
async def test_keeper_fences_immediately_when_every_tracked_session_is_confirmed_lost(monkeypatch):
    """A successful renew_many() call that legitimately returns an empty
    dict (every tracked session lost its lease in the same tick) must fence
    right away — it must not be mistaken for the call itself having failed,
    which would otherwise wait out the whole danger window before reacting."""
    lost: list[str] = []

    async def on_lost(session_id: str) -> None:
        lost.append(session_id)

    async def fake_renew_many(pool, session_ids, worker_id, *, ttl):
        return {}  # call succeeds, confirms every session lost

    monkeypatch.setattr(leasing, "renew_many", fake_renew_many)

    keeper = leasing.LeaseKeeper(object(), "w1", on_lost=on_lost, interval=0.05, danger=100.0)
    keeper.track(
        leasing.Lease(session_id="acct01", worker_id="w1", epoch=1, expires_at=datetime.now(timezone.utc))
    )
    keeper.track(
        leasing.Lease(session_id="acct02", worker_id="w1", epoch=1, expires_at=datetime.now(timezone.utc))
    )

    await keeper._tick(asyncio.get_event_loop().time())

    # Both fenced on the very first tick, well inside the 100s danger window —
    # proof this took the "confirmed lost" path, not the "call failed" path.
    assert set(lost) == {"acct01", "acct02"}


@pytest.mark.asyncio
async def test_keeper_fences_everything_when_postgres_is_unreachable_past_the_danger_window():
    class _BrokenPool:
        def acquire(self):
            raise RuntimeError("connection refused")

    lost: list[str] = []

    async def on_lost(session_id: str) -> None:
        lost.append(session_id)

    keeper = leasing.LeaseKeeper(_BrokenPool(), "w1", on_lost=on_lost, interval=0.05, danger=0.2)
    lease = leasing.Lease(
        session_id="acct01", worker_id="w1", epoch=1, expires_at=datetime.now(timezone.utc)
    )
    keeper.track(lease)

    loop_time = asyncio.get_event_loop().time
    start = loop_time()
    await keeper._tick(start)
    assert keeper.is_safe("acct01") is True  # still inside the danger window

    await keeper._tick(start + 0.3)  # past the 0.2s danger window
    assert lost == ["acct01"]


@pytest.mark.asyncio
async def test_keeper_keeps_the_other_sessions_running_when_one_is_lost(pg_pool):
    await _seed_session(pg_pool, "acct01")
    await _seed_session(pg_pool, "acct02")
    await _seed_session(pg_pool, "acct03")
    lost: list[str] = []

    async def on_lost(session_id: str) -> None:
        lost.append(session_id)

    keeper = leasing.LeaseKeeper(pg_pool, "w1", on_lost=on_lost, interval=0.05, danger=0.2)
    for sid in ("acct01", "acct02", "acct03"):
        keeper.track(await leasing.acquire(pg_pool, sid, "w1"))

    await _backdate_lease(pg_pool, "acct02", "w1", seconds_ago=1)
    await leasing.acquire(pg_pool, "acct02", "intruder")

    await keeper._tick(asyncio.get_event_loop().time())

    assert lost == ["acct02"]
    assert keeper.is_safe("acct01") is True
    assert keeper.is_safe("acct03") is True
