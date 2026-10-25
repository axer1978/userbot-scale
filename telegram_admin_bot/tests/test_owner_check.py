"""The checker for the booker's answers in her own words (owner_check.py):
the cases load and build as the runtime would give them to the reader (her
chat with its times, the bot's questions written by the real formatter), the
expectations and checks pass what they should, and a case is judged on what
the runtime would do with her message: the commands it makes of the reading,
weighed against what she typed (bookings.plan_owner_answer), and a strict
command that answers nothing carried out with no model call. The model itself
is not called."""

from __future__ import annotations

import json
from datetime import timedelta, timezone

import pytest

import ai_responder
import booking_flow
import bookings
import owner_check

TZ = "Europe/Riga"


@pytest.fixture(scope="module")
def data():
    return owner_check.load(owner_check.CASES)


def case(data, case_id):
    return next(c for c in data["cases"] if c["id"] == case_id)


def given(data, case_id):
    return owner_check.build(data, case(data, case_id), TZ, "Pamella", True)


def said(g):
    return [m["content"] for m in g["history"]]


def test_every_case_builds_her_chat_with_the_requests_in_it(data):
    assert len(data["cases"]) >= 156
    assert {c["id"].split("-")[0] for c in data["cases"]} == {"en", "ru", "lv", "es"}
    for c in data["cases"]:
        g = owner_check.build(data, c, TZ, "Pamella", True)
        assert g["history"][-1] == {"role": "user", "content": c["latest"], "at": g["now"]}
        assert all(set(m) == {"role", "content", "at"} for m in g["history"])
        assert all(f"#{b['number']} " in g["state_note"] for b in g["rows"])
        # Every line has its time, oldest first, all before her latest.
        times = [m["at"] for m in g["history"]]
        assert times == sorted(set(times)) and all(t.tzinfo is not None for t in times)
        assert g["now"] - times[0] <= timedelta(hours=3)
    one = given(data, "en-yes-confirm")
    assert "Girl: Pamella\nExtras: domination +50\nWhen: Tue 20 Oct 2026, 13:00–14:00" in one["history"][0]["content"]
    assert one["history"][0]["at"] == one["now"] - timedelta(minutes=20)
    taxi = given(data, "en-outcall-in-taxi")
    assert "YES 7" in said(taxi) and "READY 7" in said(taxi) and any(owner_check.TAXI_LINK in m for m in said(taxi))
    assert "outcall: taxi link sent" in taxi["state_note"]


def test_the_times_go_to_the_reader_and_to_the_steps_of_a_booking(data):
    g = given(data, "en-in-taxi-10-min")
    at = {m["content"]: m["at"] for m in g["history"]}
    b, now = g["rows"][0], g["now"]
    # The request twenty minutes back; what followed it up to a minute
    # before her latest message, and each step at the time of its line.
    assert b["created_at"] == now - timedelta(minutes=20)
    assert (b["ready_at"], b["in_taxi_at"]) == (at["READY 7"], at["IN 7"]) and b["taxi_link_at"] < b["in_taxi_at"]
    assert g["history"][-2]["at"] == now - timedelta(minutes=1)
    assert "outcall: in the taxi" in g["state_note"]
    transcript = ai_responder.render_transcript(g["history"], bookings.tzinfo_for(TZ)).splitlines()
    assert transcript[0].startswith("[Tue 20 Oct 12:20] me: 📅 Booking request #7")
    assert transcript[-3:] == ["[Tue 20 Oct 12:38] them: IN 7", transcript[-2], "[Tue 20 Oct 12:40] them: tell him 10 min"]


def test_what_she_said_before_comes_before_the_requests(data):
    g = given(data, "en-context-like-i-said")
    first, request = g["history"][:2]
    assert first == {"role": "user", "content": "today only after 20:00 pls", "at": g["now"] - timedelta(hours=3)}
    assert request["content"].startswith("📅 Booking request #8") and request["at"] > first["at"]
    transcript = ai_responder.render_transcript(g["history"], bookings.tzinfo_for(TZ))
    assert transcript.startswith("[Tue 20 Oct 09:40] them: today only after 20:00 pls\n[Tue 20 Oct 12:20] me: 📅")


