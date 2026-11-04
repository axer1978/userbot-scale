"""The checker for how a customer's words about a time are read
(time_check.py), and the reader's prompt around it: no real model here, the
real one is the checker's own run."""

import json

import pytest

import ai_responder
import bookings
import time_check

TZ = "Europe/Riga"
NOW = bookings.parse_local("2026-11-04T18:40", TZ)


def found(**data):
    return {"intent": "book", "start": None, "asap": False, **data}


# --------------------------------------------------------------- the cases


def test_the_cases_are_well_formed_in_every_language():
    data = time_check.load(time_check.CASES)
    languages = {case["id"].split("-")[0] for case in data["cases"]}
    assert languages == {"en", "ru", "lv", "et", "es"}
    for language in languages:
        assert sum(case["id"].startswith(language + "-") for case in data["cases"]) >= 30, language
    assert any(case.get("check") == "arrival" for case in data["cases"])
    assert any(isinstance(case["them"], list) for case in data["cases"])


@pytest.mark.parametrize("case, says", [
    ({"id": "x", "want": "none"}, "no message"),
    ({"id": "x", "them": "", "want": "none"}, "no message"),
    ({"id": "x", "them": "hi", "want": "soon"}, "unknown want"),
    ({"id": "x", "them": "hi"}, "no want"),
    ({"id": "x", "them": "hi", "want": "none", "now": "tomorrow"}, "now is"),
    ({"id": "x", "them": "hi", "want": "none", "chat": [{"bot": "hi"}]}, "chat line"),
    ({"id": "x", "them": "hi", "want": "none", "booking": {"state": "done", "start": "2026-11-04T20:00"}},
     "booking state"),
    ({"id": "x", "them": "hi", "want": "none", "booking": {"state": "confirmed", "start": "8pm"}}, "booking start"),
    ({"id": "x", "them": "here", "want": True, "check": "arrival"}, "confirmed booking"),
    ({"id": "x", "them": "here", "want": "none", "check": "arrival",
      "booking": {"state": "confirmed", "start": "2026-11-04T18:45"}}, "true or false"),
    ({"id": "x", "them": "here", "want": True}, "unknown want"),
])
def test_a_case_written_wrong_is_refused_before_any_model_call(case, says):
    assert any(says in problem for problem in time_check.case_problems(case))


def test_a_good_case_has_no_problems():
    assert not time_check.case_problems({"id": "x", "them": ["at 8", "ok?"], "want": ["2026-11-04T20:00", "none"]})
    assert not time_check.case_problems({"id": "x", "them": "here", "want": False, "check": "arrival",
                                         "booking": {"state": "confirmed", "start": "2026-11-04T18:45"}})


# ---------------------------------------------------------- how it is judged


@pytest.mark.parametrize("answer, got", [
    (None, "none"),
    ({"intent": "none"}, "none"),
    (found(asap=True), "now"),
    # A time a few minutes back is "now", as the runtime takes it.
    (found(start="2026-11-04T18:30"), "now"),
    (found(start="2026-11-04T20:00"), "2026-11-04T20:00"),
    (found(start="nonsense"), "none"),
    ({"intent": "cancel"}, "cancel"),
    ({"intent": "accept_proposal"}, "accept_proposal"),
])
def test_what_the_runtime_would_make_of_an_answer(answer, got):
    assert time_check.reading(answer, TZ, NOW) == got


def test_a_case_passes_on_any_reading_it_allows():
    case = {"id": "x", "them": "9", "want": ["2026-11-05T09:00", "2026-11-05T21:00"]}
    assert time_check.judge(case, found(start="2026-11-05T21:00"), TZ, NOW) == ("2026-11-05T21:00", True)
    assert time_check.judge(case, found(start="2026-11-05T20:00"), TZ, NOW) == ("2026-11-05T20:00", False)
    assert time_check.judge(case, None, TZ, NOW) == ("none", False)


def test_an_outcall_case_must_be_read_as_one():
    case = {"id": "x", "them": "come to me at 8", "want": "2026-11-04T20:00", "outcall": True}
    assert time_check.judge(case, found(start="2026-11-04T20:00", outcall=True), TZ, NOW)[1]
    assert not time_check.judge(case, found(start="2026-11-04T20:00"), TZ, NOW)[1]


# ------------------------------------------------------------- how it is read


@pytest.fixture
def model(monkeypatch):
    calls = []
    answer = {"text": '{"intent": "none"}'}

    async def complete(**kw):
        calls.append(kw)
        return answer["text"]

    monkeypatch.setattr(ai_responder, "_complete", complete)
    return lambda text=None: (answer.update(text=text) if text else None) or calls


@pytest.mark.asyncio
async def test_a_case_is_read_with_its_clock_its_chat_and_its_booking(model):
    data = {"now": "2026-11-04T18:40"}
    case = {"id": "en-x", "chat": [{"me": "what time love?"}], "them": ["at 8", "how much?"],
            "booking": {"state": "requested", "start": "2026-11-05T15:00"}, "want": "2026-11-04T20:00"}
    calls = model(json.dumps(found(start="2026-11-04T20:00")))
    result = await time_check.run_case(data, case, key="k", tz=TZ, ai_config={"model": "m"}, http=None)
    assert result.ok and result.got == "2026-11-04T20:00"
    system, user = (m["content"] for m in calls[0]["messages"])
    assert system == bookings.EXTRACT_SYSTEM_PROMPT
    assert user.startswith("Current date and time: Wednesday 2026-11-04 18:40 (Europe/Riga).")
    assert "Their bookings: #1 requested for" in user
    assert "me: what time love?\nthem: at 8\nthem: how much?" in user
    assert user.endswith("the ones to decide on together:\n- at 8\n- how much?")


@pytest.mark.asyncio
async def test_an_arrival_case_goes_to_the_arrival_reader(model):
    data = {"now": "2026-11-04T18:40"}
    case = {"id": "lv-x", "them": "esmu klat", "want": True, "check": "arrival",
            "booking": {"state": "confirmed", "start": "2026-11-04T18:45"}}
    calls = model('{"arrived": true}')
    result = await time_check.run_case(data, case, key="k", tz=TZ, ai_config={"model": "m"}, http=None)
    assert result.ok and result.got == "true"
    assert calls[0]["messages"][0]["content"] == bookings.ARRIVAL_SYSTEM_PROMPT


def test_the_report_shows_what_failed_and_counts_each_language():
    good = {"id": "en-a", "them": "now", "want": "now"}
    bad = {"id": "lv-b", "them": ["tagad"], "want": "now", "chat": [{"me": "hi"}]}
    text = time_check.report([time_check.Result(good, "now", True), time_check.Result(bad, "none", False, {"x": 1})],
                             verbose=False)
    assert text.startswith("1/2 messages read right\n  en     1/1\n  lv     0/1")
    assert "FAIL  lv-b: ['tagad']" in text and "got     none" in text and 'wanted  "now"' in text
    assert "en-a" not in text
    assert "PASS  en-a" in time_check.report([time_check.Result(good, "now", True)], verbose=True)
