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
    READY 7           outcall #7: she is ready, he orders the taxi
    IN 7 / TAXI 7     outcall #7: she is in the taxi, he sends the way to him

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


def _named(name: str) -> str:
    """Her name ahead of a message to the owner (booking.name, else the
    account's name), so an owner answering for several accounts sees whose
    booking it is."""
    return f"{name} — " if name else ""


def format_request(booking: Mapping[str, Any], *, changed: bool = False, name: str = "") -> str:
    """A new request, or one whose time the customer changed before you
    answered: who asks, for whom (her name, else the account's), the extras
    he asked for, when."""
    n = booking["number"]
    head = f"📅 Booking request #{n}" + (" (new time)" if changed else "")
    lines = [head, f"Customer: {_who(booking)}"]
    if name:
        lines.append(f"Girl: {name}")
    lines.append(f"Extras: {booking.get('extras') or 'none mentioned'}")
    lines.append(f"When: {describe_when(booking)}")
    if booking.get("service"):
        lines.append(f"What: {booking['service']}")
    if booking.get("notes"):
        lines.append(f"Notes: {booking['notes']}")
    lines += ["", ANSWER_HELP.format(n=n)]
    return "\n".join(lines)


def format_move_request(booking: Mapping[str, Any], *, name: str = "") -> str:
    """The customer asks to move a confirmed booking."""
    n = booking["number"]
    return "\n".join([
        f"🔁 {_named(name)}#{n}: {_who(booking)} asks to move their booking",
        f"From: {describe_when(booking)}",
        f"To:   {describe_proposal(booking)}",
        "",
        f"Reply YES {n} to move it, NO {n} to keep the old time, or a different time like {n} 15:30.",
    ])


def format_owner_update(booking: Mapping[str, Any], what: str, *, name: str = "") -> str:
    """Something the owner should know that they did not do themselves."""
    return f"{_named(name)}#{booking['number']} {_who(booking)}, {describe_when(booking)}: {what}"


def format_acknowledgement(booking: Mapping[str, Any], action: str, *, outcall_taxi: bool = False) -> str:
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
        "ready": f"ready ✅ — {who} is asked to order the taxi to your address and send its link. It will come here.",
        "in_taxi": f"in the taxi ✅ — {who} is asked for the address, floor, way in and a photo of the place. "
                   "They will come here.",
    }
    text = f"#{n} {words.get(action, action)}"
    if outcall_taxi and action in ("confirm", "accept_proposal") and is_outcall(booking):
        text += f" It's an outcall: send READY {n} when you're ready, and {who} is told to order the taxi."
    return text


def outcall_step(booking: Mapping[str, Any]) -> str:
    """Where a confirmed outcall's taxi steps stand, for the owner."""
    n = booking["number"]
    if booking.get("in_taxi_at"):
        return "in the taxi"
    if booking.get("taxi_link_at"):
        return f"taxi link sent — send IN {n} when you're in"
    if booking.get("ready_at"):
        return "ready — waiting for his taxi link"
    return f"send READY {n} when you're ready"


def format_list(bookings: Sequence[Mapping[str, Any]], outcalls: Sequence[Mapping[str, Any]] = ()) -> str:
    if not bookings and not outcalls:
        return "Nothing is waiting for your answer right now."
    lines = []
    if bookings:
        lines.append("Waiting for your answer:")
    for b in bookings:
        if b["state"] == bs.CONFIRMED and b.get("proposed_by") == bs.CUSTOMER:
            lines.append(f"  #{b['number']} — {_who(b)} wants to move {describe_when(b)} → {describe_proposal(b)}")
        elif b.get("proposed_by") == bs.OWNER:
            lines.append(f"  #{b['number']} — {_who(b)}, you proposed {describe_proposal(b)} (waiting for them)")
        else:
            lines.append(f"  #{b['number']} — {_who(b)}, {describe_when(b)}")
    if bookings:
        lines += ["", "Reply YES <number>, NO <number>, or <number> <new time>."]
    if outcalls:
        if lines:
            lines.append("")
        lines.append("Outcalls:")
        lines += [f"  #{b['number']} — {_who(b)}, {describe_when(b)}: {outcall_step(b)}" for b in outcalls]
    return "\n".join(lines)