def test_her_chat_has_what_the_runtime_sent_her(data):
    khan = given(data, "en-context-tell-him-cash")
    assert said(khan)[2] == "yes for Khan"
    assert said(khan)[3] == "#3 confirmed ✅ for Tue 20 Oct 2026, 21:00–22:00 (1 hour) — Khan will be told."
    assert "#3 Khan: confirmed" in khan["state_note"] and "#4 Marco: the customer asks" in khan["state_note"]
    which = given(data, "en-context-which-one")
    assert said(which)[-3:-1] == ["yes", bookings.format_which_one(which["rows"])]
    assert which["asked"] == {3: False, 4: False}

    waiting = given(data, "en-proof-did-he-send")
    assert waiting["proof"] and "is asked for a photo that shows he's at the place" in said(waiting)[2]
    assert "outcall: waiting for his photo of the place (READY 7 skips it)" in waiting["state_note"]
    came = given(data, "en-proof-ok-fine")
    assert said(came)[-3].startswith("📷 #7 Alex sent his photo of the place") and \
        said(came)[-2] == "[photo from booking #7]" and "READY 7" not in said(came)
    assert came["rows"][0]["proof_at"] == came["rows"][0]["ready_at"] and "his photo came" in came["state_note"]
    assert not given(data, "en-outcall-ready")["proof"]

    asked = given(data, "en-asked-2-hours-then")
    line = "#6 Juris: I'll ask him: does he want 2 hours? His answer will come here."
    assert said(asked)[-3:-1] == [line, "💬 #6 Juris:\nyes 2 hours please"]
    assert asked["rows"][0]["booker_asked_at"] == asked["history"][-3]["at"]
    assert "they asked him something" in asked["state_note"]
    here = given(data, "en-arrived-wait-5")
    assert said(here)[-2].endswith("he's here — go and meet him.\nI'm here, at the door")
    assert "he says he has arrived" in here["state_note"]
    extras = given(data, "en-one-hour-no-anal")
    assert "When: Tue 20 Oct 2026, 13:00–15:00" in said(extras)[0] and "(2 hours)" in extras["state_note"]


