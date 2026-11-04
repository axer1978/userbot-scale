"""Check how a customer's words about a time are read, against the real model.
A dry run: the reader is called exactly as the runtime calls it
(booking_flow.BookingFlow.scan: the chat, their bookings, the messages in
"them" as ones no finished reading has seen) and what it found is weighed as the runtime weighs it
(bookings.wants_now, bookings.requested_slot); nothing is sent and nothing is
written. The clock of a case is passed in, so the same words are read at the
same time on any day.

    docker compose exec manager python time_check.py --tenant 6
    docker compose exec manager python time_check.py --tenant 6 --only lv- --runs 3 --verbose

The cases are replay_scenarios/customer-times.json (--cases):

    "now":   the clock for every case ("2026-11-04T18:40", the tenant's zone)
    "cases": {"id": "<en|ru|lv|et|es>-...",
              "them": what they just sent, a string or several in a row,
              "chat": [{"me": ...} | {"them": ...}] before it,
              "now": another clock for this case,
              "booking": {"state": requested|pending|confirmed, "start": local,
                          "proposed": local, the business offered this instead},
              "outcall": true: they must be read as wanting her to come to them,
              "check": "arrival": the arrival reader instead of the booking reader,
              "want": see below}

"want" is what must come back, or a list of which any one will do:

    "none"                  nothing to act on
    "now"                   they want to come right now
    "YYYY-MM-DDTHH:MM"      that time
    "cancel", "accept_proposal", "decline_proposal", "confirm_attendance",
    "waitlist"              that intent
    true | false            with "check": "arrival": they have arrived or not

Each case read costs one model call on the tenant's own DeepSeek key, as
replay.py and owner_check.py do.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import httpx

import ai_responder
import bookings
import booking_states as bs
import database
import owner_check
import pg
import tenants

CASES = Path(__file__).with_name("replay_scenarios") / "customer-times.json"
INTENTS = {"none", "now", "cancel", "accept_proposal", "decline_proposal", "confirm_attendance", "waitlist"}
_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
STATES = {bs.REQUESTED, bs.PENDING, bs.CONFIRMED}


@dataclass
class Tokens:
    """What the run spent: the DeepSeek usage of every call."""
    cached: int = 0
    fresh: int = 0
    answer: int = 0

    async def sink(self, _model: str, usage: Mapping[str, Any]) -> None:
        self.cached += int(usage.get("prompt_cache_hit_tokens") or 0)
        self.fresh += int(usage.get("prompt_cache_miss_tokens") or 0)
        self.answer += int(usage.get("completion_tokens") or 0)


@dataclass
class Result:
    case: dict[str, Any]
    got: str
    ok: bool
    raw: Any = None


def wants(case: Mapping[str, Any]) -> list[Any]:
    want = case.get("want")
    return list(want) if isinstance(want, list) else [want]


def theirs(case: Mapping[str, Any]) -> list[str]:
    them = case.get("them")
    return [them] if isinstance(them, str) else list(them or [])


def case_problems(case: Any) -> list[str]:
    """What is wrong with a case as written (before any model call)."""
    if not isinstance(case, dict):
        return [f"not a case: {case!r}"]
    sid = case.get("id") or "?"
    problems = []
    them = case.get("them")
    if not theirs(case) or not all(isinstance(t, str) and t.strip() for t in theirs(case)) \
            or not isinstance(them, (str, list)):
        problems.append(f"{sid}: no message in \"them\"")
    for line in case.get("chat") or []:
        if not (isinstance(line, dict) and len(line) == 1 and next(iter(line)) in ("me", "them")):
            problems.append(f"{sid}: a chat line is {{\"me\": ...}} or {{\"them\": ...}}: {line!r}")
    for key in ("now",):
        if key in case and not (isinstance(case[key], str) and _AT.match(case[key])):
            problems.append(f"{sid}: {key} is YYYY-MM-DDTHH:MM")
    booking = case.get("booking")
    if booking is not None:
        if not isinstance(booking, dict) or booking.get("state") not in STATES:
            problems.append(f"{sid}: booking state is one of {sorted(STATES)}")
        elif not _AT.match(str(booking.get("start") or "")):
            problems.append(f"{sid}: booking start is YYYY-MM-DDTHH:MM")
        elif "proposed" in booking and not _AT.match(str(booking["proposed"])):
            problems.append(f"{sid}: booking proposed is YYYY-MM-DDTHH:MM")
    if case.get("check") not in (None, "arrival"):
        problems.append(f"{sid}: check is \"arrival\" or left out")
    arrival = case.get("check") == "arrival"
    if arrival and not (isinstance(booking, dict) and booking.get("state") == bs.CONFIRMED):
        problems.append(f"{sid}: an arrival check needs a confirmed booking")
    for want in wants(case):
        if arrival:
            if not isinstance(want, bool):
                problems.append(f"{sid}: an arrival check wants true or false")
        elif isinstance(want, bool) or not isinstance(want, str) or not (want in INTENTS or _AT.match(want)):
            problems.append(f"{sid}: unknown want {want!r}")
    if not case.get("want") and case.get("want") is not False:
        problems.append(f"{sid}: no want")
    return problems


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if not (isinstance(data.get("now"), str) and _AT.match(data["now"])):
        problems.append("the file's now is YYYY-MM-DDTHH:MM")
    seen = set()
    for case in data.get("cases") or []:
        problems += case_problems(case)
        cid = case.get("id") if isinstance(case, dict) else None
        if cid in seen:
            problems.append(f"{cid}: the id is used twice")
        seen.add(cid)
    if problems:
        raise SystemExit("Bad cases:\n" + "\n".join(problems))
    return data


def history_of(case: Mapping[str, Any]) -> list[dict[str, str]]:
    history = []
    for line in case.get("chat") or []:
        (who, text), = line.items()
        history.append({"role": "user" if who == "them" else "assistant", "content": text})
    history += [{"role": "user", "content": text} for text in theirs(case)]
    return history


def live_of(case: Mapping[str, Any], tz: str, now: datetime) -> list[dict[str, Any]]:
    spec = case.get("booking")
    if not spec:
        return []
    booking = owner_check.make_booking({"number": 1, "customer": "them", "state": spec["state"],
                                        "start": spec["start"]}, tz, now)
    if spec.get("proposed"):
        at = bookings.parse_local(spec["proposed"], tz)
        booking.update(proposed_by=bs.OWNER, proposed_starts_at=at,
                       proposed_ends_at=at + (booking["ends_at"] - booking["starts_at"]))
    return [booking]


def reading(found: Optional[Mapping[str, Any]], tz: str, now: datetime) -> str:
    """What the runtime would make of the reader's answer: "none", "now", a
    local time, or another intent."""
    intent = (found or {}).get("intent")
    if not intent or intent == "none":
        return "none"
    if intent != "book":
        return intent
    if bookings.wants_now(found, tz_name=tz, now=now):
        return "now"
    slot = bookings.requested_slot(found, tz_name=tz, default_duration=60, now=now)
    return "none" if slot is None else slot[0].astimezone(bookings.tzinfo_for(tz)).strftime("%Y-%m-%dT%H:%M")


def judge(case: Mapping[str, Any], found: Optional[Mapping[str, Any]], tz: str, now: datetime) -> tuple[str, bool]:
    got = reading(found, tz, now)
    ok = got in wants(case)
    if ok and case.get("outcall") and (found or {}).get("outcall") is not True:
        return f"{got}, not read as an outcall", False
    return got, ok


async def run_case(data: Mapping[str, Any], case: dict[str, Any], *, key: str, tz: str, ai_config: dict[str, Any],
                   http: httpx.AsyncClient, usage_sink: Any = None) -> Result:
    now = bookings.parse_local(case.get("now") or data["now"], tz)
    history = history_of(case)
    if case.get("check") == "arrival":
        arrived = await ai_responder.extract_arrival(api_key=key, history=history, ai_config=ai_config, client=http,
                                                     usage_sink=usage_sink)
        return Result(case, str(arrived).lower(), arrived in wants(case))
    found = await ai_responder.extract_booking(
        api_key=key, history=history, tz_name=tz, ai_config=ai_config, client=http, now=now,
        latest=theirs(case), state_note=bookings.state_note(live_of(case, tz, now)), usage_sink=usage_sink)
    got, ok = judge(case, found, tz, now)
    return Result(case, got, ok, found)


def report(results: Sequence[Result], verbose: bool) -> str:
    by_lang: dict[str, list[bool]] = {}
    for r in results:
        by_lang.setdefault(r.case["id"].split("-")[0], []).append(r.ok)
    lines = [f"{sum(r.ok for r in results)}/{len(results)} messages read right"]
    lines += [f"  {lang:<4} {sum(oks):>3}/{len(oks)}" for lang, oks in by_lang.items()]
    for r in results:
        if r.ok and not verbose:
            continue
        lines.append(f"\n{'PASS' if r.ok else 'FAIL'}  {r.case['id']}: {theirs(r.case)!r}")
        if r.case.get("chat"):
            lines.append(f"  chat    {json.dumps(r.case['chat'], ensure_ascii=False)}")
        if r.case.get("booking"):
            lines.append(f"  booking {json.dumps(r.case['booking'], ensure_ascii=False)}")
        lines.append(f"  got     {r.got}")
        lines.append(f"  wanted  {json.dumps(r.case['want'], ensure_ascii=False)}")
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
    gate = asyncio.Semaphore(max(1, args.parallel))
    started = time.monotonic()
    tokens = Tokens()
    async with httpx.AsyncClient(timeout=90) as http:
        async def one(case: dict[str, Any]) -> Result:
            async with gate:
                try:
                    return await run_case(data, case, key=key, tz=config["timezone"], ai_config=config["ai"], http=http,
                                          usage_sink=tokens.sink)
                except Exception as exc:  # a failed call fails that case, not the run
                    return Result(case, f"error: {exc}", False)

        results = await asyncio.gather(*(one(c) for c in cases))
    print(f"Tenant {args.tenant}, {len(results)} messages in {time.monotonic() - started:.0f}s; tokens: "
          f"{tokens.cached} cached, {tokens.fresh} fresh, {tokens.answer} in answers\n")
    print(report(results, args.verbose))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