# ------------------------------------------------- outcalls, to the owner


def format_ready_reminder(booking: Mapping[str, Any]) -> str:
    n = booking["number"]
    return (f"🚕 #{n} {_who(booking)}, {describe_when(booking)} (outcall): send READY {n} when you're "
            "ready — then he orders the taxi.")


def format_taxi_link(booking: Mapping[str, Any], text: str, in_taxi_minutes: Optional[int]) -> str:
    """The customer's message with the taxi link. `in_taxi_minutes`: when
    he is told she is in the taxi without her IN n; None when no IN n is
    wanted (he knows already, or is told at once)."""
    n = booking["number"]
    head = f"🚕 #{n} {_who(booking)} sent the taxi link:\n{text}"
    if in_taxi_minutes is None:
        return head
    return (f"{head}\n\nSend IN {n} when you're in the taxi (if you don't, he is told you're in it in "
            f"{in_taxi_minutes} min).")


def format_passed_on(booking: Mapping[str, Any], text: str) -> str:
    """What the customer wrote while the outcall is under way, word for word."""
    return f"💬 #{booking['number']} {_who(booking)}:\n{text}"


def format_photo_header(booking: Mapping[str, Any], caption: str = "") -> str:
    head = f"📷 #{booking['number']} {_who(booking)} sent a photo"
    return f"{head}:\n{caption}" if caption else f"{head}:"


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
_READY = {"ready", "gatava", "gatavs", "готова", "готов"}
_TAXI = {"taxi", "taksi", "taksī", "такси"}
_IN = {"in", "в", "iekšā"}

_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class OwnerCommand:
    # yes | no | propose | cancel | done | noshow | list | ready | in_taxi,
    # and from the reader of her own words: unclear | stays
    kind: str
    number: Optional[int] = None
    # For propose: the wall time and, if given, the day ("today"/"tomorrow"
    # already resolved against `today`).
    at: Optional[dtime] = None
    day: Optional[date] = None
    error: str = ""
    # A YES/NO with words after it ("ok in 20 min", "yes after 3"): the
    # words may change what it means, so the reader of her own words decides
    # when it can, and this stands only when it can't.
    loose: bool = False


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

    # The taxi steps of an outcall: "READY 7", "IN 7", "TAXI 7", "IN TAXI 7",
    # or the word alone for the only outcall under way. Anything after it
    # but the number is conversation ("in 10 min", "ready when you are").
    step, rest = None, tokens[1:]
    if head in _READY:
        step = "ready"
    elif head in _TAXI:
        step = "in_taxi"
    elif head in _IN and rest and _word(rest[0]) in _TAXI:
        step, rest = "in_taxi", rest[1:]
    elif head == "in":
        step = "in_taxi"
    if step is not None:
        if not rest:
            return OwnerCommand(step)
        if len(rest) == 1 and (number := _number(rest[0])) is not None:
            return OwnerCommand(step, number)
        return None

    kind =("yes" if head in _YES else "no" if head in _NO else "cancel" if head in _CANCEL
            else "done" if head in _DONE else "noshow" if head in _NOSHOW else None)
    if kind is None:
        return None
    rest = tokens[1:]
    number = _number(rest[0]) if rest else None
    loose = len(rest) > (1 if number is not None else 0)
    if rest and number is None and kind in ("yes", "no"):
        # "yes, see you then" to a single request is still a yes; anything
        # longer that does not start with a number is conversation.
        if len(rest) > 3:
            return None
    return OwnerCommand(kind, number, loose=loose and kind in ("yes", "no"))


def resolve_local(cmd: OwnerCommand, booking: Mapping[str, Any]) -> datetime:
    """The proposed start as an aware datetime in the booking's zone. No day
    given = the day the booking is on now."""
    zone = tzinfo_for(booking["tz"])
    day = cmd.day or booking["starts_at"].astimezone(zone).date()
    return datetime.combine(day, cmd.at, tzinfo=zone)


# ------------------------------------ the owner's answer in her own words