def test_a_broken_case_file_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"now": "2026-10-20T12:40", "bookings": {
        "one": [{"number": 8, "customer": "Khan", "state": "pending", "start": "2026-10-20T13:00",
                 "colour": "red", "proof": "maybe"}],
    }, "cases": [
        {"id": "a", "bookings": "one", "latest": "yes", "expect": "maybe 8"},
        {"id": "a", "bookings": "none", "latest": "yes", "expect": "yes"},
        {"id": "b", "bookings": "one", "latest": "yes", "expect": ["yes 8 + none", "yes 8 13:00", "yes 8 +"],
         "tell": [["cash", 5]], "tel": True, "minutes": "60", "private": "show", "reply_to": 9,
         "chat": [{"me": "hi", "them": "hi"}]},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        owner_check.load(path)
    for problem in ("a: not an expectation: 'maybe 8'", "a: id used twice", "a: no booking set 'none'",
                    "set 'one' #8: unknown key 'colour'", "set 'one' #8: proof is", "b: unknown key 'tel'",
                    "b: a bare none", "b: only propose has a time", "b: not an expectation: ''", "b: tell is",
                    "b: minutes is", "b: private is", "b: reply_to #9", "b: chat is"):
        assert problem in str(err.value)


@pytest.mark.parametrize("got, expect, ok", [
    ([("propose", 8, "13:10")], "propose 8 13:10", True),
    ([("propose", 8, "13:15")], "propose 8 13:10", False),
    ([("propose", 8, "13:10")], "propose 8", True),
    ([("propose", 8, "2026-10-21 14:00")], "propose 8 2026-10-21 14:00", True),
    ([("yes", None, None)], "yes -", True),
    ([("yes", 10, None)], "yes -", False),
    ([("yes", 10, None)], "yes 8", False),
    ([("unclear", 8, None)], "unclear", True),
    ([], "none", True),
    ([], "yes", False),
    ([("none", 8, None)], "none", False),
    ([("none", 8, None)], "none 8", True),
    # Several commands: all of them, in any order, and nothing else.
    ([("yes", 3, None), ("no", 4, None)], "yes 3 + no 4", True),
    ([("no", 4, None), ("yes", 3, None)], "yes 3 + no 4", True),
    ([("yes", 3, None), ("no", 4, None), ("none", 5, None)], "yes 3 + no 4", False),
    ([("yes", 3, None)], "yes 3 + no 4", False),
    ([("yes", 3, None), ("yes", 3, None)], "yes 3 + yes 4", False),
    ([("yes", 3, None), ("yes", 4, None)], "yes + yes 3", True),
])
def test_expectations(got, expect, ok):
    assert owner_check.matches(got, expect) is ok


def cmd(kind="yes", number=8, **fields):
    return bookings.OwnerCommand(kind, number, **fields)


@pytest.mark.parametrize("checks, cmds, standing, ok", [
    ({"tell": True}, [cmd(tell="bring cash")], "", True),
    ({"tell": True}, [cmd()], "", False),
    ({"tell": False}, [cmd(ask="cash?")], "", True),
    ({"tell": False}, [cmd(tell="bring cash")], "", False),
    # Every group must be there; a list in a group: any of its words will do.
    ({"tell": ["10", ["late", "delay"]]}, [cmd(tell="I'm 10 min LATE")], "", True),
    ({"tell": ["10", ["late", "delay"]]}, [cmd(tell="10 min")], "", False),
    ({"tell": [["efectivo", "cash"]]}, [cmd("none", 3, tell="que traiga efectivo")], "", True),
    ({"tell": ["cash"]}, [cmd("yes", 3), cmd("yes", 4, tell="bring cash")], "", True),
    ({"ask": [["photo", "pic"]]}, [cmd("none", ask="send a pic first?")], "", True),
    ({"ask": [["photo", "pic"]]}, [cmd("none", tell="send a pic first")], "", False),
    ({"words": [["photo", "pic"]]}, [cmd("none", tell="send a better photo")], "", True),
    ({"words": [["photo", "pic"]]}, [cmd("none", ask="a better photo?")], "", True),
    ({"words": False}, [cmd("none", for_me=True)], "", True),
    ({"words": False}, [cmd("none", for_me=True, ask="how long?")], "", False),
    ({"minutes": 60}, [cmd(minutes=60)], "", True),
    ({"minutes": 60}, [cmd(minutes=120)], "", False),
    ({"minutes": 60}, [cmd()], "", False),
    ({"minutes": False}, [cmd()], "", True),
    ({"minutes": False}, [cmd("none", minutes=120)], "", False),
    ({"extras_no": ["anal"]}, [cmd(extras_no=("Anal sex",))], "", True),
    ({"extras_no": ["anal"]}, [cmd(extras_ok=("anal",))], "", False),
    ({"extras_ok": [["domin", "домин"]]}, [cmd(extras_ok=("доминация",))], "", True),
    ({"extras_ok": ["domination", "anal"]}, [cmd(extras_ok=("domination",))], "", False),
    ({"extras_ok": False}, [cmd()], "", True),
    ({"extras_ok": False}, [cmd(extras_ok=("domination",))], "", False),
    ({"for_me": True}, [cmd("none", for_me=True)], "", True),
    ({"for_me": True}, [cmd("none", tell="x")], "", False),
    ({"for_me": False}, [cmd("none", for_me=True)], "", False),
    ({"private": ["show"]}, [cmd(tell="come on time")], "", True),
    ({"private": ["show"]}, [cmd(tell="he didn't SHOW last time")], "", False),
    ({"private": ["rude"]}, [cmd(ask="why were you rude?")], "", False),
    ({"standing": True}, [], "cash only", True),
    ({"standing": True}, [], "", False),
    ({"standing": False}, [], "cash only", False),
    ({"standing": [["efectivo", "cash"]]}, [], "solo efectivo", True),
    ({"standing": ["cash"]}, [cmd(tell="cash")], "", False),
])
def test_checks(checks, cmds, standing, ok):
    assert (owner_check.problems(checks, cmds, bookings.OwnerReading(standing=standing)) == []) is ok


def test_a_check_says_what_is_wrong():
    wrong = owner_check.problems(
        {"tell": [["efectivo", "cash"]], "minutes": 60, "extras_no": ["anal"], "private": ["show"], "for_me": False},
        [cmd(tell="he didn't show", extras_ok=("anal",))], bookings.OwnerReading())
    assert wrong == ['tell: no efectivo|cash in "he didn\'t show"', "extras_no: no anal in []",
                     "minutes: wanted 60, got none", "private: 'show' goes on to him in \"he didn't show\""]


def reading(*answers, standing=None):
    """The reader's JSON as the model would write it, through the real parser."""
    return bookings.parse_owner_answer(json.dumps({"answers": list(answers), "standing": standing}))


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id, found, got, ok", [
    ("en-ready-by-his-time", reading({"action": "ready_at", "in_minutes": 20}), "yes 8", True),
    ("en-give-me-30", reading({"action": "ready_at", "number": 8, "in_minutes": 30}), "propose 8 13:10", True),
    ("en-tomorrow-after-2", reading({"action": "time", "start": "2026-10-21T14:00"}),
     "propose 8 2026-10-21 14:00", True),
    ("en-two-reply-to", reading({"action": "yes"}), "yes 10", True),
    ("en-two-no-telling", reading({"action": "yes", "number": 10}), "yes 10", False),
    ("en-two-no-telling", reading({"action": "yes"}), "unclear - (which: #8, #10)", True),
    ("en-confirmed-yes-again", reading({"action": "yes", "number": 6}), "stays 6", True),
    ("en-outcall-in-taxi", reading({"action": "in_taxi"}), "in_taxi 7", True),
    ("en-chat-how-are-you", reading(), "none", True),
    ("en-chat-how-are-you", reading({"action": "none", "number": 8}), "none", True),
    ("en-yes-bring-cash", reading({"action": "yes", "tell_customer": "bring cash"}),
     'yes 8 (tell "bring cash")', True),
    ("en-yes-bring-cash", reading({"action": "yes"}), "yes 8", False),
    ("en-one-hour-no-anal", reading({"action": "yes", "duration_minutes": 60, "extras_no": ["anal"]}),
     "yes 8 (60 min; no: anal)", True),
    ("en-pair-yes3-no4", reading({"action": "no", "number": 4}, {"action": "yes", "number": 3}), "no 4 + yes 3", True),
    ("en-pair-yes3-no4", reading({"action": "yes", "number": 3}), "yes 3", False),
    # Two different changes for one booking: she is asked.
    ("en-pair-both-fine", reading({"action": "yes", "number": 3}, {"action": "yes", "number": 4},
                                  {"action": "no", "number": 4}), "yes 3 + unclear 4", False),
    ("en-context-which-one", reading({"action": "yes", "number": 3}), "yes 3", True),
    ("en-context-tell-him-cash", reading({"action": "none", "number": 3, "tell_customer": "bring cash"}),
     'none 3 (tell "bring cash")', True),
    # No number: a yes goes to the only request still waiting, Marco's.
    ("en-context-tell-him-cash", reading({"action": "yes", "tell_customer": "bring cash"}),
     'yes 4 (tell "bring cash")', False),
    ("en-context-like-i-said", reading({"action": "time", "start": "2026-10-20T20:00"}), "propose 8 20:00", True),
    ("en-yes-no-show-before", reading({"action": "yes", "tell_customer": "you didn't show last time"}),
     'yes 8 (tell "you didn\'t show last time")', False),
    ("en-for-me-how-long", reading({"action": "none", "for_me": True}), "none 8 (for me)", True),
    ("en-asked-2-hours-then", reading({"action": "yes", "duration_minutes": 120}), "stays 6 (120 min)", True),
    ("en-proof-better-photo", reading({"action": "none", "ask_customer": "can you send a better photo?"}),
     'none 7 (ask "can you send a better photo?")', True),
    ("en-standing-cash-only", reading(standing="cash only for everyone"), 'none; standing "cash only for everyone"',
     True),
    ("en-standing-cash-only", reading({"action": "none", "number": 6, "tell_customer": "cash only"}),
     'none 6 (tell "cash only")', False),
])
async def test_a_case_is_judged_on_what_the_runtime_would_do(data, monkeypatch, case_id, found, got, ok):
    seen = {}

    async def reader(**kw):
        seen.update(kw)
        return found

    monkeypatch.setattr(ai_responder, "read_owner_answer", reader)
    c = case(data, case_id)
    result = await owner_check.run_case(data, c, key="k", tz=TZ, girl="Pamella", outcall_taxi=True,
                                        ai_config={}, http=None)
    assert (result.got, result.ok) == (got, ok)
    assert result.raw == {"answers": [dict(a) for a in found.answers], "standing": found.standing}
    # Called as the runtime calls it: her chat with its times, on the
    # runtime's clock (UTC).
    built = owner_check.build(data, c, TZ, "Pamella", True)
    assert seen["latest"] == c["latest"] and seen["replying_to"] == c.get("reply_to")
    assert seen["history"] == built["history"] and seen["state_note"] == built["state_note"]
    assert seen["now"] == built["now"] and seen["now"].tzinfo == timezone.utc
    assert seen["now"].astimezone(bookings.tzinfo_for(TZ)).strftime("%Y-%m-%d %H:%M") == "2026-10-20 12:40"


