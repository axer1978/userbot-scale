"""The guards that keep the Telegram account alive.

These matter more than the rest of the suite: if they regress, the failure
mode is a banned phone number rather than a bad reply.
"""

from __future__ import annotations

import pytest
from telethon import errors

import session_runtime
from database import DIR_OUT, STATUS_SENT


async def _sent(db, chat_id: int, n: int, start: int = 0) -> None:
    await db.upsert_conversation(chat_id, f"P{chat_id}", None, False, 1)
    for i in range(n):
        await db.record_message(
            chat_id, DIR_OUT, STATUS_SENT, f"m{i}", telegram_id=start + i
        )


# --------------------------------------------------------------- daily caps


@pytest.mark.asyncio
async def test_quota_allows_sending_on_a_quiet_day(app, db):
    await app.check_daily_quota()  # no messages yet: must not raise


@pytest.mark.asyncio
async def test_total_send_limit_blocks_further_messages(app, db):
    app.config["safety"]["daily_send_limit"] = 3
    await _sent(db, 1, 3)
    with pytest.raises(session_runtime.SendBlocked, match="Daily send limit"):
        await app.check_daily_quota()


@pytest.mark.asyncio
async def test_send_limit_counts_replies_not_just_outreach(app, db):
    """Telegram counts all outbound volume, so the ceiling must too."""
    app.config["safety"]["daily_send_limit"] = 2
    await _sent(db, 42, 2)  # ordinary replies, no outreach rows at all
    with pytest.raises(session_runtime.SendBlocked):
        await app.check_daily_quota()


@pytest.mark.asyncio
async def test_distinct_people_capped_separately_from_volume(app, db):
    """Breadth is the stronger spam signal, so it has its own tighter cap."""
    app.config["safety"]["daily_send_limit"] = 10_000
    app.config["safety"]["daily_peer_limit"] = 2
    for i, chat in enumerate((10, 11, 12)):
        await _sent(db, chat, 1, start=100 * (i + 1))
    with pytest.raises(session_runtime.SendBlocked, match="distinct people"):
        await app.check_daily_quota()


@pytest.mark.asyncio
async def test_many_messages_to_one_person_do_not_trip_the_peer_cap(app, db):
    """A long conversation with one person is not spam."""
    app.config["safety"]["daily_peer_limit"] = 2
    await _sent(db, 7, 25)
    await app.check_daily_quota()  # must not raise


# ------------------------------------------------------- who we may write to


@pytest.mark.asyncio
async def test_stranger_is_refused(app, db):
    with pytest.raises(session_runtime.SendBlocked, match="stranger"):
        await app.may_message(999_999)


@pytest.mark.asyncio
async def test_known_person_is_allowed(app, db):
    await db.upsert_conversation(5, "Known", None, False, 1)
    await app.may_message(5)


@pytest.mark.asyncio
async def test_the_check_can_be_turned_off(app, db):
    app.config["safety"]["known_contacts_only"] = False
    await app.may_message(999_999)  # opt-out honoured


# ------------------------------------------------ reacting to Telegram itself


@pytest.mark.asyncio
async def test_peer_flood_halts_everything(app, db, monkeypatch):
    """PeerFloodError is Telegram's spam warning. Sending through it is what
    turns a warning into a ban, so it must stop the account."""
    halted = []
    monkeypatch.setattr(app, "halt_everything", lambda r: halted.append(r) or _noop())
    assert await app.handle_send_failure(1, errors.PeerFloodError(None)) is True
    assert halted, "PeerFloodError did not halt automation"


@pytest.mark.asyncio
async def test_peer_flood_halt_can_be_disabled(app, db, monkeypatch):
    app.config["safety"]["halt_on_peer_flood"] = False
    halted = []
    monkeypatch.setattr(app, "halt_everything", lambda r: halted.append(r) or _noop())
    assert await app.handle_send_failure(1, errors.PeerFloodError(None)) is True
    assert not halted


@pytest.mark.asyncio
async def test_short_flood_wait_is_slept_not_halted(app, db, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(session_runtime.asyncio, "sleep", fake_sleep)
    halted = []
    monkeypatch.setattr(app, "halt_everything", lambda r: halted.append(r) or _noop())

    exc = errors.FloodWaitError(None)
    exc.seconds = 30
    assert await app.handle_send_failure(1, exc) is True
    assert slept == [30], "did not wait exactly as long as Telegram asked"
    assert not halted


@pytest.mark.asyncio
async def test_long_flood_wait_halts_instead_of_blocking(app, db, monkeypatch):
    """Retrying into a long limit is what deepens it.

    asyncio.sleep is stubbed deliberately: if this guard regresses, the code
    tries to sleep for the full day Telegram asked for, and an un-stubbed
    version of this test would hang the suite instead of failing it.
    """
    app.config["safety"]["max_flood_wait_seconds"] = 60
    halted, slept = [], []
    monkeypatch.setattr(app, "halt_everything", lambda r: halted.append(r) or _noop())

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(session_runtime.asyncio, "sleep", fake_sleep)

    exc = errors.FloodWaitError(None)
    exc.seconds = 86_400
    assert await app.handle_send_failure(1, exc) is True
    assert halted, "a day-long flood wait should stop the account"
    assert not slept, "blocked a task for a day instead of halting"


@pytest.mark.asyncio
async def test_blocked_by_recipient_pauses_only_that_chat(app, db):
    await db.upsert_conversation(3, "Blocker", None, False, 1)
    await db.upsert_conversation(4, "Other", None, False, 1)

    assert await app.handle_send_failure(3, errors.UserIsBlockedError(None)) is True

    assert (await db.get_conversation(3))["automation_paused"]
    assert not (await db.get_conversation(4))["automation_paused"], \
        "an unrelated conversation must not be paused"


@pytest.mark.asyncio
async def test_revoked_session_halts(app, db, monkeypatch):
    halted = []
    monkeypatch.setattr(app, "halt_everything", lambda r: halted.append(r) or _noop())
    assert await app.handle_send_failure(1, errors.AuthKeyUnregisteredError(None)) is True
    assert halted


@pytest.mark.asyncio
async def test_unrecognised_errors_are_passed_back_to_the_caller(app, db):
    """So genuine bugs still get logged instead of silently swallowed."""
    assert await app.handle_send_failure(1, ValueError("something else")) is False


async def _noop():
    return None
