"""ics.py: RFC 5545 text for the calendar feed and single-event files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ics

NOW = datetime(2026, 9, 27, 8, 30, tzinfo=timezone.utc)
RIGA = ZoneInfo("Europe/Riga")


def booking(**overrides):
    row = {
        "id": 41,
        "number": 7,
        "state": "confirmed",
        # 14:00 in Riga (UTC+3 in October) = 11:00 UTC
        "starts_at": datetime(2026, 10, 3, 14, 0, tzinfo=RIGA),
        "ends_at": datetime(2026, 10, 3, 15, 0, tzinfo=RIGA),
        "service": "Haircut",
        "customer_name": "Anna",
        "updated_at": datetime(2026, 9, 26, 12, 0, 5, tzinfo=timezone.utc),
        "tz": "Europe/Riga",
    }
    row.update(overrides)
    return row


def unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")[:-1]


def props(text: str, name: str) -> list[str]:
    return [line.split(":", 1)[1] for line in unfold(text) if line.split(":", 1)[0].split(";")[0] == name]


def test_calendar_structure_and_utc_times():
    text = ics.calendar([booking()], name="Salon Anna", host="book.example.com", now=NOW)
    lines = unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines and "CALSCALE:GREGORIAN" in lines
    assert any(line.startswith("PRODID:") for line in lines)
    assert "X-WR-CALNAME:Salon Anna" in lines
    assert "X-WR-TIMEZONE:Europe/Riga" in lines
    assert "METHOD:PUBLISH" not in lines
    assert props(text, "UID") == ["booking-41@book.example.com"]
    assert props(text, "DTSTAMP") == ["20260927T083000Z"]
    assert props(text, "DTSTART") == ["20261003T110000Z"]
    assert props(text, "DTEND") == ["20261003T120000Z"]
    assert props(text, "LAST-MODIFIED") == ["20260926T120005Z"]
    updated = booking()["updated_at"]
    assert props(text, "SEQUENCE") == [str(int(updated.timestamp()) % 2**31)]
    assert props(text, "SUMMARY") == ["#7 Haircut – Anna"]
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 1


def test_sequence_goes_up_when_the_booking_is_updated():
    a = ics.calendar([booking()], name="x", host="h", now=NOW)
    b = ics.calendar([booking(updated_at=booking()["updated_at"] + timedelta(minutes=5))],
                     name="x", host="h", now=NOW)
    assert int(props(b, "SEQUENCE")[0]) > int(props(a, "SEQUENCE")[0])


def test_empty_calendar_has_no_timezone_and_no_events():
    text = ics.calendar([], name="Empty", host="h", now=NOW)
    assert "X-WR-TIMEZONE" not in text
    assert "BEGIN:VEVENT" not in text
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")


@pytest.mark.parametrize("state, status", [
    ("requested", "TENTATIVE"),
    ("pending", "TENTATIVE"),
    ("confirmed", "CONFIRMED"),
    ("completed", "CONFIRMED"),
    ("no_show", "CONFIRMED"),
    ("cancelled", "CANCELLED"),
])
def test_status(state, status):
    text = ics.calendar([booking(state=state)], name="x", host="h", now=NOW)
    assert props(text, "STATUS") == [status]


def test_unknown_state_is_refused():
    with pytest.raises(ValueError):
        ics.calendar([booking(state="weird")], name="x", host="h", now=NOW)


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        ics.calendar([booking(starts_at=datetime(2026, 10, 3, 14, 0))], name="x", host="h", now=NOW)


def test_summary_without_name_or_service():
    text = ics.calendar([booking(customer_name="", service="")], name="x", host="h", now=NOW)
    assert props(text, "SUMMARY") == ["#7 Booking"]
    text = ics.calendar([booking(customer_name=None)], name="x", host="h", now=NOW)
    assert props(text, "SUMMARY") == ["#7 Haircut"]


def test_text_escaping():
    assert ics.escape_text("a\\b;c,d\ne\r\nf") == "a\\\\b\\;c\\,d\\ne\\nf"
    text = ics.calendar(
        [booking(service="Cut, wash; dry", customer_name="O\\Brien\nJr")],
        name="Salon; A, B", host="h", now=NOW,
    )
    assert props(text, "SUMMARY") == ["#7 Cut\\, wash\\; dry – O\\\\Brien\\nJr"]
    assert "X-WR-CALNAME:Salon\\; A\\, B" in unfold(text)


def test_crlf_everywhere_and_trailing_crlf():
    text = ics.calendar([booking(), booking(id=42, number=8)], name="x", host="h", now=NOW)
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert "\r" not in text.replace("\r\n", "")


def test_folding_at_75_octets_never_splits_a_character():
    name = "Ž" * 30 + "日本語" * 20 + "x" * 50  # 2-, 3- and 1-byte characters
    text = ics.calendar([booking(customer_name=name)], name="x", host="h", now=NOW)
    physical = text.encode("utf-8").split(b"\r\n")[:-1]
    for raw in physical:
        assert len(raw) <= 75
        raw.decode("utf-8")  # raises if a character was cut in half
    # Folded lines really were produced.
    assert sum(raw.startswith(b" ") for raw in physical) >= 3
    assert props(text, "SUMMARY") == [f"#7 Haircut – {name}"]


def test_fold_boundaries():
    exactly = "A" * 75
    assert ics.fold(exactly) == exactly
    folded = ics.fold("A" * 76)
    assert folded == "A" * 75 + "\r\n " + "A"
    # Continuation lines hold 74 octets of content plus the leading space.
    folded = ics.fold("A" * (75 + 74 + 1))
    assert folded.split("\r\n") == ["A" * 75, " " + "A" * 74, " A"]
    # A 2-byte character that would straddle octet 75 moves to the next line.
    folded = ics.fold("A" * 74 + "é")
    assert folded.split("\r\n") == ["A" * 74, " é"]


def test_event_file():
    text = ics.event_file(booking(state="pending"), name="Salon Anna", host="book.example.com", now=NOW)
    lines = unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert "METHOD:PUBLISH" in lines
    assert "VERSION:2.0" in lines
    assert "X-WR-TIMEZONE:Europe/Riga" in lines
    assert lines.count("BEGIN:VEVENT") == 1
    assert props(text, "UID") == ["booking-41@book.example.com"]
    assert props(text, "STATUS") == ["TENTATIVE"]
    assert props(text, "DTSTART") == ["20261003T110000Z"]
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