@pytest.mark.asyncio
async def test_a_reading_that_cannot_be_used_fails_the_case(data, monkeypatch):
    async def reader(**kw):
        return bookings.parse_owner_answer("sorry, I can't help with that")

    monkeypatch.setattr(ai_responder, "read_owner_answer", reader)
    result = await owner_check.run_case(data, case(data, "en-chat-hi"), key="k", tz=TZ, girl="Pamella",
                                        outcall_taxi=True, ai_config={}, http=None)
    assert (result.got, result.ok, result.raw) == ("model could not be read", False, None)


def test_the_report_shows_what_came_back_next_to_what_was_wanted(data):
    failed = owner_check.Result(case(data, "es-yes-cash"), "yes 8", False, {"answers": [], "standing": ""},
                                ["tell: nothing came back"])
    passed = owner_check.Result(case(data, "en-yes-confirm"), "yes 8", True)
    text = owner_check.report([failed, passed], verbose=False)
    assert text.splitlines()[:3] == ["1/2 answers read right", "  es     0/1", "  en     1/1"]
    assert "\nFAIL  es-yes-cash: 'sí, pero que traiga efectivo'\n  got     yes 8\n" \
           '  wanted  yes 8; tell [["efectivo", "cash"]]\n  wrong   tell: nothing came back\n' \
           '  model   {"answers": [], "standing": ""}' in text
    assert "en-yes-confirm" not in text
    assert "PASS  en-yes-confirm: 'yes i confirm'" in owner_check.report([failed, passed], verbose=True)


# ------------------------------------------- the bot's question, her answer


def built(data, case_id, **over):
    return owner_check.build(data, dict(case(data, case_id), **over), TZ, "Pamella", True)


def test_the_bots_question_is_written_by_the_real_formatter(data):
    g = given(data, "en-answer-which-yes-3")
    held = bookings.OwnerCommand("unclear", tell="bring cash")
    assert said(g)[-3:] == ["yes, tell him to bring cash", bookings.format_which_one(g["rows"], held), "YES 3"]
    assert "With your answer: tell him bring cash" in said(g)[-2]
    # What it asked about, and that it repeats her words: kept by the
    # checker, never in her chat.
    assert g["asked"] == {3: True, 4: True}
    assert all(set(m) == {"role", "content", "at"} for m in g["history"])
    # About one booking: format_question, in its three shapes.
    g = built(data, "en-yes-confirm", chat=[{"them": "ok 2 hours"}, {"me_ask": 8, "held": {"minutes": 120}}])
    b = g["rows"][0]
    assert said(g)[-2] == bookings.format_question(bookings.OwnerCommand("unclear", 8, minutes=120), b)
    assert g["asked"] == {8: True}
    g = built(data, "en-yes-confirm", chat=[{"me_ask": 8, "missing": "time"}])
    assert said(g)[-2] == bookings.format_question(bookings.OwnerCommand("unclear", 8, missing="time"), b)
    assert g["asked"] == {8: False}
    g = built(data, "en-yes-confirm", chat=[{"me_ask": 8, "question": "Is that a yes for Khan?"}])
    assert said(g)[-2] == "#8: Is that a yes for Khan?"


