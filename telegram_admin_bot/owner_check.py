"""Check how the booker's answers in her own words are read, against the real
model. A dry run: the reader is called exactly as the runtime calls it
(booking_flow.BookingFlow.read_owner_answer: her chat with its times and the
requests in it, the open bookings in full, her latest message) and what it
found is turned into the commands the runtime would carry out; nothing is
sent and nothing is written.

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
                 "chat": [{"me": ...} | {"them": ...}] after the default chat,
                 "now": another clock for this case,
                 "expect": "yes 8" | "yes 3 + no 4" | ["stays 6", "none"] | ...,
                 and any of the checks below}

Her chat holds what the runtime would have sent her: every request (or move
request), then for each confirmed booking her yes and its acknowledgement and
what followed (READY, his photo, the taxi link, IN, his arrival, her question
to him and his answer); then "chat", then "latest". Every line has its time,
as get_history_for_ai(with_times=True) gives it, a minute apart: "before"
from three hours back, the requests from twenty minutes back, what followed
them up to a minute before "latest", which is now.

An expectation is one or more commands joined with " + ": all of them must
come back, in any order, and nothing else. A command is
"<kind> [<number>|-] [<HH:MM> | <YYYY-MM-DD> <HH:MM>]": kind is yes, no,
propose, stays, cancel, done, noshow, ready, in_taxi, list, unclear, or none
(the booking does not change: only words for him, or her question to me).
A bare "none" is no command at all: nothing about any booking, so no reply
to her. No number: any; "-": none could be told (she is asked which). Only
propose has a time. Several expectations (a list): any of them passes.

Checks, on everything that came back (all its commands together), each
optional:

    "tell", "ask":  true (some words) | false (none) | [group, ...]
    "words":        the same, on tell and ask together
    "extras_ok", "extras_no": the same, each group naming one extra
    "minutes":      60: a length she set | false: none
    "for_me":       true | false: she asked me something about a booking
    "private":      ["show", ...]: never in tell or ask
    "standing":     true | false | [group, ...]: an instruction for every customer

A group is a word, or a list of words of which any one will do; it must be in
the text (any case).

Each case costs one model call on the tenant's own DeepSeek key, as replay.py
does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
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
CHECKS = (*TEXT_CHECKS, "extras_ok", "extras_no", "minutes", "for_me", "private")
CASE_KEYS = {"id", "bookings", "latest", "reply_to", "before", "chat", "now", "expect", *CHECKS}

# Where her chat starts before her latest message: what she said before the
# requests came, and the requests.
BEFORE = timedelta(hours=3)
REQUESTS = timedelta(minutes=20)
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


def _message(line: Mapping[str, str]) -> dict[str, Any]:
    """{"me": ...} / {"them": ...} of a case, as a message of her chat."""
    return {"role": "assistant", "content": line["me"]} if "me" in line else {"role": "user", "content": line["them"]}


def build(data: dict[str, Any], case: dict[str, Any], tz: str, girl: str, outcall_taxi: bool) -> dict[str, Any]:
    """Everything the reader is given for one case."""
    now = bookings.parse_local(case.get("now") or data["now"], tz)
    specs = {s["number"]: s for s in data["bookings"][case["bookings"]]}
    rows = [make_booking(specs[n], tz, now) for n in sorted(specs)]
    # The outcall photo proof (booking.outcall_proof) is on for a set that has it.
    proof = outcall_taxi and any(s.get("proof") for s in specs.values())
    before = [_message(line) for line in case.get("before", [])]
    chat = her_chat(rows, girl, outcall_taxi=outcall_taxi, proof=proof, specs=specs)
    chat += [_message(line) for line in case.get("chat", [])]
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
    history = [*before, *chat, {"role": "user", "content": case["latest"], "at": now}]
    replied = next((b for b in rows if b["number"] == case.get("reply_to")), None)
    return {"now": now, "rows": rows, "history": history, "replied": replied, "proof": proof,
            "state_note": bookings.owner_state_note(rows, outcall_taxi=outcall_taxi, proof=proof)}


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


def outcome(cmd: bookings.OwnerCommand, now: datetime, tz: str) -> Command:
    """A command as (kind, number, time): the time of a propose as "HH:MM"
    today, else "YYYY-MM-DD HH:MM"."""
    at = None
    if cmd.kind == "propose" and cmd.at is not None:
        today = now.astimezone(bookings.tzinfo_for(tz)).date()
        day = cmd.day or today
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


def problems(case: Mapping[str, Any], cmds: Sequence[bookings.OwnerCommand],
             reading: bookings.OwnerReading) -> list[str]:
    """What is wrong with the words, extras and length that came back, by
    the case's checks on all its commands together; [] when nothing is."""
    tell = " | ".join(c.tell for c in cmds if c.tell)
    ask = " | ".join(c.ask for c in cmds if c.ask)
    texts = {"tell": tell, "ask": ask, "words": " | ".join(t for t in (tell, ask) if t),
             "standing": reading.standing}
    found = {name: [x for c in cmds for x in getattr(c, name)] for name in ("extras_ok", "extras_no")}
    wrong = []
    for name in (*TEXT_CHECKS, "extras_ok", "extras_no"):
        want = case.get(name)
        if want is None:
            continue
        got = texts.get(name, found.get(name))
        shown = _quoted(got) if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
        if want is True and not got:
            wrong.append(f"{name}: nothing came back")
        elif want is False and got:
            wrong.append(f"{name}: wanted nothing, got {shown}")
        elif isinstance(want, list):
            items = [got] if isinstance(got, str) else got
            wrong += [f"{name}: no {_shown(g)} in {shown}" for g in want if not any(_found(x, g) for x in items)]
    if "minutes" in case:
        lengths = [c.minutes for c in cmds if c.minutes is not None]
        if case["minutes"] is False and lengths:
            wrong.append(f"minutes: wanted none, got {lengths}")
        elif case["minutes"] is not False and case["minutes"] not in lengths:
            wrong.append(f"minutes: wanted {case['minutes']}, got {lengths or 'none'}")
    if "for_me" in case and any(c.for_me for c in cmds) != case["for_me"]:
        wrong.append("for_me: " + ("not answered to her" if case["for_me"] else "answered to her, wanted not"))
    wrong += [f"private: {word!r} goes on to him in {_quoted(texts['words'])}"
              for word in case.get("private", ()) if _found(texts["words"], word)]
    return wrong


