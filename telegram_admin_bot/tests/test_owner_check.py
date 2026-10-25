"""The checker for the booker's answers in her own words (owner_check.py):
the cases load and build as the runtime would give them to the reader (her
chat with its times), the expectations and checks pass what they should, and
a reading is judged on the commands the runtime would make of it. The model
itself is not called."""

from __future__ import annotations

import json
from datetime import timedelta, timezone

import pytest

import ai_responder
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
    assert len(data["cases"]) >= 93
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
