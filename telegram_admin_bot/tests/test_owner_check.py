"""The checker for the owner's answers in her own words (owner_check.py):
the cases load and build as the runtime would give them to the reader, and
the expectations pass what they should. The model itself is not called."""

from __future__ import annotations

import json

import pytest

import ai_responder
import owner_check

TZ = "Europe/Riga"


@pytest.fixture(scope="module")
def data():
    return owner_check.load(owner_check.CASES)


def case(data, case_id):
    return next(c for c in data["cases"] if c["id"] == case_id)


def test_every_case_builds_her_chat_with_the_requests_in_it(data):
    assert len(data["cases"]) >= 50
    assert {c["id"].split("-")[0] for c in data["cases"]} == {"en", "ru", "lv"}
    for c in data["cases"]:
        given = owner_check.build(data, c, TZ, "Pamella", True)
        assert given["history"][-1] == {"role": "user", "content": c["latest"]}
        assert all(f"#{b['number']} " in given["state_note"] for b in given["rows"])
    one = owner_check.build(data, case(data, "en-yes-confirm"), TZ, "Pamella", True)
    assert "Girl: Pamella\nExtras: domination +50\nWhen: Tue 20 Oct 2026, 13:00–14:00" in one["history"][0]["content"]
    taxi = owner_check.build(data, case(data, "en-outcall-in-taxi"), TZ, "Pamella", True)
    said = [m["content"] for m in taxi["history"]]
    assert "YES 7" in said and "READY 7" in said and any(owner_check.TAXI_LINK in m for m in said)
    assert "outcall: taxi link sent" in taxi["state_note"]


def test_a_broken_case_file_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"now": "2026-10-20T12:40", "bookings": {"one": []}, "cases": [
        {"id": "a", "bookings": "one", "latest": "yes", "expect": "maybe 8"},
        {"id": "a", "bookings": "none", "latest": "yes", "expect": "yes"},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError) as err:
        owner_check.load(path)
    assert "not an expectation" in str(err.value) and "id used twice" in str(err.value)
    assert "no booking set 'none'" in str(err.value)


@pytest.mark.parametrize("got, expect, ok", [
    (("propose", 8, "13:10"), "propose 8 13:10", True),
    (("propose", 8, "13:15"), "propose 8 13:10", False),
    (("propose", 8, "13:10"), "propose 8", True),
    (("yes", None, None), "yes -", True),
    (("yes", 10, None), "yes -", False),
    (("yes", 10, None), "yes 8", False),
    (("unclear", 8, None), "unclear", True),
    (("none", None, None), "yes", False),
])
def test_expectations(got, expect, ok):
    assert owner_check.matches(got, expect) is ok


def said(action, number=None, start=None, in_minutes=None):
    return {"action": action, "number": number, "start": start, "in_minutes": in_minutes}


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id, answer, got, ok", [
    ("en-ready-by-his-time", said("ready_at", in_minutes=20), "yes 8", True),
    ("en-give-me-30", said("ready_at", 8, in_minutes=30), "propose 8 13:10", True),
    ("en-tomorrow-12", said("time", start="2026-10-21T12:00"), "propose 8 2026-10-21 12:00", True),
    ("en-two-reply-to", said("yes"), "yes 10", True),
    ("en-two-no-telling", said("yes", 10), "yes 10", False),
    ("en-confirmed-yes-again", said("yes", 6), "stays 6", True),
    ("en-outcall-in-taxi", said("in_taxi"), "in_taxi 7", True),
    ("en-chat-how-are-you", None, "none", True),
])
async def test_a_case_is_judged_on_what_the_runtime_would_do(data, monkeypatch, case_id, answer, got, ok):
    seen = {}

    async def reader(**kw):
        seen.update(kw)
        return answer

    monkeypatch.setattr(ai_responder, "read_owner_answer", reader)
    c = case(data, case_id)
    result = await owner_check.run_case(data, c, key="k", tz=TZ, girl="Pamella", outcall_taxi=True,
                                        ai_config={}, http=None)
    assert (result.got, result.ok) == (got, ok)
    assert seen["latest"] == c["latest"] and seen["replying_to"] == c.get("reply_to")
    assert seen["now"].strftime("%Y-%m-%d %H:%M") == "2026-10-20 12:40"