def test_a_question_written_out_is_known_by_the_formatters_words(data):
    rows = given(data, "en-pair-yes3-no4")["rows"]
    for text, asked in [
        (bookings.format_which_one(rows), {3: False, 4: False}),
        (bookings.format_which_one(rows, bookings.OwnerCommand("unclear", minutes=120)),
         {3: True, 4: True}),
        (bookings.format_which_one(rows[1:]), {4: False}),
        (bookings.format_question(bookings.OwnerCommand("unclear", 4), rows[1]), {4: False}),
        (bookings.format_question(bookings.OwnerCommand("unclear", 3, missing="time"), rows[0]),
         {3: False}),
        # Lines of the bot's that ask her nothing.
        (bookings.format_acknowledgement(rows[0], "confirm"), {}),
        (bookings.format_passed_on(rows[0], "yes or no?"), {}),
        (bookings.format_words_ack(rows[0], bookings.OwnerCommand("none", 3, ask="2 hours?")), {}),
    ]:
        assert built(data, "en-pair-yes3-no4", chat=[{"them": "yes"}, {"me": text}])["asked"] == asked, text


WHICH = [{"them": "yes, tell him to bring cash"}, {"me_which": [3, 4], "held": {"tell": "bring cash"}}]


@pytest.mark.parametrize("chat, latest, over, answers", [
    # The question is about #3 and #4: YES 3 / NO 4 answers it, and so does
    # a YES / NO without a number.
    (WHICH, "YES 3", {}, True),
    (WHICH, "NO 4", {}, True),
    (WHICH, "ok", {}, True),
    # Another booking, not a YES / NO, or not a command at all.
    (WHICH, "YES 8", {}, False),
    (WHICH, "LIST", {}, False),
    (WHICH, "3", {}, False),
    # Small talk after it keeps it open, as in the runtime ...
    (WHICH + [{"them": "hmm"}], "YES 3", {}, True),
    # ... a command of hers on one of its bookings closes it, all of it.
    (WHICH + [{"them": "YES 4"}], "YES 3", {}, False),
    # His words passed on to her since: still open ...
    (WHICH + [{"me": "💬 #3 Khan:\nhello?"}], "YES 3", {}, True),
    # ... for half an hour.
    (WHICH + [{"me": f"line {i}"} for i in range(31)], "YES 3", {}, False),
    # The bot asked her nothing.
    ([{"them": "yes 3"}], "NO 4", {}, False),
    # The case says it outright.
    ([], "NO 4", {"answering": True}, True),
    (WHICH, "NO 4", {"answering": False}, False),
])
def test_a_strict_command_answers_the_bots_question_as_the_runtime_counts_it(data, chat, latest, over, answers):
    c = dict(case(data, "en-pair-yes3-no4"), chat=chat, latest=latest, **over)
    g = owner_check.build(data, c, TZ, "Pamella", True)
    assert owner_check.answering(c, g["asked"], owner_check.typed_command(c, g, True)) is answers


def test_the_question_stays_open_as_long_as_in_the_runtime():
    assert owner_check.ANSWER_WINDOW == booking_flow.ANSWER_WINDOW


async def run(data, monkeypatch, c, found, *, outcall_taxi=True):
    calls = []

    async def reader(**kw):
        calls.append(kw)
        return found

    monkeypatch.setattr(ai_responder, "read_owner_answer", reader)
    result = await owner_check.run_case(data, c, key="k", tz=TZ, girl="Pamella", outcall_taxi=outcall_taxi,
                                        ai_config={}, http=None)
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id, latest, got", [
    ("en-yes-confirm", "YES 8", "yes 8"),
    ("en-yes-confirm", "8 15:30", "propose 8 15:30"),
    # No number: the only request waiting, the one she quotes, the only
    # outcall at that step; else she is asked which.
    ("en-yes-confirm", "NO", "no 8"),
    ("en-two-reply-to", "YES", "yes 10"),
    ("en-pair-yes3-no4", "YES", "yes -"),
    ("en-outcall-ready", "READY", "ready 7"),
    ("en-list", "LIST", "list"),
    # A question already answered (her YES 3 closed it) is no reason to read.
    ("en-answer-which-yes-3", "YES 4", "yes 4"),
])
async def test_a_strict_command_that_answers_nothing_costs_no_model_call(data, monkeypatch, case_id, latest, got):
    c = dict(case(data, case_id), latest=latest)
    if case_id == "en-answer-which-yes-3":
        c["chat"] = [*c["chat"], {"them": "YES 3"}]
    result, calls = await run(data, monkeypatch, c, None)
    assert (result.got, result.raw, calls) == (got + "; strict, no model call", None, [])


