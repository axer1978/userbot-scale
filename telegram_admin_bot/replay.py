"""Replay scripted customer chats against a tenant's live prompt, and check
every reply. A dry run: the model is called exactly as the runtime calls it
(same prompt layers, booking notes, burst splitting, policy check), but
nothing is sent to anyone and nothing is written to the database.

    docker compose exec manager python replay.py --tenant 1
    docker compose exec manager python replay.py --tenant 1 --only "right now" --runs 3
    docker compose exec manager python replay.py --tenant 1 --candidate /tmp/candidate.json

The chats are replay_scenarios/<file>.json (--scenarios). Each scenario is a
list of steps:

    {"customer": "22:00?"}                  the customer writes; the bot answers
    {"bot": "..."}                          a message of ours already in the chat
    {"event": "confirmed"}                  the bot writes on its own (no customer message)

and per step, optionally:

    "booking":    the chat's booking state for that reply: requested,
                  requested_now, pending, confirmed_news, declined_news,
                  cancelled_news, confirmed, arrived, way_in_sent
    "state_note": the booking the extractor is told about (confirmed)
    "intent":     what the booking extractor must read: none, book, asap, cancel
    "expect":     {"has_all": [...], "has_any": [...], "lacks": [...]}, any case
    "photos":     true: the reply must send photos; false: it must not

What the system sends by itself is replayed too, as in the runtime: the
address (booking.address) where a reply wrote [address] (every chat must have
it with the prices in the first reply, except in "except_categories"), or
after a confirmation if it is not in the chat yet (not for an outcall); and
on "arrived" the way in
(booking.arrival_instructions) is the whole answer: the arrival check must
read the message as arrival, and no reply is written. Later steps are
way_in_sent. With booking.quiet_until_answered, a customer message in a
requested / requested_now / pending step gets no reply either (except the
chat's first message): the owner's answer is the next one — but photos he
asks to see go out on their own (the photo check decides), as in the runtime.

The bot has a library of TEST_PHOTOS to send from ([send N] in a reply);
what it sends is shown as "media" and goes into the history as in the
runtime. A photo he has already had is never sent again.

Every reply is also checked for what must never appear (ALWAYS_LACKS), for
agreeing to a time before the owner confirmed (UNCONFIRMED_LACKS), and by
policy.check_outbound. Top-level "every_chat_sends" names messages
({"rates message": "..."}) every chat must contain word for word, as one
message, exactly once — except in "except_categories" — and "never_says" lists
phrases no reply may contain (any case). --candidate takes {"client": {...},
"industry_boundaries": "...", "config": {...}} to try a prompt (and client
config) before saving it; "industry": {section: text} tries a whole industry
template.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

import ai_limits
import ai_responder
import bookings
import booking_states as bs
import database
import media
import pg
import policy
import prompt_layers
import tenant_config
import tenants

SCENARIO_DIR = Path(__file__).with_name("replay_scenarios")

# Never in a reply: block markers, the address tag, prompt text. (The street
# may be named: it goes out with the prices.)
ALWAYS_LACKS = ("[block]", "[/block]", "[address]", "platform rules", "appointments:",
                "news to pass on", "send now:", "[no_reply]", "[send", "[sent")
# Before the owner has confirmed: no agreeing to the time in any words.
UNCONFIRMED_LACKS = ("is booked", "you're booked", "it's booked", "booking is confirmed",
                     "text me 15", "15 mins before", "15 min before", "15 minutes before", "see you then",
                     "see you soon")
# Never, in any words: that the bot is a real person, or that it is not a bot.
HUMAN_CLAIMS = re.compile(
    r"(?i)\b(i'?m|i am|im) (not a (bot|robot|machine|program)|not (an )?ai|real|human|a real (person|girl)"
    r"|really typing|typing (this )?myself)\b(?!ly)|\bit'?s (really )?me typing\b|\bnot a bot\b"
)
# She never meets them at the entrance: they come up once the way in is sent.
AT_THE_DOOR_LACKS = ("one sec, coming", "i'm coming down", "im coming down", "i'll come down", "i will come down",
                     "i'll open the door", "meet you downstairs", "meet you at the entrance",
                     "wait for me downstairs", "спущусь", "спускаюсь", "открою дверь", "nākšu lejā",
                     "nāku lejā", "atvēršu durvis")
# The door code, until the system has sent the way in.
DOOR_CODE = "1234"
CONFIRMED_STATES = {"confirmed_news", "confirmed", "arrived", "way_in_sent"}
# A time they asked for is with the owner.
WAITING_STATES = {"requested", "requested_now", "pending"}
STATES = {"requested", "requested_now", "pending", *CONFIRMED_STATES, "declined_news", "cancelled_news"}
INTENTS = {"none", "book", "asap", "cancel"}
# The library the bot sends from in a replay (the tenant's own may be empty).
TEST_PHOTOS = [
    {"id": 1, "kind": media.PHOTO, "file": "red-dress.jpg", "description": "me in a red dress"},
    {"id": 2, "kind": media.PHOTO, "file": "mirror.jpg", "description": "mirror selfie"},
    {"id": 3, "kind": media.PHOTO, "file": "sofa.jpg", "description": "me on the sofa, smiling"},
]


# ------------------------------------------------------------- scenarios

def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = validate(data)
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return data


def load_all(names: list[str]) -> dict[str, Any]:
    """Several scenario files as one: their chats, fixed messages and
    exceptions together. The ids must be unique across all of them."""
    merged: dict[str, Any] = {"scenarios": [], "every_chat_sends": {}, "except_categories": [], "never_says": []}
    for name in names:
        data = load(SCENARIO_DIR / f"{name.strip()}.json")
        merged["scenarios"] += data["scenarios"]
        merged["every_chat_sends"].update(data.get("every_chat_sends") or {})
        merged["except_categories"] += data.get("except_categories") or []
        merged["never_says"] += data.get("never_says") or []
    problems = validate(merged)
    if problems:
        raise ValueError("; ".join(problems))
    return merged


def validate(data: dict[str, Any]) -> list[str]:
    problems, seen = [], set()
    for scenario in data.get("scenarios", []):
        sid = scenario.get("id")
        if sid in seen:
            problems.append(f"duplicate id {sid}")
        seen.add(sid)
        steps = scenario.get("steps") or []
        if not any("customer" in s or "event" in s for s in steps):
            problems.append(f"{sid}: nothing for the bot to answer")
        for step in steps:
            if sum(k in step for k in ("customer", "bot", "event")) != 1:
                problems.append(f"{sid}: a step needs exactly one of customer / bot / event")
            if step.get("booking") and step["booking"] not in STATES:
                problems.append(f"{sid}: unknown booking state {step['booking']!r}")
            if step.get("intent") and step["intent"] not in INTENTS:
                problems.append(f"{sid}: unknown intent {step['intent']!r}")
            if step.get("intent") and "customer" not in step:
                problems.append(f"{sid}: an intent check needs a customer message")
            if "photos" in step and not isinstance(step["photos"], bool):
                problems.append(f"{sid}: photos is true or false")
            if step.get("booking") == "arrived" and ("customer" not in step or step.get("expect")
                                                      or step.get("intent")):
                problems.append(f"{sid}: an arrived step is a customer message with no expect or intent "
                                "(the way in is the answer)")
    return problems


# ------------------------------------------------------------- booking notes

def booking_start(state: str, now: datetime) -> datetime:
    """When the chat's booking starts: right now for requested_now, else in
    three hours. Taken once per chat, so every note names the same time."""
    if state == "requested_now":
        return bookings.soonest_start(now)
    later = (now + timedelta(hours=3)).replace(second=0, microsecond=0)
    return later + timedelta(minutes=-later.minute % 5)


def fake_booking(state: str, now: datetime, tz: str, start: Optional[datetime] = None) -> dict[str, Any]:
    start = start or booking_start(state, now)
    status = {"confirmed_news": bs.CONFIRMED, "confirmed": bs.CONFIRMED, "arrived": bs.CONFIRMED,
              "declined_news": bs.CANCELLED, "cancelled_news": bs.CANCELLED}.get(state, bs.PENDING)
    notice = {"confirmed_news": bs.NOTICE_CONFIRMED, "declined_news": bs.NOTICE_DECLINED,
              "cancelled_news": bs.NOTICE_CANCELLED}.get(state)
    status = bs.CONFIRMED if state == "way_in_sent" else status
    return {"number": 7, "state": status, "starts_at": start, "ends_at": start + timedelta(hours=1), "tz": tz,
            "proposed_by": None, "customer_notice": notice,
            "instructions_sent_at": now if state in ("arrived", "way_in_sent") else None}


def booking_note(state: Optional[str], now: datetime, tz: str, start: Optional[datetime] = None,
                 address_follows: bool = False, address_set: bool = False, quiet: bool = False) -> str:
    lines = []
    if state:
        booking = fake_booking(state, now, tz, start)
        if quiet and state in WAITING_STATES:
            lines.append(bookings.quiet_request_line(booking))
        else:
            lines.append(bookings.notice_line(booking) if booking["customer_notice"]
                         else bookings.standing_line(booking))
        if state == "confirmed_news" and address_follows:
            lines.append(bookings.ADDRESS_FOLLOWS_LINE)
    if address_set:
        lines.append(bookings.ADDRESS_TAG_LINE)
    return bookings.context_for_reply(lines)


# ------------------------------------------------------------- checks

def _found(word: str, low: str) -> bool:
    """A word of three letters or fewer counts only as a whole word ("ai" is
    not in "wait", "bot" not in "both"); anything longer as a substring."""
    word = word.lower()
    if len(word) <= 3 and word.isalpha():
        return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low) is not None
    return word in low


def check_reply(text: str, step: dict[str, Any], state: Optional[str],
                never_says: tuple[str, ...] = (), files: int = 0) -> list[str]:
    low = text.lower()
    failed = [f"says {word!r}" for word in never_says if word.lower() in low]
    expect = step.get("expect") or {}
    for word in expect.get("has_all", []):
        if not _found(word, low):
            failed.append(f"missing {word!r}")
    if expect.get("has_any") and not any(_found(w, low) for w in expect["has_any"]):
        failed.append(f"none of {expect['has_any']}")
    for word in [*expect.get("lacks", []), *ALWAYS_LACKS]:
        if _found(word, low):
            failed.append(f"contains {word!r}")
    if HUMAN_CLAIMS.search(text):
        failed.append("claims to be a real person, or not a bot")
    if state != "way_in_sent" and DOOR_CODE in low:
        failed.append("gives the door code before he arrived")
    for word in AT_THE_DOOR_LACKS:
        if word in low:
            failed.append(f"meets them at the door ({word!r}); they come up once the way in is sent")
    if state not in CONFIRMED_STATES:
        for word in UNCONFIRMED_LACKS:
            if word in low:
                failed.append(f"agrees before the owner confirmed ({word!r})")
    if not text.strip() and not files:
        failed.append("empty reply")
    if step.get("photos") is True and not files:
        failed.append("sent no photos")
    if step.get("photos") is False and files:
        failed.append(f"sent {files} photos")
    return failed


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def check_chat(parts: list[str], must_send: dict[str, str]) -> list[str]:
    """Each fixed message once, whole, as a message of its own."""
    failed = []
    for name, text in must_send.items():
        want = _norm(text)
        count = sum(_norm(p) == want for p in parts)
        if count == 1:
            continue
        if count > 1:
            failed.append(f"sent the {name} {count} times")
        elif any(want.splitlines()[0] in p for p in parts):
            failed.append(f"the {name} was cut short, changed or merged with other text")
        else:
            failed.append(f"never sent the {name}")
    return failed


def check_intent(want: str, found: Optional[dict[str, Any]], tz: str, now: datetime) -> Optional[str]:
    intent = (found or {}).get("intent")
    if want == "none":
        return None if intent not in ("book", "cancel") else f"read as {intent}"
    if want == "cancel":
        return None if intent == "cancel" else f"read as {intent or 'nothing'}, not cancel"
    if intent != "book":
        return f"read as {intent or 'nothing'}, not a booking"
    if want == "asap":
        kwargs = {"now": now} if "now" in inspect.signature(bookings.requested_slot).parameters else {}
        slot = bookings.requested_slot(found, tz_name=tz, default_duration=60, **kwargs)
        if slot is None or slot[0] <= now:
            return f"'now' read as {found.get('start')}, which is already past"
        if slot[0] - now > timedelta(minutes=10):
            return f"'now' read as {slot[0]:%H:%M}, not now"
    return None


# ------------------------------------------------------------- running

@dataclass
class Result:
    scenario: dict[str, Any]
    turns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(not t["failed"] for t in self.turns)


@dataclass
class Context:
    key: str
    config: dict[str, Any]
    prompt: prompt_layers.Rendered
    tz: str
    http: httpx.AsyncClient
    must_send: dict[str, str] = field(default_factory=dict)
    except_categories: frozenset[str] = frozenset()
    never_says: tuple[str, ...] = ()
    photos: list[dict[str, Any]] = field(default_factory=lambda: list(TEST_PHOTOS))

    @property
    def media_note(self) -> str:
        if not self.config["media"].get("enabled", True):
            return ""
        return media.prompt_section(self.photos, ask_before_video=self.config["media"].get("ask_before_video", True))


async def run_scenario(ctx: Context, scenario: dict[str, Any]) -> Result:
    result = Result(scenario)
    history: list[dict[str, str]] = []
    burst = ctx.config["burst"]
    start: Optional[datetime] = None
    # The chat's live booking before this message, which the extractor is
    # told about as in the runtime: a request waiting, or a confirmed one.
    live: Optional[str] = None
    # The request was for an outcall (as the extractor read it): no address.
    outcall = False
    # The address is in the chat (normally from the first reply's [address]).
    address_sent = False
    first_reply = True
    # The photos he has had: one is never sent twice.
    had: set[int] = set()
    address = (ctx.config["booking"].get("address") or "").strip()
    quiet = bool(ctx.config["booking"].get("quiet_until_answered"))
    way_in = (ctx.config["booking"].get("arrival_instructions") or "").strip()
    for step in scenario["steps"]:
        if "bot" in step:
            history.append({"role": "assistant", "content": step["bot"]})
            continue
        if "customer" in step:
            history.append({"role": "user", "content": step["customer"]})
        now = datetime.now(timezone.utc)
        state = step.get("booking")
        failed: list[str] = []
        if state in ("confirmed", "arrived") and not outcall:
            # The runtime's arrival check reads every message near the time
            # (never for an outcall: there is no way in to send).
            arrived = await ai_responder.extract_arrival(api_key=ctx.key, history=[dict(m) for m in history],
                                                         ai_config=ctx.config["ai"], client=ctx.http)
            if state == "arrived" and not arrived:
                failed.append("arrival not recognised: the way in would not be sent")
            elif state == "confirmed" and arrived:
                failed.append("read as arrived: the way in would be sent now")
        if state == "arrived":
            auto = [way_in] if way_in else []
            if not way_in:
                failed.append("no booking.arrival_instructions: nothing would be sent")
            result.turns.append({"said": step["customer"], "reply": [], "auto": auto, "failed": failed})
            history.extend({"role": "assistant", "content": a} for a in auto)
            live = "confirmed"
            continue
        requesting = state in ("requested", "requested_now")
        if step.get("intent") or requesting:
            note_for = step.get("state_note") or live
            found = await ai_responder.extract_booking(
                api_key=ctx.key, history=[dict(m) for m in history], tz_name=ctx.tz, ai_config=ctx.config["ai"],
                client=ctx.http,
                state_note=bookings.state_note([fake_booking(note_for, now, ctx.tz, start)]) if note_for else "")
            if step.get("intent") and (problem := check_intent(step["intent"], found, ctx.tz, now)):
                failed.append(f"booking: {problem}")
            # The booking is for the time they asked for, as in the runtime.
            if requesting and found and found.get("intent") == "book":
                outcall = found.get("outcall") is True or outcall
                kwargs = {"now": now} if "now" in inspect.signature(bookings.requested_slot).parameters else {}
                slot = bookings.requested_slot(found, tz_name=ctx.tz, default_duration=60, **kwargs)
                if slot is not None and slot[0] > now:
                    start = slot[0]
        if start is None and (state or step.get("state_note")):
            start = booking_start(state or "", now)
        if (quiet and state in WAITING_STATES and "customer" in step
                and any(m["role"] == "assistant" for m in history)):
            # As in the runtime: nothing is written until the owner answers;
            # photos they ask to see (and have not had) go out on their own.
            unsent = (media.unsent([p for p in ctx.photos if p["kind"] == media.PHOTO], history)
                      if ctx.media_note and media.they_ask_for_photos(history) else [])
            asked = bool(unsent) and await ai_responder.extract_photo_request(
                api_key=ctx.key, history=[dict(m) for m in history], ai_config=ctx.config["ai"], client=ctx.http)
            files = unsent[:media.MAX_ATTACHMENTS_PER_REPLY] if asked else []
            if step.get("photos") is True and not files:
                failed.append("sent no photos")
            if step.get("photos") is False and files:
                failed.append(f"sent {len(files)} photos")
            turn = {"said": step["customer"], "reply": [], "auto": [], "failed": failed, "quiet": True}
            if files:
                turn["media"] = [media.label(item) for item in files]
                had.update(item["id"] for item in files)
                history.extend({"role": "assistant", "content": media.sent_placeholder(item)} for item in files)
            result.turns.append(turn)
            live = "pending"
            continue
        if (ctx.config["replies"].get("skip_bot_questions") and not (state or "").endswith("_news")
                and ai_limits.bot_question_only(history)):
            # As in the runtime: a message that only asks whether it is a
            # bot is not answered at all.
            result.turns.append({"said": step.get("customer", ""), "reply": [], "auto": [], "failed": failed,
                                 "skipped": "bot question"})
            continue
        text = await ai_responder.generate_reply(
            api_key=ctx.key, history=[dict(m) for m in history], persona={}, ai_config=ctx.config["ai"],
            client=ctx.http, adaptive_style=ctx.config["human"]["adaptive_style"],
            booking_note=booking_note(state, now, ctx.tz, start,
                                      address_follows=bool(address) and not outcall and not address_sent,
                                      address_set=bool(address), quiet=quiet),
            system_prompt=ctx.prompt.text,
            media_note="\n".join(filter(None, [ctx.media_note, ctx.media_note and media.sent_line(ctx.photos, history)])),
            language_locked=ctx.config["language_policy"] != "mirror", burst_max=burst["max_messages"],
            burst_keep_together=burst.get("keep_together", True))
        known = {item["id"]: item for item in ctx.photos}
        text, ids = media.split_attachments(ai_responder.without_no_reply(text))
        files = [known[i] for i in ids if i in known]
        # The address the reply asked for goes in as a message of its own
        # (shown as sent by the system); the reply is checked without it.
        text, tagged = bookings.take_address_tag(text)
        parts = ai_responder.split_burst(text, burst["max_messages"], burst.get("keep_together", True))
        sent = "\n".join(parts)
        if (first_reply and address and not tagged and scenario["category"] not in ctx.except_categories):
            failed.append("the address was not sent with the prices in the first reply")
        first_reply = False
        # A step's expectations are about what was said around the fixed
        # messages (rates, extras), whose words would match anything.
        fixed = {_norm(t) for t in ctx.must_send.values()}
        failed += check_reply("\n".join(p for p in parts if _norm(p) not in fixed), step, state, ctx.never_says,
                              files=len(files))
        verdict = policy.check_outbound(sent, ctx.config, ctx.prompt.business_text)
        suspends = ctx.config["anomaly"]["tripwire_suspend"]
        failed += [("policy would hold it AND SWITCH THE ACCOUNT OFF (trip-wire): " if r in verdict.tripwire and suspends
                    else "policy would hold it: ") + r for r in verdict.reasons]
        auto = [address] if address and (tagged or (state == "confirmed_news" and not outcall
                                                    and not address_sent)) else []
        address_sent = address_sent or bool(auto)
        turn = {"said": step.get("customer") or f"[{step['event']}]", "reply": parts, "auto": auto,
                "failed": failed}
        if files:
            turn["media"] = [media.label(item) for item in files]
            failed += [f"sent {media.label(item)} again: he has had it" for item in files if item["id"] in had]
            had.update(item["id"] for item in files)
        result.turns.append(turn)
        history.extend({"role": "assistant", "content": p}
                       for p in [*parts, *auto, *(media.sent_placeholder(item) for item in files)])
        live = {"requested": "pending", "requested_now": "pending", "pending": "pending", "confirmed_news": "confirmed",
                "confirmed": "confirmed", "way_in_sent": "confirmed"}.get(state or "")
    if ctx.must_send and scenario["category"] not in ctx.except_categories:
        replies = [p for t in result.turns for p in t["reply"]]
        if failed := check_chat(replies, ctx.must_send):
            result.turns.append({"said": "[the whole chat]", "reply": [], "failed": failed})
    return result


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        out[key] = _deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


async def render(pool, tenant_id: int, candidate: Optional[Path]) -> tuple[tenants.Bundle, prompt_layers.Rendered]:
    bundle = await tenants.TenantStore(pool).bundle(tenant_id)
    if candidate is None:
        return bundle, bundle.prompt
    cand = json.loads(candidate.read_text(encoding="utf-8"))
    industry = bundle.layers["industry"]
    if cand.get("industry"):
        # A whole industry template to try (its sections), instead of the tenant's.
        industry = prompt_layers.validate_industry({"sections": cand["industry"]})
    if cand.get("industry_boundaries"):
        industry = {"sections": {**industry["sections"], "boundaries": cand["industry_boundaries"]}}
    client = prompt_layers.validate_client(cand["client"]) if cand.get("client") else bundle.layers["client"]
    if cand.get("config"):
        # Client config to try with it, e.g. {"burst": {"max_messages": 4}},
        # over what the client has, validated like a save.
        bundle.resolved = tenant_config.resolve(bundle.industry["default_config"],
                                                _deep_merge(bundle.tenant.get("config_json") or {}, cand["config"]))
    return bundle, prompt_layers.render(base=bundle.layers["base"], industry=industry, client=client,
                                        business_name=bundle.tenant["name"],
                                        language_policy=bundle.config["language_policy"])


def report(results: list[Result], verbose: bool) -> str:
    lines, by_cat = [], {}
    for r in results:
        by_cat.setdefault(r.scenario["category"], []).append(r.ok)
    width = max(len(c) for c in by_cat)
    for cat, oks in by_cat.items():
        lines.append(f"  {cat:<{width}}  {sum(oks):>3}/{len(oks)}")
    for r in results:
        if r.ok and not verbose:
            continue
        lines.append(f"\n{'PASS' if r.ok else 'FAIL'}  {r.scenario['id']}")
        for t in r.turns:
            lines.append(f"  them: {t['said']}")
            for p in t["reply"]:
                lines.append("   bot: " + p.replace("\n", "\n        "))
            for p in t.get("auto", []):
                lines.append("  auto: " + p)
            for p in t.get("media", []):
                lines.append(" media: " + p)
            for f in t["failed"]:
                lines.append(f"     ✗ {f}")
    passed = sum(r.ok for r in results)
    lines.insert(0, f"{passed}/{len(results)} chats passed")
    return "\n".join(lines)


async def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--scenarios", default="maria",
                    help="files in replay_scenarios/, without .json, comma-separated")
    ap.add_argument("--only", help="run one category")
    ap.add_argument("--runs", type=int, default=1, help="run every chat this many times")
    ap.add_argument("--candidate", type=Path, help="a prompt to try instead of the live one")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--verbose", action="store_true", help="show passing chats too")
    ap.add_argument("--out", type=Path, help="also write the full results as JSON")
    args = ap.parse_args(argv)

    data = load_all(args.scenarios.split(","))
    scenarios = [s for s in data["scenarios"] if not args.only or s["category"] == args.only] * args.runs
    if not scenarios:
        print(f"No scenarios in category {args.only!r}.", file=sys.stderr)
        return 2
    pool = await pg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        bundle, prompt = await render(pool, args.tenant, args.candidate)
        key = await database.SessionRegistry(pool).load_deepseek_key(bundle.tenant["session_id"]) \
            if bundle.tenant.get("session_id") else None
        key = key or os.environ.get("DEEPSEEK_PLATFORM_KEY") or ""
    finally:
        await pool.close()
    gate = asyncio.Semaphore(max(1, args.parallel))
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=90) as http:
        ctx = Context(key, bundle.config, prompt, bundle.config["timezone"], http,
                      data.get("every_chat_sends") or {}, frozenset(data.get("except_categories") or ()),
                      tuple(dict.fromkeys(data.get("never_says") or ())))

        async def one(scenario: dict[str, Any]) -> Result:
            async with gate:
                try:
                    return await run_scenario(ctx, scenario)
                except Exception as exc:  # a failed call fails that chat, not the run
                    return Result(scenario, [{"said": "-", "reply": [], "failed": [f"error: {exc}"]}])

        results = await asyncio.gather(*(one(s) for s in scenarios))
    print(f"Tenant {args.tenant}, prompt {prompt.version_tag}{' (candidate)' if args.candidate else ''}, "
          f"{len(results)} chats in {time.monotonic() - started:.0f}s\n")
    print(report(results, args.verbose))
    if args.out:
        args.out.write_text(json.dumps([{"id": r.scenario["id"], "ok": r.ok, "turns": r.turns} for r in results],
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
