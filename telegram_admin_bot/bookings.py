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
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Collection, Mapping, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

import booker_texts
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
#
# Every line to her is said through booker_texts (`t`: her language, and the
# account's own versions of the main messages); the default, ENGLISH, is
# what these always said. Days and lengths shown to her are hers too
# (when_for, span_for, ...); describe_when and the others stay English for
# the model and the panel.

# The commands line of a request, in English ({n}: the booking number).
ANSWER_HELP = booker_texts.TEXTS["answer_help"]["en"]


def span_for(t: booker_texts.Texts, starts_at: datetime, ends_at: datetime, tz_name: str) -> str:
    """describe_span in her language."""
    zone = tzinfo_for(tz_name)
    return t.span(starts_at.astimezone(zone), ends_at.astimezone(zone))


def when_for(booking: Mapping[str, Any], t: booker_texts.Texts) -> str:
    """describe_when in her language."""
    return span_for(t, booking["starts_at"], booking["ends_at"], booking["tz"])


def proposal_for(booking: Mapping[str, Any], t: booker_texts.Texts) -> str:
    """describe_proposal in her language."""
    return span_for(t, booking["proposed_starts_at"], booking["proposed_ends_at"], booking["tz"])


def at_for(t: booker_texts.Texts, at: datetime, tz_name: str) -> str:
    """'Fri 03 Oct 14:00' (availability.describe) in her language."""
    local = at.astimezone(tzinfo_for(tz_name))
    return f"{t.day(local, year=False)} {local:%H:%M}"


def why_for(t: booker_texts.Texts, reason: str) -> str:
    """Why a time can't be had (availability's reason), in her language."""
    key = f"why_{reason}"
    return t.say(key) if t.has(key) else reason.replace("_", " ")


def _customer(booking: Mapping[str, Any], t: booker_texts.Texts) -> str:
    return booking.get("customer_name") or t.say("unknown_customer")


def _handle(booking: Mapping[str, Any]) -> str:
    return f" (@{booking['customer_username']})" if booking.get("customer_username") else ""