@pytest.mark.asyncio
async def test_ready_while_the_taxi_steps_are_off_is_read(data, monkeypatch):
    c = dict(case(data, "en-outcall-ready"), latest="READY 7")
    result, calls = await run(data, monkeypatch, c, reading({"action": "none", "number": 7, "tell_customer": "ready"}),
                              outcall_taxi=False)
    assert len(calls) == 1 and result.got == 'none 7 (tell "ready")'


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id, found, got, ok", [
    # A strict YES 3 answering "which one?": read with her message before
    # it, and her words go with it when the reading agrees.
    ("en-answer-which-yes-3", reading({"action": "yes", "number": 3, "tell_customer": "bring cash"}),
     'yes 3 (tell "bring cash")', True),
    # Nothing that agrees: as she typed it, and she is told her words were lost.
    ("en-answer-which-yes-3", reading({"action": "yes", "number": 4, "tell_customer": "bring cash"}),
     'yes 3; her earlier words lost; read as yes 4 (tell "bring cash")', False),
    ("en-answer-which-yes-3", reading(), "yes 3; her earlier words lost; read as none", False),
    # NO 4 never turns into a yes for #3: nothing is confirmed.
    ("en-answer-which-no-4", reading({"action": "yes", "number": 3, "tell_customer": "bring cash"}),
     'no 4; her earlier words lost; read as yes 3 (tell "bring cash")', True),
    ("en-answer-which-no-4", reading({"action": "no", "number": 4, "tell_customer": "bring cash"}),
     'no 4 (tell "bring cash")', False),
    # Not a command: every command of the reading.
    ("en-answer-which-3", reading({"action": "yes", "number": 3, "tell_customer": "bring cash"}),
     'yes 3 (tell "bring cash")', True),
    # A loose YES 8 the reading turns into a no, or has nothing for: she is
    # asked instead.
    ("en-yes-8-2-hours", reading({"action": "yes", "number": 8, "duration_minutes": 120}), "yes 8 (120 min)", True),
    ("en-yes-8-2-hours", reading({"action": "no", "number": 8}), "unclear 8; she is asked instead; read as no 8",
     False),
    ("en-yes-8-2-hours", reading(), "unclear 8; she is asked instead; read as none", False),
    # ... her "no 4", which she named, still goes.
    ("en-pair-yes3-no4", reading({"action": "no", "number": 4}), "no 4 + unclear 3; she is asked instead; read as no 4",
     False),
    # Asked instead: nothing is carried out but an instruction for everyone.
    ("en-yes-8-2-hours", reading(standing="cash only"),
     'unclear 8; standing "cash only"; she is asked instead; read as none; standing "cash only"', False),
    # A booking that is not open and that she did not name is the model's
    # guess: she is asked which.
    ("en-yes-confirm", reading({"action": "yes", "number": 12}), "unclear - (which: #8)", False),
    # A loose yes read as about no booking: asked about the one waiting, or
    # which one; no answer at all when none is waiting.
    ("en-yes-confirm", reading(), "unclear 8; she is asked instead; read as none", False),
    ("en-two-no-telling", reading(), "unclear - (which: #8, #10); she is asked instead; read as none", True),
    ("en-proof-ok-fine", reading(), "none", True),
])
async def test_the_reading_is_weighed_against_what_she_typed(data, monkeypatch, case_id, found, got, ok):
    result, calls = await run(data, monkeypatch, case(data, case_id), found)
    assert (result.got, result.ok) == (got, ok)
    assert len(calls) == 1 and result.raw == {"answers": [dict(a) for a in found.answers], "standing": found.standing}


@pytest.mark.asyncio
async def test_a_plain_yes_to_a_booking_already_his_changes_nothing(data, monkeypatch):
    # Asked about #6, already confirmed: her plain YES is read as words for him.
    c = dict(case(data, "en-soon-10-late"), chat=[{"me_ask": 6}], latest="YES")
    result, _ = await run(data, monkeypatch, c, reading({"action": "none", "number": 6, "tell_customer": "bring cash"}))
    assert result.got == 'stays 6 (tell "bring cash"); read as none 6 (tell "bring cash")'


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id, found, ok", [
    # Thanks gets no answer at all (decision c), never a question to her.
    ("en-chat-thanks", reading(), True),
    ("en-chat-thanks", reading({"action": "unclear", "number": 8}), False),
    # Who is he? Answered to her if at all, never put to him.
    ("en-question-who", reading(), True),
    ("en-question-who", reading({"action": "none", "number": 8, "for_me": True}), True),
    ("en-question-who", reading({"action": "none", "number": 8, "ask_customer": "are you nice?"}), False),
    ("en-question-who", reading({"action": "none", "number": 8, "tell_customer": "she asks if you're nice"}), False),
    # Her words on the customer she named, never on the other.
    ("en-pair-both-words-for-marco", reading({"action": "yes", "number": 3},
                                             {"action": "yes", "number": 4, "tell_customer": "bring cash"}), True),
    ("en-pair-both-words-for-marco", reading({"action": "yes", "number": 3, "tell_customer": "bring cash"},
                                             {"action": "yes", "number": 4}), False),
    ("es-pair-both-words-for-4", reading({"action": "yes", "number": 3, "tell_customer": "que traiga efectivo"},
                                         {"action": "yes", "number": 4, "tell_customer": "que traiga efectivo"}),
     False),
    # "Tell the other one": never the one she quotes.
    ("en-pair-quoted-tell-the-other", reading({"action": "yes", "number": 3},
                                              {"action": "none", "tell_customer": "I'm 10 min late"}), True),
    ("en-pair-quoted-tell-the-other", reading({"action": "yes", "number": 3},
                                              {"action": "none", "number": 4, "tell_customer": "I'm 10 min late"}),
     True),
    ("en-pair-quoted-tell-the-other", reading({"action": "yes", "number": 3, "tell_customer": "I'm 10 min late"}),
     False),
    # A yes with another time is never a plain yes.
    ("en-yes-but-at-15", reading({"action": "yes", "number": 8, "start": "2026-10-20T15:00"}), True),
    ("en-yes-but-at-15", reading({"action": "time", "number": 8, "start": "2026-10-20T15:00"}), True),
    ("en-yes-but-at-15", reading({"action": "yes", "number": 8}), False),
    # The length after a YES is not a booking number.
    ("en-yes-2-hours", reading({"action": "yes", "number": 8, "duration_minutes": 120}), True),
    ("en-yes-2-hours", reading({"action": "yes", "number": 2}), False),
    # Ready in 10 on his confirmed booking: he is told, not only her.
    ("en-soon-ready-in-10", reading({"action": "ready_at", "number": 6, "in_minutes": 10,
                                     "tell_customer": "I'll be ready in 10 min"}), True),
    ("en-soon-ready-in-10", reading({"action": "ready_at", "number": 6, "in_minutes": 10}), False),
])
async def test_the_cases_want_what_the_user_decided(data, monkeypatch, case_id, found, ok):
    result, _ = await run(data, monkeypatch, case(data, case_id), found)
    assert result.ok is ok, (result.got, result.wrong)


