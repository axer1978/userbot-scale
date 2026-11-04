"""booking.shifts: which booker is on duty when (booker_shifts), and what
the panel may save."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import booker_shifts
import tenant_config

RIGA = ZoneInfo("Europe/Riga")


def at(day: int, hhmm: str) -> datetime:
    """4 March 2030 is a Monday: day 0 = Monday."""
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2030, 3, 4 + day, hour, minute, tzinfo=RIGA)


EVENING = {"start": "16:00", "end": "24:00", "provider": "+37120000001"}
NIGHT = {"start": "00:00", "end": "09:00", "provider": "+37120000002"}


@pytest.mark.parametrize("hhmm,on", [("15:59", False), ("16:00", True), ("23:59", True), ("00:00", False)])
def test_a_shift_to_midnight_ends_at_midnight(hhmm, on):
    assert booker_shifts.covers(EVENING, at(0, hhmm)) is on


@pytest.mark.parametrize("hhmm,on", [("21:59", False), ("22:00", True), ("23:30", True), ("00:00", True),
                                     ("05:59", True), ("06:00", False), ("12:00", False)])
def test_a_shift_may_run_past_midnight(hhmm, on):
    shift = {"start": "22:00", "end": "06:00", "provider": "x"}
    assert booker_shifts.covers(shift, at(1, hhmm)) is on


def test_days_are_the_days_a_shift_starts_on():
    friday_night = {"start": "22:00", "end": "06:00", "provider": "x", "days": ["fri"]}
    assert booker_shifts.covers(friday_night, at(4, "23:00"))       # Friday evening
    assert booker_shifts.covers(friday_night, at(5, "03:00"))       # Saturday morning: Friday's shift
    assert not booker_shifts.covers(friday_night, at(5, "23:00"))   # Saturday evening
    assert not booker_shifts.covers(friday_night, at(4, "03:00"))   # Friday morning: Thursday's


def test_the_same_start_and_end_is_the_whole_day():
    sunday = {"start": "08:00", "end": "08:00", "provider": "x", "days": ["sun"]}
    assert booker_shifts.covers(sunday, at(6, "08:00"))
    assert booker_shifts.covers(sunday, at(6, "23:59"))
    assert booker_shifts.covers(sunday, at(0, "07:59"))     # Monday, before 08:00: still Sunday's
    assert not booker_shifts.covers(sunday, at(0, "08:00"))
    assert booker_shifts.covers({"start": "00:00", "end": "24:00", "provider": "x"}, at(2, "13:00"))


def test_the_first_shift_on_is_on_duty_else_the_provider():
    shifts = [EVENING, NIGHT, {"start": "20:00", "end": "22:00", "provider": "+37120000003"}]
    assert booker_shifts.on_duty(" @main ", shifts, at(0, "21:00")) == "+37120000001"
    assert booker_shifts.on_duty("@main", shifts, at(0, "03:00")) == "+37120000002"
    assert booker_shifts.on_duty("@main", shifts, at(0, "12:00")) == "@main"
    assert booker_shifts.on_duty("", shifts, at(0, "12:00")) == ""
    assert booker_shifts.on_duty("@main", [], at(0, "21:00")) == "@main"


def test_every_booker_is_named_once():
    booking = {"provider": "+37120000001", "shifts": [EVENING, NIGHT, {**NIGHT, "start": "09:00", "end": "10:00"}]}
    assert booker_shifts.all_bookers(booking) == ["+37120000001", "+37120000002"]
    assert booker_shifts.all_bookers({"provider": "", "shifts": [NIGHT]}) == ["+37120000002"]
    assert booker_shifts.all_bookers({"provider": " "}) == []


# ------------------------------------------------------------ the schema


def shifts(*items):
    return tenant_config.resolve(None, {"booking": {"shifts": list(items)}}).config.booking.shifts


def test_shifts_are_saved_as_typed_with_the_days_in_order():
    [shift] = shifts({"start": "16:00", "end": "24:00", "provider": " +371 2000 0001 ", "days": ["sun", "mon"]})
    assert (shift.start, shift.end, shift.provider, shift.days) == ("16:00", "24:00", "+371 2000 0001", ["mon", "sun"])
    assert shifts() == []
    assert tenant_config.resolve(None, None).config.booking.shifts == []


@pytest.mark.parametrize("bad", [
    {"start": "24:00", "end": "08:00", "provider": "x"},
    {"start": "8:00", "end": "16:00", "provider": "x"},
    {"start": "16:00", "end": "25:00", "provider": "x"},
    {"start": "16:00", "end": "24:00", "provider": "  "},
    {"start": "16:00", "end": "24:00"},
    {"start": "16:00", "end": "24:00", "provider": "x", "days": ["monday"]},
    {"start": "16:00", "end": "24:00", "provider": "x", "who": "y"},
])
def test_a_shift_that_cannot_be_used_is_refused(bad):
    with pytest.raises(tenant_config.ConfigError):
        shifts(bad)


def test_at_most_twelve_shifts():
    with pytest.raises(tenant_config.ConfigError):
        shifts(*[{"start": "16:00", "end": "24:00", "provider": f"+3712000000{n}"} for n in range(13)])
