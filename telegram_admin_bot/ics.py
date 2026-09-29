"""iCalendar (RFC 5545) text for bookings: a business's subscribable feed
and a single-event file for an e-mail attachment.

Pure functions, no I/O: public_app.py fetches the rows (scoped to one
tenant) and hands them here, so everything about the file format can be
tested without a database.

Decisions:
- All times are written in UTC ("Z" form). That avoids shipping VTIMEZONE
  blocks, which every client handles slightly differently; the calendar
  app shows them in the viewer's own zone anyway. X-WR-TIMEZONE still
  names the business's zone as a display hint for apps that honour it.
- UID is stable per booking (booking-<id>@<host>) and SEQUENCE comes from
  updated_at, so a moved or cancelled booking updates the existing event
  instead of adding a second one. A calendar app only replaces an event
  whose SEQUENCE went up; seconds since the epoch always do (mod 2**31,
  since SEQUENCE is a signed 32-bit integer in most clients).
- Cancelled bookings stay in the feed for a while with STATUS:CANCELLED
  (the caller decides how long) — simply dropping them leaves a stale
  event behind in some clients.
- Lines are folded at 75 octets of UTF-8, never inside a multi-byte
  character; customer names and services are routinely non-ASCII.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

PRODID = "-//userbot-scale//Bookings//EN"
CRLF = "\r\n"
_MAX_OCTETS = 75

_STATUS = {
    "requested": "TENTATIVE",
    "pending": "TENTATIVE",
    "confirmed": "CONFIRMED",
    "completed": "CONFIRMED",
    "no_show": "CONFIRMED",
    "cancelled": "CANCELLED",
}


def escape_text(value: str) -> str:
    """Escape a TEXT property value (RFC 5545 3.3.11)."""
    return (
        str(value)
        .replace("\\", "\\\\")  # first, so the escapes added below stay single
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold one content line so no physical line exceeds 75 octets.

    Continuation lines start with a single space, which counts towards
    their 75, so they carry at most 74 octets of content.
    """
    if len(line.encode("utf-8")) <= _MAX_OCTETS:
        return line
    parts: list[str] = []
    current: list[str] = []
    size = 0
    limit = _MAX_OCTETS
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append("".join(current))
            current, size, limit = [], 0, _MAX_OCTETS - 1
        current.append(ch)
        size += n
    parts.append("".join(current))
    return (CRLF + " ").join(parts)


def utc_stamp(dt: datetime) -> str:
    """20261003T110000Z. Naive datetimes are refused: guessing their zone
    would silently shift a booking by hours."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime {dt!r}; bookings carry aware timestamps")
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def summary(booking: Mapping) -> str:
    text = f"#{booking['number']} {booking.get('service') or 'Booking'}"
    name = (booking.get("customer_name") or "").strip()
    if name:
        text += f" – {name}"
    return text


def _event_lines(booking: Mapping, *, host: str, now: datetime) -> list[str]:
    state = booking["state"]
    if state not in _STATUS:
        raise ValueError(f"unknown booking state {state!r}")
    updated_at: datetime = booking["updated_at"]
    uid = f"booking-{booking['id']}@{host}"
    return [
        "BEGIN:VEVENT",
        f"UID:{escape_text(uid)}",
        f"DTSTAMP:{utc_stamp(now)}",
        f"DTSTART:{utc_stamp(booking['starts_at'])}",
        f"DTEND:{utc_stamp(booking['ends_at'])}",
        f"LAST-MODIFIED:{utc_stamp(updated_at)}",
        f"SEQUENCE:{int(updated_at.timestamp()) % 2**31}",
        f"SUMMARY:{escape_text(summary(booking))}",
        f"STATUS:{_STATUS[state]}",
        "END:VEVENT",
    ]


def _render(lines: Iterable[str]) -> str:
    return "".join(fold(line) + CRLF for line in lines)


def _header(name: str, tz: str | None, *, method: str | None = None) -> list[str]:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
    ]
    if method:
        lines.append(f"METHOD:{method}")
    lines.append(f"X-WR-CALNAME:{escape_text(name)}")
    if tz:
        lines.append(f"X-WR-TIMEZONE:{escape_text(tz)}")
    return lines


def calendar(bookings: Sequence[Mapping], *, name: str, host: str, now: datetime) -> str:
    """A whole feed: one VEVENT per booking, in the order given."""
    lines = _header(name, bookings[0].get("tz") if bookings else None)
    # Hints for Apple Calendar / Outlook to re-fetch more often than their
    # default (often a day); Google ignores them and polls on its own.
    lines += ["REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    for booking in bookings:
        lines += _event_lines(booking, host=host, now=now)
    lines.append("END:VCALENDAR")
    return _render(lines)


def event_file(booking: Mapping, *, name: str, host: str, now: datetime) -> str:
    """One booking as a standalone .ics (METHOD:PUBLISH), for an e-mail
    attachment. Same UID as in the feed, so importing it next to a
    subscription doesn't duplicate the event."""
    lines = _header(name, booking.get("tz"), method="PUBLISH")
    lines += _event_lines(booking, host=host, now=now)
    lines.append("END:VCALENDAR")
    return _render(lines)
