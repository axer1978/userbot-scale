"""Who books for her at what time (booking.shifts).

One booker can take the requests in the evening (16:00–24:00) and another
at night: each shift names its hours, the booker (what booking.provider
takes: a person or a group) and, if it is not every day, its days. Outside
every shift, booking.provider is the booker.

- A shift runs from `start` to `end` in the account's time zone. An end at
  or before the start runs past midnight (22:00–06:00); "24:00" is
  midnight; the same start and end is the whole day.
- `days` are the days the shift STARTS on: a Friday 22:00–06:00 shift
  covers Saturday 03:00. No days: every day.
- Two shifts at the same time: the first one listed is on duty.

Every booker in the list is a booker all the time, on duty or not: her chat
never gets a customer's reply, and an answer of hers still counts after her
shift (booking_flow). Only what goes OUT is for the booker on duty.

Nothing here touches the database or the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_SHIFTS = 12


def minutes_of(hhmm: str) -> int:
    """'16:30' -> 990; '24:00' -> 1440."""
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def covers(shift: Mapping[str, Any], local: datetime) -> bool:
    """Whether `shift` is on at `local` (a wall-clock time in the account's
    zone)."""
    start, end = minutes_of(shift["start"]), minutes_of(shift["end"]) % (24 * 60)
    now = local.hour * 60 + local.minute
    days = shift.get("days") or ()

    def starts_on(day: datetime) -> bool:
        return not days or DAYS[day.weekday()] in days

    if start == end:
        # The whole day, from its start to the same time the next day.
        return starts_on(local if now >= start else local - timedelta(days=1))
    if start < end:
        return start <= now < end and starts_on(local)
    # Past midnight: the evening part is today's shift, the morning part
    # yesterday's.
    if now >= start:
        return starts_on(local)
    return now < end and starts_on(local - timedelta(days=1))


def on_duty(provider: str, shifts: Sequence[Mapping[str, Any]], local: datetime) -> str:
    """The booker at `local`: the first shift that covers it, else
    booking.provider ("" when that is empty too)."""
    for shift in shifts:
        if covers(shift, local):
            return str(shift.get("provider") or "").strip()
    return (provider or "").strip()


def all_bookers(booking: Mapping[str, Any]) -> list[str]:
    """Every booker the settings name (booking.provider, then each shift's),
    once each, in that order."""
    out: list[str] = []
    for value in [booking.get("provider") or ""] + [s.get("provider") or "" for s in booking.get("shifts") or ()]:
        value = str(value).strip()
        if value and value not in out:
            out.append(value)
    return out


def describe(shift: Mapping[str, Any]) -> str:
    """'16:00–24:00 mon, tue' / '22:00–06:00 every day', for notes."""
    days = shift.get("days") or ()
    return f"{shift['start']}–{shift['end']} " + (", ".join(days) if days else "every day")