OWNER_ANSWER_SYSTEM_PROMPT = (
    "You read the chat between a booking assistant ('me') and the person who "
    "takes its bookings ('them': the owner, or the girl herself). 'me' sends "
    "them the customers' booking requests; they answer, often in their own "
    "words rather than with a command. Decide what their LATEST message means "
    "for the bookings listed, given what each customer asked for. Answer with "
    "a single JSON object and nothing else:\n"
    '{"action": "yes"|"no"|"ready_at"|"time"|"cancel"|"done"|"noshow"|"ready"|'
    '"in_taxi"|"list"|"unclear"|"none", "number": <booking number or null>, '
    '"start": "YYYY-MM-DDTHH:MM" or null, "in_minutes": <integer or null>}\n'
    "Actions:\n"
    "- yes: they take the booking at the time the customer asked: 'yes', 'ok', "
    "'yes I confirm', 'sure, let him come', 'fine', 'da', 'jā', '👍'. For a "
    "customer asking to move a booking: they agree to the new time.\n"
    "- no: they turn it down and give no other time: 'no', 'not today', "
    "'I'm busy', 'can't'.\n"
    "- ready_at: they say WHEN they will be ready or free, instead of a plain "
    "yes: 'I'm ready in 20 minutes', 'give me half an hour', 'I will be able "
    "to meet in 1 hour', 'free after 15:00', 'yes but in 40 min'. A delay goes "
    "in in_minutes (counted from their latest message), a clock time in start.\n"
    "- time: they put forward a different meeting time: 'can he come at "
    "15:30?', 'tomorrow 12:00 instead', 'better at 8'. Put it in start.\n"
    "- cancel: they call off a booking that is already confirmed.\n"
    "- done / noshow: after the meeting: it happened / the customer did not come.\n"
    "- ready: an outcall, and they are ready to leave NOW ('I'm ready', "
    "'ready, he can order the taxi'). 'Ready in 20 minutes' is ready_at.\n"
    "- in_taxi: an outcall, and they are in the taxi now.\n"
    "- list: they ask what is waiting.\n"
    "- unclear: it is about a booking but you cannot tell which answer it is.\n"
    "- none: not about the bookings: a greeting, thanks, a question, chat.\n"
    "number: the booking it is about: the one their message replies to (when "
    "given below), the one they name by number or customer name, else the "
    "only one waiting for their answer. null when you cannot tell.\n"
    "Use 24-hour local time in the given timezone and resolve 'today', "
    "'tomorrow' and bare hours ('at 8' in the afternoon is 20:00) from the "
    "current date and time; never invent a time that was not said."
)

OWNER_ACTIONS = ("yes", "no", "ready_at", "time", "cancel", "done", "noshow", "ready", "in_taxi", "list",
                 "unclear", "none")


def owner_state_note(rows: Sequence[Mapping[str, Any]], *, outcall_taxi: bool = False) -> str:
    """The bookings she may be answering about, for the reader of her words."""
    lines = []
    for b in rows:
        if b["state"] == bs.CONFIRMED and b.get("proposed_by") == bs.CUSTOMER:
            what = (f"confirmed for {describe_when(b)}; the customer asks to move it to "
                    f"{describe_proposal(b)}, waiting for their answer")
        elif b.get("proposed_by") == bs.OWNER:
            what = (f"the customer asked for {describe_when(b)}; they proposed {describe_proposal(b)} instead, "
                    "waiting for the customer")
        elif b["state"] in (bs.REQUESTED, bs.PENDING):
            what = f"the customer asks for {describe_when(b)}, waiting for their answer"
        else:
            what = f"confirmed for {describe_when(b)}"
            if outcall_taxi and is_outcall(b):
                what += f"; outcall: {outcall_step(b)}"
        if is_outcall(b) and "outcall" not in what:
            what += " (outcall)"
        extras = f"; extras: {b['extras']}" if b.get("extras") else ""
        lines.append(f"#{b['number']} {b.get('customer_name') or 'unknown customer'}: {what}{extras}")
    return "\n".join(lines)


def owner_answer_prompt(
    transcript: str, tz_name: str, *, now: datetime, state_note: str, latest: str,
    replying_to: Optional[int] = None,
) -> str:
    parts = [f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({tz_name}).",
             "Bookings:\n" + (state_note or "none."),
             "Conversation, oldest first:\n\n" + transcript]
    if replying_to is not None:
        parts.append(f"\nTheir latest message replies to the message about booking #{replying_to}.")
    parts.append("Their latest message, the one to decide on: " + " ".join(latest.split()))
    return "\n".join(parts)