# --------------------------------------------- checks on one booking each


@pytest.mark.parametrize("checks, cmds, ok", [
    ({"tell": {"4": ["cash"], "3": False}}, [cmd("yes", 3), cmd("yes", 4, tell="bring cash")], True),
    ({"tell": {"4": ["cash"], "3": False}}, [cmd("yes", 3, tell="bring cash"), cmd("yes", 4)], False),
    ({"tell": {"4": ["cash"], "3": False}}, [cmd("yes", 3, tell="cash"), cmd("yes", 4, tell="cash")], False),
    # A booking with no command fails every check but false.
    ({"tell": {"4": ["cash"]}}, [cmd("yes", 3, tell="bring cash")], False),
    ({"tell": {"4": True}}, [cmd("yes", 3, tell="bring cash")], False),
    ({"tell": {"4": False}}, [cmd("yes", 3, tell="bring cash")], True),
    ({"words": {"3": False}}, [cmd("yes", 3), cmd("unclear", None, tell="I'm 10 min late")], True),
    ({"words": {"3": False}}, [cmd("yes", 3, ask="10 min?")], False),
    ({"ask": {"8": [["photo", "pic"]]}}, [cmd("none", 8, ask="send a pic?")], True),
    ({"minutes": {"8": 120}}, [cmd(minutes=120)], True),
    ({"minutes": {"8": 120}}, [cmd(), cmd("yes", 3, minutes=120)], False),
    ({"minutes": {"8": False}}, [cmd(), cmd("yes", 3, minutes=120)], True),
    ({"minutes": {"8": False}}, [cmd(minutes=60)], False),
    ({"for_me": {"8": True}}, [cmd("none", 8, for_me=True)], True),
    ({"for_me": {"8": True}}, [cmd("none", 3, for_me=True), cmd("none", 8, tell="x")], False),
    ({"for_me": {"3": False}}, [cmd("none", 8, for_me=True)], True),
    ({"extras_no": {"8": ["anal"]}}, [cmd(extras_no=("anal",))], True),
    ({"extras_no": {"8": ["anal"]}}, [cmd("yes", 3, extras_no=("anal",)), cmd()], False),
    ({"extras_ok": {"8": False}}, [cmd("yes", 3, extras_ok=("anal",)), cmd()], True),
    # Both ways in one case: the words somewhere, and none on #3.
    ({"tell": ["10"], "words": {"3": False}}, [cmd("yes", 3), cmd("none", 4, tell="10 min late")], True),
    ({"tell": ["10"], "words": {"3": False}}, [cmd("yes", 3, tell="10 min late")], False),
])
def test_checks_per_booking(checks, cmds, ok):
    assert (owner_check.problems(checks, cmds, bookings.OwnerReading()) == []) is ok


def test_a_check_per_booking_says_which_booking():
    wrong = owner_check.problems({"tell": {"4": ["cash"], "3": False}, "minutes": {"5": 60}},
                                 [cmd("yes", 3, tell="bring cash"), cmd("yes", 4)], bookings.OwnerReading())
    assert wrong == ['tell #4: no cash in ""', 'tell #3: wanted nothing, got "bring cash"',
                     "minutes #5: no command for #5"]


@pytest.mark.parametrize("want, lost, wrong", [
    (True, True, []), (False, False, []),
    (True, False, ["earlier_lost: she is not told her earlier words were lost"]),
    (False, True, ["earlier_lost: she is told her earlier words were lost"]),
])
def test_earlier_lost_is_checked(want, lost, wrong):
    assert owner_check.problems({"earlier_lost": want}, [], bookings.OwnerReading(), earlier_lost=lost) == wrong


