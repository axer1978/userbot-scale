"""Storage behaviour the rest of the app depends on.

Needs real Postgres (via the `db` fixture -> `pg_pool`); skipped when
PG_TEST_DSN isn't set. Every call here uses the exact same method names and
signatures the old SQLite-backed Database had — this file is intentionally
almost unchanged from before the Postgres rewrite.
"""

from __future__ import annotations

import pytest

from database import (
    DIR_IN,
    DIR_OUT,
    STATUS_PENDING,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    STATUS_SENT,
)

pytestmark = pytest.mark.requires_pg


@pytest.mark.asyncio
async def test_the_same_telegram_message_is_only_stored_once(db):
    """We send via Telethon *and* watch outgoing events, so the same message
    arrives twice."""
    await db.upsert_conversation(1, "A", None, False, 1)
    first = await db.record_message(1, DIR_OUT, STATUS_SENT, "hi", telegram_id=555)
    second = await db.record_message(1, DIR_OUT, STATUS_SENT, "hi", telegram_id=555)
    assert first is not None
    assert second is None, "duplicate telegram_id was stored a second time"


@pytest.mark.asyncio
async def test_history_for_ai_is_oldest_first(db):
    await db.upsert_conversation(1, "A", None, False, 1)
    for i, t in enumerate(["first", "second", "third"]):
        await db.record_message(1, DIR_IN, STATUS_RECEIVED, t, telegram_id=i)
    history = await db.get_history_for_ai(1)
    assert [m["content"] for m in history] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_history_excludes_drafts_and_rejections(db):
    """Only what actually crossed the wire is context for the model."""
    await db.upsert_conversation(1, "A", None, False, 1)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "real", telegram_id=1)
    await db.record_message(1, DIR_OUT, STATUS_PENDING, "unapproved draft", telegram_id=2)
    await db.record_message(1, DIR_OUT, STATUS_REJECTED, "rejected", telegram_id=3)

    contents = [m["content"] for m in await db.get_history_for_ai(1)]
    assert contents == ["real"]


@pytest.mark.asyncio
async def test_history_is_scoped_to_one_chat(db):
    """The whole multi-conversation design rests on this."""
    for chat in (1, 2):
        await db.upsert_conversation(chat, f"P{chat}", None, False, 1)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "for one", telegram_id=1)
    await db.record_message(2, DIR_IN, STATUS_RECEIVED, "for two", telegram_id=2)

    assert [m["content"] for m in await db.get_history_for_ai(1)] == ["for one"]
    assert [m["content"] for m in await db.get_history_for_ai(2)] == ["for two"]


@pytest.mark.asyncio
async def test_incoming_and_outgoing_map_to_the_right_roles(db):
    await db.upsert_conversation(1, "A", None, False, 1)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "them", telegram_id=1)
    await db.record_message(1, DIR_OUT, STATUS_SENT, "us", telegram_id=2)
    roles = [m["role"] for m in await db.get_history_for_ai(1)]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_history_limit_keeps_the_most_recent(db):
    await db.upsert_conversation(1, "A", None, False, 1)
    for i in range(10):
        await db.record_message(1, DIR_IN, STATUS_RECEIVED, f"m{i}", telegram_id=i)
    history = await db.get_history_for_ai(1, limit=3)
    assert [m["content"] for m in history] == ["m7", "m8", "m9"]


@pytest.mark.asyncio
async def test_sent_since_counts_only_what_we_sent(db):
    await db.upsert_conversation(1, "A", None, False, 1)
    await db.record_message(1, DIR_OUT, STATUS_SENT, "ours", telegram_id=1)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "theirs", telegram_id=2)
    await db.record_message(1, DIR_OUT, STATUS_PENDING, "draft", telegram_id=3)
    assert await db.sent_since("1970-01-01T00:00:00") == 1


@pytest.mark.asyncio
async def test_distinct_peers_counts_people_not_messages(db):
    for chat in (1, 2):
        await db.upsert_conversation(chat, f"P{chat}", None, False, 1)
    for i in range(5):
        await db.record_message(1, DIR_OUT, STATUS_SENT, f"m{i}", telegram_id=i)
    await db.record_message(2, DIR_OUT, STATUS_SENT, "one", telegram_id=99)

    assert await db.sent_since("1970-01-01T00:00:00") == 6
    assert await db.distinct_peers_since("1970-01-01T00:00:00") == 2


@pytest.mark.asyncio
async def test_counters_respect_the_since_cutoff(db):
    await db.upsert_conversation(1, "A", None, False, 1)
    await db.record_message(1, DIR_OUT, STATUS_SENT, "today", telegram_id=1)
    assert await db.sent_since("2999-01-01T00:00:00") == 0


@pytest.mark.asyncio
async def test_pausing_one_conversation_leaves_others_alone(db):
    for chat in (1, 2):
        await db.upsert_conversation(chat, f"P{chat}", None, False, 1)
    await db.set_paused(1, True)
    assert (await db.get_conversation(1))["automation_paused"]
    assert not (await db.get_conversation(2))["automation_paused"]
