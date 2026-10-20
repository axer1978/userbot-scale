"""The replay checker (replay.py): the scenario files load, and the checks
fail what they should and nothing else. The model itself is not called."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import replay


@pytest.fixture(scope="module")
def maria():
    return replay.load(replay.SCENARIO_DIR / "maria.json")


def test_the_scenario_files_are_valid(maria):
    assert len(maria["scenarios"]) >= 100
    assert set(maria["every_chat_sends"]) == {"rates message", "extras message"}


def test_a_broken_scenario_is_refused():
    problems = replay.validate({"scenarios": [
        {"id": "a", "steps": [{"bot": "hi"}]},
        {"id": "a", "steps": [{"customer": "x", "booking": "maybe", "intent": "dance"}]},
    ]})
    assert "a: nothing for the bot to answer" in problems
    assert "duplicate id a" in problems
    assert any("unknown booking state" in p for p in problems)
    assert any("unknown intent" in p for p in problems)


def test_the_fixed_messages_must_come_once_whole_and_alone(maria):
    must = maria["every_chat_sends"]
    rates, extras = must["rates message"], must["extras message"]
    assert replay.check_chat(["hi love", rates + "  ", extras], must) == []
    assert replay.check_chat(["hi", rates], must) == ["never sent the extras message"]
    assert replay.check_chat([rates, extras + "\nwhat time?"], must) == [
        "the extras message was cut short, changed or merged with other text"]
    assert replay.check_chat([rates, extras, rates], must) == ["sent the rates message 2 times"]


def test_agreeing_to_a_time_fails_until_the_owner_confirmed():
    step = {"customer": "22:00?"}
    assert replay.check_reply("yes love, text me 15 mins before to come", step, "requested")
    assert replay.check_reply("yes love, text me 15 mins before to come", step, "confirmed_news") == []
    assert replay.check_reply("let me check love, one sec", step, "requested") == []


def test_block_markers_and_an_early_door_code_fail():
    assert replay.check_reply("[block]\nrates\n[/block]", {"customer": "hi"}, None)
    assert replay.check_reply("code 1234 love", {"customer": "code?"}, "confirmed")
    assert replay.check_reply("the gate code is 1234 love", {"customer": "gate?"}, "way_in_sent") == []
    assert replay.check_reply("here love\nganu street 4", {"customer": "where?"}, None) == []


def test_meeting_them_at_the_door_fails_they_come_up():
    step = {"customer": "here"}
    assert replay.check_reply("perfect, one sec, coming", step, "way_in_sent")
    assert replay.check_reply("I'll come down love", step, "way_in_sent")
    assert replay.check_reply("come up love, see you upstairs 😘", step, "way_in_sent") == []


def test_an_arrival_step_has_nothing_to_check_the_way_in_is_the_answer():
    def problems(step):
        return replay.validate({"scenarios": [{"id": "a", "steps": [{"customer": "hi"}, step]}]})
    assert problems({"customer": "I'm here", "booking": "arrived"}) == []
    assert problems({"customer": "I'm here", "booking": "arrived", "expect": {"has_any": ["sec"]}})
    assert problems({"event": "confirmed", "booking": "arrived"})


def test_the_address_tag_is_offered_and_the_address_follows_only_a_confirmation():
    now = datetime(2026, 10, 5, 19, 20, tzinfo=timezone.utc)
    assert "[address]" in replay.booking_note(None, now, "Europe/Riga", address_set=True)
    assert "[address]" not in replay.booking_note(None, now, "Europe/Riga")
    assert "address is sent" in replay.booking_note("confirmed_news", now, "Europe/Riga", address_follows=True)
    assert "address is sent" not in replay.booking_note("confirmed_news", now, "Europe/Riga")
    assert "address is sent" not in replay.booking_note("requested", now, "Europe/Riga", address_follows=True)
    assert "already sent the entry instructions" in replay.booking_note("way_in_sent", now, "Europe/Riga")


def test_the_outcall_taxi_chats_load_with_the_others():
    data = replay.load_all(["maria", "outcall-taxi"])
    taxi = [s for s in data["scenarios"] if s.get("outcall")]
    assert len(taxi) >= 10
    # They start mid-flow: no fixed messages or first-reply address are due.
    assert {s["category"] for s in taxi} <= set(data["except_categories"])
    assert {"outcall_ready_news", "outcall_link", "outcall_in_taxi_news", "outcall_in_taxi"} <= {
        step.get("booking") for s in taxi for step in s["steps"]}


def test_the_taxi_steps_are_told_to_the_reply_as_in_the_runtime():
    now = datetime(2026, 10, 5, 19, 20, tzinfo=timezone.utc)

    def note(state, **kw):
        return replay.booking_note(state, now, "Europe/Riga", address_set=True, **kw)

    confirmed = note("confirmed_news", outcall=True)
    assert "outcall" in confirmed and "CONFIRMED" in confirmed and "Do not ask them to order" in confirmed
    # Without the outcall (or with the taxi steps off) it is the plain confirmation.
    assert "outcall" not in note("confirmed_news")
    assert "Do not ask them to order" not in note("confirmed_news", outcall=True, outcall_taxi=False)
    assert "only then do they order the taxi" in note("confirmed", outcall=True)

    ready = note("outcall_ready_news")
    assert "order a taxi to pick you up now" in ready and "Your address, for the taxi, is sent" in ready
    assert "send you its link" in note("outcall_ready")
    link = note("outcall_link")
    assert "taxi link they sent has reached you" in link and "about to get in" in link
    assert "you are in the taxi now" in note("outcall_in_taxi_news")
    assert "ask for what is missing" in note("outcall_in_taxi")
    # Confirmed: agreeing words are fine from here on.
    assert replay.check_reply("see you soon love", {"customer": "ok"}, "outcall_in_taxi") == []


def test_what_she_never_says_fails_every_reply():
    never = ("let me check", "agency")
    assert replay.check_reply("Let me check love, one sec", {"customer": "22:00?"}, "requested", never) == [
        "says 'let me check'"]
    assert replay.check_reply("one sec love", {"customer": "22:00?"}, "requested", never) == []


def test_short_check_words_match_whole_words_only():
    step = {"customer": "are you a bot?", "expect": {"has_any": ["ai", "bot"]}}
    assert replay.check_reply("wait for me again, both of us", step, None) == ["none of ['ai', 'bot']"]
    assert replay.check_reply("I'm Nikol's automated assistant, an AI", step, None) == []


def test_claiming_to_be_a_person_fails():
    step = {"customer": "are you real? how much?"}
    for text in ("yes love i'm real 😘 1 hour 200", "I'm not a bot love", "it's me typing love", "im human 😘"):
        assert replay.check_reply(text, step, None) == ["claims to be a real person, or not a bot"], text
    assert replay.check_reply("1 hour 200 love, you'll see me in person 😘 really", step, None) == []


def test_photos_are_checked_both_ways():
    asks = {"customer": "pics?", "photos": True}
    assert replay.check_reply("here love", asks, None) == ["sent no photos"]
    assert replay.check_reply("", asks, None, files=2) == []
    must_not = {"customer": "a photo of the door?", "photos": False}
    assert replay.check_reply("you'll see when you're here love", must_not, None, files=1) == ["sent 1 photos"]
    assert replay.validate({"scenarios": [{"id": "a", "steps": [{"customer": "x", "photos": "yes"}]}]}) == [
        "a: photos is true or false"]


def test_expectations_are_checked_in_any_case():
    step = {"customer": "kiss?", "expect": {"has_all": ["Kissing"], "has_any": ["50", "fifty"], "lacks": ["SORRY"]}}
    assert replay.check_reply("kissing is +50 love", step, None) == []
    assert replay.check_reply("sorry love, I don't do that", step, None) == [
        "missing 'Kissing'", "none of ['50', 'fifty']", "contains 'SORRY'"]


def test_now_must_be_read_as_a_time_ahead():
    now = datetime(2026, 10, 5, 19, 20, 40, tzinfo=timezone.utc)
    assert replay.check_intent("asap", {"intent": "book", "asap": True}, "Europe/Riga", now) is None
    assert replay.check_intent("asap", {"intent": "book", "start": "2026-10-06T01:00"}, "Europe/Riga", now)
    assert replay.check_intent("cancel", {"intent": "book", "start": "x"}, "Europe/Riga", now)
    assert replay.check_intent("none", None, "Europe/Riga", now) is None
