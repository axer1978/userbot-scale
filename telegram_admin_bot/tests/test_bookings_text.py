"""The words around bookings: the owner's typed answers, what is read out
of the model's extraction, and the lines the reply writer is given."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import booking_states as bs
import bookings

TODAY = date(2026, 10, 1)
RIGA = ZoneInfo("Europe/Riga")


def parse(text):
    return bookings.parse_owner_reply(text, today=TODAY)


@pytest.mark.parametrize("text,kind,number", [
    ("YES 7", "yes", 7), ("yes #7", "yes", 7), ("Jā 7", "yes", 7), ("да 7", "yes", 7), ("👍 7", "yes", 7),
    ("NO 7", "no", 7), ("nē 7", "no", 7), ("нет 12", "no", 12),
    ("CANCEL 7", "cancel", 7), ("atcelt 7", "cancel", 7),
    ("done 7", "done", 7), ("NOSHOW 7", "noshow", 7), ("no-show 7", "noshow", 7),
    ("yes", "yes", None), ("ok!", "yes", None), ("LIST", "list", None), ("?", "list", None),
])
def test_owner_commands(text, kind, number):
    cmd = parse(text)
    assert (cmd.kind, cmd.number) == (kind, number)


@pytest.mark.parametrize("text,at,day", [
    ("7 15:30", time(15, 30), None),
    ("7 15.30", time(15, 30), None),
    ("7 04.10 15:30", time(15, 30), date(2026, 10, 4)),
    ("7 15:30 04.10", time(15, 30), date(2026, 10, 4)),
    ("7 04/10/2026 9:05", time(9, 5), date(2026, 10, 4)),
    ("7 2026-10-04 15:30", time(15, 30), date(2026, 10, 4)),
    ("7 tomorrow 15:30", time(15, 30), date(2026, 10, 2)),
    ("7 rīt 15:30", time(15, 30), date(2026, 10, 2)),
    ("time 7 today 18:00", time(18, 0), TODAY),
    ("7 02.01 10:00", time(10, 0), date(2027, 1, 2)),  # written in autumn: next January
])
def test_owner_proposes_a_new_time(text, at, day):
    cmd = parse(text)
    assert (cmd.kind, cmd.number, cmd.at, cmd.day, cmd.error) == ("propose", 7, at, day, "")


def test_a_bad_time_is_an_error_to_explain_not_silence():
    assert parse("7 25:00").error
    assert parse("7 31.02 10:00").error
    assert parse("7 tomorrow").error == "no time given (use HH:MM)"


def test_ordinary_conversation_is_not_a_command():
    for text in ("7 people are coming", "hello", "no problem at all, see you on friday then", "", "yes I think so and more"):
        assert parse(text) is None, text


def test_a_date_that_looks_like_a_time_is_read_as_the_date():
    cmd = parse("7 12.10 9.30")
    assert (cmd.day, cmd.at) == (date(2026, 10, 12), time(9, 30))


def test_a_proposed_time_without_a_day_is_on_the_bookings_day():
    b = {"starts_at": datetime(2026, 10, 3, 11, 0, tzinfo=timezone.utc), "tz": "Europe/Riga"}
    assert bookings.resolve_local(parse("7 15:30"), b).isoformat() == "2026-10-03T15:30:00+03:00"
    assert bookings.resolve_local(parse("7 04.10 15:30"), b).isoformat() == "2026-10-04T15:30:00+03:00"


def test_extraction_needs_an_intent_and_a_time_to_book():
    assert bookings.parse_extraction('{"intent": "none"}') is None
    assert bookings.parse_extraction('{"intent": "book"}') is None
    assert bookings.parse_extraction('{"intent": "dance"}') is None
    assert bookings.parse_extraction("no json") is None
    found = bookings.parse_extraction('```json\n{"intent": "book", "start": "2026-10-04T15:00"}\n```')
    assert found["start"] == "2026-10-04T15:00"
    assert bookings.parse_extraction('{"intent": "cancel"}')["intent"] == "cancel"


def test_a_requested_slot_lasts_real_minutes_across_the_clock_change():
    # Riga goes back an hour at 04:00 on 25 Oct 2026.
    start, end = bookings.requested_slot({"start": "2026-10-25T02:30", "duration_minutes": 120},
                                         tz_name="Europe/Riga", default_duration=60)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=2)
    start, end = bookings.requested_slot({"start": "2026-10-04T15:00"}, tz_name="Europe/Riga", default_duration=45)
    assert end - start == timedelta(minutes=45)


def test_reminder_answers_are_only_bare_1_or_2():
    assert bookings.reminder_answer("1") == "confirm"
    assert bookings.reminder_answer(" 2 ") == "cancel"
    assert bookings.reminder_answer("1 or 2?") is None
    assert bookings.reminder_answer("I'll be 2 minutes late") is None


def _b(**extra):
    start = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    return {"number": 7, "state": bs.PENDING, "starts_at": start, "ends_at": start + timedelta(hours=1),
            "tz": "Europe/Riga", "customer_name": "Anna", "customer_username": "anna", "service": "haircut",
            "notes": "", "proposed_by": None, "proposed_starts_at": None, "proposed_ends_at": None,
            "customer_notice": None, "instructions_sent_at": None, **extra}


def test_the_owner_request_names_the_number_and_how_to_answer():
    text = bookings.format_request(_b())
    assert "Booking request #7" in text and "Anna (@anna)" in text
    assert "Sun 04 Oct 2026, 15:00–16:00" in text
    assert "YES 7" in text and "NO 7" in text and "7 15:30" in text


def test_a_pending_booking_is_never_presented_as_confirmed():
    line = bookings.standing_line(_b())
    assert "Do NOT say it is confirmed" in line


def test_news_lines_carry_the_change():
    assert "CONFIRMED" in bookings.notice_line(_b(state=bs.CONFIRMED, customer_notice=bs.NOTICE_CONFIRMED))
    proposed = _b(customer_notice=bs.NOTICE_PROPOSED, proposed_by=bs.OWNER,
                  proposed_starts_at=datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc),
                  proposed_ends_at=datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc))
    assert "16:00–17:00" in bookings.notice_line(proposed)
    assert bookings.notice_line(_b()) == ""


def test_an_unavailable_time_offers_alternatives_and_the_waitlist():
    requested = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    alts = [requested + timedelta(hours=2)]
    line = bookings.unavailable_line(requested, "conflict", alts, "Europe/Riga", waitlist=True)
    assert "already taken" in line and "Sun 04 Oct 17:00" in line and "waitlist" in line
    assert "waitlist" not in bookings.unavailable_line(requested, "closed_day", [], "Europe/Riga", waitlist=False)


def test_the_reminder_line_asks_for_1_or_2_and_uses_the_instruction():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    line = bookings.reminder_line(_b(state=bs.CONFIRMED), "Mention parking is free.", now, "https://x/b/tok")
    assert "Mention parking is free." in line and "reply 1" in line and "https://x/b/tok" in line
    assert "in about 24 hours" in line