def parse_owner_answer(text: str) -> Optional[dict[str, Any]]:
    """The reader's JSON, cleaned: {"action", "number", "start",
    "in_minutes"}. None when it is not about the bookings or unusable."""
    match = re.search(r"\{.*\}", (text or "").strip(), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("action") not in OWNER_ACTIONS or data["action"] == "none":
        return None
    number = data.get("number")
    if isinstance(number, str):
        number = _number(number)
    minutes = data.get("in_minutes")
    if isinstance(minutes, float) and minutes.is_integer():
        minutes = int(minutes)
    start = data.get("start")
    return {
        "action": data["action"],
        "number": number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None,
        "start": start if isinstance(start, str) and start.strip() else None,
        "in_minutes": minutes if isinstance(minutes, int) and not isinstance(minutes, bool)
        and 0 <= minutes <= 24 * 60 else None,
    }


def ready_time(at: datetime) -> datetime:
    """When she says she will be ready, as a start: that minute or the next
    five-minute mark after it."""
    if at.second or at.microsecond:
        at = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return at + timedelta(minutes=-at.minute % 5)


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
    '"asap": true|false, "outcall": true|false, '
    '"duration_minutes": <integer or null>, "service": "<a few words or empty>", '
    '"extras": "<the extras they asked for, or empty>", '
    '"notes": "<anything the business should know, or empty>", '
    '"waitlist_from": "YYYY-MM-DDTHH:MM" or null, "waitlist_to": "YYYY-MM-DDTHH:MM" or null}\n'
    "Intents:\n"
    "- book: they ask for or agree to one concrete day AND time of day, or a "
    "fixed offset like 'in 2 hours'. Also when they ask to MOVE an existing "
    "booking to a concrete new time. Put that time in start.\n"
    "  RIGHT NOW is book too, with asap true and start null (the time is worked "
    "out elsewhere): they want to meet now or as soon as possible — 'now', "
    "'now?', 'rn', 'asap', 'can I come now', 'are you free now', 'available "
    "now?', 'I'm close, can I come' — or they ask a bare 'free?', 'are you "
    "free?', 'available?' without naming any day or time. asap is false for "
    "everything else.\n"
    "- cancel: they clearly call off their booking.\n"
    "- accept_proposal / decline_proposal: the business proposed a different "
    "time (see 'Their bookings') and they say yes / no to it. If they answer "
    "with yet another concrete time, that is book.\n"
    "- confirm_attendance: after a reminder, they say they are still coming.\n"
    "- waitlist: they ask to be told if a time frees up; give the range they "
    "would take in waitlist_from/waitlist_to.\n"
    "- none: anything else, including a day without a time ('are you free "
    "tomorrow?'), a vague window ('tonight', 'later'), or what they said "
    "earlier and then moved away from. Also none when their latest message "
    "names no time of its own: a nudge while they wait ('??', 'so?', 'is it "
    "ok?', 'hello?'), thanks, a plain 'ok', or a question with no time in it. "
    "A length alone ('1 hour?', '30 min incall?') is not a time. A time "
    "already in 'Their bookings' is never a new request unless they ask to "
    "change it: their latest message following up on a request that is "
    "already there (a nudge, a question, thanks, 'already on my way') is none, "
    "also when the request was 'now' or 'in 30 min'. A message that does name "
    "a time ('tonight 22:00 ok?', 'tomorrow at 14:00?') is book, question mark "
    "or not.\n"
    "outcall: true when they want the business to come to them (their hotel, "
    "their place, 'outcall'), false otherwise.\n"
    "extras: the extra services on top of the basic meeting that they asked "
    "for or said they want anywhere in the conversation, in a few words each, "
    "comma-separated, with the price when the chat named one ('domination "
    "+50, cum on face +50'). Not the length, the place, incall/outcall or the "
    "rate. Leave out what they asked about and then dropped, or what 'me' "
    "said is not offered. Empty when none.\n"
    "Resolve relative dates from the current date and time given; an offset "
    "('in 40 min') counts from the message that said it. Use 24-hour local time "
    "in the given timezone; never invent a time that was not said. Half-hour "
    "words: in Latvian and Russian 'half ten' ('pusdesmitos', 'полдесятого') "
    "is 9:30; in English 'half ten' is 10:30. "
    "Whether 'me' agreed is irrelevant: confirmation is decided elsewhere."
)


