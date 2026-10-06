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


def test_the_street_and_block_markers_always_fail():
    assert replay.check_reply("Ganu iela 4 love", {"customer": "address?"}, None)
    assert replay.check_reply("[block]\nrates\n[/block]", {"customer": "hi"}, None)


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