def test_a_typed_time_is_on_the_day_of_the_booking(data):
    g = given(data, "en-yes-confirm")
    tomorrow = dict(g["rows"][0], starts_at=g["rows"][0]["starts_at"] + timedelta(days=1))
    typed = bookings.parse_owner_reply("8 15:30", today=g["now"].date())
    assert owner_check.outcome(typed, g["now"], TZ, [tomorrow]) == ("propose", 8, "2026-10-21 15:30")
    assert owner_check.outcome(typed, g["now"], TZ, g["rows"]) == ("propose", 8, "15:30")


def test_the_new_shapes_are_checked_when_the_file_loads(tmp_path):
    path = tmp_path / "x.json"
    pair = [{"number": 3, "customer": "Khan", "state": "pending", "start": "2026-10-20T21:00"},
            {"number": 4, "customer": "Marco", "state": "pending", "start": "2026-10-20T22:00"}]
    path.write_text(json.dumps({"now": "2026-10-20T12:40", "bookings": {"pair": pair}, "cases": [
        {"id": "a", "bookings": "pair", "latest": "YES 3", "expect": "yes 3",
         "tell": {"x": ["cash"], "9": True, "4": "cash"}, "minutes": {"3": "60"}, "for_me": {"3": "yes"},
         "standing": {"3": True}, "private": {"3": ["x"]}, "earlier_lost": "yes", "answering": 1, "words": {}},
        {"id": "b", "bookings": "pair", "latest": "3", "expect": "yes 3", "before": [{"me_which": [3, 4]}],
         "chat": [{"me_which": [3, 9]}, {"me_which": []}, {"me_ask": 3, "missing": "which"},
                  {"me_ask": 3, "held": {"colour": "red"}}, {"me_which": [3, 4], "question": "x"},
                  {"me_ask": 4, "question": ""}]},
        {"id": "c", "bookings": "pair", "latest": "3", "expect": "yes 3",
         "chat": [{"me_which": [3, 4], "held": {"tell": "bring cash", "extras_no": ["anal"], "minutes": 120}},
                  {"me_ask": 4, "missing": "time", "question": "what time?"}],
         "tell": {"3": [["cash", "efectivo"]], "4": False}, "minutes": {"3": 120}, "earlier_lost": False,
         "answering": True},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        owner_check.load(path)
    found = str(err.value).split("; ")
    for problem in ("a: tell: 'x' is not a booking number", "a: tell: #9 is not in the case's bookings",
                    "a: tell #4 is true, false or a list of groups", "a: minutes #3 is a length in minutes",
                    "a: for_me #3 is true or false", "a: standing is true, false or a list of groups",
                    "a: private is a list of words", "a: earlier_lost is true or false",
                    "a: answering is true or false", "a: words names no booking", "b: before is a list of",
                    "b: chat: #9 is not in the case's bookings", "b: chat: me_which is a list of booking numbers",
                    'b: chat: missing is "time"', "b: chat: held is", "b: chat: unknown key in",
                    "b: chat: question is a text"):
        assert any(p.startswith(problem) for p in found), problem
    assert not [p for p in found if p.startswith("c: ")]


@pytest.mark.asyncio
@pytest.mark.parametrize("found, ok", [
    (reading(), False),  # only the plan's question: the reader had nothing
    (reading({"action": "time", "number": 8, "start": "2026-10-20T15:00"}), True),
])
async def test_from_reading_needs_the_reading_itself(data, monkeypatch, found, ok):
    result, _ = await run(data, monkeypatch, case(data, "en-yes-but-at-15"), found)
    assert result.ok is ok
    if not ok:
        assert any("from_reading" in problem for problem in result.wrong)


@pytest.mark.asyncio
async def test_a_bare_yes_after_the_ack_of_another_booking_is_read_as_in_the_runtime(data, monkeypatch):
    # pair_khan_yes: her chat ends with the ack of #3; #4 waits. "YES" is
    # read (its booking is in doubt); read as nothing, she is asked about #4.
    c = dict(case(data, "en-context-tell-him-cash"), latest="YES")
    result, calls = await run(data, monkeypatch, c, reading())
    assert len(calls) == 1 and result.got.startswith("unclear 4")


@pytest.mark.asyncio
async def test_a_quoted_ok_to_his_answer_is_read_on_his_booking(data, monkeypatch):
    c = dict(case(data, "en-context-tell-him-cash"), latest="ok", quotes=3)
    result, calls = await run(data, monkeypatch, c,
                              reading({"action": "none", "number": 3, "duration_minutes": 120}))
    assert len(calls) == 1 and result.got.startswith("stays 3 (120 min)")


@pytest.mark.asyncio
async def test_ready_is_never_read_even_after_a_question(data, monkeypatch):
    c = dict(case(data, "en-outcall-ready"), latest="READY 7", chat=[{"me_ask": 7}])
    result, calls = await run(data, monkeypatch, c, None)
    assert calls == [] and result.got == "ready 7; strict, no model call"
