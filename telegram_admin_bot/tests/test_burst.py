"""Replies sent as several short messages in a row.

The model is asked to separate a burst with '|||'; split_burst takes the
separator back off, and send_burst turns the parts into consecutive Telegram
messages. The separator must never reach Telegram as literal text, and each
part has to count as a real message everywhere it matters — the database, the
panel, and the daily ceilings above all.
"""

from __future__ import annotations

import pytest

import ai_responder
from database import DIR_OUT, STATUS_PENDING, STATUS_SENT


# ------------------------------------------------------------ splitting

def test_a_reply_without_a_separator_is_one_message():
    assert ai_responder.split_burst("just the one") == ["just the one"]


def test_a_separator_splits_the_reply():
    assert ai_responder.split_burst("ахах|||ну да") == ["ахах", "ну да"]


def test_the_separator_is_stripped_off_its_own_line():
    """What the prompt actually asks for: the pipes alone on a line."""
    assert ai_responder.split_burst("first\n|||\nsecond") == ["first", "second"]


def test_a_line_break_is_a_boundary_too():
    """What the model actually does: it presses Enter between thoughts and
    ignores the separator. Each line has to go out as its own message."""
    assert ai_responder.split_burst("да не холодно\nпросто войс настроить надо было") == [
        "да не холодно", "просто войс настроить надо было"
    ]
    assert ai_responder.split_burst("ахах точно\r\n\r\nщас") == ["ахах точно", "щас"]


def test_separator_and_line_breaks_mix():
    assert ai_responder.split_burst("one\n|||\ntwo\nthree") == ["one", "two", "three"]


def test_padding_and_extra_pipes_are_tolerated():
    """Models pad the separator and lean on the key; none of it may leak."""
    assert ai_responder.split_burst("one  ||||  two ||| three") == [
        "one", "two", "three"
    ]


def test_empty_segments_are_dropped():
    assert ai_responder.split_burst("|||hey|||\n\n|||there|||") == ["hey", "there"]


def test_nothing_but_separators_is_not_a_message():
    """Better an empty reply the caller reports than a blank message sent."""
    assert ai_responder.split_burst("|||") == []
    assert ai_responder.split_burst("  ||| \n |||  ") == []


def test_an_empty_reply_splits_to_nothing():
    assert ai_responder.split_burst("") == []


def test_a_runaway_burst_is_capped_not_flooded():
    """Every part is a real message, so the count cannot be left to the model."""
    parts = ai_responder.split_burst("a|||b|||c|||d|||e|||f")
    assert len(parts) == ai_responder.MAX_BURST_MESSAGES
    # The overflow rides along on the last message rather than vanishing.
    assert parts[-1] == "d e f"


# ------------------------------------------------------------ the prompt

@pytest.mark.asyncio
async def test_the_model_is_told_how_to_burst(monkeypatch):
    """The note was dead code until it was actually put in the system prompt."""
    seen: dict = {}

    async def fake_complete(*, api_key, messages, ai_config, client=None):
        seen["system"] = messages[0]["content"]
        return "ok"

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    await ai_responder.generate_reply(
        api_key="k",
        history=[{"role": "user", "content": "hi"}],
        persona={},
        ai_config={},
    )
    assert ai_responder.BURST_OUTPUT_NOTE in seen["system"]
    assert ai_responder.BURST_SEPARATOR in seen["system"]


@pytest.mark.asyncio
async def test_an_opener_never_bursts(monkeypatch):
    """A cold first message arriving as three bubbles reads as a bot."""
    async def fake_complete(*, api_key, messages, ai_config, client=None):
        assert ai_responder.BURST_OUTPUT_NOTE not in messages[0]["content"]
        return "hey|||long time"

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    text = await ai_responder.generate_opener(
        api_key="k", goal="say hello", recipient_name="J",
        persona={}, ai_config={},
    )
    assert text == "hey long time"


# ------------------------------------------------------------ sending

@pytest.fixture
def outbox(app, monkeypatch):
    """Capture what would go to Telegram, without the delays."""
    sent: list[str] = []

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.append(text)
        return type("Sent", (), {"id": 1000 + len(sent)})()

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "BURST_GAP_MIN_SECONDS", 0)
    monkeypatch.setattr(app, "BURST_GAP_MAX_SECONDS", 0)
    return sent


@pytest.mark.asyncio
async def test_each_part_is_sent_as_its_own_message(app, db, outbox):
    await db.upsert_conversation(7, "J", None, False, 1)
    await app.send_burst(7, ["ахах", "ну да", "жесть"])

    assert outbox == ["ахах", "ну да", "жесть"]
    rows = await db.get_messages(7)
    assert [r["text"] for r in rows] == ["ахах", "ну да", "жесть"]
    assert all(r["status"] == STATUS_SENT for r in rows)


@pytest.mark.asyncio
async def test_the_separator_never_reaches_telegram(app, db, outbox):
    """The end-to-end point of the feature."""
    await db.upsert_conversation(7, "J", None, False, 1)
    await app.send_burst(7, ai_responder.split_burst("hey|||you around?"))

    assert outbox == ["hey", "you around?"]
    assert not any(ai_responder.BURST_SEPARATOR in t for t in outbox)


@pytest.mark.asyncio
async def test_a_burst_counts_fully_against_the_daily_limit(app, db, outbox):
    """Three bubbles are three messages, not one reply."""
    await db.upsert_conversation(7, "J", None, False, 1)
    await app.send_burst(7, ["a", "b", "c"])

    assert await db.sent_since("1970-01-01T00:00:00+00:00") == 3


@pytest.mark.asyncio
async def test_the_daily_limit_stops_a_burst_partway(app, db, outbox):
    """The guard runs per part, so a burst cannot overshoot the ceiling."""
    app.config["safety"]["daily_send_limit"] = 2
    await db.upsert_conversation(7, "J", None, False, 1)

    with pytest.raises(app.SendBlocked):
        await app.send_burst(7, ["a", "b", "c"])

    # Stopped at the limit rather than sending all three.
    assert outbox == ["a", "b"]


@pytest.mark.asyncio
async def test_approving_a_draft_settles_it_on_the_first_part(app, db, outbox):
    """The pending row becomes the first message; the rest are new rows."""
    await db.upsert_conversation(7, "J", None, False, 1)
    draft = await db.record_message(
        7, DIR_OUT, STATUS_PENDING, "hey|||you around?", bump_preview=False
    )

    await app.send_burst(
        7, ai_responder.split_burst(draft["text"]), draft_id=draft["id"]
    )

    rows = await db.get_messages(7)
    assert [r["text"] for r in rows] == ["hey", "you around?"]
    assert all(r["status"] == STATUS_SENT for r in rows)
    # Settled in place: approving must not leave the draft behind as a second row.
    assert rows[0]["id"] == draft["id"]
    assert len(rows) == 2
