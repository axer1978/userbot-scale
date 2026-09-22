"""Appointments a client asks for in chat, and the provider's yes or no.

The flow: a client agrees a date and time in the conversation → the request is
put to the provider on a separate Telegram account (a person or a bot) → they
answer YES or NO → the client is told. Optionally the same request is mirrored
into a Google Calendar as a tentative event that becomes confirmed or is
removed with the provider's answer.

This module holds the parts that need neither Telegram nor the network: the
booking record, the small JSON-backed store that stands in for a database
table until one exists, the parser for the provider's reply, and the text
that goes into the prompts. main.py does the wiring.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

# Waiting for the provider's answer.
PENDING = "pending"
# The provider said yes / no.
CONFIRMED = "confirmed"
DECLINED = "declined"
# The client asked for a different time before the provider answered; the
# newer request replaces this one.
SUPERSEDED = "superseded"

ACTIVE = (PENDING, CONFIRMED)


@dataclass
class Booking:
    id: int
    chat_id: int
    client_name: str
    client_username: Optional[str]
    # ISO 8601 with offset, e.g. "2026-09-23T15:00:00+02:00".
    start: str
    end: str
    timezone: str
    title: str = ""
    notes: str = ""
    status: str = PENDING
    # Telegram id of the request message in the provider chat, so a bare
    # "yes" sent as a reply to it is unambiguous.
    provider_message_id: Optional[int] = None
    provider_chat_id: Optional[int] = None
    calendar_event_id: Optional[str] = None
    replaces_id: Optional[int] = None
    created_at: str = ""
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    # Set once the client has been told the outcome, so a restart between the
    # decision and the reply does not lose the message.
    client_notified: bool = False
    # The "are you still coming?" check-in: requested by the reminder loop,
    # sent by the next draft. Then the arrival: when they say they are at the
    # place, the entry instructions go out and that is noted here.
    reminder_requested_at: Optional[str] = None
    reminder_sent: bool = False
    arrived_at: Optional[str] = None
    instructions_sent_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Booking":
        known = {f: raw.get(f) for f in cls.__dataclass_fields__ if f in raw}
        return cls(**known)  # type: ignore[arg-type]

    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    def end_dt(self) -> datetime:
        return datetime.fromisoformat(self.end)


# ------------------------------------------------------------------ store


class BookingStore:
    """All bookings, in memory, mirrored to a JSON file.

    A proper table is coming; until then this keeps a pending request alive
    across a restart, which matters because the provider may take hours to
    answer. Writes are atomic for the same reason config.json's are.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: dict[int, Booking] = {}
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for item in raw.get("bookings", []) if isinstance(raw, dict) else []:
            try:
                booking = Booking.from_dict(item)
            except TypeError:
                continue
            self._items[booking.id] = booking
        if self._items:
            self._next_id = max(self._items) + 1

    def save(self) -> None:
        payload = {"bookings": [b.to_dict() for b in self.all()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".bookings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def add(self, **fields: Any) -> Booking:
        booking = Booking(id=self._next_id, created_at=utcnow(), **fields)
        self._next_id += 1
        self._items[booking.id] = booking
        self.save()
        return booking

    def get(self, booking_id: int) -> Optional[Booking]:
        return self._items.get(booking_id)

    def all(self) -> list[Booking]:
        return [self._items[k] for k in sorted(self._items)]

    def pending(self) -> list[Booking]:
        return [b for b in self.all() if b.status == PENDING]

    def for_chat(self, chat_id: int, statuses: tuple[str, ...] = ACTIVE) -> list[Booking]:
        return [b for b in self.all() if b.chat_id == chat_id and b.status in statuses]

    def find_same_slot(self, chat_id: int, start: datetime) -> Optional[Booking]:
        """An active booking for this chat at this exact time, if one exists."""
        for booking in self.for_chat(chat_id):
            if booking.start_dt() == start:
                return booking
        return None

    def due_for_reminder(self, now: datetime, minutes_before: int) -> list[Booking]:
        """Confirmed bookings inside the reminder window, not yet asked about."""
        if minutes_before <= 0:
            return []
        out = []
        for booking in self.all():
            if booking.status != CONFIRMED or booking.reminder_requested_at:
                continue
            start = booking.start_dt()
            if now < start and (start - now) <= timedelta(minutes=minutes_before):
                out.append(booking)
        return out

    def awaiting_arrival(self, chat_id: int, now: datetime, minutes_before: int) -> Optional[Booking]:
        """The confirmed booking this chat may be arriving for right now.

        The window opens with the reminder (or an hour before, whichever is
        earlier) and closes half an hour after the slot ends, so "I'm here"
        the day before is not taken as an arrival.
        """
        lead = timedelta(minutes=max(minutes_before, 60))
        for booking in self.for_chat(chat_id, (CONFIRMED,)):
            if booking.instructions_sent_at:
                continue
            if booking.start_dt() - lead <= now <= booking.end_dt() + timedelta(minutes=30):
                return booking
        return None

    def update(self, booking: Booking, **changes: Any) -> Booking:
        for key, value in changes.items():
            setattr(booking, key, value)
        self.save()
        return booking


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------ time handling


def tzinfo_for(name: str) -> Any:
    """The IANA zone from Settings, or UTC when it cannot be loaded."""
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def parse_local(value: str, tz_name: str) -> Optional[datetime]:
    """A naive 'YYYY-MM-DDTHH:MM' from the model, placed in the configured zone."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo_for(tz_name))
    return parsed.replace(second=0, microsecond=0)


def describe_when(booking: Booking) -> str:
    """'Tue 23 Sep 2026, 15:00–16:00 (Europe/Madrid)' — for people, not machines."""
    start, end = booking.start_dt(), booking.end_dt()
    day = start.strftime("%a %d %b %Y")
    if end.date() == start.date():
        span = f"{start:%H:%M}–{end:%H:%M}"
    else:
        span = f"{start:%H:%M} – {end:%a %d %b %H:%M}"
    return f"{day}, {span} ({booking.timezone})"


# ------------------------------------------------ messages to the provider


def format_request(booking: Booking) -> str:
    who = booking.client_name or "Unknown client"
    if booking.client_username:
        who += f" (@{booking.client_username})"
    lines = [f"📅 Booking request #{booking.id}"]
    if booking.replaces_id:
        lines[0] += f" (replaces #{booking.replaces_id})"
    lines.append(f"Client: {who}")
    lines.append(f"When: {describe_when(booking)}")
    if booking.title:
        lines.append(f"What: {booking.title}")
    if booking.notes:
        lines.append(f"Notes: {booking.notes}")
    lines.append("")
    lines.append(f"Reply YES {booking.id} to confirm or NO {booking.id} to decline.")
    return "\n".join(lines)


def format_acknowledgement(booking: Booking, confirmed: bool) -> str:
    verb = "confirmed ✅" if confirmed else "declined ❌"
    return f"#{booking.id} {verb} — {booking.client_name} will be told."


def format_help(pending: list[Booking]) -> str:
    if not pending:
        return "There is nothing waiting for an answer right now."
    lines = ["Which one? Reply YES <number> or NO <number>:"]
    for booking in pending:
        lines.append(f"  #{booking.id} — {booking.client_name}, {describe_when(booking)}")
    return "\n".join(lines)


# ----------------------------------------------- the provider's answer

_YES = {
    "yes", "y", "yes.", "yep", "yeah", "ok", "okay", "confirm", "confirmed",
    "accept", "accepted", "approve", "approved", "sure", "✅", "👍",
    "да", "si", "sí", "ja", "oui", "tak", "sim", "da",
}
_NO = {
    "no", "n", "no.", "nope", "decline", "declined", "reject", "rejected",
    "deny", "denied", "cancel", "busy", "❌", "👎",
    "нет", "nein", "non", "nie", "não", "nu",
}
_NUMBER = re.compile(r"#?\s*(\d+)")


@dataclass
class Decision:
    confirmed: bool
    booking: Optional[Booking] = None
    # True when the answer was clear but it is not clear which booking it was
    # for — the caller asks rather than guessing.
    ambiguous: bool = False


def parse_provider_reply(
    text: str,
    pending: list[Booking],
    reply_to_message_id: Optional[int] = None,
) -> Optional[Decision]:
    """Turn what the provider wrote into a decision, or None if it was not one.

    The first word decides yes or no. The booking is picked, in order, by a
    number in the message, by the request message it was sent in reply to,
    and finally by there being exactly one thing waiting. Anything else is an
    answer with no clear target, and the provider gets asked which.
    """
    words = (text or "").strip().lower().split()
    if not words:
        return None
    head = words[0].strip("!,;:")
    if head in _YES:
        confirmed = True
    elif head in _NO:
        confirmed = False
    else:
        return None

    rest = " ".join(words[1:])
    number = _NUMBER.search(rest)
    if number:
        wanted = int(number.group(1))
        for booking in pending:
            if booking.id == wanted:
                return Decision(confirmed, booking)
        return Decision(confirmed, None, ambiguous=True)

    if reply_to_message_id is not None:
        for booking in pending:
            if booking.provider_message_id == reply_to_message_id:
                return Decision(confirmed, booking)

    if len(pending) == 1:
        return Decision(confirmed, pending[0])
    return Decision(confirmed, None, ambiguous=True)


# ------------------------------------------------- text for the prompts

EXTRACT_SYSTEM_PROMPT = (
    "You read a private chat between a service provider's account ('me') and "
    "a client ('them') and decide whether the client has asked for or agreed "
    "to an appointment at one specific date AND time. Answer with a single "
    "JSON object and nothing else:\n"
    '{"booked": true|false, "start": "YYYY-MM-DDTHH:MM", '
    '"duration_minutes": <integer or null>, "title": "<what the appointment '
    'is for, a few words, or empty>", "notes": "<anything the provider '
    'should know, or empty>"}\n'
    "You extract what the CLIENT asked for. Whether 'me' has agreed is "
    "irrelevant — 'me' saying 'let me confirm', 'not yet', 'waiting on "
    "confirmation' or nothing at all does not make it false; the confirmation "
    "is decided elsewhere, from your output.\n"
    "Rules: 'booked' is true only when the client's latest position names a "
    "concrete day and a concrete time of day, OR clearly asks to meet right "
    "away ('now', 'right now', 'I'm ready now', 'ASAP') or at a fixed offset "
    "('in 2 hours', 'in 30 minutes'). For 'now'-style requests set start to "
    "the current time rounded up to the next 5 minutes; for offsets add them "
    "to the current time. A day with no time, a vague "
    "window ('sometime next week', 'in the afternoon', 'later', 'tonight'), "
    "a question about availability with no time picked, or an appointment "
    "the client then cancelled or moved away from are all false. Resolve "
    "relative dates ('tomorrow', 'next Tuesday', 'the 3rd') from the current "
    "date and time given. If the time was changed during the chat, use the "
    "most recent one. A time already agreed stays agreed when only the place, "
    "price or service changes afterwards ('come to mine instead', 'send me the "
    "address') — that is not moving away from the appointment. A bare "
    "duration ('2h') is duration_minutes, not a time. "
    "Use 24-hour local time in the given timezone; never invent a time that "
    "was not said. Set duration_minutes only if a length was mentioned."
)


def extraction_prompt(transcript: str, tz_name: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(tzinfo_for(tz_name))
    return (
        f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({tz_name}).\n"
        "Conversation, oldest first:\n\n" + transcript
    )


def parse_extraction(text: str) -> Optional[dict[str, Any]]:
    """The model's JSON, tolerating a code fence or a sentence around it."""
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("booked"):
        return None
    if not isinstance(data.get("start"), str):
        return None
    return data


def build_booking(
    data: dict[str, Any],
    *,
    tz_name: str,
    default_duration: int,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Fields for BookingStore.add from a parsed extraction, or None if unusable.

    A time already in the past is not a booking — it is the model resolving a
    weekday to last week, or the client recounting something that happened.
    """
    start = parse_local(data.get("start", ""), tz_name)
    if start is None:
        return None
    now = now or datetime.now(tzinfo_for(tz_name))
    if start <= now:
        return None
    minutes = data.get("duration_minutes")
    try:
        minutes = int(minutes) if minutes else default_duration
    except (TypeError, ValueError):
        minutes = default_duration
    minutes = max(5, min(24 * 60, minutes))
    return {
        "start": start.isoformat(timespec="minutes"),
        "end": (start + timedelta(minutes=minutes)).isoformat(timespec="minutes"),
        "timezone": tz_name,
        "title": str(data.get("title") or "").strip()[:120],
        "notes": str(data.get("notes") or "").strip()[:500],
    }


def describe_until(booking: Booking, now: Optional[datetime] = None) -> str:
    """'in about 2 hours' / 'in about 45 minutes' — for the reminder wording."""
    now = now or datetime.now(booking.start_dt().tzinfo)
    minutes = int((booking.start_dt() - now).total_seconds() // 60)
    if minutes < 1:
        return "right about now"
    if minutes < 90:
        return f"in about {minutes} minutes"
    hours = round(minutes / 60)
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


NO_DIRECTIONS_RULE = (
    "Never give the address, directions, door codes or how to get in "
    "yourself — that is sent automatically once they say they have arrived."
)


def context_for_reply(
    bookings: list[Booking],
    news: Optional[Booking] = None,
    news_kind: str = "decision",
    now: Optional[datetime] = None,
) -> str:
    """What the reply-writer must know about this chat's appointments.

    `news` is something that has just happened and the client has not been
    told about — the provider's answer (`news_kind` "decision") or the
    check-in being due ("reminder"): the reply has to carry it. The rest is
    standing context so a pending request is never presented as a done deal.
    """
    lines = []
    if news is not None and news_kind == "reminder":
        lines.append(
            "SEND NOW: their appointment on "
            f"{describe_when(news)} is {describe_until(news, now)}. Ask, briefly "
            "and naturally, whether they are still coming. " + NO_DIRECTIONS_RULE
        )
    elif news is not None:
        if news.status == CONFIRMED:
            lines.append(
                "NEWS TO PASS ON IN THIS REPLY: the appointment on "
                f"{describe_when(news)} has just been CONFIRMED. Tell them it is "
                "booked, restating the day and time."
            )
        elif news.status == DECLINED:
            lines.append(
                "NEWS TO PASS ON IN THIS REPLY: the appointment on "
                f"{describe_when(news)} is NOT available after all. Say so "
                "politely and ask what other day or time would suit them."
            )
    for booking in bookings:
        if news is not None and booking.id == news.id:
            continue
        if booking.status == PENDING:
            lines.append(
                f"An appointment on {describe_when(booking)} has been requested "
                "and is waiting for confirmation. Do NOT say it is confirmed or "
                "booked. If they ask, say you are checking and will confirm shortly."
            )
        elif booking.status == CONFIRMED:
            line = f"The appointment on {describe_when(booking)} is confirmed."
            if booking.instructions_sent_at:
                line += " They have arrived and were already sent the entry instructions."
            elif booking.reminder_sent:
                line += (
                    " You have already asked whether they are coming. If they say "
                    "they have arrived, just acknowledge briefly. " + NO_DIRECTIONS_RULE
                )
            lines.append(line)
    if not lines:
        return ""
    return "APPOINTMENTS:\n" + "\n".join(lines)


ARRIVAL_SYSTEM_PROMPT = (
    "You read the end of a private chat between a service provider ('me') and "
    "a client ('them') who has an appointment about now. Decide whether the "
    "client's LATEST message says they have physically arrived at the meeting "
    "place — for example 'I'm here', 'at the door', 'downstairs', 'я на месте', "
    "'estoy aquí', 'just parked outside'. Being on the way, running late, or "
    "asking where to go is NOT arrival. Answer with a single JSON object and "
    'nothing else: {"arrived": true|false}'
)


def parse_arrival(text: str) -> bool:
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return False
    try:
        return bool(json.loads(match.group(0)).get("arrived"))
    except (json.JSONDecodeError, AttributeError):
        return False