def check_problems(case: Mapping[str, Any]) -> list[str]:
    """What is wrong with the shape of a case's checks."""
    wrong = [f"{name} is true, false or a list of groups (a word, or a list of words)"
             for name in (*TEXT_CHECKS, "extras_ok", "extras_no")
             if name in case and not (isinstance(case[name], bool) or _groups(case[name]))]
    if "minutes" in case and case["minutes"] is not False and not (
            type(case["minutes"]) is int and case["minutes"] > 0):
        wrong.append("minutes is a length in minutes, or false")
    if "for_me" in case and not isinstance(case["for_me"], bool):
        wrong.append("for_me is true or false")
    if "private" in case and not (isinstance(case["private"], list) and case["private"]
                                  and all(_word(w) for w in case["private"])):
        wrong.append("private is a list of words")
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


def case_problems(case: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    """What is wrong with one case: it could not be built or judged as written."""
    wrong = [f"unknown key {key!r}" for key in sorted(set(case) - CASE_KEYS)]
    wrong += [f"no {key!r}" for key in ("bookings", "latest", "expect") if key not in case]
    if not isinstance(case.get("bookings"), str) or case["bookings"] not in data["bookings"]:
        wrong.append(f"no booking set {case.get('bookings')!r}")
    elif "reply_to" in case and case["reply_to"] not in [
            s.get("number") for s in data["bookings"][case["bookings"]] if isinstance(s, dict)]:
        wrong.append(f"reply_to #{case['reply_to']} is not in set {case['bookings']!r}")
    if "latest" in case and not _word(case["latest"]):
        wrong.append("latest is her message, a text")
    if "now" in case and bookings.parse_local(str(case["now"]), "UTC") is None:
        wrong.append(f"now is not a local time: {case['now']!r}")
    for key in ("before", "chat"):
        lines = case.get(key, [])
        if not isinstance(lines, list) or not all(_is_line(line) for line in lines):
            wrong.append(f'{key} is a list of {{"me": ...}} or {{"them": ...}}')
    expect = case.get("expect", "none")
    for one in expect if isinstance(expect, list) and expect else [expect]:
        try:
            if not isinstance(one, str):
                raise ValueError(f"not an expectation: {one!r}")
            parse_expect(one)
        except ValueError as exc:
            wrong.append(str(exc))
    return wrong + check_problems(case)


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


def describe_command(cmd: bookings.OwnerCommand, now: datetime, tz: str) -> str:
    """'yes 3 (60 min; tell "bring cash"; no: anal)'."""
    kind, number, at = outcome(cmd, now, tz)
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


def describe(cmds: Sequence[bookings.OwnerCommand], reading: bookings.OwnerReading, now: datetime, tz: str) -> str:
    """What came back: 'yes 3 (tell "bring cash") + no 4', 'none; standing "..."'."""
    text = " + ".join(describe_command(c, now, tz) for c in cmds) or "none"
    return text + (f"; standing {_quoted(reading.standing)}" if reading.standing else "")


def wanted(case: Mapping[str, Any]) -> str:
    """What the case wants: 'yes 8; tell ["cash"]'."""
    checks = [f"{name} {json.dumps(case[name], ensure_ascii=False)}" for name in CHECKS if name in case]
    return "; ".join([" | ".join(expectations(case)), *checks])


async def run_case(data: dict[str, Any], case: dict[str, Any], *, key: str, tz: str, girl: str,
                   outcall_taxi: bool, ai_config: dict[str, Any], http: httpx.AsyncClient) -> Result:
    given = build(data, case, tz, girl, outcall_taxi)
    # The runtime's clock (BookingFlow.now) is in UTC.
    now = given["now"].astimezone(timezone.utc)
    reading = await ai_responder.read_owner_answer(
        api_key=key, history=given["history"], latest=case["latest"], tz_name=tz, now=now,
        state_note=given["state_note"], replying_to=case.get("reply_to"), ai_config=ai_config, client=http,
    )
    if reading is None:
        return Result(case, "model could not be read", False)
    cmds = bookings.owner_commands_from(reading.answers, given["rows"], now=now, tz_name=tz,
                                        replied=given["replied"], outcall_taxi=outcall_taxi)
    got = [outcome(c, now, tz) for c in cmds]
    wrong = [] if any(matches(got, e) for e in expectations(case)) else ["not the commands wanted"]
    wrong += problems(case, cmds, reading)
    raw = {"answers": [dict(a) for a in reading.answers], "standing": reading.standing}
    return Result(case, describe(cmds, reading, now, tz), not wrong, raw, wrong)


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
