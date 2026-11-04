"""The bot does not say the same thing over and over (repeats.py): a client
who keeps sending the same message stops getting answers to it, and a reply
line already used in the chat or with many other clients is written again in
other words."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

import ai_responder
import repeats
import session_runtime
import tenant_config
import unanswered
from database import DIR_IN, DIR_OUT, STATUS_RECEIVED, STATUS_SENT

NOW = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)
RATES = "120 - 30 min\n200 - 1 hour"


def chat(*lines: str) -> list[dict[str, str]]:
    """'> text' is theirs, anything else ours."""
    return [{"role": "user", "content": line[2:]} if line.startswith("> ") else
            {"role": "assistant", "content": line} for line in lines]


def minutes_apart(history: list, start: datetime = NOW - timedelta(hours=1)) -> tuple:
    return tuple(start + timedelta(minutes=i) for i in range(len(history)))


# ------------------------------------------------------------- the keys

def test_a_key_leaves_out_case_emoji_and_punctuation_but_keeps_digits():
    assert repeats.key("Send me MORE pics!!! 😘😘") == "send me more pics"
    assert repeats.key("that's all my photos love ❤️") == "thats all my photos love"
    assert repeats.key("at 22:30?") == "at 22 30"
    assert repeats.key("😘 ?") == ""


def test_lines_all_but_equal_are_the_same_but_never_with_other_digits():
    assert repeats.same("can i see more pics", "can i see more pics pls")
    assert not repeats.same("can i come at 22", "can i come at 23")
    # Short ones must be equal.
    assert not repeats.same("send video", "send video pls")
    assert repeats.same("video", "video")
    assert not repeats.same("", "")


# ------------------------------------------------------------- his side

LOOP = ("> send video", "you'll see me in person love",
        "> send video", "no video love, come and see me",
        "> send video", "you'll see everything when you come")


def test_the_same_message_answered_three_times_is_not_answered_a_fourth():
    history = chat(*LOOP, "> send video")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 3


def test_answered_only_twice_it_is_answered_again():
    history = chat(*LOOP[2:], "> send video")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


def test_no_limit_means_no_limit():
    history = chat(*LOOP, "> send video")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 0) == 0


def test_the_same_words_all_but_equal_count_too():
    history = chat("> can I see more pics", "here love", "> can i see more pics?", "that's all love",
                   "> can I see more pics pls", "you'll see me in person", "> Can I see more pics!!")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 3


def test_a_message_that_names_a_time_is_always_answered():
    history = chat("> at 22?", "what time love?", "> at 22?", "ok love", "> at 22?", "yes love", "> at 22?")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


def test_something_new_with_the_repeat_is_answered():
    history = chat(*LOOP, "> send video", "> ok I come tomorrow evening")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


def test_a_photo_or_a_question_mark_is_never_a_repeat():
    history = chat("> [photo]", "nice love", "> [photo]", "thank you", "> [photo]", "😘", "> [photo]")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0
    history = chat("> ?", "yes love", "> ?", "here", "> ?", "love?", "> ?")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


def test_only_the_last_day_counts():
    history = chat(*LOOP, "> send video")
    two_days_ago = NOW - timedelta(days=2)
    sent_at = (two_days_ago,) * 2 + minutes_apart(history)[2:]
    # The first ask is two days old: answered twice in the last day.
    assert repeats.repeated_ask(history, sent_at, NOW, 3) == 0


def test_without_times_the_whole_history_counts():
    history = chat(*LOOP, "> send video")
    assert repeats.repeated_ask(history, (), NOW, 3) == 3


def test_sending_it_twice_before_an_answer_is_one_ask():
    history = chat("> send video", "> send video", "no love", "> send video", "no love",
                   "> send video", "no love", "> send video")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 3
    history = chat("> send video", "> send video", "no love", "> send video", "no love", "> send video")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


def test_nothing_waits_when_we_wrote_last():
    history = chat(*LOOP)
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


def test_a_time_or_a_day_in_their_latest_messages_is_found():
    assert repeats.names_when_now(chat("hi love", "> ok", "> I come at 22"))
    assert not repeats.names_when_now(chat("> at 22", "ok love", "> maybe later"))
    for when in ("now?", "tonight", "tomorrow evening", "after 22", "in an hour", "22h", "saturday", "завтра"):
        assert repeats.names_when(when), when
    assert not repeats.names_when("send me a video first")


@pytest.mark.parametrize("when", [
    "rn", "free atm?", "right away", "2nite", "2moro", "8", "20?", "20.30", "20 30", "2030", "20h30", "8ish",
    "щас можно", "прямо сейчас", "послезавтра", "через час", "в субботу", "seichas mozhno", "cherez chas",
    "tagad", "tūlīt", "tulit", "šobrīd", "sobrid", "pašlaik", "parīt", "sestdien", "pēc stundas", "pusdeviņos", "8os",
    "kohe", "nüüd", "nuud", "õhtul", "laupäeval", "kell 8", "pool üheksa", "tunni pärast",
    "ahora", "hoy", "mañana", "esta noche", "a las 8", "el sábado", "en media hora",
])
def test_a_time_or_a_day_in_any_of_their_words(when):
    assert repeats.names_when(when)


@pytest.mark.parametrize("other", ["send me a video first", "how much", "сколько стоит", "cik maksā", "mis hind",
                                   "cuánto cuesta", "are you real", "more photos", "ты настоящая"])
def test_what_names_no_time_stays_that(other):
    assert not repeats.names_when(other)


@pytest.mark.parametrize("message", ["nearby", "almost there", "рядом", "подъезжаю", "tuvumā", "esmu lejā", "olen kohal",
                                     "kohe jõuan", "llegando", "estoy abajo"])
def test_on_his_way_or_there_in_any_of_their_words_is_never_a_repeat(message):
    history = chat(f"> {message} love ok then", "what time love?", f"> {message} love ok then", "1 hour?",
                   f"> {message} love ok then", "ok?", f"> {message} love ok then")
    assert repeats.repeated_ask(history, (), NOW, 3, ["ok", "thanks"]) == 0


@pytest.mark.parametrize("message", ["yes", "ok", "hi", "I'm here", "outside", "where are you?", "on my way",
                                     "tomorrow evening", "send video tonight", "ok thanks"])
def test_answers_arriving_where_and_when_are_never_a_repeat(message):
    """'yes' to four different questions, 'I'm here' while he waits outside,
    'where are you' on his way: always answered."""
    history = chat(f"> {message}", "what time love?", f"> {message}", "1 hour?", f"> {message}", "ok?",
                   f"> {message}")
    assert repeats.repeated_ask(history, (), NOW, 3, ["ok", "thanks"]) == 0


def test_a_photo_with_the_repeat_is_something_new():
    history = chat(*LOOP, "> send video", "> [photo] a man at a door")
    assert repeats.repeated_ask(history, minutes_apart(history), NOW, 3) == 0


# ------------------------------------------------------------- our side

def test_own_lines_leave_out_blocks_tags_and_short_lines():
    text = ("hi love 😘\nwhat time do you want to come?\n[block]\n" + RATES + "\n[/block]\n"
            "[address]\nhere are my photos love [send 3]\n[NO_REPLY]")
    assert repeats.own_lines(text) == ["what time do you want to come?", "here are my photos love"]
    # The prompt's own block, written without the markers, is a block too.
    assert repeats.own_lines("so what time babe?\n" + RATES, fixed=(RATES,)) == ["so what time babe?"]


def test_a_line_this_chat_already_had_is_a_repeat():
    history = chat("> hi", "hi love 😘 what time do you want to come?", "> how much")
    said = repeats.said_in_chat(history)
    assert repeats.repeated_lines("what time do you want to come love?", chat_keys=said, used_elsewhere=[]) \
        == ["what time do you want to come love?"]
    # A line sent merged with another counts on its own too.
    assert repeats.repeated_lines("hi love\nwhat time do you want to come", chat_keys=said,
                                  used_elsewhere=[]) == ["what time do you want to come"]
    assert repeats.repeated_lines("so when are you coming babe?", chat_keys=said, used_elsewhere=[]) == []
    # Nearly the same words, in another order, are the same question.
    assert repeats.repeated_lines("love what time do you want to come", chat_keys=said, used_elsewhere=[]) \
        == ["love what time do you want to come"]
    # A few words in common are not.
    assert repeats.repeated_lines("do you want me to come to you love", chat_keys=said, used_elsewhere=[]) == []


def test_other_digits_are_another_line():
    said = repeats.said_in_chat(chat("yes love it's booked for today 22:00"))
    assert repeats.repeated_lines("yes love it's booked for today 23:00", chat_keys=said, used_elsewhere=[]) == []


def test_lines_sent_to_many_chats_are_overused():
    rows = [(1, "hi love what time do you want to come?"), (2, "Hi love, what time do you want to come"),
            (3, "hi love what time do you want to come 😘"), (3, "hi love what time do you want to come"),
            (4, "so when are you coming babe"), (5, "so when are you coming babe"),
            (1, RATES), (2, RATES), (3, RATES), (4, RATES),
            (1, "[sent photo #3]"), (2, "[sent photo #3]"), (3, "[sent photo #3]"),
            (1, "Vene 11, Tallinn, Old Town"), (2, "Vene 11, Tallinn, Old Town"), (3, "Vene 11, Tallinn, Old Town"),
            (1, "ok my love"), (2, "ok my love"), (3, "ok my love")]
    # The rates (several lines), media, the address and short lines never count.
    found = repeats.overused(rows, 3, skip=["Vene 11, Tallinn"])
    assert [(repeats.key(text), chats) for text, chats in found] == [("hi love what time do you want to come", 3)]
    assert repeats.overused(rows, 2, skip=["Vene 11, Tallinn"])[1] == ("so when are you coming babe", 2)
    # Shown the way it was written most often.
    rows.append((5, "hi love what time do you want to come 😘"))
    assert repeats.overused(rows, 3)[0] == ("hi love what time do you want to come 😘", 4)


def test_lines_with_a_number_are_never_shown_and_pet_names_are_one_line():
    rows = [(c, "come up to the 3rd floor love") for c in (1, 2, 3)]
    rows += [(1, "hi love what time do you want to come"), (2, "hi babe what time do you want to come"),
             (3, "hi my love what time do you want to come")]
    assert [chats for _, chats in repeats.overused(rows, 3)] == [3]
    assert repeats.key(repeats.overused(rows, 3)[0][0]).endswith("what time do you want to come")


def test_a_fallback_phrase_is_never_reworded_nor_shown():
    rows = [(c, "let me check with my manager and come back to you") for c in (1, 2, 3)]
    assert repeats.overused(rows, 3, skip=["let me check"]) == []
    said = repeats.said_in_chat(chat("let me check with my manager love"))
    assert repeats.repeated_lines("let me check with my manager love", chat_keys=said, used_elsewhere=[],
                                  skip=["let me check"]) == []


def test_an_overused_line_is_a_repeat_anywhere():
    assert repeats.repeated_lines("Hi love 😘 what time do you want to come?", chat_keys=[],
                                  used_elsewhere=["hi love what time do you want to come"]) \
        == ["Hi love 😘 what time do you want to come?"]


def test_the_note_shows_each_line_once():
    assert repeats.shown(["a b c d", "A b c d!", "x" * 200], limit=5) == ["a b c d", "x" * 159 + "…"]
    assert ai_responder.avoid_note([]) == ""
    note = ai_responder.avoid_note(["so what time love?"])
    assert note.startswith(ai_responder.AVOID_NOTE) and note.endswith("\n- so what time love?")


@pytest.mark.asyncio
async def test_the_model_reads_the_lines_after_the_chat_and_before_the_news(monkeypatch):
    seen: dict = {}

    async def fake_complete(*, api_key, messages, ai_config, client=None):
        seen["messages"] = messages
        return "ok"

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    await ai_responder.generate_reply(api_key="k", history=chat("> hi"), persona={}, ai_config={},
                                      booking_note="NEWS TO PASS ON: confirmed", avoid_lines=["so what time love?"])
    closing = seen["messages"][-1]
    assert closing["role"] == "system"
    assert closing["content"].index("so what time love?") < closing["content"].index("NEWS TO PASS ON")
    assert "so what time love?" not in seen["messages"][0]["content"]

    await ai_responder.generate_reply(api_key="k", history=chat("> hi"), persona={}, ai_config={})
    assert ai_responder.AVOID_NOTE not in str(seen["messages"])


# ------------------------------------------------------------- the runtime

@pytest.fixture
def outbox(app, monkeypatch):
    sent: list[str] = []

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.append(text)
        return type("Sent", (), {"id": 1000 + len(sent)})()

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    app.config["auto_send"] = True
    return sent


@pytest.fixture
def model(app, monkeypatch):
    """The model's replies in order (the last one again when they run
    out), and what each call was given."""
    script: dict = {"replies": ["ok"], "calls": []}

    async def no_sleep(_s):
        return None

    async def fake_generate_reply(**kwargs):
        script["calls"].append(kwargs)
        replies = script["replies"]
        return replies[min(len(script["calls"]), len(replies)) - 1]

    async def nothing(*a, **k):
        return None

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(session_runtime.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_generate_reply)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "deepseek_key", "k")
    return script


@pytest.fixture
def notes(app, monkeypatch):
    out: dict = {"notes": [], "queued": []}

    async def post_note(chat_id, text):
        out["notes"].append(text)

    async def queue(chat_id, reason, detail="", message_id=None):
        out["queued"].append(reason)

    monkeypatch.setattr(app, "post_note", post_note)
    monkeypatch.setattr(app, "queue_unanswered", queue)
    return out


_IDS = itertools.count(1)


async def play(db, chat_id: int, *lines: str) -> None:
    await db.upsert_conversation(chat_id, "J", None, False, 1)
    for line in lines:
        if line.startswith("> "):
            await db.record_message(chat_id, DIR_IN, STATUS_RECEIVED, line[2:], telegram_id=next(_IDS))
        else:
            await db.record_message(chat_id, DIR_OUT, STATUS_SENT, line, llm_model="m")


@pytest.mark.asyncio
async def test_a_fourth_time_the_same_message_gets_no_reply(app, db, model, outbox, notes):
    await play(db, 7, *LOOP, "> send video")

    await app.draft_worker(7)

    assert outbox == [] and model["calls"] == []
    assert notes["notes"] == [f"No reply: {session_runtime.REPEAT_SKIP}."]
    # In the unanswered queue, so the operator sees the bot stopped.
    assert notes["queued"] == [unanswered.SKIPPED]


@pytest.mark.asyncio
async def test_im_here_four_times_is_still_answered(app, db, model, outbox, notes):
    await play(db, 7, "> I'm here", "one sec love", "> I'm here", "coming love", "> I'm here", "1 min babe",
               "> I'm here")

    await app.draft_worker(7)

    assert len(model["calls"]) == 1


@pytest.mark.asyncio
async def test_off_the_same_message_is_answered_again(app, db, model, outbox, notes):
    app.config["replies"]["max_same_answers"] = 0
    model["replies"] = ["you'll see me in person love"]
    await play(db, 7, *LOOP, "> send video")

    await app.draft_worker(7)

    assert outbox == ["you'll see me in person love"]


@pytest.mark.asyncio
async def test_his_time_is_answered_even_with_the_no_reply_instruction(app, db, model, outbox, notes):
    app.config["replies"]["no_reply_instruction"] = "He is a time-waster."
    await play(db, 7, "> hi", "hi love", "> maybe later")
    await app.draft_worker(7)
    assert model["calls"][-1]["no_reply_instruction"] == "He is a time-waster."

    await play(db, 7, "> ok I come at 22")
    await app.draft_worker(7)
    assert model["calls"][-1]["no_reply_instruction"] == ""


@pytest.mark.asyncio
@pytest.mark.repeats
async def test_a_line_the_chat_already_had_is_written_again(app, db, model, outbox, notes):
    model["replies"] = ["yes I can love\nwhat time do you want to come?", "yes I can love\nso when are you coming?"]
    await play(db, 7, "> hi", "hi love 😘 what time do you want to come?", "> domination?")

    await app.draft_worker(7)

    assert len(model["calls"]) == 2
    assert model["calls"][1]["avoid_lines"] == ["what time do you want to come?"]
    # Written again without the examples: new words must not be an example's.
    assert model["calls"][0]["examples_note"] == "" and "examples_note" not in model["calls"][1]
    assert outbox == ["yes I can love", "so when are you coming?"]


@pytest.mark.asyncio
@pytest.mark.repeats
@pytest.mark.variation
async def test_a_variation_never_swaps_a_line_back_into_a_repeat(app, db, model, outbox, notes):
    """The phrase list may turn "so when are you coming" into the line the
    chat already had: that variation is rolled again, or the model's words go."""
    app.config["variation"] = tenant_config.resolve(client={"variation": {
        "phrases": ["so when are you coming | what time do you want to come"], "emoji_drop": 0.0,
        "emoji_swap": 0.0, "lowercase_start": 0.0, "drop_end_punctuation": 0.0, "merge_messages": 0.0,
    }}).as_dict()["variation"]
    model["replies"] = ["so when are you coming love?"]
    for chat_id in range(20, 32):
        await play(db, chat_id, "> hi", "hi love what time do you want to come love?", "> domination?")
        await app.draft_worker(chat_id)
    assert len(outbox) == 12
    assert all("what time do you want to come" not in text for text in outbox)


