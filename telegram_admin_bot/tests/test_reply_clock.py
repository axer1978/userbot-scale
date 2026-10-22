"""The reply's clock (ai_responder.Clock): the model is told what time it is
and when each message came, so "tonight" from two days ago, a client back
after a day, and "are you free now?" at four in the morning are not answered
by guessing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ai_responder
from ai_responder import Clock

RIGA = ZoneInfo("Europe/Riga")
# A Friday evening in Riga.
NOW = datetime(2026, 10, 23, 21, 35, tzinfo=RIGA)


def utc(local: datetime) -> datetime:
    """Rows come back from Postgres in UTC."""
    return local.astimezone(timezone.utc)


# ------------------------------------------------------------- the words

def test_a_time_reads_as_today_yesterday_or_the_day():
    assert ai_responder.when(utc(NOW - timedelta(minutes=30)), NOW) == "today 21:05"
    assert ai_responder.when(utc(datetime(2026, 10, 22, 23, 40, tzinfo=RIGA)), NOW) == "yesterday 23:40"
    assert ai_responder.when(utc(datetime(2026, 10, 21, 22, 10, tzinfo=RIGA)), NOW) == "Wednesday 21 October, 22:10"


def test_a_time_is_read_in_the_accounts_timezone_not_utc():
    # 00:30 in Riga on Friday is still Thursday in UTC.
    just_after_midnight = datetime(2026, 10, 23, 0, 30, tzinfo=RIGA)
    assert ai_responder.when(utc(just_after_midnight), NOW) == "today 00:30"


@pytest.mark.parametrize("delta, words", [
    (timedelta(seconds=40), "just now"),
    (timedelta(minutes=3), "3 minutes ago"),
    (timedelta(minutes=75), "1 hour ago"),
    (timedelta(hours=30), "30 hours ago"),
    (timedelta(days=3, hours=2), "3 days ago"),
])
def test_how_long_ago(delta, words):
    assert ai_responder.ago(delta) == words


# ------------------------------------------------------------ the marks

HISTORY = [
    {"role": "user", "content": "hi, free tonight?"},
    {"role": "assistant", "content": "yes love 😘"},
    {"role": "user", "content": "hey again"},
    {"role": "user", "content": "now?"},
]
SENT = (
    utc(datetime(2026, 10, 21, 22, 10, tzinfo=RIGA)),
    utc(datetime(2026, 10, 21, 22, 12, tzinfo=RIGA)),
    utc(NOW - timedelta(minutes=4)),
    utc(NOW - timedelta(minutes=3)),
)


def test_the_first_message_and_one_after_a_gap_get_their_time():
    marked = ai_responder.with_times(HISTORY, Clock(now=NOW, sent_at=SENT))
    assert [m["content"] for m in marked] == [
        "⟦Wednesday 21 October, 22:10⟧ hi, free tonight?",
        "yes love 😘",
        "⟦today 21:31⟧ hey again",
        "now?",
    ]
    # The caller's rows stay as they were (describe_style reads them).
    assert HISTORY[0]["content"] == "hi, free tonight?"


def test_times_that_do_not_line_up_mark_nothing():
    marked = ai_responder.with_times(HISTORY, Clock(now=NOW, sent_at=SENT[:2]))
    assert marked == HISTORY


def test_the_note_says_now_and_when_they_last_wrote():
    note = ai_responder.clock_note(HISTORY, Clock(now=NOW, sent_at=SENT, zone="Europe/Riga"))
    assert note.startswith("Now it is Friday 23 October, 21:35 (Europe/Riga). Their last message came "
                           "3 minutes ago.")
    assert ai_responder.CLOCK_NOTE in note


def test_the_note_without_a_message_from_them():
    ours = [{"role": "assistant", "content": "see you at 22 ❤️"}]
    note = ai_responder.clock_note(ours, Clock(now=NOW, sent_at=(utc(NOW - timedelta(hours=3)),)))
    assert note.startswith("Now it is Friday 23 October, 21:35.") and "Their last message" not in note


def test_a_copied_mark_is_taken_out_of_the_reply():
    assert ai_responder.without_time_marks("⟦today 21:36⟧ yes love|||⟦today 21:36⟧ come 😘") == "yes love|||come 😘"
    assert ai_responder.without_time_marks("at 22 [block]x[/block]") == "at 22 [block]x[/block]"


# ------------------------------------------------- what the model is sent

async def _sent(monkeypatch, history, *, clock=None, booking_note="", reply="ok"):
    seen: dict = {}

    async def fake_complete(*, api_key, messages, ai_config, client=None):
        seen["messages"] = messages
        return reply

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    text = await ai_responder.generate_reply(api_key="k", history=history, persona={}, ai_config={},
                                             booking_note=booking_note, clock=clock)
    return seen["messages"], text


@pytest.mark.asyncio
async def test_the_model_reads_the_times_and_now_last(monkeypatch):
    clock = Clock(now=NOW, sent_at=SENT, zone="Europe/Riga")
    messages, _ = await _sent(monkeypatch, HISTORY, clock=clock)

    assert messages[1] == {"role": "user", "content": "⟦Wednesday 21 October, 22:10⟧ hi, free tonight?"}
    # Their two messages in a row are one turn, the time in front of it.
    assert messages[3] == {"role": "user", "content": "⟦today 21:31⟧ hey again\nnow?"}
    assert messages[-1] == {"role": "system", "content": ai_responder.clock_note(HISTORY, clock)}
    # Nothing that changes by the minute in the system prompt, which DeepSeek caches.
    assert "21:35" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_the_clock_comes_before_the_note_to_write_on_our_own(monkeypatch):
    history = [{"role": "user", "content": "22:00 ok?"}, {"role": "assistant", "content": "let me check"}]
    clock = Clock(now=NOW, sent_at=(utc(NOW - timedelta(minutes=10)), utc(NOW - timedelta(minutes=9))))
    messages, _ = await _sent(monkeypatch, history, clock=clock)

    # One closing turn: the clock, then what to do, which stays the last thing read.
    assert messages[-2]["role"] == "assistant"
    assert messages[-1]["role"] == "system"
    assert messages[-1]["content"] == (ai_responder.clock_note(history, clock) + "\n\n"
                                       + ai_responder.OWN_INITIATIVE_NOTE)


@pytest.mark.asyncio
async def test_a_reply_that_copies_a_mark_comes_back_without_it(monkeypatch):
    _, text = await _sent(monkeypatch, HISTORY, clock=Clock(now=NOW, sent_at=SENT), reply="⟦today 21:36⟧ yes now 😘")
    assert text == "yes now 😘"


@pytest.mark.asyncio
async def test_without_a_clock_nothing_changes(monkeypatch):
    messages, _ = await _sent(monkeypatch, HISTORY)
    assert messages[-1] == {"role": "user", "content": "hey again\nnow?"}
    assert not any("⟦" in m["content"] for m in messages)