def extraction_prompt(
    transcript: str, tz_name: str, now: Optional[datetime] = None, state_note: str = "", latest: str = "",
) -> str:
    now = now or datetime.now(tzinfo_for(tz_name))
    parts = [f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({tz_name})."]
    parts.append("Their bookings: " + (state_note or "none."))
    parts.append("Conversation, oldest first:\n\n" + transcript)
    if latest:
        parts.append("\nTheir latest message, the one to decide on: " + " ".join(latest.split()))
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
    if intent == "book" and data.get("asap") is not True and not isinstance(data.get("start"), str):
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


# "Now" that the model turned into a clock time a few minutes back (it sees
# the time only to the minute, and rounds as it likes) still means now.
JUST_PASSED = timedelta(minutes=15)


def soonest_start(now: datetime) -> datetime:
    """'Now' as a booking start: the next 5-minute mark at least 2 minutes
    ahead, so it is still in the future when the slot is checked."""
    at = now + timedelta(minutes=2)
    if at.second or at.microsecond:
        at = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return at + timedelta(minutes=-at.minute % 5)


def requested_slot(
    data: Mapping[str, Any], *, tz_name: str, default_duration: int, now: Optional[datetime] = None,
) -> Optional[tuple[datetime, datetime]]:
    """The slot the customer asked for. With `now`, "right now" (asap, or a
    time that passed moments ago) becomes soonest_start(now)."""
    start = parse_local(str(data.get("start") or ""), tz_name)
    if now is not None and (data.get("asap") is True or (start is not None and now - JUST_PASSED < start <= now)):
        start = soonest_start(now).astimezone(tzinfo_for(tz_name))
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

def own_name_line(name: str) -> str:
    """booking.name, for the customer: who they are meeting."""
    return (f"Your name is {name}. Tell them once when you tell them an appointment is confirmed, and again "
            "when they say they have arrived, so they know who they are meeting.")


NO_DIRECTIONS_RULE = (
    "Never give directions, door codes or how to get in yourself — the way "
    "in is sent automatically once they have arrived."
)

_NOTICE_TEXT = {
    bs.NOTICE_CONFIRMED: "the appointment on {when} has just been CONFIRMED. Tell them it is booked, restating the day and time.",
    bs.NOTICE_DECLINED: "the appointment on {when} is NOT available after all. Say so politely and ask what other day or time would suit them.",
    bs.NOTICE_PROPOSED: "the requested time does not work; the business proposes {proposal} instead. Ask whether that suits them.",
    bs.NOTICE_RESCHEDULED: "the appointment has been MOVED; it is now on {when}. Tell them, restating the new day and time.",
    bs.NOTICE_CANCELLED: "the appointment on {when} has been CANCELLED. Tell them so.",
    bs.NOTICE_EXPIRED: "the request for {when} could not be confirmed in time and has lapsed. Apologise briefly and offer to find another time.",
    bs.NOTICE_REQUESTED: "their request for {when} has been passed on; it is NOT confirmed yet. Say you will confirm it in a moment.",
    bs.NOTICE_KEPT: "the appointment could not be moved; it stays on {when}. Tell them so.",
}


def quiet_request_line(booking: Mapping[str, Any]) -> str:
    """The request notice when the business answers before anything else
    is said (booking.quiet_until_answered)."""
    return (f"Their request for {describe_when(booking)} has gone to the business and is NOT confirmed. "
            "Say nothing about that time now: the answer comes in a later message.")


def notice_line(booking: Mapping[str, Any]) -> str:
    template = _NOTICE_TEXT.get(booking.get("customer_notice") or "")
    if not template:
        return ""
    proposal = describe_proposal(booking) if booking.get("proposed_starts_at") else ""
    return "NEWS TO PASS ON IN THIS REPLY: " + template.format(when=describe_when(booking), proposal=proposal)


def standing_line(booking: Mapping[str, Any], *, outcall_taxi: bool = False) -> str:
    when = describe_when(booking)
    state = booking["state"]
    if state in (bs.REQUESTED, bs.PENDING):
        if booking.get("proposed_by") == bs.OWNER:
            return (f"The business proposed {describe_proposal(booking)} instead of {when}; waiting for "
                    "their answer. Do NOT say anything is confirmed.")
        return (f"An appointment on {when} has been requested and is waiting for confirmation. Do NOT "
                "say it is confirmed or booked. If they ask, say you will confirm it in a moment.")
    if state == bs.CONFIRMED:
        line = f"The appointment on {when} is confirmed."
        if booking.get("proposed_by") == bs.CUSTOMER:
            line += f" They asked to move it to {describe_proposal(booking)}; that is NOT confirmed yet."
        elif booking.get("proposed_by") == bs.OWNER:
            line += f" The business proposed moving it to {describe_proposal(booking)}; waiting for their answer."
        if outcall_taxi and is_outcall(booking):
            return line + " " + outcall_taxi_line(booking)
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


# Always in the reply prompt while bookings are on: only the owner confirms,
# so the reply may never say a time is booked on its own — even when the
# chat's own booking section (or its writing samples) would.
ONLY_OWNER_CONFIRMS_RULE = (
    "Only the business confirms appointments, and you will be told here when it has. "
    "Unless a line below says an appointment is CONFIRMED, do not agree to their time in "
    "any words: no 'ok', 'deal', 'booked', 'confirmed', 'see you then', 'come over', and "
    "no arrival steps such as 'text me before you come'. When they name a day and time, "
    "say only that you will confirm it in a moment — nothing more about the time."
)

ADDRESS_FOLLOWS_LINE = (
    "The address is sent to them automatically in a separate message right after this reply — "
    "do not write any address yourself."
)

# Where a reply wants the street address (booking.address): the system puts
# it there word for word, as a message of its own, so the model never types
# (or misremembers) it.
ADDRESS_TAG = "[address]"
_ADDRESS_TAG = re.compile(r"\[\s*address\s*\]", re.IGNORECASE)
ADDRESS_TAG_LINE = (
    "The street address is set. Write [address] on a line of its own where it should go (with the prices "
    "in your first reply, and again when they ask where you are) and the system puts the address there, "
    "word for word, as a message of its own. Never write the address yourself."
)


def take_address_tag(text: str) -> tuple[str, bool]:
    """The reply without its [address] tags, and whether it had one."""
    if not _ADDRESS_TAG.search(text or ""):
        return text, False
    cleaned = "\n".join(line.strip() for line in _ADDRESS_TAG.sub("", text).splitlines())
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip(), True


def fill_address(text: str, address: str) -> str:
    """The reply with booking.address where it wrote [address] (once), as a
    block of its own; without an address the tag is just dropped."""
    address = (address or "").strip()
    match = _ADDRESS_TAG.search(text or "")
    if match is None:
        return text
    if not address:
        return take_address_tag(text)[0]
    rest, _ = take_address_tag(text[match.end():])
    return (text[:match.start()].rstrip() + f"\n[block]\n{address}\n[/block]\n" + rest).strip()


def is_outcall(booking: Mapping[str, Any]) -> bool:
    """The business goes to them (the request's notes say outcall)."""
    return "outcall" in str(booking.get("notes") or "").lower()


# ------------------------------------------------- outcalls, to the customer
# booking.outcall_taxi: she goes to him by a taxi he orders. The reply
# writer speaks as her, so these say "you" for her and "them" for him.

_NEWS = "NEWS TO PASS ON IN THIS REPLY: "


def outcall_notice_line(booking: Mapping[str, Any], *, now: datetime, ready_minutes: int,
                        has_address: bool) -> str:
    """The news of a confirmed outcall's taxi steps."""
    when = describe_when(booking)
    notice = booking.get("customer_notice")
    if notice == bs.NOTICE_READY:
        line = (f"you are ready for the outcall on {when}. Tell them to order a taxi to pick you up now and "
                "to send you the taxi's link here, so you can see it coming.")
        if has_address:
            line += " Your address, for the taxi, follows in a separate message."
        return _NEWS + line
    if notice == bs.NOTICE_IN_TAXI:
        return _NEWS + (f"you are in the taxi now, on your way to them for the outcall on {when}. Ask them to "
                        "send the exact address, the floor and flat number, how to get in (entrance, door "
                        "code) and a photo of the entrance or the building, so you find it easily. Do not "
                        "write [address].")
    soon = booking["starts_at"] - now <= timedelta(minutes=ready_minutes + 60)
    getting_ready = (f"You need about {ready_minutes} minutes to get ready; say you will text them as soon as "
                     "you are ready" if soon else "Say you will text them when you are getting ready")
    return _NEWS + (f"the outcall on {when} has just been CONFIRMED. Tell them it is booked, restating the day "
                    f"and time. {getting_ready}, and that they then order the taxi to pick you up. Do not ask "
                    "them to order it before that.")


def outcall_taxi_line(booking: Mapping[str, Any]) -> str:
    """Where a confirmed outcall's taxi steps stand, for every reply."""
    line = "It is an outcall: you go to them by a taxi they order."
    if booking.get("in_taxi_at"):
        return line + (" You are in the taxi on your way to them. If they have not sent the exact address, "
                       "the floor and flat, how to get in and a photo of the entrance yet, ask for what is "
                       "missing. Do not write [address]: you are coming to them.")
    if booking.get("taxi_link_at"):
        return line + (" They ordered the taxi and sent its link; you are about to get in. Do not write "
                       "[address] again.")
    if booking.get("ready_at"):
        return line + (" You are ready and asked them to order a taxi to pick you up at your address and "
                       "to send you its link.")
    return line + (" You are getting ready and will text them when you are ready; only then do they order "
                   "the taxi. Do not ask them to order it before that.")


TAXI_LINK_LINE = _NEWS + "the taxi link they sent has reached you. Thank them briefly; do not repeat the link."

PICKUP_ADDRESS_LINE = (
    "Your address, for the taxi, is sent to them automatically in a separate message right after this "
    "reply — do not write any address yourself."
)

# What the customer sends for an outcall photo, in the chat history: the
# photo itself went on to the owner.
PHOTO_PASSED_ON = "[photo for the outcall]"

_LINK = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s<>\"']*", re.IGNORECASE)
# A link from a taxi app's "share my ride" (Bolt, Uber, Yandex Go, Yango,
# Forus, Free Now, any ...taxi... site).
_TAXI_HOST = re.compile(r"bolt\.|uber\.|ubr\.to|yango|go\.yandex|taxi|taksi|forus|free-?now|lyft\.|cabify|"
                        r"careem|didi|gett\.|grab\.|taxify", re.IGNORECASE)
