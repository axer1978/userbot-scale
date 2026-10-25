"""Check how the booker's answers in her own words are read, against the real
model. A dry run: the reader is called exactly as the runtime calls it
(booking_flow.BookingFlow.read_owner_answer: her chat with its times and the
requests in it, the open bookings in full, her latest message), what it
found is turned into the commands the runtime would make of it
(bookings.owner_commands_from) and weighed against what she typed, as the
runtime weighs it (bookings.plan_owner_answer); nothing is sent and nothing
is written.

    docker compose exec manager python owner_check.py --tenant 6
    docker compose exec manager python owner_check.py --tenant 6 --runs 3 --verbose

The cases are replay_scenarios/owner-answers.json (--cases):

    "now":      the clock for every case ("2026-10-20T12:40", the tenant's zone)
    "bookings": named sets of bookings, each a list of
                {"number", "customer", "state": requested|pending|confirmed,
                 "start" (local), "minutes" (60), "extras" (what he asked for),
                 "notes", "outcall": true, "move_to" (local: he asks to move it),
                 "her_yes": what she wrote to confirm it ("YES <number>"),
                 "ready": true (she sent READY), "taxi_link": true,
                 "in_taxi": true (she sent IN),
                 "proof": "waiting" | "came" (the outcall photo proof is on:
                          his photo is awaited / it came and went on to her),
                 "arrived": true | his words (an incall: he says he's here),
                 "extras_ok", "extras_no": extras she took / refused already,
                 "asked": her question to him (his answers go to her),
                 "his_answer": his answer to it, passed on to her}
    "cases":    {"id": "<en|ru|lv|es>-...", "bookings": <set>,
                 "latest": what she writes,
                 "reply_to": <number of the request she quotes>,
                 "before": [{"me": ...} | {"them": ...}] before the requests came,
                 "chat": [<line>, ...] after the default chat,
                 "now": another clock for this case,
                 "answering": true | false (see "What she typed"),
                 "expect": "yes 8" | "yes 3 + no 4" | ["stays 6", "none"] | ...,
                 and any of the checks below}

A line of "chat" is {"them": ...} (hers), {"me": ...} (the bot's), or a
question the bot asked her, written by the real formatter so that it stays
right when the wording changes:

    {"me_which": [3, 4]}   which one of these bookings (format_which_one)
    {"me_ask": 8}          about #8: yes or no? (format_question; with
                           "missing": "time": what time should I offer him?,
                           or "question": the question in other words)

either with "held": {"tell", "ask", "minutes", "extras_ok", "extras_no"}:
what she said in the message it asks about, repeated with the question
("With your answer: tell him bring cash").

Her chat holds what the runtime would have sent her: every request (or move
request), then for each confirmed booking her yes and its acknowledgement and
what followed (READY, his photo, the taxi link, IN, his arrival, her question
to him and his answer); then "chat", then "latest". Every line has its time,
as get_history_for_ai(with_times=True) gives it, a minute apart: "before"
from three hours back, the requests from twenty minutes back, what followed
them up to a minute before "latest", which is now.

What she typed counts as the runtime counts it (BookingFlow._owner_message):
"latest" as a command (bookings.parse_owner_reply on the case's day; READY or
IN while the tenant's taxi steps are off is none). A strict one ("YES 8",
"8 15:30") costs no model call and is carried out as typed ("strict, no
model call"), unless it answers the bot's question about her message before
it: a question among the bot's lines after her last message, under half an
hour old, about the booking she typed (a YES / NO without a number answers
any). That is a "me_which" / "me_ask" line, or a "me" line worded as
format_which_one / format_question word it for the case's bookings;
"answering" says it outright for a question in other words. Anything else is
read, and the reading weighed against what she typed: a loose command the
reading has nothing for, or turns into its opposite, gets her a question
instead ("unclear <n>", or "unclear -": which one?, or no answer at all when
nothing waits for her answer), and a strict one goes through as typed, with
what she said before it only when the reading agrees ("her earlier words
lost" when not).

An expectation is one or more commands joined with " + ": all of them must
come back, in any order, and nothing else. A command is
"<kind> [<number>|-] [<HH:MM> | <YYYY-MM-DD> <HH:MM>]": kind is yes, no,
propose, stays, cancel, done, noshow, ready, in_taxi, list, unclear, or none
(the booking does not change: only words for him, or her question to me).
A bare "none" is no command at all: nothing about any booking, so no reply
to her. No number: any; "-": none could be told (she is asked which). Only
propose has a time. Several expectations (a list): any of them passes.

Checks, each optional, on everything that came back (all its commands
together):

    "tell", "ask":  true (some words) | false (none) | [group, ...]
    "words":        the same, on tell and ask together
    "extras_ok", "extras_no": the same, each group naming one extra
    "minutes":      60: a length she set | false: none
    "for_me":       true | false: she asked me something about a booking
    "private":      ["show", ...]: never in tell or ask
    "standing":     true | false | [group, ...]: an instruction for every customer
    "earlier_lost": true | false: her typed command went through without what
                    she said before it, and she is told so
    "from_reading": true: the reading itself must have something for the case
                    (an empty reading that only leads to a question fails)

The first seven may instead be checked on each booking's own command, by its
number: "tell": {"4": ["cash"], "3": false}. A booking with no command fails
every such check but false.

A group is a word, or a list of words of which any one will do; it must be in
the text (any case).

Each case read costs one model call on the tenant's own DeepSeek key, as
replay.py does.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import httpx

import ai_responder
import bookings
import booking_states as bs
import database
import pg
import tenants

CASES = Path(__file__).with_name("replay_scenarios") / "owner-answers.json"
KINDS = {"yes", "no", "propose", "stays", "cancel", "done", "noshow", "ready", "in_taxi", "list", "unclear", "none"}
TAXI_LINK = "https://bolt.eu/ride/x7k2"

# What a booking in a set may say, and a case.
BOOKING_KEYS = {"number", "customer", "state", "start", "minutes", "extras", "notes", "outcall", "move_to", "her_yes",
                "ready", "taxi_link", "in_taxi", "proof", "arrived", "extras_ok", "extras_no", "asked", "his_answer"}
_FLAGS = ("outcall", "ready", "taxi_link", "in_taxi")
_TEXTS = ("customer", "extras", "notes", "her_yes", "extras_ok", "extras_no", "asked", "his_answer")
TEXT_CHECKS = ("tell", "ask", "words", "standing")
CHECKS = (*TEXT_CHECKS, "extras_ok", "extras_no", "minutes", "for_me", "private", "earlier_lost")
# The checks that may be made on each booking's own command instead.
PER_BOOKING = ("tell", "ask", "words", "extras_ok", "extras_no", "minutes", "for_me")
CASE_KEYS = {"id", "bookings", "latest", "reply_to", "before", "chat", "now", "answering", "from_reading", "expect",
             *CHECKS}
# What a question of the bot's in a case's chat may repeat of hers.
HELD_KEYS = {"tell", "ask", "minutes", "extras_ok", "extras_no"}

# Where her chat starts before her latest message: what she said before the
# requests came, and the requests.
BEFORE = timedelta(hours=3)
REQUESTS = timedelta(minutes=20)
# How long the bot's question to her stays open for a strict command of
# hers to answer it (booking_flow.ANSWER_WINDOW).
ANSWER_WINDOW = timedelta(minutes=30)
_AT = re.compile(r"^(\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}$")

# A command as it is judged: (kind, number, time of a propose).
Command = tuple[str, Optional[int], Optional[str]]


def make_booking(spec: Mapping[str, Any], tz: str, now: datetime) -> dict[str, Any]:
    """A booking as booking_store returns it, with every column the reader
    and the runtime's answers to her look at. The times of its steps (READY,
    his photo, ...) are set again by build() to the times of their lines in
    her chat."""
    n = spec["number"]
    start = bookings.parse_local(spec["start"], tz)
    end = start + timedelta(minutes=spec.get("minutes", 60))
    notes = spec.get("notes", "")
    if spec.get("outcall") and "outcall" not in notes.lower():
        notes = ("Outcall. " + notes).strip()
    earlier = now - timedelta(days=1)
    b = {"id": n, "number": n, "chat_id": 9000 + n, "state": spec["state"], "starts_at": start, "ends_at": end,
         "blocked_until": end, "tz": tz, "customer_name": spec["customer"], "customer_username": None, "service": "",
         "notes": notes, "extras": spec.get("extras", ""), "extras_ok": spec.get("extras_ok", ""),
         "extras_no": spec.get("extras_no", ""), "proposed_by": None, "proposed_starts_at": None,
         "proposed_ends_at": None, "provider_message_id": 1000 + n, "provider_wa_message_id": None,
         "customer_notice": None, "created_at": earlier,
         "decided_at": earlier if spec["state"] == bs.CONFIRMED else None, "arrived_at": None,
         "instructions_sent_at": None, "ready_at": None, "taxi_link": None, "taxi_link_at": None, "in_taxi_at": None,
         "in_taxi_by": None, "proof_at": None, "booker_news": [], "booker_asked_at": None}
    if spec.get("move_to"):
        moved = bookings.parse_local(spec["move_to"], tz)
        b.update(proposed_by=bs.CUSTOMER, proposed_starts_at=moved, proposed_ends_at=moved + (end - start))
    if spec.get("proof") == "came":
        b.update(proof_at=now, ready_at=now)  # as BookingFlow.mark_proof sets them
    if spec.get("ready"):
        b["ready_at"] = now
    if spec.get("taxi_link"):
        b.update(taxi_link=TAXI_LINK, taxi_link_at=now)
    if spec.get("in_taxi"):
        b.update(in_taxi_at=now, in_taxi_by=bs.OWNER)
    if spec.get("arrived"):
        b["arrived_at"] = now
    if spec.get("asked"):
        b["booker_asked_at"] = now
    return b


def her_chat(rows: Sequence[dict[str, Any]], girl: str, *, outcall_taxi: bool, proof: bool = False,
             specs: Optional[Mapping[int, Mapping[str, Any]]] = None) -> list[dict[str, Any]]:
    """What the runtime would have sent her about these bookings, and her
    answers so far, oldest first, as get_history_for_ai returns it (build()
    adds the times): every request first, then for each confirmed booking
    her yes and what came after it. A line that is a step of a booking names
    the booking and the columns that hold its time, under "steps"."""
    specs = specs or {}
    chat: list[dict[str, Any]] = []

    def say(role: str, text: str, booking: Optional[dict[str, Any]] = None, *columns: str) -> None:
        chat.append({"role": role, "content": text, "steps": [(booking, column) for column in columns]})

    for b in rows:
        if b.get("proposed_by") == bs.CUSTOMER:
            say("assistant", bookings.format_move_request(b, name=girl))
        else:
            say("assistant", bookings.format_request(b, name=girl), b, "created_at")
    for b in rows:
        if b["state"] != bs.CONFIRMED or b.get("proposed_by") == bs.CUSTOMER:
            continue
        n, spec = b["number"], specs.get(b["number"], {})
        say("user", spec.get("her_yes") or f"YES {n}")
        say("assistant", bookings.format_acknowledgement(b, "confirm", outcall_taxi=outcall_taxi, proof=proof),
            b, "decided_at")
        if b.get("proof_at"):
            say("assistant", bookings.format_proof_header(b), b, "proof_at", "ready_at")
            say("assistant", f"[photo from booking #{n}]")
        elif b.get("ready_at"):
            say("user", f"READY {n}", b, "ready_at")
            say("assistant", bookings.format_acknowledgement(b, "ready"))
        if b.get("taxi_link"):
            say("assistant", bookings.format_taxi_link(b, f"here {TAXI_LINK}", 5), b, "taxi_link_at")
        if b.get("in_taxi_at"):
            say("user", f"IN {n}", b, "in_taxi_at")
            say("assistant", bookings.format_acknowledgement(b, "in_taxi"))
        if b.get("arrived_at"):
            said = spec["arrived"] if isinstance(spec.get("arrived"), str) else ""
            say("assistant", bookings.format_arrived(b, said, way_in_sent=False, name=girl), b, "arrived_at")
        if b.get("booker_asked_at"):
            asked = bookings.OwnerCommand("none", n, ask=spec.get("asked", ""))
            say("assistant", bookings.format_words_ack(b, asked), b, "booker_asked_at")
            if spec.get("his_answer"):
                say("assistant", bookings.format_passed_on(b, spec["his_answer"]))
    return chat


def held_command(held: Mapping[str, Any]) -> bookings.OwnerCommand:
    """What she said with the message a question of the bot's asks about
    (a line's "held"), as the command it was held on."""
    return bookings.OwnerCommand("unclear", minutes=held.get("minutes"), tell=held.get("tell", ""),
                                 ask=held.get("ask", ""), extras_ok=tuple(held.get("extras_ok", ())),
                                 extras_no=tuple(held.get("extras_no", ())))


def _message(line: Mapping[str, Any], rows: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """A line of a case as a message of her chat. A question of the bot's
    ("me_which", "me_ask") is written by the real formatter, and says what
    it asked about under "asks" until build() takes it off."""
    if "them" in line:
        return {"role": "user", "content": line["them"]}
    if "me" in line:
        return {"role": "assistant", "content": line["me"]}
    held = held_command(line.get("held", {}))
    by_number = {b["number"]: b for b in rows}
    if "me_which" in line:
        numbers = tuple(line["me_which"])
        text = bookings.format_which_one([by_number[n] for n in numbers], held)
    else:
        numbers = (line["me_ask"],)
        cmd = replace(held, number=line["me_ask"], missing=line.get("missing", ""), question=line.get("question", ""))
        text = bookings.format_question(cmd, by_number[line["me_ask"]])
    return {"role": "assistant", "content": text,
            "asks": {"numbers": numbers, "held": held.has_words or held.minutes is not None}}


def bot_questions(rows: Sequence[dict[str, Any]]) -> list[tuple[tuple[int, ...], str]]:
    """The questions the bot asks her about her message, as format_which_one
    and format_question word them for these bookings (with nothing of hers
    repeated), each with the bookings it is about."""
    asks = [((), bookings.format_which_one([]))]
    for size in range(1, len(rows) + 1):
        asks += [(tuple(b["number"] for b in some), bookings.format_which_one(some))
                 for some in itertools.combinations(rows, size)]
    for b in rows:
        asks += [((b["number"],), bookings.format_question(bookings.OwnerCommand("unclear", b["number"], missing=m), b))
                 for m in ("", "time")]
    return asks


def _question(text: str, rows: Sequence[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A "me" line that is one of the bot's questions to her, as "asks"
    (what more follows the question is what of hers it repeats); None when
    it is not one."""
    text = text.strip()
    for numbers, bare in bot_questions(rows):
        if text == bare or text.startswith(bare + " "):
            return {"numbers": numbers, "held": text != bare}
    return None


def asked_before(history: list[dict[str, Any]], rows: Sequence[dict[str, Any]], now: datetime) -> Optional[dict]:
    """The bot's question that her next message may answer, as the runtime
    keeps it (BookingFlow): the last one among its lines after her last
    message, if it is under ANSWER_WINDOW old — {"numbers": the bookings it
    is about, "held": it repeats words of hers}; else None. Takes "asks" off
    the messages."""
    asks = [message.pop("asks", None) for message in history]
    for message, ask in zip(reversed(history), reversed(asks)):
        if message["role"] == "user":
            break
        ask = ask or _question(message["content"], rows)
        if ask is not None:
            return ask if now - message["at"] < ANSWER_WINDOW else None
    return None


def build(data: dict[str, Any], case: dict[str, Any], tz: str, girl: str, outcall_taxi: bool) -> dict[str, Any]:
    """Everything the reader is given for one case, and the bot's question
    her latest message may answer ("asked", asked_before)."""
    now = bookings.parse_local(case.get("now") or data["now"], tz)
    specs = {s["number"]: s for s in data["bookings"][case["bookings"]]}
    rows = [make_booking(specs[n], tz, now) for n in sorted(specs)]
    # The outcall photo proof (booking.outcall_proof) is on for a set that has it.
    proof = outcall_taxi and any(s.get("proof") for s in specs.values())
    before = [_message(line) for line in case.get("before", [])]
    chat = her_chat(rows, girl, outcall_taxi=outcall_taxi, proof=proof, specs=specs)
    chat += [_message(line, rows) for line in case.get("chat", [])]
    # The times, a minute apart: what she said before from three hours back,
    # the requests (one per booking, first) from twenty minutes back, and
    # what followed them up to a minute before her latest message.
    for i, message in enumerate(before):
        message["at"] = now - BEFORE + timedelta(minutes=i)
    first = now - max(REQUESTS, timedelta(minutes=len(chat) + 1))
    then = max(first + timedelta(minutes=len(rows)), now - timedelta(minutes=len(chat) - len(rows)))
    for i, message in enumerate(chat):
        message["at"] = first + timedelta(minutes=i) if i < len(rows) else then + timedelta(minutes=i - len(rows))
        for booking, column in message.pop("steps", ()):
            booking[column] = message["at"]
    history = [*before, *chat]
    asked = asked_before(history, rows, now)
    history.append({"role": "user", "content": case["latest"], "at": now})
    replied = next((b for b in rows if b["number"] == case.get("reply_to")), None)
    return {"now": now, "rows": rows, "history": history, "replied": replied, "proof": proof, "asked": asked,
            "state_note": bookings.owner_state_note(rows, outcall_taxi=outcall_taxi, proof=proof)}


# ------------------------------------------------- what the runtime does


def typed_command(case: Mapping[str, Any], given: Mapping[str, Any], outcall_taxi: bool
                  ) -> Optional[bookings.OwnerCommand]:
    """Her latest message as the command she typed, on her own day; None
    when it is not one (READY / IN with no taxi steps is not)."""
    typed = bookings.parse_owner_reply(case["latest"], today=given["now"].date())
    if typed is not None and typed.kind in ("ready", "in_taxi") and not outcall_taxi:
        return None
    return typed


def answering(case: Mapping[str, Any], asked: Optional[Mapping[str, Any]],
              typed: Optional[bookings.OwnerCommand]) -> bool:
    """Her command answers the bot's question about her message before it
    (`asked`, build's), so the runtime reads it together with that one: a
    question about the booking she typed, or any for a YES / NO without a
    number — or as the case says ("answering")."""
    if "answering" in case:
        return case["answering"]
    if typed is None or asked is None:
        return False
    return typed.number in asked["numbers"] if typed.number is not None else typed.kind in ("yes", "no")


def typed_target(cmd: bookings.OwnerCommand, rows: Sequence[dict[str, Any]],
                 replied: Optional[dict[str, Any]]) -> bookings.OwnerCommand:
    """A command she typed without a number, on the booking the runtime
    carries it out on (BookingFlow._target, outcall_command): the request
    she quotes, or the only one waiting for her YES / NO, or the only
    outcall still to take that step; else it stays without (she is asked
    which)."""
    if cmd.number is not None or cmd.kind == "list":
        return cmd
    if cmd.kind in ("ready", "in_taxi"):
        step = "ready_at" if cmd.kind == "ready" else "in_taxi_at"
        fits = [b for b in rows if b["state"] == bs.CONFIRMED and bookings.is_outcall(b) and not b.get(step)]
        started = [b for b in fits if b.get("ready_at") or b.get("taxi_link_at")]
        fits = started if cmd.kind == "in_taxi" and len(started) == 1 else fits
    else:
        waiting = [b for b in rows if bookings.awaits_owner(b)]
        fits = [replied] if replied in waiting else waiting if cmd.kind in ("yes", "no") else []
    return replace(cmd, number=fits[0]["number"]) if len(fits) == 1 else cmd


def asked_instead(number: int, rows: Sequence[dict[str, Any]]) -> list[bookings.OwnerCommand]:
    """The one question she gets instead of her message being carried out
    (OwnerPlan.ask, as BookingFlow._ask_about asks it), as a command: about
    booking `number`, or (0) the only one waiting for her answer, else which
    of those; nothing at all when there is none to ask about."""
    waiting = [b for b in rows if bookings.awaits_owner(b)]
    booking = next((b for b in rows if b["number"] == number), None) if number else (
        waiting[0] if len(waiting) == 1 else None)
    if booking is not None:
        return [bookings.OwnerCommand("unclear", booking["number"])]
    if not waiting:
        return []
    return [bookings.OwnerCommand("unclear", missing="which", candidates=tuple(b["number"] for b in waiting))]


# ------------------------------------------------------------ expectations


def parse_command(text: str) -> tuple[str, Any, Optional[str]]:
    """'propose 8 13:10' → ("propose", 8, "13:10"); number None = any, "-" = none."""
    words = text.split()
    if not words or words[0] not in KINDS:
        raise ValueError(f"not an expectation: {text!r}")
    number: Any = None
    if len(words) > 1:
        if words[1] != "-" and not words[1].isdigit():
            raise ValueError(f"not a booking number in {text!r}")
        number = "-" if words[1] == "-" else int(words[1])
    at = " ".join(words[2:]) or None
    if at is not None and (words[0] != "propose" or not _AT.match(at)):
        raise ValueError(f"only propose has a time (HH:MM or YYYY-MM-DD HH:MM): {text!r}")
    return words[0], number, at


def parse_expect(text: str) -> list[tuple[str, Any, Optional[str]]]:
    """One expectation as its commands ('yes 3 + no 4'); [] for a bare
    'none': no command at all."""
    parts = [part.strip() for part in text.split("+")]
    if parts == ["none"]:
        return []
    if "none" in parts:
        raise ValueError(f"a bare none (no command at all) can't be joined with others: {text!r}")
    return [parse_command(part) for part in parts]


def expectations(case: Mapping[str, Any]) -> list[str]:
    return [case["expect"]] if isinstance(case["expect"], str) else list(case["expect"])


def outcome(cmd: bookings.OwnerCommand, now: datetime, tz: str, rows: Sequence[dict[str, Any]] = ()) -> Command:
    """A command as (kind, number, time): the time of a propose as "HH:MM"
    today, else "YYYY-MM-DD HH:MM". A typed time without a day is on the
    day of the booking (bookings.resolve_local)."""
    at = None
    if cmd.kind == "propose" and cmd.at is not None:
        zone = bookings.tzinfo_for(tz)
        today = now.astimezone(zone).date()
        booking = next((b for b in rows if b["number"] == cmd.number), None)
        day = cmd.day or (booking["starts_at"].astimezone(zone).date() if booking is not None else today)
        at = f"{cmd.at:%H:%M}" if day == today else f"{day.isoformat()} {cmd.at:%H:%M}"
    return cmd.kind, cmd.number, at


def _fits(got: Command, want: tuple[str, Any, Optional[str]]) -> bool:
    kind, number, at = want
    if got[0] != kind:
        return False
    if number == "-" and got[1] is not None:
        return False
    if isinstance(number, int) and got[1] != number:
        return False
    return at is None or got[2] == at


def matches(got: Sequence[Command], expect: str) -> bool:
    """Every command of the expectation came back, each matched by a command
    of its own, and nothing else did."""
    wanted = parse_expect(expect)
    if len(got) != len(wanted):
        return False

    def paired(i: int, left: list[Command]) -> bool:
        return i == len(wanted) or any(_fits(c, wanted[i]) and paired(i + 1, left[:j] + left[j + 1:])
                                       for j, c in enumerate(left))

    return paired(0, list(got))


# ------------------------------------------------------------------ checks


def _word(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _groups(value: Any) -> bool:
    """[group, ...]: each a word, or a list of words of which any will do."""
    return isinstance(value, list) and bool(value) and all(
        _word(g) or (isinstance(g, list) and bool(g) and all(_word(w) for w in g)) for g in value)


def _found(text: str, group: Any) -> bool:
    """One of the group's words is in the text, in any case."""
    return any(word.casefold() in text.casefold() for word in ([group] if isinstance(group, str) else group))


def _shown(group: Any) -> str:
    return group if isinstance(group, str) else "|".join(group)


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _came_back(name: str, cmds: Sequence[bookings.OwnerCommand], standing: str = "") -> Any:
    """What came back for one check: a text, or a list of extras."""
    if name in ("extras_ok", "extras_no"):
        return [x for c in cmds for x in getattr(c, name)]
    tell = " | ".join(c.tell for c in cmds if c.tell)
    ask = " | ".join(c.ask for c in cmds if c.ask)
    return {"tell": tell, "ask": ask, "words": " | ".join(t for t in (tell, ask) if t), "standing": standing}[name]


def _judged(name: str, want: Any, cmds: Sequence[bookings.OwnerCommand], standing: str = "") -> list[str]:
    """What is wrong with one check on these commands; [] when nothing is."""
    if name == "minutes":
        lengths = [c.minutes for c in cmds if c.minutes is not None]
        if want is False:
            return [f"wanted none, got {lengths}"] if lengths else []
        return [] if want in lengths else [f"wanted {want}, got {lengths or 'none'}"]
    if name == "for_me":
        if any(c.for_me for c in cmds) == want:
            return []
        return ["not answered to her" if want else "answered to her, wanted not"]
    got = _came_back(name, cmds, standing)
    shown = _quoted(got) if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
    if want is True:
        return [] if got else ["nothing came back"]
    if want is False:
        return [f"wanted nothing, got {shown}"] if got else []
    items = [got] if isinstance(got, str) else got
    return [f"no {_shown(g)} in {shown}" for g in want if not any(_found(x, g) for x in items)]


def problems(case: Mapping[str, Any], cmds: Sequence[bookings.OwnerCommand],
             reading: bookings.OwnerReading, *, earlier_lost: bool = False) -> list[str]:
    """What is wrong with the words, extras and length that came back, by
    the case's checks on all its commands together, or on one booking's own
    command; [] when nothing is."""
    wrong = []
    for name in (*TEXT_CHECKS, "extras_ok", "extras_no", "minutes", "for_me"):
        if name not in case:
            continue
        if not isinstance(case[name], dict):
            wrong += [f"{name}: {p}" for p in _judged(name, case[name], cmds, reading.standing)]
            continue
        for key, want in case[name].items():
            own = [c for c in cmds if c.number == int(key)]
            if not own and want is not False:
                wrong.append(f"{name} #{key}: no command for #{key}")
            else:
                wrong += [f"{name} #{key}: {p}" for p in _judged(name, want, own)]
    words = _came_back("words", cmds)
    wrong += [f"private: {word!r} goes on to him in {_quoted(words)}"
              for word in case.get("private", ()) if _found(words, word)]
    if "earlier_lost" in case and earlier_lost != case["earlier_lost"]:
        wrong.append("earlier_lost: " + ("she is not told her earlier words were lost" if case["earlier_lost"]
                                         else "she is told her earlier words were lost"))
    return wrong


def _shape(name: str, value: Any) -> str:
    """What is wrong with the value of one check ("" when nothing), said
    after its name."""
    if name == "minutes":
        return "" if value is False or (type(value) is int and value > 0) else " is a length in minutes, or false"
    if name == "for_me":
        return "" if isinstance(value, bool) else " is true or false"
    if isinstance(value, bool) or _groups(value):
        return ""
    return " is true, false or a list of groups (a word, or a list of words)"


def check_problems(case: Mapping[str, Any], numbers: Optional[set[int]] = None) -> list[str]:
    """What is wrong with the shape of a case's checks (`numbers`: the
    bookings of its set, for a check per booking)."""
    wrong = []
    for name in (*TEXT_CHECKS, "extras_ok", "extras_no", "minutes", "for_me"):
        if name not in case:
            continue
        if not (name in PER_BOOKING and isinstance(case[name], dict)):
            wrong += [name + p for p in [_shape(name, case[name])] if p]
            continue
        if not case[name]:
            wrong.append(f"{name} names no booking")
        for key, want in case[name].items():
            if not (key.isdigit() and int(key) > 0):
                wrong.append(f"{name}: {key!r} is not a booking number")
            elif numbers is not None and int(key) not in numbers:
                wrong.append(f"{name}: #{key} is not in the case's bookings")
            wrong += [f"{name} #{key}{p}" for p in [_shape(name, want)] if p]
    if "private" in case and not (isinstance(case["private"], list) and case["private"]
                                  and all(_word(w) for w in case["private"])):
        wrong.append("private is a list of words")
    wrong += [f"{key} is true or false" for key in ("earlier_lost", "answering", "from_reading")
              if key in case and not isinstance(case[key], bool)]
    return wrong


# --------------------------------------------------------------- the file


def set_problems(name: str, specs: Any) -> list[str]:
    """What is wrong with one booking set."""
    if not isinstance(specs, list):
        return [f"set {name!r}: not a list of bookings"]
    wrong, numbers = [], []
    for spec in specs:
        if not isinstance(spec, dict):
            wrong.append(f"set {name!r}: a booking is an object")
            continue
        where = f"set {name!r} #{spec.get('number', '?')}"
        numbers.append(spec.get("number"))
        wrong += [f"{where}: unknown key {key!r}" for key in sorted(set(spec) - BOOKING_KEYS)]
        wrong += [f"{where}: no {key!r}" for key in ("number", "customer", "state", "start") if key not in spec]
        if "number" in spec and not (type(spec["number"]) is int and spec["number"] > 0):
            wrong.append(f"{where}: number is a booking number")
        if spec.get("state", bs.PENDING) not in (bs.REQUESTED, bs.PENDING, bs.CONFIRMED):
            wrong.append(f"{where}: state is requested, pending or confirmed")
        wrong += [f"{where}: {key} is not a local time: {spec[key]!r}" for key in ("start", "move_to")
                  if key in spec and bookings.parse_local(str(spec[key]), "UTC") is None]
        if "minutes" in spec and not (type(spec["minutes"]) is int and spec["minutes"] > 0):
            wrong.append(f"{where}: minutes is a length in minutes")
        wrong += [f"{where}: {key} is true or false" for key in _FLAGS
                  if key in spec and not isinstance(spec[key], bool)]
        wrong += [f"{where}: {key} is a text" for key in _TEXTS if key in spec and not isinstance(spec[key], str)]
        if "arrived" in spec and not isinstance(spec["arrived"], (bool, str)):
            wrong.append(f"{where}: arrived is true or his words")
        if spec.get("proof", "waiting") not in ("waiting", "came"):
            wrong.append(f"{where}: proof is \"waiting\" or \"came\"")
        if "his_answer" in spec and not spec.get("asked"):
            wrong.append(f"{where}: his_answer without asked")
    if len(numbers) != len(set(map(str, numbers))):
        wrong.append(f"set {name!r}: a number used twice")
    return wrong


def _is_line(line: Any) -> bool:
    return isinstance(line, dict) and len(line) == 1 and set(line) <= {"me", "them"} and _word(*line.values())


def _held_problem(held: Any) -> bool:
    """A question's "held" is not what she can have said."""
    if not isinstance(held, dict) or not held or set(held) - HELD_KEYS:
        return True
    return not (all(_word(held[k]) for k in ("tell", "ask") if k in held)
                and ("minutes" not in held or (type(held["minutes"]) is int and held["minutes"] > 0))
                and all(isinstance(held[k], list) and held[k] and all(_word(x) for x in held[k])
                        for k in ("extras_ok", "extras_no") if k in held))


def _question_problem(line: Any, numbers: Optional[set[int]]) -> str:
    """What is wrong with a line of the bot's question ("" when nothing;
    "-" when it is not one at all)."""
    if not isinstance(line, dict) or len({"me_which", "me_ask"} & set(line)) != 1:
        return "-"
    which = "me_which" in line
    if set(line) - ({"me_which", "held"} if which else {"me_ask", "missing", "question", "held"}):
        return f"unknown key in {line!r}"
    asked = line["me_which"] if which else [line["me_ask"]]
    if not (isinstance(asked, list) and asked and all(type(n) is int for n in asked)
            and len(set(asked)) == len(asked)):
        return "me_which is a list of booking numbers" if which else "me_ask is a booking number"
    if numbers is not None and set(asked) - numbers:
        return f"#{min(set(asked) - numbers)} is not in the case's bookings"
    if line.get("missing", "time") != "time":
        return 'missing is "time"'
    if "question" in line and not _word(line["question"]):
        return "question is a text"
    if "held" in line and _held_problem(line["held"]):
        return 'held is {"tell", "ask": a text, "minutes": a length, "extras_ok", "extras_no": a list of words}'
    return ""


def case_problems(case: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    """What is wrong with one case: it could not be built or judged as written."""
    wrong = [f"unknown key {key!r}" for key in sorted(set(case) - CASE_KEYS)]
    wrong += [f"no {key!r}" for key in ("bookings", "latest", "expect") if key not in case]
    numbers: Optional[set[int]] = None
    if not isinstance(case.get("bookings"), str) or case["bookings"] not in data["bookings"]:
        wrong.append(f"no booking set {case.get('bookings')!r}")
    else:
        numbers = {s.get("number") for s in data["bookings"][case["bookings"]] if isinstance(s, dict)}
        if "reply_to" in case and case["reply_to"] not in numbers:
            wrong.append(f"reply_to #{case['reply_to']} is not in set {case['bookings']!r}")
    if "latest" in case and not _word(case["latest"]):
        wrong.append("latest is her message, a text")
    if "now" in case and bookings.parse_local(str(case["now"]), "UTC") is None:
        wrong.append(f"now is not a local time: {case['now']!r}")
    lines = case.get("before", [])
    if not isinstance(lines, list) or not all(_is_line(line) for line in lines):
        wrong.append('before is a list of {"me": ...} or {"them": ...}')
    lines = case.get("chat", [])
    found = ["" if _is_line(line) else _question_problem(line, numbers)
             for line in (lines if isinstance(lines, list) else [None])]
    if "-" in found:
        wrong.append('chat is a list of {"me": ...}, {"them": ...}, {"me_which": [...]} or {"me_ask": ...}')
    wrong += [f"chat: {p}" for p in found if p not in ("", "-")]
    expect = case.get("expect", "none")
    for one in expect if isinstance(expect, list) and expect else [expect]:
        try:
            if not isinstance(one, str):
                raise ValueError(f"not an expectation: {one!r}")
            parse_expect(one)
        except ValueError as exc:
            wrong.append(str(exc))
    return wrong + check_problems(case, numbers)


def load(path: Path) -> dict[str, Any]:
    """The case file; ValueError naming every problem in it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    wrong = []
    if bookings.parse_local(str(data.get("now") or ""), "UTC") is None:
        wrong.append(f"now is not a local time: {data.get('now')!r}")
    for name, specs in data["bookings"].items():
        wrong += set_problems(name, specs)
    ids = set()
    for case in data["cases"]:
        cid = case.get("id", "?")
        if cid in ids:
            wrong.append(f"{cid}: id used twice")
        ids.add(cid)
        wrong += [f"{cid}: {p}" for p in case_problems(case, data)]
    if wrong:
        raise ValueError("; ".join(wrong))
    return data


# -------------------------------------------------------------------- run


@dataclass
class Result:
    case: dict[str, Any]
    got: str
    ok: bool
    raw: Any = None
    wrong: list[str] = field(default_factory=list)


def describe_command(cmd: bookings.OwnerCommand, now: datetime, tz: str, rows: Sequence[dict[str, Any]] = ()) -> str:
    """'yes 3 (60 min; tell "bring cash"; no: anal)'."""
    kind, number, at = outcome(cmd, now, tz, rows)
    head = " ".join(str(p) for p in (kind, "-" if number is None and kind != "list" else number, at) if p is not None)
    parts = [f"{cmd.minutes} min"] if cmd.minutes is not None else []
    parts += [f"tell {_quoted(cmd.tell)}"] if cmd.tell else []
    parts += [f"ask {_quoted(cmd.ask)}"] if cmd.ask else []
    parts += ["ok: " + ", ".join(cmd.extras_ok)] if cmd.extras_ok else []
    parts += ["no: " + ", ".join(cmd.extras_no)] if cmd.extras_no else []
    parts += ["for me"] if cmd.for_me else []
    parts += [f"question {_quoted(cmd.question)}"] if cmd.question else []
    parts += ["which: " + ", ".join(f"#{n}" for n in cmd.candidates)] if cmd.candidates else []
    return head + (f" ({'; '.join(parts)})" if parts else "")


def describe(cmds: Sequence[bookings.OwnerCommand], reading: bookings.OwnerReading, now: datetime, tz: str,
             rows: Sequence[dict[str, Any]] = ()) -> str:
    """What came back: 'yes 3 (tell "bring cash") + no 4', 'none; standing "..."'."""
    text = " + ".join(describe_command(c, now, tz, rows) for c in cmds) or "none"
    return text + (f"; standing {_quoted(reading.standing)}" if reading.standing else "")


def wanted(case: Mapping[str, Any]) -> str:
    """What the case wants: 'yes 8; tell ["cash"]'."""
    checks = [f"{name} {json.dumps(case[name], ensure_ascii=False)}" for name in CHECKS if name in case]
    return "; ".join([" | ".join(expectations(case)), *checks])


def judge(case: dict[str, Any], cmds: Sequence[bookings.OwnerCommand], reading: bookings.OwnerReading, *,
          now: datetime, tz: str, rows: Sequence[dict[str, Any]], earlier_lost: bool = False) -> list[str]:
    """What is wrong with what the runtime would do with her message: the
    commands, then the checks."""
    got = [outcome(c, now, tz, rows) for c in cmds]
    wrong = [] if any(matches(got, e) for e in expectations(case)) else ["not the commands wanted"]
    return wrong + problems(case, cmds, reading, earlier_lost=earlier_lost)


async def run_case(data: dict[str, Any], case: dict[str, Any], *, key: str, tz: str, girl: str,
                   outcall_taxi: bool, ai_config: dict[str, Any], http: httpx.AsyncClient) -> Result:
    given = build(data, case, tz, girl, outcall_taxi)
    rows, replied, asked = given["rows"], given["replied"], given["asked"]
    # The runtime's clock (BookingFlow.now) is in UTC.
    now = given["now"].astimezone(timezone.utc)
    typed = typed_command(case, given, outcall_taxi)
    if typed is not None and not typed.loose and not answering(case, asked, typed):
        # A strict command that answers no question of the bot's costs no
        # model call: carried out as she typed it.
        cmds = [typed_target(typed, rows, replied)]
        nothing = bookings.OwnerReading()
        wrong = judge(case, cmds, nothing, now=now, tz=tz, rows=rows)
        return Result(case, describe(cmds, nothing, now, tz, rows) + "; strict, no model call", not wrong, None, wrong)
    reading = await ai_responder.read_owner_answer(
        api_key=key, history=given["history"], latest=case["latest"], tz_name=tz, now=now,
        state_note=given["state_note"], replying_to=case.get("reply_to"), ai_config=ai_config, client=http,
    )
    if reading is None:
        return Result(case, "model could not be read", False)
    commands = bookings.owner_commands_from(reading.answers, rows, now=now, tz_name=tz, replied=replied,
                                            outcall_taxi=outcall_taxi, typed=case["latest"])
    # As the runtime: a remark about one customer is no rule for all of them.
    heard, reading = reading, replace(reading, standing=bookings.standing_for_all(reading.standing, rows))
    plan = bookings.plan_owner_answer(typed, commands, standing=reading.standing,
                                      held_words=bool(asked and asked["held"]),
                                      settled={b["number"] for b in rows if bookings.is_settled(b)})
    notes = []
    if plan.ask is not None:
        # Asked instead: nothing of her message is carried out but an
        # instruction for every customer, which is still saved.
        cmds, done = asked_instead(plan.ask, rows), bookings.OwnerReading(standing=reading.standing)
        notes += ["she is asked instead"] if cmds else []
    else:
        cmds, done = list(plan.commands), reading
    if plan.earlier_lost:
        notes.append("her earlier words lost")
    got = describe(cmds, done, now, tz, rows)
    read = describe(commands, reading, now, tz, rows)
    if read != got:
        notes.append(f"read as {read}")
    wrong = judge(case, cmds, done, now=now, tz=tz, rows=rows, earlier_lost=plan.earlier_lost)
    if case.get("from_reading") and not commands:
        wrong.append("from_reading: the reading had nothing for it; only the question the plan leads to")
    raw = {"answers": [dict(a) for a in heard.answers], "standing": heard.standing}
    return Result(case, "; ".join([got, *notes]), not wrong, raw, wrong)


def report(results: list[Result], verbose: bool) -> str:
    by_lang: dict[str, list[bool]] = {}
    for r in results:
        by_lang.setdefault(r.case["id"].split("-")[0], []).append(r.ok)
    lines = [f"{sum(r.ok for r in results)}/{len(results)} answers read right"]
    lines += [f"  {lang:<4} {sum(oks):>3}/{len(oks)}" for lang, oks in by_lang.items()]
    for r in results:
        if r.ok and not verbose:
            continue
        lines.append(f"\n{'PASS' if r.ok else 'FAIL'}  {r.case['id']}: {r.case['latest']!r}")
        lines.append(f"  got     {r.got}")
        lines.append(f"  wanted  {wanted(r.case)}")
        if r.wrong:
            lines.append(f"  wrong   {'; '.join(r.wrong)}")
        if r.raw is not None:
            lines.append(f"  model   {json.dumps(r.raw, ensure_ascii=False)}")
    return "\n".join(lines)


async def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--only", help="run the cases whose id starts with this")
    ap.add_argument("--runs", type=int, default=1, help="run every case this many times")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--verbose", action="store_true", help="show passing cases too")
    args = ap.parse_args(argv)

    data = load(args.cases)
    cases = [c for c in data["cases"] if not args.only or c["id"].startswith(args.only)] * args.runs
    if not cases:
        print(f"No cases starting with {args.only!r}.", file=sys.stderr)
        return 2
    pool = await pg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        bundle = await tenants.TenantStore(pool).bundle(args.tenant)
        key = await database.SessionRegistry(pool).load_deepseek_key(bundle.tenant["session_id"]) \
            if bundle.tenant.get("session_id") else None
        key = key or os.environ.get("DEEPSEEK_PLATFORM_KEY") or ""
    finally:
        await pool.close()
    config = bundle.config
    girl = (config["booking"].get("name") or "").strip() or bundle.tenant["name"]
    outcall_taxi = bool(config["booking"].get("outcall_taxi", True))
    gate = asyncio.Semaphore(max(1, args.parallel))
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=90) as http:
        async def one(case: dict[str, Any]) -> Result:
            async with gate:
                try:
                    return await run_case(data, case, key=key, tz=config["timezone"], girl=girl,
                                          outcall_taxi=outcall_taxi, ai_config=config["ai"], http=http)
                except Exception as exc:  # a failed call fails that case, not the run
                    return Result(case, f"error: {exc}", False)

        results = await asyncio.gather(*(one(c) for c in cases))
    print(f"Tenant {args.tenant} ({girl}), {len(results)} answers in {time.monotonic() - started:.0f}s\n")
    print(report(results, args.verbose))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