@pytest.mark.asyncio
@pytest.mark.repeats
async def test_lines_sent_to_many_other_chats_are_shown_first(app, db, model, outbox, notes):
    for other in (11, 12, 13):
        await play(db, other, "> hi", "hi love what time do you want to come?", RATES)
    model["replies"] = ["hey babe 😘 when would you like to come?"]
    await play(db, 7, "> hi")

    await app.draft_worker(7)

    # Shown before it wrote; the rates (several lines) are not a line.
    assert model["calls"][0]["avoid_lines"] == ["hi love what time do you want to come?"]
    assert len(model["calls"]) == 1
    assert outbox == ["hey babe 😘 when would you like to come?"]


@pytest.mark.asyncio
@pytest.mark.repeats
async def test_an_overused_line_written_anyway_is_written_again_once(app, db, model, outbox, notes):
    for other in (11, 12, 13):
        await play(db, other, "> hi", "hi love what time do you want to come?")
    model["replies"] = ["Hi love what time do you want to come?"]
    await play(db, 7, "> hi")

    await app.draft_worker(7)

    # Once more, and whatever the second try says goes out.
    assert len(model["calls"]) == 2
    assert outbox == ["Hi love what time do you want to come?"]


@pytest.mark.asyncio
@pytest.mark.repeats
async def test_lines_in_two_chats_only_or_typed_by_hand_are_not_overused(app, db, model, outbox, notes):
    for other in (11, 12):
        await play(db, other, "> hi", "hi love what time do you want to come?")
    await db.upsert_conversation(13, "K", None, False, 1)
    await db.record_message(13, DIR_OUT, STATUS_SENT, "hi love what time do you want to come?")
    await play(db, 7, "> hi")

    await app.draft_worker(7)

    assert model["calls"][0]["avoid_lines"] == []


@pytest.mark.asyncio
@pytest.mark.repeats
async def test_the_address_never_counts_as_a_line(app, db, model, outbox, notes):
    app.config["booking"]["address"] = "Vene street 11, Tallinn Old Town"
    for other in (11, 12, 13):
        await play(db, other, "> where are you", "Vene street 11, Tallinn Old Town")
    await play(db, 7, "> where are you")

    await app.draft_worker(7)

    assert model["calls"][0]["avoid_lines"] == []


@pytest.mark.asyncio
@pytest.mark.repeats
async def test_with_the_guard_off_nothing_is_written_twice(app, db, model, outbox, notes):
    app.config["variation"]["avoid_repeats"] = False
    for other in (11, 12, 13):
        await play(db, other, "> hi", "hi love what time do you want to come?")
    model["replies"] = ["hi love what time do you want to come?"]
    await play(db, 7, "> hi")

    await app.draft_worker(7)

    assert len(model["calls"]) == 1 and model["calls"][0]["avoid_lines"] == []