# Where he is, not his ride.
_MAP_HOST = re.compile(r"maps?\.|/maps|goo\.gl/maps|waze\.|2gis|openstreetmap|what3words", re.IGNORECASE)


def find_links(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:!?)»”") for m in _LINK.finditer(text or "")]


def is_taxi_link(link: str, *, ready: bool) -> bool:
    """A taxi app's ride link. Once she is ready (he was asked for the
    link) any link but a map is taken for it."""
    if _MAP_HOST.search(link):
        return False
    host = re.sub(r"^[a-z]+://", "", link, flags=re.IGNORECASE).split("/", 1)[0]
    return bool(_TAXI_HOST.search(host)) or ready


def context_for_reply(lines: Sequence[str]) -> str:
    lines = [line for line in lines if line]
    return "APPOINTMENTS:\n" + "\n".join([ONLY_OWNER_CONFIRMS_RULE, *lines])


# ---------------------------------------------------------------- arrival

ARRIVAL_SYSTEM_PROMPT = (
    "You read the end of a private chat between a business ('me') and a "
    "customer ('them') who has an appointment about now. Decide whether the "
    "customer's LATEST message says they have physically arrived at the meeting "
    "place — for example 'I'm here', 'at the door', 'downstairs', 'outside', "
    "'я на месте', 'я внизу', 'я у подъезда', 'esmu klāt', 'esmu pie mājas', "
    "'just parked outside'. Someone already at the building who "
    "asks which door, entrance, floor or code ('I'm at the building, which "
    "door?', 'here, two doors, which one?') HAS arrived. Being on the way, "
    "almost there, running late, in the area or the city but not at the place, "
    "or asking for the address is NOT arrival. Answer with a single JSON object "
    'and nothing else: {"arrived": true|false}'
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
