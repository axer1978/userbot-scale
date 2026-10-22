"""Check how the owner's answers in her own words are read, against the real
model. A dry run: the reader is called exactly as the runtime calls it
(booking_flow.BookingFlow.read_owner_answer: her chat with the requests in
it, the open bookings, her latest message) and its answer turned into the
command the runtime would carry out; nothing is sent and nothing is written.

    docker compose exec manager python owner_check.py --tenant 6
    docker compose exec manager python owner_check.py --tenant 6 --runs 3 --verbose

The cases are replay_scenarios/owner-answers.json (--cases):

    "now":      the clock for every case ("2026-10-20T12:40", the tenant's zone)
    "bookings": named sets of bookings, each a list of
                {"number", "customer", "state": requested|pending|confirmed,
                 "start" (local), "minutes" (60), "extras", "notes",
                 "outcall": true, "move_to" (local: he asks to move it),
                 "ready": true (she sent READY), "taxi_link": true}
    "cases":    {"id", "bookings": <set>, "latest": what she writes,
                 "reply_to": <number of the request she quotes>,
                 "chat": [{"me": ...} | {"them": ...}] after the default one,
                 "expect": "yes 8" | ["unclear", "none"] | ...}

Her chat holds what the runtime would have sent her for each booking (the
request, or the move request; for a confirmed one her YES and the answer to
it, READY and the taxi link for an outcall), then "chat", then "latest".

An expectation is "<kind> [<number>|-] [<HH:MM> | <YYYY-MM-DD> <HH:MM>]":
kind is yes, no, propose, stays, cancel, done, noshow, ready, in_taxi, list,
unclear, or none (no command: she gets an ordinary reply). No number: any;
"-": none could be told (she is asked which). A number the runtime would
fill in itself (the only request waiting, the only outcall under way) counts
as given. Several expectations: any of them passes.

Each case costs one small model call on the tenant's own DeepSeek key, as
replay.py does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

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


def make_booking(spec: dict[str, Any], tz: str, now: datetime) -> dict[str, Any]:
    start = bookings.parse_local(spec["start"], tz)
    end = start + timedelta(minutes=spec.get("minutes", 60))
    notes = spec.get("notes", "")
    if spec.get("outcall") and "outcall" not in notes.lower():
        notes = ("Outcall. " + notes).strip()
    b = {"id": spec["number"], "number": spec["number"], "state": spec["state"], "starts_at": start, "ends_at": end,
         "tz": tz, "customer_name": spec["customer"], "customer_username": None, "service": "", "notes": notes,
         "extras": spec.get("extras", ""), "proposed_by": None, "proposed_starts_at": None, "proposed_ends_at": None,
         "provider_message_id": 1000 + spec["number"], "provider_wa_message_id": None,
         "ready_at": None, "taxi_link": None, "taxi_link_at": None, "in_taxi_at": None}
    if spec.get("move_to"):
        moved = bookings.parse_local(spec["move_to"], tz)
        b.update(proposed_by=bs.CUSTOMER, proposed_starts_at=moved, proposed_ends_at=moved + (end - start))
    if spec.get("ready"):
        b["ready_at"] = now - timedelta(minutes=10)
    if spec.get("taxi_link"):
        b.update(taxi_link=TAXI_LINK, taxi_link_at=now - timedelta(minutes=3))
    return b


def her_chat(rows: list[dict[str, Any]], girl: str, *, outcall_taxi: bool) -> list[dict[str, str]]:
    """What the runtime would have sent her about these bookings, and her
    answers so far, as get_history_for_ai returns it."""
    me = lambda text: {"role": "assistant", "content": text}  # noqa: E731
    them = lambda text: {"role": "user", "content": text}  # noqa: E731
    chat = []
    for b in rows:
        n = b["number"]
        if b.get("proposed_by") == bs.CUSTOMER:
            chat.append(me(bookings.format_move_request(b, name=girl)))
            continue
        chat.append(me(bookings.format_request(b, name=girl)))
        if b["state"] != bs.CONFIRMED:
            continue
        chat += [them(f"YES {n}"), me(bookings.format_acknowledgement(b, "confirm", outcall_taxi=outcall_taxi))]
        if b.get("ready_at"):
            chat += [them(f"READY {n}"), me(bookings.format_acknowledgement(b, "ready"))]
        if b.get("taxi_link"):
            chat.append(me(bookings.format_taxi_link(b, f"here {TAXI_LINK}", 5)))
    return chat


def parse_expect(text: str) -> tuple[str, Any, Optional[str]]:
    """'propose 8 13:10' → ("propose", 8, "13:10"); number None = any, "-" = none."""
    words = text.split()
    if not words or words[0] not in KINDS:
        raise ValueError(f"not an expectation: {text!r}")
    number: Any = None
    if len(words) > 1:
        number = "-" if words[1] == "-" else int(words[1])
    at = " ".join(words[2:]) or None
    return words[0], number, at


def outcome(cmd: Optional[bookings.OwnerCommand], rows: list[dict[str, Any]], now: datetime,
            tz: str) -> tuple[str, Optional[int], Optional[str]]:
    """The command as (kind, number, time), with the number the runtime
    fills in itself when she names none."""
    if cmd is None:
        return "none", None, None
    number = cmd.number
    if number is None:
        if cmd.kind in ("yes", "no", "unclear"):
            waiting = [b for b in rows if bookings.awaits_owner(b)]
            number = waiting[0]["number"] if len(waiting) == 1 else None
        elif cmd.kind in ("ready", "in_taxi"):
            outcalls = [b for b in rows if b["state"] == bs.CONFIRMED and bookings.is_outcall(b)]
            number = outcalls[0]["number"] if len(outcalls) == 1 else None
    at = None
    if cmd.kind == "propose":
        day = cmd.day or now.astimezone(bookings.tzinfo_for(tz)).date()
        at = cmd.at.strftime("%H:%M") if day == now.astimezone(bookings.tzinfo_for(tz)).date() \
            else f"{day.isoformat()} {cmd.at:%H:%M}"
    return cmd.kind, number, at


def matches(got: tuple[str, Optional[int], Optional[str]], expect: str) -> bool:
    kind, number, at = parse_expect(expect)
    if got[0] != kind:
        return False
    if number == "-" and got[1] is not None:
        return False
    if isinstance(number, int) and got[1] != number:
        return False
    return at is None or got[2] == at


def describe(got: tuple[str, Optional[int], Optional[str]]) -> str:
    kind, number, at = got
    if number is None and kind not in ("none", "list"):
        number = "-"
    return " ".join(str(p) for p in (kind, number, at) if p is not None)


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    ids = set()
    for case in data["cases"]:
        if case["id"] in ids:
            problems.append(f"{case['id']}: id used twice")
        ids.add(case["id"])
        if case["bookings"] not in data["bookings"]:
            problems.append(f"{case['id']}: no booking set {case['bookings']!r}")
        for expect in [case["expect"]] if isinstance(case["expect"], str) else case["expect"]:
            try:
                parse_expect(expect)
            except ValueError as exc:
                problems.append(f"{case['id']}: {exc}")
    if problems:
        raise ValueError("; ".join(problems))
    return data


@dataclass
class Result:
    case: dict[str, Any]
    got: str
    ok: bool
    raw: Any = None


def build(data: dict[str, Any], case: dict[str, Any], tz: str, girl: str, outcall_taxi: bool) -> dict[str, Any]:
    """Everything the reader is given for one case."""
    now = bookings.parse_local(case.get("now") or data["now"], tz)
    rows = sorted((make_booking(s, tz, now) for s in data["bookings"][case["bookings"]]), key=lambda b: b["number"])
    history = her_chat(rows, girl, outcall_taxi=outcall_taxi)
    for line in case.get("chat", []):
        history.append({"role": "assistant", "content": line["me"]} if "me" in line
                       else {"role": "user", "content": line["them"]})
    history.append({"role": "user", "content": case["latest"]})
    replied = next((b for b in rows if b["number"] == case.get("reply_to")), None)
    return {"now": now, "rows": rows, "history": history, "replied": replied,
            "state_note": bookings.owner_state_note(rows, outcall_taxi=outcall_taxi)}


async def run_case(data: dict[str, Any], case: dict[str, Any], *, key: str, tz: str, girl: str,
                   outcall_taxi: bool, ai_config: dict[str, Any], http: httpx.AsyncClient) -> Result:
    given = build(data, case, tz, girl, outcall_taxi)
    found = await ai_responder.read_owner_answer(
        api_key=key, history=given["history"], latest=case["latest"], tz_name=tz, now=given["now"],
        state_note=given["state_note"], replying_to=case.get("reply_to"), ai_config=ai_config, client=http,
    )
    cmd = bookings.owner_command_from(found, given["rows"], now=given["now"], tz_name=tz, replied=given["replied"],
                                      outcall_taxi=outcall_taxi) if found else None
    got = outcome(cmd, given["rows"], given["now"], tz)
    expects = [case["expect"]] if isinstance(case["expect"], str) else case["expect"]
    return Result(case, describe(got), any(matches(got, e) for e in expects), found)


def report(results: list[Result], verbose: bool) -> str:
    by_lang: dict[str, list[bool]] = {}
    for r in results:
        by_lang.setdefault(r.case["id"].split("-")[0], []).append(r.ok)
    lines = [f"{sum(r.ok for r in results)}/{len(results)} answers read right"]
    lines += [f"  {lang:<4} {sum(oks):>3}/{len(oks)}" for lang, oks in by_lang.items()]
    for r in results:
        if r.ok and not verbose:
            continue
        expects = r.case["expect"] if isinstance(r.case["expect"], str) else " | ".join(r.case["expect"])
        lines.append(f"\n{'PASS' if r.ok else 'FAIL'}  {r.case['id']}: {r.case['latest']!r}")
        lines.append(f"  got {r.got}   (wanted {expects})   model: {json.dumps(r.raw, ensure_ascii=False)}")
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