def _who(booking: Mapping[str, Any], t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    return _customer(booking, t) + _handle(booking)


def _him(booking: Mapping[str, Any], t: booker_texts.Texts) -> str:
    """The customer's name in her answers back, else "the customer"."""
    return booking.get("customer_name") or t.say("the_customer")


def _named(name: str) -> str:
    """Her name ahead of a message to the owner (booking.name, else the
    account's name), so an owner answering for several accounts sees whose
    booking it is."""
    return f"{name} — " if name else ""


def _blanks(booking: Mapping[str, Any], t: booker_texts.Texts, *, name: str = "",
            who: str = "") -> dict[str, str]:
    """The blanks every template of a booking has (booker_texts.TEMPLATES)."""
    return {
        "number": str(booking["number"]), "customer": who or _customer(booking, t), "handle": _handle(booking),
        "girl": name, "girl_tag": _named(name), "when": when_for(booking, t),
        "length": t.minutes(booking_minutes(booking)),
    }


def format_request(booking: Mapping[str, Any], *, changed: bool = False, name: str = "",
                   t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """A new request, or one whose time the customer changed before you
    answered: who asks, for whom (her name, else the account's), the extras
    he asked for, when."""
    n = booking["number"]
    return t.template(
        "request", **_blanks(booking, t, name=name),
        changed=t.say("request_new_time") if changed else "",
        extras=booking.get("extras") or t.say("request_no_extras"),
        service=booking.get("service") or "", notes=booking.get("notes") or "",
        commands=t.say("answer_help", n=n))


def format_move_request(booking: Mapping[str, Any], *, name: str = "",
                        t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """The customer asks to move a confirmed booking."""
    return t.template("move_request", **_blanks(booking, t, name=name), to=proposal_for(booking, t),
                      commands=t.say("move_commands", n=booking["number"]))


def format_owner_update(booking: Mapping[str, Any], what: str, *, name: str = "",
                        t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Something the owner should know that they did not do themselves."""
    return t.say("owner_update", girl_tag=_named(name), n=booking["number"], who=_who(booking, t),
                 when=when_for(booking, t), what=what)


def format_customer_cancelled(booking: Mapping[str, Any], *, name: str = "",
                              t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """The customer cancelled his booking."""
    return t.template("customer_cancelled", **_blanks(booking, t, name=name))


# Her answer back for the actions that have a template of their own.
_ACK_TEMPLATES = {"confirm": "confirmed", "accept_proposal": "confirmed", "decline": "declined",
                  "reschedule": "moved", "cancel": "cancelled"}


def format_acknowledgement(booking: Mapping[str, Any], action: str, *, outcall_taxi: bool = False,
                           proof: bool = False, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    n, who = booking["number"], _him(booking, t)
    if action == "propose":
        return t.template("proposed", **_blanks(booking, t, who=who), proposed=proposal_for(booking, t))
    # When and how long, so she sees which booking it was and what she gave him.
    when, length = when_for(booking, t), t.minutes(booking_minutes(booking))
    if action in _ACK_TEMPLATES:
        text = t.template(_ACK_TEMPLATES[action], **_blanks(booking, t, who=who))
    elif t.has(f"ack_{action}"):
        text = t.say(f"ack_{action}", n=n, who=who, when=when, length=length)
    else:
        text = t.say("ack_other", n=n, action=action)
    if outcall_taxi and action in ("confirm", "accept_proposal") and is_outcall(booking):
        text += " " + t.say("ack_outcall_proof" if proof else "ack_outcall_ready", n=n, who=who)
    return text


def outcall_step(booking: Mapping[str, Any], *, proof: bool = False,
                 t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Where a confirmed outcall's taxi steps stand, for the owner (and, in
    English, for the reader of her words)."""
    n = booking["number"]
    if booking.get("in_taxi_at"):
        return t.say("step_in_taxi")
    if booking.get("taxi_link_at"):
        return t.say("step_taxi_link", n=n)
    if booking.get("proof_at"):
        return t.say("step_proof_came")
    if booking.get("ready_at"):
        return t.say("step_ready")
    if proof:
        return t.say("step_wait_proof", n=n)
    return t.say("step_send_ready", n=n)


def format_list(bookings: Sequence[Mapping[str, Any]], outcalls: Sequence[Mapping[str, Any]] = (), *,
                proof: bool = False, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    if not bookings and not outcalls:
        return t.say("list_nothing")
    lines = []
    if bookings:
        lines.append(t.say("list_waiting"))
    for b in bookings:
        n, who = b["number"], _who(b, t)
        if b["state"] == bs.CONFIRMED and b.get("proposed_by") == bs.CUSTOMER:
            lines.append("  " + t.say("list_move", n=n, who=who, when=when_for(b, t), to=proposal_for(b, t)))
        elif b.get("proposed_by") == bs.OWNER:
            lines.append("  " + t.say("list_proposed", n=n, who=who, proposed=proposal_for(b, t)))
        else:
            lines.append("  " + t.say("list_request", n=n, who=who, when=when_for(b, t)))
    if bookings:
        lines += ["", t.say("list_commands")]
    if outcalls:
        if lines:
            lines.append("")
        lines.append(t.say("list_outcalls"))
        lines += ["  " + t.say("list_outcall", n=b["number"], who=_who(b, t), when=when_for(b, t),
                               step=outcall_step(b, proof=proof, t=t))
                  for b in outcalls]
    return "\n".join(lines)


# ------------------------------------------------- outcalls, to the owner


def format_ready_reminder(booking: Mapping[str, Any], *, name: str = "",
                          t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    return t.template("ready_reminder", **_blanks(booking, t, name=name),
                      commands=t.say("ready_reminder_commands", n=booking["number"]))


def format_taxi_link(booking: Mapping[str, Any], text: str, in_taxi_minutes: Optional[int], *,
                     t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """The customer's message with the taxi link. `in_taxi_minutes`: when
    he is told she is in the taxi without her IN n; None when no IN n is
    wanted (he knows already, or is told at once)."""
    n = booking["number"]
    head = t.say("taxi_link", n=n, who=_who(booking, t), text=text)
    if in_taxi_minutes is None:
        return head
    return f"{head}\n\n" + t.say("taxi_link_in", n=n, minutes=in_taxi_minutes)


def format_proof_header(booking: Mapping[str, Any], caption: str = "", *,
                        t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Before his proof photo goes on to her: what it is, and what comes next."""
    head = t.say("proof_header", n=booking["number"], who=_who(booking, t))
    return f"{head}\n{caption}" if caption else head


def format_arrived(booking: Mapping[str, Any], said: str, *, way_in_sent: bool, name: str = "",
                   t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """An incall customer says he has arrived: what he wrote, and what the
    owner does now."""
    return t.template("arrived", **_blanks(booking, t, name=name), said=said or "",
                      way_in=t.say("way_in_sent" if way_in_sent else "way_in_meet"))


def format_passed_on(booking: Mapping[str, Any], text: str, *,
                     t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """What the customer wrote while the outcall is under way, word for word."""
    return t.say("passed_on", n=booking["number"], who=_who(booking, t), text=text)


def format_photo_header(booking: Mapping[str, Any], caption: str = "", *,
                        t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    head = t.say("photo_header", n=booking["number"], who=_who(booking, t))
    return f"{head}\n{caption}" if caption else head


def format_which(kind: str, *, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    return t.say("which", kind=t.command(kind))


def preview_messages(t: booker_texts.Texts, *, tz: str, now: datetime, name: str = "Mia") -> dict[str, str]:
    """Each main message (booker_texts.TEMPLATES) as the bot would send it
    for a made-up booking, through the same functions as the real ones: for
    the panel's live preview."""
    zone = tzinfo_for(tz)
    today = now.astimezone(zone).date()
    start = datetime.combine(today + timedelta(days=1), dtime(21, 0), tzinfo=zone)
    to = datetime.combine(today + timedelta(days=2), dtime(20, 0), tzinfo=zone)
    booking = {
        "number": 3, "state": bs.PENDING, "customer_name": "Alex", "customer_username": "alex_m",
        "extras": "kissing, shower together", "service": "", "notes": "", "tz": tz,
        "starts_at": start, "ends_at": plus_minutes(start, 60), "proposed_by": None,
        "proposed_starts_at": None, "proposed_ends_at": None,
    }
    moving = {**booking, "state": bs.CONFIRMED, "proposed_by": bs.CUSTOMER, "proposed_starts_at": to,
              "proposed_ends_at": plus_minutes(to, 60)}
    offered = {**moving, "state": bs.PENDING, "proposed_by": bs.OWNER}
    confirmed = {**booking, "state": bs.CONFIRMED}
    moved = {**confirmed, "starts_at": to, "ends_at": plus_minutes(to, 60)}
    return {
        "request": format_request(booking, name=name, t=t),
        "move_request": format_move_request(moving, name=name, t=t),
        "confirmed": format_acknowledgement(confirmed, "confirm", t=t),
        "declined": format_acknowledgement({**booking, "state": bs.CANCELLED}, "decline", t=t),
        "proposed": format_acknowledgement(offered, "propose", t=t),
        "moved": format_acknowledgement(moved, "reschedule", t=t),
        "cancelled": format_acknowledgement({**booking, "state": bs.CANCELLED}, "cancel", t=t),
        "customer_cancelled": format_customer_cancelled({**booking, "state": bs.CANCELLED}, name=name, t=t),
        "arrived": format_arrived(confirmed, "I'm at the door 🙂", way_in_sent=True, name=name, t=t),
        "ready_reminder": format_ready_reminder(confirmed, name=name, t=t),
    }


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
# A length or a time of day after YES / NO ("yes 2 hours", "ok 30 min.",
# "jā 2 st.", "sí 2 hs", "yes 9 pm"): the number is not a booking number.
_UNIT = re.compile(
    r"(?i)^(?:h|hs|hrs?|hours?|min|mins|minutes?|st|ч|час|часа|часов|мин|минут\w*|stund\w*|min[ūu]t\w*|horas?|"
    r"minutos?|am|pm|o'?clock|вечера|утра|дня|ночи|vakarā|rītā|noche|tarde|mañana)$")
_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class OwnerCommand:
    # yes | no | propose | cancel | done | noshow | list | ready | in_taxi,
    # and from the reader of her own words: unclear | stays | none (the
    # booking does not change: only words for him, or her own question)
    kind: str
    number: Optional[int] = None
    # For propose: the wall time and, if given, the day ("today"/"tomorrow"
    # already resolved against `today`).
    at: Optional[dtime] = None
    day: Optional[date] = None
    error: str = ""
    # A YES / NO / CANCEL / DONE / NOSHOW with words after it ("ok in 20
    # min", "CANCEL 7 tell him I'm sick"): the words may change what it
    # means or carry more, so the reader of her own words decides when it
    # can, and this stands only when it can't (BookingFlow.on_owner_message).
    loose: bool = False
    # From the reader of her own words; empty for a strict command. The
    # length she set, in minutes.
    minutes: Optional[int] = None
    # What goes on to him: facts to tell, a question to ask, extras she
    # takes or refuses.
    tell: str = ""
    ask: str = ""
    extras_ok: tuple[str, ...] = ()
    extras_no: tuple[str, ...] = ()
    # She asked about the booking herself: answered to her, nothing to him.
    for_me: bool = False
    # unclear: the question to ask her (else one by `missing`: "which" when
    # several bookings fit — `candidates` — or "time" when no time could be
    # read).
    question: str = ""
    missing: str = ""
    candidates: tuple[int, ...] = ()

    @property
    def has_words(self) -> bool:
        """Something of hers goes on to him."""
        return bool(self.tell or self.ask or self.extras_ok or self.extras_no)


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


def parse_owner_reply(text: str, *, today: date, lengths: bool = True) -> Optional[OwnerCommand]:
    """What the owner wrote, as a command, or None when it is not one (then
    it is an ordinary message and gets an ordinary reply). `lengths`: a
    number followed by a length or a time of day ("yes 2 hours") is no
    booking number — only while her own words are read (the model then
    reads it); with commands only it is as it always was."""
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
    if lengths and number is not None and len(rest) > 1 and _UNIT.match(_word(rest[1]).rstrip(".?…)")):
        number = None  # "yes 2 hours": a length, read by the model
    loose = len(rest) > (1 if number is not None else 0)
    if rest and number is None and kind in ("yes", "no"):
        # "yes, see you then" to a single request is still a yes; anything
        # longer that does not start with a number is conversation.
        if len(rest) > 3:
            return None
    return OwnerCommand(kind, number, loose=loose)


def resolve_local(cmd: OwnerCommand, booking: Mapping[str, Any]) -> datetime:
    """The proposed start as an aware datetime in the booking's zone. No day
    given = the day the booking is on now."""
    zone = tzinfo_for(booking["tz"])
    day = cmd.day or booking["starts_at"].astimezone(zone).date()
    return datetime.combine(day, cmd.at, tzinfo=zone)


# ------------------------------------ the owner's answer in her own words

OWNER_ANSWER_SYSTEM_PROMPT = (
    "You read the chat between a booking assistant ('me') and the person who takes its bookings "
    "('them': the girl herself, or whoever books for her). 'me' sends them each customer's booking "
    "request and passes on what customers write; they answer in their own words, in any language "
    "(English, Russian, Latvian, Spanish, or a mix), rarely with a command. Work out everything "
    "their LATEST message means for the bookings listed, reading it together with the conversation "
    "before it. Missing a part of it is the worst mistake: go through it piece by piece — every "
    "booking it is about, every yes, no or time, every length, extra, thing to tell the customer, "
    "question for the customer and question for me.\n"
    "Answer with a single JSON object and nothing else:\n"
    '{"answers": [{"number": <booking number or null>, "action": "yes"|"no"|"ready_at"|"time"|"cancel"|'
    '"done"|"noshow"|"ready"|"in_taxi"|"list"|"unclear"|"none", "start": "YYYY-MM-DDTHH:MM" or null, '
    '"in_minutes": <integer or null>, "duration_minutes": <integer or null>, "extras_ok": [<extra>, ...], '
    '"extras_no": [<extra>, ...], "tell_customer": "<words for the customer>" or null, '
    '"ask_customer": "<question for the customer>" or null, "for_me": true|false, '
    '"question": "<one short question for them>" or null}], "standing": "<instruction>" or null}\n'
    "One answer per booking their message is about, in the order they mention them. An empty list "
    "when it is about no booking: a greeting, thanks, small talk, a remark.\n"
    "action:\n"
    "- yes: they take the booking at the time the customer asked: 'yes', 'ok', 'yes I confirm', "
    "'sure, let him come', 'fine', 'both fine', 'da', 'да', 'jā', 'sí', 'vale', '👍'. For a customer "
    "asking to move a booking: they agree to the new time. A yes with a condition ('yes if he comes "
    "alone', 'yes but only 1 hour', 'ok, cash only') is still yes; the condition goes in the fields "
    "below.\n"
    "- no: they turn it down and give no other time, now or earlier in the chat: 'no', 'not "
    "today', 'I'm busy', 'can't', 'нет', 'nē', 'no puedo'.\n"
    "- ready_at: they say WHEN they will be ready or free instead of a plain yes: 'I'm ready in 20 "
    "minutes', 'give me half an hour', 'I can meet in 1 hour', 'free after 15:00', 'yes but in 40 "
    "min'. A delay goes in in_minutes (counted from their latest message), a clock time in start.\n"
    "- time: they put forward a different meeting time: 'can he come at 15:30?', 'tomorrow 12:00 "
    "instead', 'better at 8', 'not today, tomorrow any time after 2' (the earliest time they name "
    "goes in start: tomorrow 14:00).\n"
    "- cancel: they call off a booking that is already confirmed.\n"
    "- done / noshow: after the meeting: it happened / the customer did not come.\n"
    "- ready: an outcall, and they are ready to leave NOW ('I'm ready', 'ready, he can order the "
    "taxi'). 'Ready in 20 minutes' is ready_at.\n"
    "- in_taxi: an outcall, and they are in the taxi or on the way to him now.\n"
    "- list: they ask what is waiting for them.\n"
    "- unclear: it is about a booking but you cannot tell what they want; put the one short "
    "question to ask them in question.\n"
    "- none: about this booking, but the booking itself does not change: only something to tell "
    "or ask the customer, extras, or a question for me.\n"
    "The fields of an answer:\n"
    "- number: the booking it is about: the one their message replies to (when given below), the "
    "one they name by number or by the customer's name, else the only booking it can be about; "
    "null when you cannot tell. A yes or no that names no booking while several wait for their "
    "answer is ONE answer with number null — never one answer per booking — unless they clearly "
    "mean all of them ('both fine', 'all ok', 'оба', 'abi', 'los dos'). When my last message asked "
    "them something (which booking? what time?), their latest message answers it: read the two of "
    "theirs together. When their latest message points back at something they said before ('like "
    "I said', 'как я и говорила', 'kā jau teicu', 'como te dije'), act on what they said then: a "
    "time they gave then ('today only after 20:00') is the time now (action time), not a no.\n"
    "- start / in_minutes: only with ready_at and time. 24-hour local time in the given timezone; "
    "resolve 'today', 'tomorrow' and bare hours ('at 8' in the afternoon is 20:00) from the "
    "current date and time. Never invent a time that was not said.\n"
    "- duration_minutes: the length they set ('1 hour only' is 60, 'max 30 min' is 30), with yes, "
    "ready_at or time, or for a booking that is already confirmed; else null.\n"
    "- extras_ok / extras_no: the extra services they accept / refuse for this customer, a word or "
    "two each ('anal', 'domination'); empty lists when they name none.\n"
    "- tell_customer: what they want the customer told, in short plain words, close to how they "
    "said it: the condition of their yes, 'bring cash', 'I'm 10 min late', 'come alone'. Only what "
    "they clearly want him to hear. Never their remarks or opinions about him ('he didn't show "
    "last time', 'he's weird', 'don't take him again' stay private), never what they said to me "
    "only, never what the action itself tells him (that a booking waiting for their answer is "
    "confirmed or declined, a new time or length for it, the extras). For a booking that is "
    "already confirmed, a time they will be ready or a delay they want him told ('tell him I'll be "
    "ready in 10 min', 'I'm 5 min late') is action none with those words here. null when there is "
    "nothing.\n"
    "- ask_customer: a question they want put to the customer ('ask him if he wants 2 hours', "
    "'ask him for a photo first', 'pregúntale el número de habitación'); null when there is none.\n"
    "- for_me: true when they ask ME something about the booking: 'how long does he want?', 'what "
    "extras did he ask for?', 'is it outcall?', 'did he send the photo?'.\n"
    "standing: an instruction about ALL future customers rather than one booking ('from now on "
    "cash only', 'no more anal for anyone'), in their words; else null. Never about one customer: "
    "'don't take him again', 'after today not him', 'he's banned' are private remarks and go "
    "nowhere.\n"
    "Put each thing in one place only, and make nothing up: an empty list, null or false is always "
    "better than a guess."
)

OWNER_ACTIONS = ("yes", "no", "ready_at", "time", "cancel", "done", "noshow", "ready", "in_taxi", "list",
                 "unclear", "none")
# Words the model uses now and then instead of the actions it was given.
ACTION_SYNONYMS = {
    "confirm": "yes", "confirmed": "yes", "accept": "yes", "accepted": "yes", "approve": "yes", "ok": "yes",
    "decline": "no", "declined": "no", "reject": "no", "rejected": "no", "refuse": "no",
    "propose": "time", "move": "time", "reschedule": "time", "new_time": "time",
    "taxi": "in_taxi", "on_the_way": "in_taxi", "complete": "done", "completed": "done", "no_show": "noshow",
    "noshowed": "noshow", "cancelled": "cancel", "canceled": "cancel", "finished": "done",
    "tell": "none", "ask": "none", "relay": "none", "message": "none", "question": "none",
}

# What one reading may hold: answers per message, characters of the words
# for him (or of a question / standing instruction), extras per list and
# characters per extra.
MAX_ANSWERS = 8
MAX_WORDS = 300
MAX_EXTRAS = 8
MAX_EXTRA = 60
# The length she can set, in minutes.
MIN_LENGTH, MAX_LENGTH = 15, 12 * 60


def describe_minutes(minutes: int) -> str:
    """'1 hour', '2 hours', '1 h 30 min', '45 min'."""
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if minutes > 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"


def booking_minutes(booking: Mapping[str, Any]) -> int:
    return int((booking["ends_at"] - booking["starts_at"]).total_seconds() // 60)


def _short_at(booking: Mapping[str, Any], when: datetime, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """'Tue 21:00' for `when` in the booking's zone."""
    local = when.astimezone(tzinfo_for(booking["tz"]))
    return f"{t.weekday(local)} {local:%H:%M}"


def _short_when(booking: Mapping[str, Any], t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """'Tue 21:00' in the booking's zone, for a question to the owner."""
    return _short_at(booking, booking["starts_at"], t)


def extras_list(text: str) -> list[str]:
    """A comma list (bookings.extras_ok / extras_no) as its items."""
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def extras_joined(old: str, new: Sequence[str]) -> str:
    """The comma list `old` with `new` added, each extra once (any case)."""
    items = extras_list(old)
    seen = {item.lower() for item in items}
    for item in new:
        item = " ".join(str(item).split()).strip(",")
        if item and item.lower() not in seen:
            seen.add(item.lower())
            items.append(item)
    return ", ".join(items)


def owner_state_note(rows: Sequence[Mapping[str, Any]], *, outcall_taxi: bool = False, proof: bool = False) -> str:
    """The bookings she may be answering about, in full, for the reader of
    her words."""
    lines = []
    for b in rows:
        length = describe_minutes(booking_minutes(b))
        if b["state"] in bs.FINAL:
            over = {bs.CANCELLED: "cancelled", bs.COMPLETED: "done", bs.NO_SHOW: "he did not come"}[b["state"]]
            what = f"{over} (it was for {describe_when(b)}, {length})"
        elif b["state"] == bs.CONFIRMED and b.get("proposed_by") == bs.CUSTOMER:
            what = (f"confirmed for {describe_when(b)} ({length}); the customer asks to move it to "
                    f"{describe_proposal(b)}, waiting for their answer")
        elif b.get("proposed_by") == bs.OWNER:
            what = (f"the customer asked for {describe_when(b)} ({length}); they proposed "
                    f"{describe_proposal(b)} instead, waiting for the customer")
        elif b["state"] in (bs.REQUESTED, bs.PENDING):
            what = f"the customer asks for {describe_when(b)} ({length}), waiting for their answer"
        else:
            what = f"confirmed for {describe_when(b)} ({length})"
            if outcall_taxi and is_outcall(b):
                what += f"; outcall: {outcall_step(b, proof=proof)}"
        if is_outcall(b) and "outcall" not in what:
            what += "; outcall"
        elif not is_outcall(b):
            what += "; incall"
            if b.get("arrived_at") and b["state"] == bs.CONFIRMED:
                what += ", he says he has arrived"
        if b.get("extras"):
            what += f"; extras he asked for: {b['extras']}"
        if b.get("extras_ok"):
            what += f"; extras they said yes to: {b['extras_ok']}"
        if b.get("extras_no"):
            what += f"; extras they refused: {b['extras_no']}"
        if b.get("notes"):
            what += f"; notes: {' '.join(str(b['notes']).split())}"
        if b.get("booker_asked_at"):
            what += "; they asked him something, his answers come to them"
        lines.append(f"#{b['number']} {b.get('customer_name') or 'unknown customer'}: {what}")
    return "\n".join(lines)


def owner_answer_prompt(
    transcript: str, tz_name: str, *, now: datetime, state_note: str, latest: str,
    replying_to: Optional[int] = None,
) -> str:
    # The runtime's clock is UTC; she means her own wall clock.
    now = now.astimezone(tzinfo_for(tz_name))
    parts = [f"Current date and time: {now:%A %Y-%m-%d %H:%M} ({tz_name}).",
             "Bookings:\n" + (state_note or "none."),
             "Conversation, oldest first:\n\n" + transcript]
    if replying_to is not None:
        parts.append(f"\nTheir latest message replies to the message about booking #{replying_to}.")
    parts.append("Their latest message, the one to decide on: " + " ".join(latest.split()))
    return "\n".join(parts)


@dataclass(frozen=True)
class OwnerReading:
    """What the reader found in her message: one answer per booking (the
    JSON's keys, cleaned by parse_owner_answer), and an instruction meant
    for every customer from now on."""
    answers: tuple[dict[str, Any], ...] = ()
    standing: str = ""


def _text(value: Any, limit: int = MAX_WORDS) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _whole(value: Any, low: int, high: int) -> Optional[int]:
    """An integer the model gave (7, 7.0, "7") within [low, high], else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and low <= value <= high else None


def _extras(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for item in value:
        text = _text(item, MAX_EXTRA).strip(",")
        if text and text.lower() not in {i.lower() for i in items}:
            items.append(text)
    return items[:MAX_EXTRAS]


def _clean_answer(data: Any) -> Optional[dict[str, Any]]:
    """One answer of the reader's JSON, every field checked; None when it
    holds nothing usable. An action it does not know, or another word for
    `none`, keeps what it carries (words for him, a length, a time); with
    only a booking number or a question for her it is `unclear`, so she is
    asked rather than her answer lost."""
    if not isinstance(data, dict):
        return None
    number = data.get("number")
    if isinstance(number, str):
        number = _number(number)
    elif isinstance(number, float) and number.is_integer():
        number = int(number)
    start = data.get("start")
    answer = {
        "action": None,
        "number": number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None,
        "start": start.strip() if isinstance(start, str) and start.strip() else None,
        "in_minutes": _whole(data.get("in_minutes"), 0, 24 * 60),
        "duration_minutes": _whole(data.get("duration_minutes"), MIN_LENGTH, MAX_LENGTH),
        "extras_ok": _extras(data.get("extras_ok")),
        "extras_no": _extras(data.get("extras_no")),
        "tell_customer": _text(data.get("tell_customer")) or None,
        "ask_customer": _text(data.get("ask_customer")) or None,
        "for_me": data.get("for_me") is True,
        "question": _text(data.get("question")) or None,
    }
    carries = any(answer[key] for key in ("extras_ok", "extras_no", "tell_customer", "ask_customer", "for_me"))
    timed = answer["start"] is not None or answer["in_minutes"] is not None
    raw = data.get("action")
    word = raw.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(raw, str) else ""
    if word in OWNER_ACTIONS:
        answer["action"] = word
        if word == "none" and not (carries or timed or answer["duration_minutes"] is not None):
            if not answer["question"]:
                return None  # nothing about this booking
            answer["action"] = "unclear"  # only a question for her: she is asked it
        return answer
    mapped = ACTION_SYNONYMS.get(word, "")
    if mapped and mapped != "none":
        answer["action"] = mapped
    elif carries or timed or answer["duration_minutes"] is not None:
        answer["action"] = "none"
    elif answer["number"] is not None or answer["question"]:
        answer["action"] = "unclear"
    else:
        return None
    return answer


def parse_owner_answer(text: str) -> Optional[OwnerReading]:
    """The reader's JSON, cleaned. None when it is unusable (no JSON, broken
    JSON, not an object): the caller treats that like a model that could not
    be asked. An empty reading: nothing about the bookings."""
    match = re.search(r"\{.*\}", (text or "").strip(), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "answers" in data:
        raw = data["answers"] if data["answers"] is not None else []
        if not isinstance(raw, list):
            return None
    elif "action" in data:
        raw = [data]  # one answer, the shape before there could be several
    else:
        return None
    answers = tuple(a for a in (_clean_answer(item) for item in raw[:MAX_ANSWERS]) if a is not None)
    standing = _text(data.get("standing"))
    if raw and not answers and not standing and any(_unusable(item) for item in raw[:MAX_ANSWERS]):
        # It meant to say something about a booking, in a shape that can't
        # be used: not read, rather than read as nothing.
        return None
    return OwnerReading(answers, standing)


def _unusable(item: Any) -> bool:
    """An answer that is not an empty `none`: something was meant by it."""
    return not (isinstance(item, dict) and item.get("action") == "none")


def ready_time(at: datetime) -> datetime:
    """When she says she will be ready, as a start: that minute or the next
    five-minute mark after it."""
    if at.second or at.microsecond:
        at = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return at + timedelta(minutes=-at.minute % 5)


# "Ready in 20 minutes" when he asked for 20 past is a yes, and so is a few
# minutes after it ("now" is the next 5-minute mark; she rounds).
READY_SLACK = timedelta(minutes=5)


def awaits_owner(booking: Mapping[str, Any]) -> bool:
    """A request, or a move the customer asked for (BookingStore.awaiting_owner)."""
    return booking["state"] in (bs.REQUESTED, bs.PENDING) or (
        booking["state"] == bs.CONFIRMED and booking.get("proposed_by") == bs.CUSTOMER)


def is_settled(booking: Mapping[str, Any]) -> bool:
    """Confirmed with nothing to answer: a yes to it changes nothing."""
    return booking["state"] == bs.CONFIRMED and booking.get("proposed_by") != bs.CUSTOMER


def _fitting(action: str, rows: Sequence[Mapping[str, Any]], now: datetime) -> list[Mapping[str, Any]]:
    """The bookings an answer naming none can be about."""
    if action in ("yes", "no", "ready_at", "time", "unclear"):
        fits = [b for b in rows if awaits_owner(b)]
    elif action in ("ready", "in_taxi"):
        fits = [b for b in rows if b["state"] == bs.CONFIRMED and is_outcall(b)]
    elif action == "cancel":
        fits = [b for b in rows if b["state"] == bs.CONFIRMED and b["starts_at"] > now]
    elif action in ("done", "noshow"):
        fits = [b for b in rows if b["state"] == bs.CONFIRMED and b["starts_at"] <= now]
    else:
        # Words alone or her own question: a booking that is still on before
        # one that is over.
        fits = [b for b in rows if b["state"] in bs.LIVE] or list(rows)
    live = [b for b in rows if b["state"] in bs.LIVE]
    return fits or (live if len(live) == 1 else list(rows) if len(rows) == 1 else [])


def _typed_numbers(text: str) -> set[int]:
    return {int(digits) for digits in re.findall(r"\d+", text or "") if len(digits) <= 7}


def _offer(number: int, at: datetime, booking: Mapping[str, Any]) -> str:
    """The strict command that offers him `at`: "8 15:30", or "8 21.10 15:30"
    on another day than his booking."""
    zone = tzinfo_for(booking["tz"])
    local = at.astimezone(zone)
    same_day = local.date() == booking["starts_at"].astimezone(zone).date()
    return f"{number} {local:%H:%M}" if same_day else f"{number} {local:%d.%m} {local:%H:%M}"


def _command_from(
    answer: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, now: datetime, tz_name: str,
    replied: Optional[Mapping[str, Any]], outcall_taxi: bool, typed: Optional[str],
    t: booker_texts.Texts = booker_texts.ENGLISH,
) -> Optional[OwnerCommand]:
    action = answer["action"]
    words = {
        "minutes": answer.get("duration_minutes"), "tell": answer.get("tell_customer") or "",
        "ask": answer.get("ask_customer") or "", "extras_ok": tuple(answer.get("extras_ok") or ()),
        "extras_no": tuple(answer.get("extras_no") or ()), "for_me": bool(answer.get("for_me")),
        "question": answer.get("question") or "",
    }
    if action == "list":
        return OwnerCommand("list")
    if action in ("ready", "in_taxi") and not outcall_taxi:
        # No taxi steps here: nothing happens to the booking, and she is
        # told so (with her words, if any, passed on).
        action = "none"
    number = answer.get("number")
    if (number is not None and typed is not None and number not in {b["number"] for b in rows}
            and number not in _typed_numbers(typed) and not (replied and replied["number"] == number)):
        # A booking that is not open and that she did not name: the model's
        # guess, never acted on.
        fits = _fitting(action, rows, now)
        return OwnerCommand("unclear", None, missing="which", candidates=tuple(b["number"] for b in (fits or rows)),
                            **words)
    if number is None and replied is not None:
        number = replied["number"]
    if number is None:
        fits = _fitting(action, rows, now)
        if len(fits) == 1:
            number = fits[0]["number"]
        elif action not in ("ready", "in_taxi"):
            # READY / IN with no number: outcall_command picks the outcall.
            return OwnerCommand("unclear", None, missing="which",
                                candidates=tuple(b["number"] for b in (fits or rows)), **words)
    booking = next((b for b in rows if b["number"] == number), None)
    timed = answer.get("start") is not None or answer.get("in_minutes") is not None
    if action in ("no", "cancel") and timed:
        # Turned down with another time ("not today, tomorrow 15:00"; "cancel
        # the 15:00, make it 17:00"): that time is what she offers him.
        action = "time"
    if action in ("yes", "none") and timed and not (action == "none" and words["tell"]):
        # A yes, or words alone, that come with a time: the time is never
        # dropped. By his time (or ready by it) the yes stands; any other
        # time is asked about rather than confirmed or offered on a guess.
        at = _moment(answer, now, tz_name)
        if booking is None or at is None:
            return OwnerCommand("unclear", number, missing="time", **words)
        asked = _asked_start(booking)
        # A delay ("yes, ready in 10") is a yes when she is ready by his
        # time; a clock time only when it is his time.
        delay = answer.get("start") is None
        if action == "yes" and (at <= asked + READY_SLACK if delay else abs(at - asked) <= READY_SLACK):
            return OwnerCommand("stays" if is_settled(booking) else "yes", number, **words)
        who = _him(booking, t)
        offer = _offer(number, at, booking)
        offered, his = offer.split(" ", 1)[1], _short_at(booking, asked, t)
        key = ("ask_confirm_or_offer" if action == "yes" else "ask_offer_instead_booked" if is_settled(booking)
               else "ask_offer_or_confirm")
        question = t.say(key, n=number, who=who, his=his, offered=offered, offer=offer)
        return OwnerCommand("unclear", number, **{**words, "question": question})
    if action == "yes":
        settled = booking is not None and is_settled(booking)
        return OwnerCommand("stays" if settled else "yes", number, **words)
    if action not in ("ready_at", "time"):
        return OwnerCommand(action, number, **words)
    # A moment she named.
    if booking is None:
        return OwnerCommand("unclear", number, **words)
    at = _moment(answer, now, tz_name)
    if at is None:
        return OwnerCommand("unclear", number, missing="time", **words)
    asked = _asked_start(booking)
    # Ready by the time he asked for, or that very time: a yes.
    if at == asked or (action == "ready_at" and at <= asked + READY_SLACK):
        if is_settled(booking) and at != asked:
            # Already his: the time she will be ready is kept for her line
            # (nothing goes to him unless she says to tell him).
            local = at.astimezone(tzinfo_for(booking["tz"]))
            return OwnerCommand("stays", number, at=local.time(), day=local.date(), **words)
        return OwnerCommand("stays" if is_settled(booking) else "yes", number, **words)
    local = at.astimezone(tzinfo_for(booking["tz"]))
    return OwnerCommand("propose", number, at=local.time(), day=local.date(), **words)


def _moment(answer: Mapping[str, Any], now: datetime, tz_name: str) -> Optional[datetime]:
    """The moment an answer names (start, or in_minutes from now), as a
    start: that minute or the next five-minute mark."""
    if answer.get("start"):
        at = parse_local(answer["start"], tz_name)
    elif answer.get("in_minutes") is not None:
        at = now + timedelta(minutes=answer["in_minutes"])
    else:
        at = None
    return ready_time(at) if at is not None else None


def _asked_start(booking: Mapping[str, Any]) -> datetime:
    """The start he asked for: the move he asked for, else the booking's."""
    return booking["proposed_starts_at"] if booking.get("proposed_by") == bs.CUSTOMER else booking["starts_at"]


_NO_CHANGE = ("none", "stays")


def _joined(first: str, second: str) -> str:
    if not first or not second or second.lower() in first.lower():
        return first or second
    return f"{first}; {second}"


def _merge(first: OwnerCommand, second: OwnerCommand) -> OwnerCommand:
    """Two answers about one booking as one command: the change one of them
    makes, and everything both carry. Two different changes: she is asked."""
    changes = [c for c in (first, second) if c.kind not in _NO_CHANGE]
    if len(changes) == 2 and (changes[0].kind, changes[0].at, changes[0].day) != \
            (changes[1].kind, changes[1].at, changes[1].day):
        base = replace(first, kind="unclear", at=None, day=None, missing="", question="")
    elif changes:
        base = changes[0]
    else:
        base = first if first.kind == "stays" else second
    return replace(
        base,
        minutes=first.minutes if first.minutes is not None else second.minutes,
        tell=_joined(first.tell, second.tell), ask=_joined(first.ask, second.ask),
        extras_ok=tuple(dict.fromkeys(first.extras_ok + second.extras_ok)),
        extras_no=tuple(dict.fromkeys(first.extras_no + second.extras_no)),
        for_me=first.for_me or second.for_me, question=base.question or first.question or second.question,
    )


def owner_commands_from(
    answers: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], *, now: datetime, tz_name: str,
    replied: Optional[Mapping[str, Any]] = None, outcall_taxi: bool = True, typed: Optional[str] = None,
    t: booker_texts.Texts = booker_texts.ENGLISH,
) -> list[OwnerCommand]:
    """What the reader found (OwnerReading.answers) as the commands it
    amounts to for the bookings in `rows`: one per booking, in the order
    she named them, each carrying what goes on to him (tell, ask, extras),
    the length she set and whether she asked something herself. A booking she
    did not name is the replied-to request when her message holds one answer
    only (with several, "tell the other one ..." must not land on the quoted
    one), else the only one that fits; when several fit, the command is
    `unclear` (missing "which") so she is asked which one. `typed`: her
    message — a booking number the model gives that is neither open nor in
    it is a guess, and she is asked instead. `t`: her words, for the
    questions to her a reading may come with."""
    commands: list[OwnerCommand] = []
    replied = replied if len(answers) == 1 else None
    for answer in answers:
        cmd = _command_from(answer, rows, now=now, tz_name=tz_name, replied=replied, outcall_taxi=outcall_taxi,
                            typed=typed, t=t)
        if cmd is None:
            continue
        if cmd.kind == "list":
            if not any(c.kind == "list" for c in commands):
                commands.append(cmd)
            continue
        same = next((i for i, c in enumerate(commands) if cmd.number is not None and c.number == cmd.number), None)
        if same is None:
            commands.append(cmd)
        else:
            commands[same] = _merge(commands[same], cmd)
    return _unnamed_asked(commands, rows, typed)


# Her words mean every booking waiting for her answer ("both fine", "all ok").
_ALL = re.compile(r"(?i)\b(?:both|all|each|every|оба|обе|обоих|все|всех|abi|abas|abiem|visi|visas|visiem|ambos|"
                  r"ambas|todos|todas|los\s+dos|las\s+dos)\b")


def _names_booking(booking: Mapping[str, Any], typed: str) -> bool:
    """Her message names this booking: its number, or the customer's name."""
    if booking.get("number") in _typed_numbers(typed):
        return True
    name = " ".join(str(booking.get("customer_name") or "").split()).casefold()
    return len(name) >= 2 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", typed.casefold()) is not None


def _unnamed_asked(commands: list[OwnerCommand], rows: Sequence[Mapping[str, Any]],
                   typed: Optional[str]) -> list[OwnerCommand]:
    """A yes / no the reading gives to several bookings waiting for her answer
    when her message names none of them and does not say "both" or "all"
    ("yes i confirm" with two requests waiting): never carried out on the
    reading's word. The ones she named stay; for the others she is asked
    (yes or no, or which one)."""
    if typed is None or _ALL.search(typed):
        return commands
    waiting = {b["number"]: b for b in rows if awaits_owner(b)}
    changing = [c for c in commands if c.kind in ("yes", "no") and c.number in waiting]
    if len(changing) < 2:
        return commands
    unnamed = [c for c in changing if not _names_booking(waiting[c.number], typed)]
    if not unnamed:
        return commands
    kept = [c for c in commands if c not in unnamed]
    first = unnamed[0]
    if len(unnamed) == 1:
        return kept + [replace(first, kind="unclear", at=None, day=None)]
    return kept + [replace(first, kind="unclear", number=None, missing="which",
                           candidates=tuple(c.number for c in unnamed), at=None, day=None)]


def named_in(typed: str, rows: Sequence[Mapping[str, Any]]) -> set[int]:
    """The bookings her message names: by number, or by the customer's name."""
    return {b["number"] for b in rows if _names_booking(b, typed)}


def standing_for_all(standing: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """Her instruction for every customer, as read — "" when it names one of
    them ("after today don't take Juris again"): a remark about one man
    stays private, it is no rule."""
    if standing and any(_names_booking({"customer_name": b.get("customer_name")}, standing) for b in rows):
        return ""
    return standing


def owner_command_from(
    data: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, now: datetime, tz_name: str,
    replied: Optional[Mapping[str, Any]] = None, outcall_taxi: bool = True, typed: Optional[str] = None,
    t: booker_texts.Texts = booker_texts.ENGLISH,
) -> Optional[OwnerCommand]:
    """One answer of the reader as its command (see owner_commands_from).
    None: nothing to do (an outcall step while the taxi steps are off)."""
    commands = owner_commands_from([data], rows, now=now, tz_name=tz_name, replied=replied,
                                   outcall_taxi=outcall_taxi, typed=typed, t=t)
    return commands[0] if commands else None


@dataclass(frozen=True)
class OwnerPlan:
    """What is done with her message once it is read (BookingFlow, and
    owner_check exactly the same): the commands, carried out in order; and
    the one booking she is asked about (`ask`; 0: the one waiting for her
    answer, or which of those), with what of hers waits on that question
    (`ask_with`: repeated in it, held for her answer) — the commands for
    her other bookings still go. `earlier_lost`: her typed command went
    through as she typed it, without what she said before it (the message
    it answers) — she is told so."""
    commands: tuple[OwnerCommand, ...] = ()
    ask: Optional[int] = None
    ask_with: Optional[OwnerCommand] = None
    earlier_lost: bool = False


# A typed YES / NO and the reading of the same booking that agree: the
# reading may add her words (a length, extras, what to tell him) to it.
def _agrees(typed: str, read: str) -> bool:
    return typed == read or (typed == "yes" and read in ("stays", "none")) or \
        (typed in ("no", "cancel", "done", "noshow") and read == "none")


def _carries(cmd: OwnerCommand) -> bool:
    """It holds something of hers beyond the change: words, a length, extras."""
    return cmd.has_words or cmd.minutes is not None


# A typed command the reading turns into its opposite: never carried out on
# the reading's word.
def _contradicts(typed: str, read: str) -> bool:
    return (typed in ("no", "cancel", "noshow") and read in ("yes", "stays")) or \
        (typed == "yes" and read in ("no", "cancel", "noshow"))


def _typed_kind(kind: str, number: Optional[int], settled: Collection[int],
                waiting: Optional[Collection[int]]) -> str:
    """Her typed YES / NO on a booking: a YES to one already confirmed
    changes nothing but what she adds; otherwise a YES / NO on one that
    does not wait for her answer (confirmed, or over) is her words only —
    a NO to his question is no decline of his booking (CANCEL is)."""
    if kind == "yes" and number in settled:
        return "stays"
    if kind in ("yes", "no") and waiting is not None and number not in waiting:
        return "none"
    return kind


def plan_owner_answer(typed: Optional[OwnerCommand], commands: Sequence[OwnerCommand], *, standing: str = "",
                      held_words: bool = False, settled: Collection[int] = (),
                      waiting: Optional[Collection[int]] = None, focus: Collection[int] = (),
                      named: Optional[Collection[int]] = None) -> OwnerPlan:
    """Her message as read (`commands`, owner_commands_from) against what
    she typed, if it parses as a command (`typed`, parse_owner_reply).
    `settled`: the bookings confirmed with nothing to answer; `waiting`:
    those waiting for her answer; `focus`: the bookings the bot's open
    question to her (or the message she quoted) is about; `named`: those her
    message names (named_in).

    - a strict command (read only because it answers the bot's question,
      or because its booking is in doubt) goes through as she typed it, for
      the booking she typed; the reading only adds her words to it when it
      agrees. `held_words`: the message the question was about had words, a
      length or extras, which are then lost when the reading has none;
    - a plain YES / NO without a number takes the one booking of the
      question (`focus`) the reading agrees on; with no question open and
      several requests waiting, she is asked which — never the reading's
      pick; words alone for a booking that does not wait stay words;
    - a loose one ("YES 3 tell him cash") is the reading's — keeping her
      typed change when the reading has only her words for it; when the
      reading has nothing for the booking she typed, or turns her yes into
      a no or her no into a yes, she is asked about it — her words for
      other bookings, and changes to bookings she named, still go (a change
      to one she did not name is the misreading itself);
    - a loose YES / NO read as about no booking ("no problem babe"): she is
      asked about the request waiting for her, never left thinking it
      counted;
    - otherwise every command of the reading."""
    if typed is None:
        return OwnerPlan(tuple(commands))
    if not typed.loose:
        n = typed.number
        if n is None:
            if not focus and waiting is not None and len(waiting) > 1:
                return OwnerPlan(ask=0)
            fits = [c for c in commands if c.number is not None and _agrees(typed.kind, c.kind)
                    and (not focus or c.number in focus)]
            if len(fits) != 1:
                return OwnerPlan(ask=0)
            fit = fits[0]
            kind = _typed_kind(typed.kind if fit.kind == "none" else fit.kind, fit.number, settled, waiting)
            return OwnerPlan((replace(fit, kind=kind),), earlier_lost=held_words and not _carries(fit))
        read = next((c for c in commands if c.number == n), None)
        if read is not None and _agrees(typed.kind, read.kind):
            merged = replace(typed, kind=_typed_kind(typed.kind, n, settled, waiting), minutes=read.minutes,
                             tell=read.tell, ask=read.ask, extras_ok=read.extras_ok, extras_no=read.extras_no,
                             for_me=read.for_me)
            # The reading agrees but has none of what she said before: she is
            # told that part did not go.
            return OwnerPlan((merged,), earlier_lost=held_words and not _carries(read))
        lost = held_words or (read is not None and _carries(read))
        return OwnerPlan((typed,), earlier_lost=lost)
    if typed.number is not None:
        read = next((c for c in commands if c.number == typed.number), None)
        others = tuple(c for c in commands if c.number != typed.number and (
            c.kind in ("none", "stays") or named is None or c.number in named))
        if read is None or _contradicts(typed.kind, read.kind):
            return OwnerPlan(others, ask=typed.number, ask_with=read if read is not None and _carries(read) else None)
        if read.kind == "none" and typed.kind in ("yes", "no", "cancel", "done", "noshow"):
            # The reading kept only her words for it ("CANCEL 6 tell him I'm
            # sick" read as words): her typed change goes with them.
            kept = replace(read, kind=_typed_kind(typed.kind, typed.number, settled, waiting))
            return OwnerPlan(tuple(kept if c is read else c for c in commands))
    if not commands and not standing:
        return OwnerPlan(ask=typed.number if typed.number is not None else 0)
    return OwnerPlan(tuple(commands))


# --------------------------------- her answer, back to her and on to him


def _extras_said(ok: Sequence[str], no: Sequence[str], t: booker_texts.Texts) -> list[str]:
    """The extras she takes / refuses, as she can recognise them."""
    return [t.say("extra_yes", extra=x) for x in ok] + [t.say("extra_no", extra=x) for x in no]


def passing_on_words(cmd: OwnerCommand, *, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """What goes on to him from her answer, for her acknowledgement, so she
    can catch a misread: " I'll tell him: bring cash; no anal. I'll ask him:
    does he want 2 hours?" ("" when nothing goes to him)."""
    told = ([cmd.tell.rstrip(". ")] if cmd.tell else []) + _extras_said(cmd.extras_ok, cmd.extras_no, t)
    text = " " + t.say("words_tell", items="; ".join(told)) if told else ""
    if cmd.ask:
        question = cmd.ask.rstrip() + ("" if cmd.ask.rstrip().endswith("?") else "?")
        text += " " + t.say("words_ask", question=question)
    return text


def format_words_ack(booking: Mapping[str, Any], cmd: OwnerCommand, *,
                     t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her words for him alone (nothing about the booking changes)."""
    text = t.say("words_ack", n=booking["number"], who=_him(booking, t)) + passing_on_words(cmd, t=t)
    return text + (" " + t.say("answer_comes") if cmd.ask else "")


def format_facts(booking: Mapping[str, Any], *, outcall_taxi: bool = False, proof: bool = False,
                 t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her question about a booking ("how long?", "is it outcall?"), answered
    from the booking itself."""
    n, state = booking["number"], booking["state"]
    where = t.say("facts_outcall" if is_outcall(booking) else "facts_incall")
    lines = [t.say("facts_head", n=n, who=_who(booking, t), when=when_for(booking, t),
                   length=t.minutes(booking_minutes(booking)), where=where)]
    if state in (bs.REQUESTED, bs.PENDING):
        lines.append(t.say("facts_you_proposed", proposed=proposal_for(booking, t)) if
                     booking.get("proposed_by") == bs.OWNER else t.say("facts_waiting"))
    elif state == bs.CONFIRMED:
        line = t.say("facts_confirmed")
        if booking.get("proposed_by") == bs.CUSTOMER:
            line += " " + t.say("facts_move", to=proposal_for(booking, t))
        elif booking.get("proposed_by") == bs.OWNER:
            line += " " + t.say("facts_you_proposed_move", proposed=proposal_for(booking, t))
        if outcall_taxi and is_outcall(booking):
            line += " " + t.say("facts_outcall_step", step=outcall_step(booking, proof=proof, t=t))
        elif booking.get("arrived_at"):
            line += " " + t.say("facts_arrived")
        lines.append(line)
    else:
        over = {bs.CANCELLED: "facts_cancelled", bs.COMPLETED: "facts_completed", bs.NO_SHOW: "facts_no_show"}
        lines.append(t.say(over[state]) if state in over else state)
    lines.append(t.say("facts_extras", extras=booking.get("extras") or t.say("facts_none")))
    if booking.get("extras_ok") or booking.get("extras_no"):
        lines.append(t.say("facts_extras_said", ok=booking.get("extras_ok") or "-", no=booking.get("extras_no") or "-"))
    if booking.get("service"):
        lines.append(t.say("facts_service", service=booking["service"]))
    if booking.get("notes"):
        lines.append(t.say("facts_notes", notes=booking["notes"]))
    if booking.get("booker_asked_at"):
        lines.append(t.say("facts_asked"))
    return "\n".join(lines)


def _held_items(cmd: Optional[OwnerCommand], t: booker_texts.Texts = booker_texts.ENGLISH) -> list[str]:
    """What of her answer waits on a question to her, as she can recognise it."""
    if cmd is None:
        return []
    items = [t.say("held_for", length=t.minutes(cmd.minutes))] if cmd.minutes else []
    return items + ([t.say("held_tell", words=cmd.tell)] if cmd.tell else []) + \
        _extras_said(cmd.extras_ok, cmd.extras_no, t) + ([t.say("held_ask", words=cmd.ask)] if cmd.ask else [])


def _held(cmd: Optional[OwnerCommand], t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """What of her answer waits until she says which booking, or yes or no:
    said back with the question, so nothing she wrote is lost from view."""
    items = _held_items(cmd, t)
    return " " + t.say("held", items="; ".join(items)) if items else ""


def format_held_lost(number: int, cmd: Optional[OwnerCommand], *,
                     t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her plain command came long after the question it answers: what she
    said with that question did not go with it."""
    items = _held_items(cmd, t)
    return t.say("held_lost", n=number, what=f" ({'; '.join(items)})" if items else "")


def format_which_one(rows: Sequence[Mapping[str, Any]], cmd: Optional[OwnerCommand] = None, *,
                     t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Several bookings fit her answer: which one? (With what she said for
    it, `cmd`, repeated.)"""
    if not rows:
        return t.say("which_about") + _held(cmd, t)
    named = [f"#{b['number']} {b.get('customer_name') or t.say('unknown')}, {_short_when(b, t)}" for b in rows]
    listed = named[0] if len(named) == 1 else t.say("or_last", items=", ".join(named[:-1]), last=named[-1])
    return t.say("which_one", listed=listed) + _held(cmd, t)


def format_question(cmd: OwnerCommand, booking: Optional[Mapping[str, Any]] = None, *,
                    t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """The one short question she is asked when her answer can't be carried
    out as it stands."""
    if booking is None:
        return (cmd.question or t.say("which_about")) + _held(cmd, t)
    n = booking["number"]
    if cmd.question:
        return (cmd.question if f"#{n}" in cmd.question else f"#{n}: {cmd.question}") + _held(cmd, t)
    who = _him(booking, t)
    if cmd.missing == "time":
        return t.say("question_time", n=n, who=who) + _held(cmd, t)
    return t.say("question_yes_no", n=n, who=who, when=_short_when(booking, t)) + _held(cmd, t)


def read_failed(numbers: Sequence[int], *, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her words could not be read (the model is down or out of budget)."""
    n = str(numbers[0]) if len(numbers) == 1 else t.say("number_placeholder")
    return t.say("read_failed", n=n)


# The fixed lines below, in English, under their old names; what goes to
# her is t.say(<key>) in her language.
#
# A loose YES / NO / ... carried out as typed because her words could not be
# read: she is told the rest of her message did nothing.
READ_REST_FAILED = booker_texts.ENGLISH.say("read_rest_failed")
# Her typed command answered the bot's question, and what she wrote before
# it did not fit it (she typed NO where she had asked for a yes with words).
EARLIER_NOT_APPLIED = booker_texts.ENGLISH.say("earlier_not_applied")
# Her words for him while his chat is not being answered (paused, taken
# over, the account off): they wait, and are dropped when too old.
WORDS_WAIT = booker_texts.ENGLISH.say("words_wait")


def words_after_quiet_hours(until: datetime, *, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her words for him while replies wait for the quiet hours to end."""
    return t.say("words_after_quiet_hours", until=f"{until:%H:%M}")


# Her message could not be read and there is no booking to put it on.
READ_NOTHING_FAILED = booker_texts.ENGLISH.say("read_nothing_failed")
# The same for a plain command that answered the bot's question about her
# message before it (the length, the words for him in that one).
READ_EARLIER_FAILED = booker_texts.ENGLISH.say("read_earlier_failed")


# Words after a typed command that may turn it into something else: a no
# ("ok, not today") turns a YES; another time, day or length ("yes at 9",
# "yes 3 hours", "no 3, tomorrow at 15:00 instead", "ok 9pm", "half an hour",
# "jā, rīt", "si, manana", "да, к 9", "ok friday") turns any of them; so does
# any other number than the booking's. When they can't be read, the command
# does not stand alone. Phrases that say yes in no-words ("no problem", "не
# вопрос", "no hay problema") are no no.
_YES_PHRASES = re.compile(
    r"(?i)\b(?:no\s+problem\w*|no\s+prob|no\s+worries|don'?t\s+worry|не\s+вопрос|без\s+проблем|"
    r"nav\s+problēm\w*|nav\s+problem\w*|no\s+hay\s+problema|no\s+pasa\s+nada|sin\s+problema)\b")
_DOUBT_NO = re.compile(r"(?i)\b(?:no|not|nope|never|don'?t|can'?t|won'?t|cannot|нет|не|нельзя|nē|ne|nevar|nunca)\b")
_DOUBT_TIME = re.compile(
    r"(?i)\b(?:later|earlier|sooner|tomorrow|tonight|tonite|after|before|instead|half|quarter|by|around|till|"
    r"until|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|"
    r"позже|попозже|раньше|завтра|после|через|полчас\w*|четверть|вечер\w*|утр\w*|ноч\w*|понедельник\w*|"
    r"вторник\w*|сред[ауы]|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*|выходн\w*|"
    r"vēlāk|velak|agrāk|agrak|rīt|rit|pēc|pec|pusstund\w*|ceturksn\w*|vakar\w*|pirmdien\w*|otrdien\w*|"
    r"trešdien\w*|tresdien\w*|ceturtdien\w*|piektdien\w*|sestdien\w*|svētdien\w*|svetdien\w*|"
    r"mañana|manana|después|despues|luego|antes|tarde|media|noche|lunes|martes|miércoles|miercoles|jueves|"
    r"viernes|sábado|sabado|domingo|"
    r"hours?|hrs?|hs|час\w*|минут\w*|stund\w*|minūt\w*|minut\w*|horas?)\b"
    r"|\b(?:at|в|во|к|на|до|около|plkst\.?|uz|ap|līdz|lidz|um|en|para|sobre|hasta)\s*\d{1,2}\b"
    r"|\b(?:a|para|sobre|hasta)\s+las?\s*\d{1,2}\b|\b\d{1,2}\s*(?:am|pm)\b|\bin\s+\d+|\d{1,2}[:.]\d{2}"
    r"|\b\d+\s*(?:h|hs|hrs?|hours?|min|mins|minutes?|st|ч|час\w*|мин\w*|stund\w*|min[ūu]t\w*|horas?)\b")


def loose_doubt(text: str, kind: str = "yes", number: Optional[int] = None) -> bool:
    """Her typed command (`kind`, on booking `number`) has words after it
    that may change what it means: for a YES a no, and for any of them
    another time, day or length, or another number than the booking's."""
    words = (text or "").strip().split(None, 1)
    if len(words) < 2:
        return False
    rest = words[1]
    if any(int(d) != number for d in re.findall(r"\b\d{1,2}\b", rest)):
        return True
    return bool(_DOUBT_TIME.search(rest) or (kind == "yes" and _DOUBT_NO.search(_YES_PHRASES.sub(" ", rest))))


def format_standing_saved(text: str, *, t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    return t.say("standing_saved", text=text)


def booker_news_item(cmd: OwnerCommand, *, at: datetime) -> dict[str, Any]:
    """Her words for his next reply, as kept in bookings.booker_news until a
    reply carries them."""
    return {"id": uuid.uuid4().hex, "at": at.isoformat(), "tell": cmd.tell, "ask": cmd.ask,
            "extras_ok": list(cmd.extras_ok), "extras_no": list(cmd.extras_no)}


BOOKER_WORDS_RULE = (
    "Say it as yourself, in your own words and their language, first person, short. Never mention "
    "anyone else (no booker, owner, manager or friend, no 'she says'), never 'let me check'. Keep "
    "every fact exactly as given, and add no price, time, address or promise that is not in it."
)


def booker_news_line(items: Sequence[Mapping[str, Any]]) -> str:
    """Her words for him (booker_news items), for the reply writer. The bot
    is the girl herself, so it is said in the first person."""
    tell = [str(i["tell"]) for i in items if i.get("tell")]
    ask = [str(i["ask"]) for i in items if i.get("ask")]
    # Her last word on each extra wins ("no anal" ... "ok, anal is fine").
    said: dict[str, tuple[bool, str]] = {}
    for item in items:
        for extra in item.get("extras_ok") or ():
            said[str(extra).lower()] = (True, str(extra))
        for extra in item.get("extras_no") or ():
            said[str(extra).lower()] = (False, str(extra))
    yes = [name for ok, name in said.values() if ok]
    no = [name for ok, name in said.values() if not ok]
    parts = []
    if tell:
        parts.append("tell them " + "; ".join(f'"{t}"' for t in tell))
    if yes:
        parts.append("these extras are fine for them: " + ", ".join(yes))
    if no:
        parts.append("these extras are NOT on offer for them: " + ", ".join(no))
    if ask:
        parts.append("ask them " + "; ".join(f'"{q}"' for q in ask))
    if not parts:
        return ""
    return _NEWS + "; ".join(parts) + ". " + BOOKER_WORDS_RULE


def _said(items: Sequence[Mapping[str, Any]], t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her words in booker_news items, as she can recognise them."""
    parts = []
    for item in items:
        told = ([str(item["tell"])] if item.get("tell") else []) + \
            _extras_said(item.get("extras_ok") or (), item.get("extras_no") or (), t)
        if told:
            parts.append("; ".join(told))
        if item.get("ask"):
            parts.append(t.say("held_ask", words=item["ask"]))
    return " / ".join(f'"{part}"' for part in parts)


def format_words_dropped(booking: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                         t: booker_texts.Texts = booker_texts.ENGLISH) -> str:
    """Her words that no reply carried in time (his chat was not answered,
    or the meeting is over since): dropped, and she is told which."""
    return t.say("words_dropped", n=booking["number"], who=_him(booking, t), said=_said(items, t))


def refused_extras_line(extras: Sequence[str]) -> str:
    """Extras she refused for this customer: never offered again in the chat."""
    if not extras:
        return ""
    return ("Extras NOT on offer for them, whatever your lists say: " + ", ".join(extras) + ". Never offer, "
            "agree to or price these; if they ask, a short no in your own words.")


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
    bs.NOTICE_LENGTH: "the appointment now lasts a different time: it is {when}. Tell them, restating when it starts and ends.",
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


def standing_line(booking: Mapping[str, Any], *, outcall_taxi: bool = False, proof: bool = False,
                  meets_outside: bool = False) -> str:
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
            return line + " " + outcall_taxi_line(booking, proof=proof)
        if booking.get("instructions_sent_at"):
            line += " They have arrived and were already sent the entry instructions."
        elif meets_outside and booking.get("arrived_at"):
            line += (" They have arrived and you are coming out to meet them: say so in a few words of your "
                     "own. " + NO_DIRECTIONS_RULE)
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
    "The street address is set. Write [address] on a line of its own where it should go (with your prices, "
    "and again when they ask where you are) and the system puts the address there, "
    "word for word, as a message of its own. Never write the address yourself."
)
# booking.address_timing "when_asked".
ADDRESS_WHEN_ASKED_LINE = (
    "The street address is set. Write [address] on a line of its own only when they ask where you are "
    "(not with your prices) and the system puts the address there, word for word, as a message of its "
    "own. Never write the address yourself."
)


def address_tag_line(timing: str) -> str:
    return ADDRESS_WHEN_ASKED_LINE if timing == "when_asked" else ADDRESS_TAG_LINE


# Their message asks where she is (booking.address_timing "when_asked"):
# EN, LV, RU, ET and the usual short forms.
_ASKS_WHERE = re.compile(
    r"(?i)(\bwhere\b|\bwher\b|\bwhr\b|addr?ess?|\badres|\blocat|\bloc\b|\bpin\b|\barea\b|\bstreet\b|"
    r"\bkur\b|adrese|atrodies|\bkus\b|aadress|asukoht|где|адрес|локац|район|улиц|куда)"
)


def asks_where(text: str) -> bool:
    return bool(_ASKS_WHERE.search(text or ""))


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
                        has_address: bool, proof: bool = False) -> str:
    """The news of a confirmed outcall's taxi steps. `proof`: he is asked
    for a photo of the place before the taxi (booking.outcall_proof)."""
    when = describe_when(booking)
    notice = booking.get("customer_notice")
    if notice == bs.NOTICE_READY:
        if booking.get("proof_at"):
            line = ("their photo of the place reached you, all good. In your own words, short: ask them to order "
                    "a taxi for you now and to send you its link here, so you can see it coming.")
        else:
            line = (f"you are ready for the outcall on {when}. In your own words, short: ask them to order a "
                    "taxi for you now and to send you its link here, so you can see it coming.")
        if has_address:
            line += " Your address, for the taxi, follows in a separate message."
        return _NEWS + line
    if notice == bs.NOTICE_IN_TAXI:
        return _NEWS + (f"you are in the taxi now, on your way to them for the outcall on {when}. Tell them in "
                        "your own words, and ask only for what you still need to find them: the exact address "
                        "if they have not given it, the room or flat number, the floor, how to get in. Do not "
                        "write [address].")
    if proof:
        return _NEWS + (f"the outcall on {when} has just been CONFIRMED. In your own words, short: tell them it "
                        "is booked, and ask for a photo that shows they are at the place you are coming to (the "
                        "hotel room key card, or the building or door). As soon as it comes they order your "
                        "taxi; do not ask for the taxi yet.")
    soon = booking["starts_at"] - now <= timedelta(minutes=ready_minutes + 60)
    getting_ready = (f"You need about {ready_minutes} minutes to get ready; say you will text them as soon as "
                     "you are ready" if soon else "Say you will text them when you are getting ready")
    return _NEWS + (f"the outcall on {when} has just been CONFIRMED. In your own words: tell them it is booked "
                    f"for that day and time. {getting_ready}, and that they then order the taxi to pick you up. "
                    "Do not ask them to order it before that.")


def outcall_taxi_line(booking: Mapping[str, Any], *, proof: bool = False) -> str:
    """Where a confirmed outcall's taxi steps stand, for every reply."""
    line = "It is an outcall: you go to them by a taxi they order."
    if booking.get("in_taxi_at"):
        return line + (" You are in the taxi on your way to them. If you still need something to find them "
                       "(the exact address, the room or flat, the floor, how to get in), ask for it. Do not "
                       "write [address]: you are coming to them.")
    if booking.get("taxi_link_at"):
        return line + (" They ordered your taxi and sent its link; you are about to get in. Do not write "
                       "[address] again.")
    if booking.get("proof_at"):
        return line + (" Their photo of the place came, and you asked them to order your taxi and send you "
                       "its link.")
    if booking.get("ready_at"):
        return line + (" You are ready and asked them to order a taxi to pick you up at your address and "
                       "to send you its link.")
    if proof:
        return line + (" You asked them for a photo that shows they are at the place (the hotel room key card, "
                       "or the building or door). No taxi until it comes: if they write without it, ask for it "
                       "again in a few words of your own.")
    return line + (" You are getting ready and will text them when you are ready; only then do they order "
                   "the taxi. Do not ask them to order it before that.")


TAXI_LINK_LINE = _NEWS + ("the taxi link they sent has reached you. A short thanks in your own words; do not "
                          "repeat the link.")

# His photo for the proof did not reach her (WhatsApp keeps view-once
# photos from linked devices).
PROOF_AGAIN_LINE = _NEWS + ("the photo they just sent did not come through to you (it may have been 'view "
                            "once'). Ask them, short, to send it again as a normal photo.")

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
