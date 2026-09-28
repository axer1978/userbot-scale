"""The words around bookings: what goes to the owner, what the owner can
answer, what the model is asked to extract from a chat, and what the reply
writer is told about a chat's bookings.

The owner is reached only through the tenant's own Telegram account, by
text (there is no bot with buttons). They answer:

    YES 7             confirm #7 (or accept the new time the customer asked for)
    NO 7              decline #7 (or keep the old time the customer wanted to move)
    7 15:30           propose a different time for #7 (same day)
    7 04.10 15:30     ... on another day; also "7 tomorrow 15:30", "7 2026-10-04 15:30"
    CANCEL 7          cancel #7
    DONE 7 / NOSHOW 7 after the slot: it happened / they did not come
    LIST              what is waiting for an answer

Nothing here touches the database or the network; session_runtime does the
wiring, booking_states decides what is allowed, booking_store writes it.
Bookings are the dicts booking_store returns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

import booking_states as bs

# ------------------------------------------------------------ time handling


def tzinfo_for(name: str) -> Any:
    """The IANA zone from the config, or UTC when it cannot be loaded."""
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def parse_local(value: str, tz_name: str) -> Optional[datetime]:
    """A naive 'YYYY-MM-DDTHH:MM' from the model, placed in the given zone."""
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


def plus_minutes(start: datetime, minutes: int) -> datetime:
    """start + minutes of real time (in UTC, so a clock change in between
    does not stretch or shrink the booking), back in start's zone."""
    return (start.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(start.tzinfo)


def describe_span(starts_at: datetime, ends_at: datetime, tz_name: str) -> str:
    """'Tue 23 Sep 2026, 15:00–16:00' in the tenant's zone, for people."""
    zone = tzinfo_for(tz_name)
    start, end = starts_at.astimezone(zone), ends_at.astimezone(zone)
    day = start.strftime("%a %d %b %Y")
    if end.date() == start.date():
        return f"{day}, {start:%H:%M}–{end:%H:%M}"
    return f"{day}, {start:%H:%M} – {end:%a %d %b %H:%M}"


def describe_when(booking: Mapping[str, Any]) -> str:
    return describe_span(booking["starts_at"], booking["ends_at"], booking["tz"])


def describe_proposal(booking: Mapping[str, Any]) -> str:
    return describe_span(booking["proposed_starts_at"], booking["proposed_ends_at"], booking["tz"])


def describe_until(starts_at: datetime, now: datetime) -> str:
    """'in about 2 hours' / 'in about 45 minutes'."""
    minutes = int((starts_at - now).total_seconds() // 60)
    if minutes < 1:
        return "right about now"
    if minutes < 90:
        return f"in about {minutes} minutes"
    hours = round(minutes / 60)
    if hours < 36:
        return f"in about {hours} hour{'s' if hours != 1 else ''}"
    days = round(hours / 24)
    return f"in about {days} days"


# ------------------------------------------------ messages to the owner

ANSWER_HELP = "Reply YES {n} or NO {n}, or a new time like {n} 15:30 or {n} 04.10 15:30."


def _who(booking: Mapping[str, Any]) -> str:
    who = booking.get("customer_name") or "Unknown customer"
    if booking.get("customer_username"):
        who += f" (@{booking['customer_username']})"
    return who


def format_request(booking: Mapping[str, Any], *, changed: bool = False) -> str:
    """A new request, or one whose time the customer changed before you answered."""
    n = booking["number"]
    head = f"📅 Booking request #{n}" + (" (new time)" if changed else "")
    lines = [head, f"Customer: {_who(booking)}", f"When: {describe_when(booking)}"]
    if booking.get("service"):
        lines.append(f"What: {booking['service']}")
    if booking.get("notes"):
        lines.append(f"Notes: {booking['notes']}")
    lines += ["", ANSWER_HELP.format(n=n)]
    return "\n".join(lines)


def format_move_request(booking: Mapping[str, Any]) -> str:
    """The customer asks to move a confirmed booking."""
    n = booking["number"]
    return "\n".join([
        f"🔁 #{n}: {_who(booking)} asks to move their booking",
        f"From: {describe_when(booking)}",
        f"To:   {describe_proposal(booking)}",
        "",
        f"Reply YES {n} to move it, NO {n} to keep the old time, or a different time like {n} 15:30.",
    ])


def format_owner_update(booking: Mapping[str, Any], what: str) -> str:
    """Something the owner should know that they did not do themselves."""
    return f"#{booking['number']} {_who(booking)}, {describe_when(booking)}: {what}"


def format_acknowledgement(booking: Mapping[str, Any], action: str) -> str:
    n, who = booking["number"], booking.get("customer_name") or "the customer"
    if action == "propose":
        return f"#{n}: {describe_proposal(booking)} proposed to {who}. You'll hear back when they answer."
    words = {
        "confirm": f"confirmed ✅ — {who} will be told.",
        "accept_proposal": f"confirmed ✅ — {who} will be told.",
        "reschedule": f"moved to {describe_when(booking)} ✅ — {who} will be told.",
        "decline": f"declined ❌ — {who} will be told.",
        "reject_proposal": f"stays at {describe_when(booking)} — {who} will be told.",
        "cancel": f"cancelled ❌ — {who} will be told.",
        "mark_completed": "marked as done.",
        "mark_no_show": "marked as missed.",
    }
    return f"#{n} {words.get(action, action)}"


def format_list(bookings: Sequence[Mapping[str, Any]]) -> str:
    if not bookings:
        return "Nothing is waiting for your answer right now."
    lines = ["Waiting for your answer:"]
    for b in bookings:
        if b["state"] == bs.CONFIRMED and b.get("proposed_by") == bs.CUSTOMER:
            lines.append(f"  #{b['number']} — {_who(b)} wants to move {describe_when(b)} → {describe_proposal(b)}")
        elif b.get("proposed_by") == bs.OWNER:
            lines.append(f"  #{b['number']} — {_who(b)}, you proposed {describe_proposal(b)} (waiting for them)")
        else:
            lines.append(f"  #{b['number']} — {_who(b)}, {describe_when(b)}")
    lines += ["", "Reply YES <number>, NO <number>, or <number> <new time>."]
    return "\n".join(lines)


def format_which(kind: str) -> str:
    return f"Which booking? Add its number, e.g. {kind.upper()} 7. Send LIST to see them."


# -------------------------------------------------- the owner's answer

_YES = {
    "yes", "y", "yep", "yeah", "ok", "okay", "confirm", "confirmed", "accept", "accepted",
    "approve", "approved", "sure", "✅", "👍", "da", "да", "jā", "ja", "si", "sí", "oui", "tak", "sim",
}
_NO = {
    "no", "n", "nope", "decline", "declined", "reject", "rejected", "deny", "denied", "busy",
    "❌", "👎", "нет", "nē", "ne", "nein", "non", "nie", "não", "nu",
}
_CANCEL = {"cancel", "atcelt", "отмена", "отменить"}
_DONE = {"done", "completed", "notika", "готово"}
_NOSHOW = {"noshow", "no-show", "missed", "neieradās", "неявка"}
_LIST = {"list", "?", "saraksts", "список", "pending"}
_PROPOSE = {"time", "propose", "move", "laiks", "время", "перенести"}
_TODAY = {"today", "šodien", "sodien", "сегодня"}
_TOMORROW = {"tomorrow", "rīt", "rit", "завтра"}

_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class OwnerCommand:
    kind: str                      # yes | no | propose | cancel | done | noshow | list
    number: Optional[int] = None
    # For propose: the wall time and, if given, the day ("today"/"tomorrow"
    # already resolved against `today`).
    at: Optional[dtime] = None
    day: Optional[date] = None
    error: str = ""


def _word(token: str) -> str:
    return token.strip().strip("!,;:").lower()


def _number(token: str) -> Optional[int]:
    token = token.strip().lstrip("#").rstrip(".,;:")
    return int(token) if token.isdigit() and len(token) <= 7 else None


def _parse_when(tokens: Sequence[str], today: date) -> tuple[Optional[dtime], Optional[date], str]:
    """A time (HH:MM, or HH.MM) and optionally a day. "04.10 15:30" is a
    date and a time: a token with a colon is always the time, and a dotted
    one is the time only when nothing else is."""
    words = [_word(raw) for raw in tokens if _word(raw)]
    colon = [w for w in words if ":" in w and _TIME.match(w)]
    if colon:
        time_word = colon[0]
    else:
        dotted = [w for w in words if _TIME.match(w)]
        time_word = dotted[-1] if dotted else None
    if time_word is None:
        if any(not (_DMY.match(w) or _ISO.match(w) or w in _TODAY or w in _TOMORROW) for w in words):
            return None, None, f"I did not understand {' '.join(tokens)!r}"
        return None, None, "no time given (use HH:MM)"
    m = _TIME.match(time_word)
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None, None, f"{time_word} is not a time"
    at = dtime(hour, minute)
    day: Optional[date] = None
    rest = list(words)
    rest.remove(time_word)
    for token in rest:
        if token in _TODAY:
            day = today
        elif token in _TOMORROW:
            day = today + timedelta(days=1)
        elif m := _ISO.match(token):
            try:
                day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None, None, f"{token} is not a date"
        elif m := _DMY.match(token):
            dd, mm, yy = int(m.group(1)), int(m.group(2)), m.group(3)
            year = (int(yy) + (2000 if len(yy) == 2 else 0)) if yy else today.year
            try:
                day = date(year, mm, dd)
            except ValueError:
                return None, None, f"{token} is not a date"
            # "02.01" written in the autumn means next January.
            if not yy and day < today - timedelta(days=1):
                day = date(year + 1, mm, dd)
        else:
            return None, None, f"I did not understand {token!r}"
    return at, day, ""


def parse_owner_reply(text: str, *, today: date) -> Optional[OwnerCommand]:
    """What the owner wrote, as a command, or None when it is not one (then
    it is an ordinary message and gets an ordinary reply)."""
    tokens = (text or "").strip().split()
    if not tokens:
        return None
    head = _word(tokens[0])

    if head in _LIST and len(tokens) == 1:
        return OwnerCommand("list")

    # "7 15:30", "7 04.10 15:30"
    if (n := _number(tokens[0])) is not None and len(tokens) >= 2:
        at, day, error = _parse_when(tokens[1:], today)
        if at is None and error.startswith("I did not"):
            return None  # "7 people are coming" is not a command
        return OwnerCommand("propose", n, at, day, error)

    if head in _PROPOSE and len(tokens) >= 3 and (n := _number(tokens[1])) is not None:
        at, day, error = _parse_when(tokens[2:], today)
        return OwnerCommand("propose", n, at, day, error)

    kind = ("yes" if head in _YES else "no" if head in _NO else "cancel" if head in _CANCEL
            else "done" if head in _DONE else "noshow" if head in _NOSHOW else None)
    if kind is None:
        return None
    rest = tokens[1:]
    number = _number(rest[0]) if rest else None
    if rest and number is None and kind in ("yes", "no"):
        # "yes, see you then" to a single request is still a yes; anything
        # longer that does not start with a number is conversation.
        if len(rest) > 3:
            return None
    return OwnerCommand(kind, number)


def resolve_local(cmd: OwnerCommand, booking: Mapping[str, Any]) -> datetime:
    """The proposed start as an aware datetime in the booking's zone. No day
    given = the day the booking is on now."""
    zone = tzinfo_for(booking["tz"])
    day = cmd.day or booking["starts_at"].astimezone(zone).date()
    return datetime.combine(day, cmd.at, tzinfo=zone)


# ------------------------------------------------------- the customer's side

REMINDER_YES = {"1", "1.", "yes", "jā", "да", "👍", "✅"}
REMINDER_NO = {"2", "2."}


def reminder_answer(text: str) -> Optional[str]:
    """A bare 1 / 2 after a reminder: 'confirm' or 'cancel'. Anything else
    goes through the normal extraction."""
    word = (text or "").strip().lower()
    if word in REMINDER_YES:
        return "confirm"
    if word in REMINDER_NO:
        return "cancel"
    return None


EXTRACT_SYSTEM_PROMPT = (
    "You read a private chat between a business's account ('me') and a "
    "customer ('them') and report what the CUSTOMER wants regarding an "
    "appointment in their LATEST messages. Answer with a single JSON object "
    "and nothing else:\n"
    '{"intent": "none"|"book"|"cancel"|"accept_proposal"|"decline_proposal"|'
    '"confirm_attendance"|"waitlist", "start": "YYYY-MM-DDTHH:MM" or null, '
    '"duration_minutes": <integer or null>, "service": "<a few words or empty>", '
    '"notes": "<anything the business should know, or empty>", '
    '"waitlist_from": "YYYY-MM-DDTHH:MM" or null, "waitlist_to": "YYYY-MM-DDTHH:MM" or null}\n'
    "Intents:\n"
    "- book: they ask for or agree to one concrete day AND time of day (or "
    "'now'/'ASAP' = current time rounded up to 5 minutes, or a fixed offset "
    "like 'in 2 hours'). Also when they ask to MOVE an existing booking to a "
    "concrete new time. Put that time in start.\n"
    "- cancel: they clearly call off their booking.\n"
    "- accept_proposal / decline_proposal: the business proposed a different "
    "time (see 'Their bookings') and they say yes / no to it. If they answer "
    "with yet another concrete time, that is book.\n"
    "- confirm_attendance: after a reminder, they say they are still coming.\n"
    "- waitlist: they ask to be told if a time frees up; give the range they "
    "would take in waitlist_from/waitlist_to.\n"
    "- none: anything else, including a day without a time, a vague window, "
    "questions about availability, or what they said earlier and then moved away from.\n"
    "Resolve relative dates from the current date and time given. Use 24-hour "
    "local time in the given timezone; never invent a time that was not said. "
    "Whether 'me' agreed is irrelevant: confirmation is decided elsewhere."
)


def extraction_prompt(
    transcript: str, tz_name: str, now: Optional[datetime] = None, state_note: str = "",
) -> str:
    now = now or datetime.now(tzinfo_for(tz_name))
    parts = [f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({tz_name})."]
    parts.append("Their bookings: " + (state_note or "none."))
    parts.append("Conversation, oldest first:\n\n" + transcript)
    return "\n".join(parts)


INTENTS = ("none", "book", "cancel", "accept_proposal", "decline_proposal", "confirm_attendance", "waitlist")


def parse_extraction(text: str) -> Optional[dict[str, Any]]:
    """The model's JSON, tolerating a code fence or a sentence around it.
    None for no usable intent."""
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in INTENTS or intent == "none":
        return None
    if intent == "book" and not isinstance(data.get("start"), str):
        return None
    return data


def state_note(bookings: Sequence[Mapping[str, Any]]) -> str:
    """The chat's live bookings, for the extraction prompt."""
    lines = []
    for b in bookings:
        line = f"#{b['number']} {b['state']} for {describe_when(b)}"
        if b.get("proposed_by") == bs.OWNER:
            line += f"; the business proposed {describe_proposal(b)} instead"
        elif b.get("proposed_by") == bs.CUSTOMER:
            line += f"; they asked to move it to {describe_proposal(b)}"
        lines.append(line)
    return "; ".join(lines)


def requested_slot(
    data: Mapping[str, Any], *, tz_name: str, default_duration: int,
) -> Optional[tuple[datetime, datetime]]:
    start = parse_local(str(data.get("start") or ""), tz_name)
    if start is None:
        return None
    minutes = data.get("duration_minutes")
    try:
        minutes = int(minutes) if minutes else default_duration
    except (TypeError, ValueError):
        minutes = default_duration
    minutes = max(5, min(24 * 60, minutes))
    return start, plus_minutes(start, minutes)


# ----------------------------------------------- what the reply writer knows

NO_DIRECTIONS_RULE = (
    "Never give the address, directions, door codes or how to get in "
    "yourself — that is sent automatically once they have arrived."
)

_NOTICE_TEXT = {
    bs.NOTICE_CONFIRMED: "the appointment on {when} has just been CONFIRMED. Tell them it is booked, restating the day and time.",
    bs.NOTICE_DECLINED: "the appointment on {when} is NOT available after all. Say so politely and ask what other day or time would suit them.",
    bs.NOTICE_PROPOSED: "the requested time does not work; the business proposes {proposal} instead. Ask whether that suits them.",
    bs.NOTICE_RESCHEDULED: "the appointment has been MOVED; it is now on {when}. Tell them, restating the new day and time.",
    bs.NOTICE_CANCELLED: "the appointment on {when} has been CANCELLED. Tell them so.",
    bs.NOTICE_EXPIRED: "the request for {when} could not be confirmed in time and has lapsed. Apologise briefly and offer to find another time.",
    bs.NOTICE_REQUESTED: "their request for {when} has been passed on; it is NOT confirmed yet. Say it is being checked.",
    bs.NOTICE_KEPT: "the appointment could not be moved; it stays on {when}. Tell them so.",
}


def notice_line(booking: Mapping[str, Any]) -> str:
    template = _NOTICE_TEXT.get(booking.get("customer_notice") or "")
    if not template:
        return ""
    proposal = describe_proposal(booking) if booking.get("proposed_starts_at") else ""
    return "NEWS TO PASS ON IN THIS REPLY: " + template.format(when=describe_when(booking), proposal=proposal)


def standing_line(booking: Mapping[str, Any]) -> str:
    when = describe_when(booking)
    state = booking["state"]
    if state in (bs.REQUESTED, bs.PENDING):
        if booking.get("proposed_by") == bs.OWNER:
            return (f"The business proposed {describe_proposal(booking)} instead of {when}; waiting for "
                    "their answer. Do NOT say anything is confirmed.")
        return (f"An appointment on {when} has been requested and is waiting for confirmation. Do NOT "
                "say it is confirmed or booked. If they ask, say it is being checked.")
    if state == bs.CONFIRMED:
        line = f"The appointment on {when} is confirmed."
        if booking.get("proposed_by") == bs.CUSTOMER:
            line += f" They asked to move it to {describe_proposal(booking)}; that is NOT confirmed yet."
        elif booking.get("proposed_by") == bs.OWNER:
            line += f" The business proposed moving it to {describe_proposal(booking)}; waiting for their answer."
        if booking.get("instructions_sent_at"):
            line += " They have arrived and were already sent the entry instructions."
        else:
            line += " " + NO_DIRECTIONS_RULE
        return line
    return ""


def unavailable_line(requested: datetime, reason: str, alternatives: Sequence[datetime], tz_name: str,
                     waitlist: bool) -> str:
    zone = tzinfo_for(tz_name)
    when = requested.astimezone(zone).strftime("%a %d %b %H:%M")
    why = {
        "past": "that time has already passed",
        "too_soon": "that is too short notice",
        "too_far": "that is too far ahead to book yet",
        "closed_day": "the business is closed that day",
        "outside_hours": "that is outside opening hours",
        "conflict": "that time is already taken",
    }.get(reason, "that time is not available")
    line = f"NEWS TO PASS ON IN THIS REPLY: they asked for {when}, but {why}. It is NOT booked."
    if alternatives:
        options = ", ".join(a.astimezone(zone).strftime("%a %d %b %H:%M") for a in alternatives)
        line += f" Offer these free times instead: {options}."
    if waitlist:
        line += " If none suits them, offer to put them on the waitlist and tell them if a time frees up."
    return line


def reminder_line(booking: Mapping[str, Any], instruction: str, now: datetime, page_url: str = "") -> str:
    when = describe_when(booking)
    base = (instruction or "").strip() or "Ask, briefly and naturally, whether they are still coming."
    line = (f"SEND NOW: their appointment on {when} is {describe_until(booking['starts_at'], now)}. "
            f"{base} Say they can reply 1 to confirm or 2 to cancel.")
    if page_url:
        line += f" Include this link to their booking exactly as written: {page_url}"
    return line + " " + NO_DIRECTIONS_RULE


def waitlist_offer_line(starts_at: datetime, ends_at: datetime, tz_name: str) -> str:
    return (f"NEWS TO PASS ON IN THIS REPLY: a time they were waiting for has freed up: "
            f"{describe_span(starts_at, ends_at, tz_name)}. Ask whether they want it. It is NOT booked until they say yes.")


def context_for_reply(lines: Sequence[str]) -> str:
    lines = [line for line in lines if line]
    return "APPOINTMENTS:\n" + "\n".join(lines) if lines else ""


# ---------------------------------------------------------------- arrival

ARRIVAL_SYSTEM_PROMPT = (
    "You read the end of a private chat between a business ('me') and a "
    "customer ('them') who has an appointment about now. Decide whether the "
    "customer's LATEST message says they have physically arrived at the meeting "
    "place — for example 'I'm here', 'at the door', 'downstairs', 'я на месте', "
    "'esmu klāt', 'just parked outside'. Being on the way, running late, or "
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


def arrival_window(booking: Mapping[str, Any], now: datetime) -> bool:
    """'I'm here' counts from an hour before the start until half an hour
    after the end, so the same words the day before are not an arrival."""
    return booking["starts_at"] - timedelta(hours=1) <= now <= booking["ends_at"] + timedelta(minutes=30)
