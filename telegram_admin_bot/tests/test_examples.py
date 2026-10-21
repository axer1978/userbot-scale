"""Reply examples (examples.py): which turns of real chats become examples,
masked, with the fixed rules that keep an example from teaching the wrong
thing; storing them once, embedding them (and later, when the embedder was
down), wording the client's side; finding the closest for a new message
(ranking, k, threshold, the same chat, the phase, the industry's examples
without figures, never another industry's); the note; and the copy guard
that writes a reply again when it copied one.

All messages here are made up. The embedder and DeepSeek are stand-ins."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import ai_responder
import audit
import embeddings
import examples
import finetune
import session_runtime
import tenants
import vision
from conftest import seed_session
from database import DIR_IN, STATUS_RECEIVED
from examples import AFTER, BEFORE_TIME, BOOKED, EDITED, FLAGGED, HAND, NOTE_HEADER, RATES, TIME_PENDING
from examples import Flag, Row, Snapshot, Turn

BASE = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
NOW = BASE + timedelta(days=2)
ADDRESS = "Brivibas iela 1-5, Riga"
WAY_IN = "Door code 1234#\nThird floor, left door"
RATES_MESSAGE = "30 min 100\n1 h 150\n2 h 280\nnight 800"
MASKS = finetune.masks_for(display_name="Toms Berzins", username="toms_lv", address=ADDRESS, way_in=WAY_IN)


# --------------------------------------------------------------- pairing


def row(id, direction, text, minute=0, status=None, **fields) -> Row:
    status = status or ("received" if direction == "in" else "sent")
    return Row(id=id, direction=direction, status=status, text=text, created_at=BASE + timedelta(minutes=minute),
               **fields)


def shape(turns):
    return [([r.id for r in t.client], [r.id for r in t.business], t.context.id if t.context else None)
            for t in turns]


def test_a_turn_is_the_clients_messages_then_the_business_messages():
    rows = [row(1, "in", "hi"), row(2, "in", "free now?"), row(3, "out", "hey love"), row(4, "out", "yes"),
            row(5, "in", "how much?"), row(6, "out", "100"), row(7, "in", "ok")]

    assert shape(examples.pair(rows, now=NOW)) == [([1, 2], [3, 4], None), ([5], [6], 4)]


def test_a_draft_waiting_for_approval_keeps_its_turn_open_until_it_was_left():
    rows = [row(1, "in", "hi"), row(2, "out", "the bot's draft", 1, status="pending_approval", llm_model="m"),
            row(3, "out", "typed hello", 2), row(4, "in", "?", 3)]

    assert examples.pair(rows, now=BASE + timedelta(hours=1)) == []
    assert shape(examples.pair(rows, now=BASE + timedelta(days=2))) == [([1], [3], None)]


def turn(*business, client=("hi",)):
    return Turn([row(i + 1, "in", text) for i, text in enumerate(client)], list(business), None)


def test_only_replies_a_person_typed_or_corrected_count():
    typed = turn(row(10, "out", "hey"), row(11, "out", "how are you"))
    assert examples.classify(typed, None) == (HAND, typed.business)
    bot = turn(row(10, "out", "hey", llm_model="m", by_machine=True))
    assert examples.classify(bot, None) == (None, [])
    # One message of the reply was the bot's: not a reply typed by hand.
    mixed = turn(row(10, "out", "hey"), row(11, "out", "how are you", llm_model="m", by_machine=True))
    assert examples.classify(mixed, None) == (None, [])
    # A plain chat flag lets no bot reply in.
    assert examples.classify(bot, Flag("plain", 99)) == (None, [])


def test_an_edited_draft_is_what_was_sent_up_to_what_the_system_sent_by_itself():
    edited = turn(row(10, "out", "let me check love", llm_model="m", draft_text="yes come at 22"),
                  row(11, "out", "one sec", llm_model="m"),  # the rest of the approved draft
                  row(12, "out", ADDRESS, by_machine=True))  # the address, sent by the system
    source, reply = examples.classify(edited, None)
    assert source == EDITED and [r.id for r in reply] == [10, 11]


def test_a_chat_flagged_good_counts_the_bots_replies_up_to_the_flag():
    flagged = turn(row(10, "out", "hey handsome", llm_model="m", by_machine=True),
                   row(11, "out", "what time?", llm_model="m", by_machine=True),
                   row(12, "out", "reminder", by_machine=True))
    source, reply = examples.classify(flagged, Flag("good", 99))
    assert source == FLAGGED and [r.id for r in reply] == [10, 11]
    source, reply = examples.classify(flagged, Flag("good", 10))
    assert source == FLAGGED and [r.id for r in reply] == [10]
    assert examples.classify(flagged, Flag("good", 5)) == (None, [])


# ---------------------------------------------------------- fixed rules


def make(client, reply, *, phase=BEFORE_TIME, context=None, fixed=()):
    t = Turn([row(i + 1, "in", text) for i, text in enumerate(client)],
             [row(10 + i, "out", text) for i, text in enumerate(reply)],
             row(0, "out", context) if context is not None else None)
    return examples.build(t, t.business, source=HAND, phase=phase, masks=MASKS, fixed=fixed)


def test_both_sides_are_masked():
    example, why = make(["Hi, Toms here, free tonight? +37126942162", "or write @toms_lv"],
                        ["hey Toms 😘 yes, mail me at mia@example.com"])

    assert why == ""
    assert example["client_text"] == "Hi, [client] here, free tonight? [number]\nor write [username]"
    assert example["reply_text"] == "hey [client] 😘 yes, mail me at [email]"


def test_a_reply_with_the_address_or_the_way_in_is_left_out():
    assert make(["where are you?"], ["Brivibas iela 1-5, Riga"]) == (None, "address")
    assert make(["I'm here"], ["Door code 1234#"]) == (None, "address")


def test_a_rates_message_becomes_a_marker_and_alone_is_left_out():
    example, _ = make(["how much?"], ["hi love", RATES_MESSAGE, "when do you come?"])
    assert example["reply_text"] == f"hi love\n{RATES}\nwhen do you come?"
    assert example["has_figures"] is False
    assert make(["how much?"], [RATES_MESSAGE]) == (None, "rates_only")
    # The prompt's own one-piece messages, a one-liner too, and the markers.
    fixed = ("Kisses, massage, shower together",)
    example, _ = make(["extras?"], ["kisses, massage, shower together!", "anything else?"], fixed=fixed)
    assert example["reply_text"] == f"{RATES}\nanything else?"
    example, _ = make(["price?"], ["[block]\n30 min 100\n1 h 150\n[/block]\nok?"])
    assert example["reply_text"] == f"{RATES}\nok?"


def test_photos_alone_and_long_replies_are_left_out():
    assert make(["send pics"], ["[non-text message]"]) == (None, "media_only")
    assert make(["send pics"], ["[sent photo #3: the beach]"]) == (None, "media_only")
    example, _ = make(["send pics"], ["[non-text message]", "like it? 😘"])
    assert example["reply_text"] == "like it? 😘"
    assert make(["tell me about you"], ["word " * 100]) == (None, "too_long")
    assert make(["[photo]"], ["nice"]) == (None, "no_client_text")


def test_figures_are_marked():
    assert make(["when?"], ["at 23 love"])[0]["has_figures"] is True
    assert make(["when?"], ["soon love"])[0]["has_figures"] is False


def test_a_time_waiting_for_the_owner_is_never_an_example():
    assert make(["can I come at 23?"], ["yes come at 23"], phase=TIME_PENDING) == (None, "time_pending")


NAMES_A_TIME = ["can I come at 22?", "22:30 ok?", "в 9 вечера можно?", "pl. 21 var?", "around 10pm", "можно к 11?",
                "ap 23", "in 20 min", "через 20 минут буду", "pēc 20 min", "tomorrow 9.15", "в 2 часа можно?"]
NAMES_NO_TIME = ["how much for 1h?", "200 for an hour?", "2 часа сколько?", "cik maksā 2 stundas?", "I am 25",
                 "about 2 hours how much", "до 200 евро", "are you free now?", "price for 3 hours", "1.5h?"]


@pytest.mark.parametrize("text", NAMES_A_TIME)
def test_a_time_they_name_is_recognised(text):
    assert examples.names_a_time(text)


@pytest.mark.parametrize("text", NAMES_NO_TIME)
def test_a_length_or_a_price_is_not_a_time(text):
    assert not examples.names_a_time(text)


def test_a_client_naming_a_time_is_never_an_example():
    """Only the owner answers a time, even where no booking records the
    request: her "yes come" must never teach the bot to say yes."""
    assert make(["hi, can I come at 23?"], ["yes come love"]) == (None, "time_named")
    assert make(["hi, how much for 1h?"], ["150 love"])[0] is not None


def test_our_line_before_is_kept_as_context():
    example, _ = make(["ok"], ["when?"], context="Hi Toms, " + RATES_MESSAGE.replace("\n", " / "))
    assert example["context_text"].startswith("Hi [client], ")
    example, _ = make(["ok"], ["when?"], context=RATES_MESSAGE)
    assert example["context_text"] == RATES


# ----------------------------------------------------------------- phase


def at(hours=0.0, minutes=0.0):
    return BASE + timedelta(hours=hours, minutes=minutes)


def test_the_phase_of_a_chat():
    assert examples.phase_of([], BASE) == BEFORE_TIME
    assert examples.phase_of([Snapshot("pending", None, at(1))], BASE) == TIME_PENDING
    assert examples.phase_of([Snapshot("requested", None, at(1))], BASE) == TIME_PENDING
    # The owner offered another time: it is the client's turn.
    assert examples.phase_of([Snapshot("pending", "owner", at(1))], BASE) == BEFORE_TIME
    assert examples.phase_of([Snapshot("confirmed", None, at(1))], BASE) == BOOKED
    # Asking to move a confirmed time puts it with the owner again.
    assert examples.phase_of([Snapshot("confirmed", "customer", at(1))], BASE) == TIME_PENDING
    assert examples.phase_of([Snapshot("completed", None, at(-2))], BASE) == AFTER
    assert examples.phase_of([Snapshot("confirmed", None, at(-2))], BASE) == AFTER
    assert examples.phase_of([Snapshot("completed", None, at(-72))], BASE) == BEFORE_TIME
    assert examples.phase_of([Snapshot("cancelled", None, at(1))], BASE) == BEFORE_TIME


def test_a_booking_is_replayed_from_its_events():
    booking = {"state": "confirmed", "proposed_by": None, "starts_at": at(5), "created_at": BASE}
    events = [{"to_state": "requested", "action": "create", "actor": "customer", "payload": "{}", "created_at": BASE},
              {"to_state": "pending", "action": "submitted", "actor": "system", "payload": "{}",
               "created_at": at(minutes=1)},
              {"to_state": "pending", "action": "propose", "actor": "owner", "payload": "{}",
               "created_at": at(minutes=5)},
              {"to_state": "confirmed", "action": "accept_proposal", "actor": "customer",
               "payload": json.dumps({"starts_at": str(at(6))}), "created_at": at(minutes=10)}]

    assert examples.snapshot_at(booking, events, at(minutes=-1)) is None
    assert examples.snapshot_at(booking, events, at(minutes=2)) == Snapshot("pending", None, at(5))
    assert examples.snapshot_at(booking, events, at(minutes=6)) == Snapshot("pending", "owner", at(5))
    assert examples.snapshot_at(booking, events, at(minutes=20)) == Snapshot("confirmed", None, at(6))
    # Imported, no events: as it is now, from when it was made.
    assert examples.snapshot_at(booking, [], at(1)) == Snapshot("confirmed", None, at(5))


# ---------------------------------------------------------- note and guard


def ex(id, client, reply, context="", **more):
    return {"id": id, "client_text": client, "reply_text": reply, "context_text": context, **more}


def test_the_note():
    text, used = examples.render([ex(1, "hi\nfree now?", "hey love\nyes 😘"), ex(2, "ok", "when?", context=RATES),
                                  ex(3, "and where", "centre", context="come by taxi")], 1200)

    assert text == (NOTE_HEADER + "\n"
                    "- Client: hi / free now?\n  She: hey love / yes 😘\n"
                    "- (She had just sent her rates message.)\n  Client: ok\n  She: when?\n"
                    "- (She had just said: come by taxi)\n  Client: and where\n  She: centre")
    assert [e["id"] for e in used] == [1, 2, 3]
    assert "Never copy their words" in text and "APPOINTMENTS" in text


def test_the_note_keeps_to_its_size_best_first():
    long = ex(1, "price?", "x" * 300)
    short = ex(2, "price??", "100 love")
    text, used = examples.render([long, short], 200)
    assert [e["id"] for e in used] == [2]
    assert examples.render([long], 200) == ("", [])


def test_the_note_takes_the_best_k_above_the_threshold():
    matches = [examples.Match(1, 0.9, 11), examples.Match(2, 0.7, 12), examples.Match(3, 0.5, 13)]
    rows = {i: ex(i, f"c{i}", f"r{i}") for i in (1, 2, 3)}
    assert [e["id"] for e in examples.pick(matches, rows, k=3, min_similarity=0.55, max_chars=1200)[1]] == [1, 2]
    assert [e["id"] for e in examples.pick(matches, rows, k=1, min_similarity=0.55, max_chars=1200)[1]] == [1]
    # A switched-off example (not in rows) makes room for the next.
    assert [e["id"] for e in examples.pick(matches, {2: rows[2]}, k=1, min_similarity=0.55,
                                           max_chars=1200)[1]] == [2]


COPIED = "hey love 😘 yes I'm free tonight, just tell me what time you want to come"


def test_the_copy_guard_finds_a_reply_that_copies_an_example():
    assert examples.copies("Hey love! yes I'm free tonight, just tell me what time you want to come 😘", [COPIED])
    assert examples.copies("ok\nyes I'm free tonight, just tell me what time you want to come", [COPIED])
    assert not examples.copies("hi babe, tonight works. what hour suits you?", [COPIED])
    # Too short to matter.
    assert not examples.copies("ok love", ["ok love"])
    # The rates go out word for word to everyone by design.
    rates = "30 min 100 eur for a short visit\n1 hour 150 eur for everything"
    assert not examples.copies(f"[block]\n{rates}\n[/block]", [rates.replace("\n", " ")])
    assert examples.Shown(text="note", replies=(COPIED,)).copied_in(COPIED)
    assert not examples.NOTHING.copied_in(COPIED)


def test_the_query_is_their_messages_since_our_last_reply():
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
               {"role": "user", "content": "Toms here, free now?"}, {"role": "user", "content": "[photo]"},
               {"role": "user", "content": "call +37126942162"}]
    assert examples.query_text(history, MASKS) == "[client] here, free now?\ncall [number]"
    assert examples.query_text(history[:2], MASKS) == ""


def test_wordings_are_checked():
    originals = {1: "how much for 1 hour?", 2: "free now?"}
    raw = "```json\n" + json.dumps({"items": [
        {"id": 1, "wordings": ["how much 1h", "HOW MUCH FOR 1 HOUR?", "cik maksā stunda?", "price pls",
                               "price pls", "call me +37126942162", "x" * 400, 7]},
        {"id": 2, "wordings": ["free rn?", "u free at 22?", "tagad brīva?"]},
        {"id": 3, "wordings": ["not asked about"]},
    ]}) + "\n```"
    assert examples.parse_wordings(raw, originals) == {
        # A figure the original has may come back ("1h"); one it did not
        # have never does ("u free at 22?").
        1: ["how much 1h", "cik maksā stunda?", "price pls", "call me [number]"],
        2: ["free rn?", "tagad brīva?"],
    }
    assert examples.parse_wordings("no json here", originals) == {}


def test_the_wordings_prompt_has_the_client_side_only():
    prompt = examples.wordings_prompt([(7, "free now?")], examples.MARKET_MIXED)
    assert '{"id": 7, "text": "free now?"}' in prompt and "Latvian" in prompt and "JSON" in prompt
    assert examples.market({"timezone": "Europe/Riga"}) == examples.MARKET_MIXED
    assert examples.market({"timezone": "Europe/London", "language_policy": "fixed:ru"}) == examples.MARKET_MIXED
    assert examples.market({"timezone": "Europe/London", "language_policy": "mirror"}) == examples.MARKET_OWN


# ------------------------------------------------------------ stand-ins

# A stand-in embedder: what a text is about decides its vector, plus a
# little noise from its hash. Two texts about the same thing score ~0.95,
# about one of two things ~0.7, about different things ~0.
TOPICS = {
    "price": ("price", "how much", "cik maksā", "сколько", "cost"),
    "now": ("now", "tagad", "сейчас", "right away"),
    "photos": ("photo", "pic", "foto", "фото"),
    "where": ("where", "kur ", "где", "location"),
}
MODEL = "fake-minilm"


def vector(text: str) -> list[float]:
    lowered = text.lower()
    values = [1.0 if any(word in lowered for word in words) else 0.0 for words in TOPICS.values()]
    values += [(b - 128) / 128 * 0.1 for b in hashlib.sha256(lowered.encode()).digest()[:6]]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


@pytest.fixture
def embedder(monkeypatch):
    state = {"down": False, "calls": [], "model": MODEL}

    async def fake_embed(texts, *, client=None, timeout=embeddings.TIMEOUT_SECONDS):
        state["calls"].append((list(texts), timeout))
        if state["down"]:
            return None
        return embeddings.Embedded(model=state["model"], vectors=[vector(t) for t in texts])

    async def fake_health(*, client=None, timeout=embeddings.TIMEOUT_SECONDS):
        return None if state["down"] else state["model"]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    monkeypatch.setattr(embeddings, "health", fake_health)
    return state


@pytest.fixture
def deepseek(monkeypatch):
    """DeepSeek for the wordings: answers each client text with three
    wordings, meters a usage, and keeps what it was sent."""
    state = {"calls": [], "fail": None}

    async def fake_complete(*, api_url, api_key, payload, client=None, usage_sink=None, service="", key_name=""):
        state["calls"].append({"url": api_url, "key": api_key, "payload": payload})
        if state["fail"]:
            raise vision.VisionError(state["fail"])
        if usage_sink is not None:
            await usage_sink(payload["model"], {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 500,
                                                "completion_tokens": 200})
        content = payload["messages"][0]["content"]
        items = [json.loads(line) for line in content.split("Messages:\n", 1)[1].splitlines()]
        return json.dumps({"items": [{"id": item["id"], "wordings": [f"{item['text']} pls", f"so {item['text']}",
                                                                     "cik maksā tagad 50?"]} for item in items]})

    monkeypatch.setattr(vision, "_complete", fake_complete)
    return state


# ------------------------------------------------------------- the chats


class Chat:
    """A made-up chat of an account, written the way the runtime stores it:
    the bot's replies carry the model and a message_sent audit row by the
    bot; the system's sends (the address, a reminder) an audit row by the
    system; a draft approved in the panel the model and a row by the admin;
    a reply typed on the phone neither."""

    WHO = {
        "client": ("in", "received", None, None),
        "hand": ("out", "sent", None, None),
        "panel": ("out", "sent", None, audit.ADMIN),
        "bot": ("out", "sent", "deepseek-chat", audit.BOT),
        "approved": ("out", "sent", "deepseek-chat", audit.ADMIN),
        "system": ("out", "sent", None, audit.SYSTEM),
        "pending": ("out", "pending_approval", "deepseek-chat", None),
    }

    def __init__(self, pool, tenant, chat_id, *, session="acct", name="Toms Berzins", username="toms_lv",
                 start=BASE):
        self.pool, self.tenant, self.chat_id, self.session = pool, tenant, chat_id, session
        self.name, self.username, self.at = name, username, start

    async def open(self) -> "Chat":
        await self.pool.execute(
            "INSERT INTO conversations (session_id, tenant_id, chat_id, display_name, username) "
            "VALUES ($1, $2, $3, $4, $5)", self.session, self.tenant, self.chat_id, self.name, self.username)
        return self

    async def say(self, who: str, text: str, *, draft=None, attachments=(), gap=timedelta(minutes=1)) -> int:
        self.at += gap
        direction, status, model, actor = self.WHO[who]
        message_id = await self.pool.fetchval(
            "INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text, llm_model, draft_text, "
            "attachments, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            self.session, self.tenant, self.chat_id, direction, status, text, model, draft, list(attachments),
            self.at)
        if actor:
            await self.pool.execute(
                "INSERT INTO audit_log (tenant_id, actor, event, payload, created_at) VALUES ($1, $2, $3, $4::jsonb, $5)",
                self.tenant, actor, audit.MESSAGE_SENT, json.dumps({"message_id": message_id}),
                self.at + timedelta(seconds=1))
        if status != "pending_approval":
            await self.pool.execute(
                "UPDATE conversations SET last_message_at = $3 WHERE tenant_id = $1 AND chat_id = $2",
                self.tenant, self.chat_id, self.at)
        return message_id

    async def flag(self, verdict: str, upto: int, *, corrections=None, outcome="", client_kind="") -> None:
        await self.pool.execute(
            "INSERT INTO finetune_examples (tenant_id, chat_id, upto_message_id, verdict, outcome, client_kind, "
            "corrections, created_by, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'admin', $8) "
            "ON CONFLICT (tenant_id, chat_id) DO UPDATE SET verdict = EXCLUDED.verdict, "
            "upto_message_id = EXCLUDED.upto_message_id, outcome = EXCLUDED.outcome, "
            "client_kind = EXCLUDED.client_kind, corrections = EXCLUDED.corrections, updated_at = EXCLUDED.updated_at",
            self.tenant, self.chat_id, upto, verdict, outcome, client_kind, json.dumps(corrections or {}),
            self.at + timedelta(minutes=5))


_numbers = iter(range(1, 10_000))


async def booking(pool, tenant, chat_id, state, starts_at, events=(), *, session="acct", cancelled_by=None):
    """A booking and its state changes: (to_state, action, actor, at)."""
    booking_id = await pool.fetchval(
        "INSERT INTO bookings (tenant_id, session_id, number, chat_id, customer_ref, starts_at, ends_at, blocked_until, "
        "tz, state, cancelled_by, created_at) VALUES ($1, $2, $3, $4, 'ref', $5::timestamptz, "
        "$5::timestamptz + interval '1 hour', $5::timestamptz + interval '1 hour', 'Europe/Riga', $6, $7, $8) "
        "RETURNING id",
        tenant, session, next(_numbers), chat_id, starts_at, state, cancelled_by,
        events[0][3] if events else BASE)
    for to_state, action, actor, when in events:
        await pool.execute(
            "INSERT INTO booking_events (tenant_id, booking_id, to_state, action, actor, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)", tenant, booking_id, to_state, action, actor, when)
    return booking_id


@pytest_asyncio.fixture
async def account(pg_pool):
    tenant = await seed_session(pg_pool, "acct", name="Mia")
    await tenants.TenantStore(pg_pool).save_config(
        tenant, {"booking": {"address": ADDRESS, "arrival_instructions": WAY_IN, "provider": "@mia_owner"}},
        actor="admin")
    return tenant


async def stored(pool, tenant=None):
    rows = await pool.fetch("SELECT * FROM reply_examples WHERE ($1::int IS NULL OR tenant_id = $1) "
                            "ORDER BY chat_id, id",
                            tenant)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- ingest


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_turns_a_person_wrote_or_corrected_are_stored_masked(pg_pool, account):
    toms = await Chat(pg_pool, account, 501).open()
    await toms.say("client", "Hi, Toms here, free tonight? +37126942162")
    await toms.say("hand", "hey Toms 😘 yes from 20, how long you want?")
    await toms.say("client", "how much?")
    await toms.say("bot", "1h 150 love")                       # the bot's: never
    await toms.say("client", "where are you?")
    await toms.say("hand", "Brivibas iela 1-5, Riga")           # the address: the system sends it
    await toms.say("client", "and for 2h?")
    await toms.say("hand", RATES_MESSAGE)                       # the rates alone
    await toms.say("client", "send a pic")
    await toms.say("hand", "[non-text message]")                # a photo from the phone
    await toms.say("client", "nice, how much for 2 hours then")
    await toms.say("panel", "for you 280 love 😘")              # typed in the panel

    edit = await Chat(pg_pool, account, 502, name="Ann").open()
    await edit.say("client", "do you do domination?")
    await edit.say("approved", "yes love, +50 😘", draft="sorry love, I don't do that")
    await edit.say("approved", "when do you want to come?")
    await edit.say("system", ADDRESS)

    good = await Chat(pg_pool, account, 503, name="Karlis").open()
    await good.say("client", "hello beautiful")
    await good.say("bot", "hey handsome 😘 what time are you thinking?")
    await good.say("client", "how much for an hour")
    priced = await good.say("bot", "200 per hour")
    upto = await good.say("client", "ok later")
    await good.say("client", "back, still free?")
    await good.say("bot", "yes love")                          # after the flag
    await good.flag("good", upto, corrections={str(priced): "too cheap: our rate is 150"}, outcome="came",
                    client_kind="serious")

    bad = await Chat(pg_pool, account, 504, name="Peteris").open()
    await bad.say("client", "hi")
    hello = await bad.say("hand", "hello love, what's your plan?")
    await bad.flag("bad", hello)

    owner = await Chat(pg_pool, account, 505, name="Mia owner", username="mia_owner").open()
    await owner.say("client", "YES 3")
    await owner.say("hand", "ok")

    got = await examples.ingest_account(pg_pool, account, now=NOW)

    rows = await stored(pg_pool, account)
    assert [(r["chat_id"], r["source"], r["verdict"], r["phase"], r["client_text"], r["reply_text"])
            for r in rows] == [
        (501, HAND, "good", BEFORE_TIME, "Hi, [client] here, free tonight? [number]",
         "hey [client] 😘 yes from 20, how long you want?"),
        (501, HAND, "good", BEFORE_TIME, "nice, how much for 2 hours then", "for you 280 love 😘"),
        (502, EDITED, "good", BEFORE_TIME, "do you do domination?", "yes love, +50 😘\nwhen do you want to come?"),
        (503, FLAGGED, "good", BEFORE_TIME, "hello beautiful", "hey handsome 😘 what time are you thinking?"),
    ]
    assert [r["has_figures"] for r in rows] == [True, True, True, False]
    assert rows[1]["context_text"] == ""  # a photo before it says nothing
    assert (rows[3]["outcome"], rows[3]["client_kind"]) == ("came", "serious")
    assert got["added"] == 4 and got["more"] is False
    assert got["skipped"] == {"bot": 2, "address": 1, "rates_only": 1, "media_only": 1, "corrected": 1}
    # Nothing that names or finds anyone is stored.
    everything = json.dumps([[r["context_text"], r["client_text"], r["reply_text"]] for r in rows])
    for secret in ("Toms", "Berzins", "toms_lv", "26942162", "Brivibas", "1234#", "Third floor"):
        assert secret not in everything

    # Once only: the same chats again, or all of them, add nothing.
    assert (await examples.ingest_account(pg_pool, account, now=NOW))["added"] == 0
    assert (await examples.ingest_account(pg_pool, account, now=NOW, from_start=True))["added"] == 0
    assert len(await stored(pg_pool, account)) == 4


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_reply_while_the_time_waits_for_the_owner_is_never_stored(pg_pool, account):
    """A hand-typed "yes come at 23" is the owner deciding; once the owner
    confirmed, the reply after it is an example of the booked phase."""
    chat = await Chat(pg_pool, account, 601).open()
    asked = BASE + timedelta(minutes=1)
    await chat.say("client", "can I come at 23?")
    await booking(pg_pool, account, 601, "confirmed", BASE + timedelta(hours=5), events=[
        ("requested", "create", "customer", asked + timedelta(seconds=5)),
        ("pending", "submitted", "system", asked + timedelta(seconds=6)),
        ("confirmed", "confirm", "owner", asked + timedelta(minutes=10))])
    await chat.say("hand", "yes come at 23 love")
    await chat.say("client", "great, on my way soon", gap=timedelta(minutes=20))
    await chat.say("hand", "see you soon love 😘")

    got = await examples.ingest_account(pg_pool, account, now=NOW)

    assert got["skipped"] == {"time_pending": 1}
    [example] = await stored(pg_pool, account)
    assert (example["client_text"], example["phase"]) == ("great, on my way soon", BOOKED)


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_chat_still_being_written_waits_and_the_next_run_goes_on_from_the_last(pg_pool, account):
    chat = await Chat(pg_pool, account, 701).open()
    await chat.say("client", "hi, free now?")
    await chat.say("hand", "yes love")
    early = chat.at + timedelta(minutes=10)  # within SETTLE of the last message

    assert (await examples.ingest_account(pg_pool, account, now=early))["chats"] == 0
    mark = await examples._setting(pg_pool, examples.watermark_key(account))
    assert mark["ingested_at"] == early.isoformat()

    assert (await examples.ingest_account(pg_pool, account, now=NOW))["added"] == 1
    # Nothing new: nothing read.
    assert (await examples.ingest_account(pg_pool, account, now=NOW + timedelta(hours=1)))["chats"] == 0
    # A new message brings the chat back; the turn stored before is not stored again.
    await chat.say("client", "where are you", gap=timedelta(days=3))
    await chat.say("hand", "centre love")
    got = await examples.ingest_account(pg_pool, account, now=chat.at + timedelta(hours=1))
    assert (got["chats"], got["added"]) == (1, 1)
    assert await examples.last_ingest(pg_pool, account) == (chat.at + timedelta(hours=1)).isoformat()


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_bounded_run_says_there_is_more(pg_pool, account):
    for chat_id in (801, 802, 803):
        chat = await Chat(pg_pool, account, chat_id).open()
        await chat.say("client", f"hi from {chat_id}, free now?")
        await chat.say("hand", "yes love")

    first = await examples.ingest_account(pg_pool, account, now=NOW, max_chats=2)
    second = await examples.ingest_account(pg_pool, account, now=NOW, max_chats=2)

    assert (first["chats"], first["added"], first["more"]) == (2, 2, True)
    assert (second["chats"], second["added"], second["more"]) == (1, 1, False)


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_changed_flag_and_how_the_chat_ended_are_followed(pg_pool, account):
    chat = await Chat(pg_pool, account, 901).open()
    await chat.say("client", "hello, free tonight?")
    await chat.say("hand", "yes love, what time?")
    await chat.say("client", "how much for an hour")
    last = await chat.say("bot", "150 love")
    await chat.flag("good", last)
    await examples.ingest_account(pg_pool, account, now=NOW)
    assert [(r["source"], r["verdict"]) for r in await stored(pg_pool)] == [(HAND, "good"), (FLAGGED, "good")]

    # Judged ordinary: the bot's reply no longer counts, the typed one is plain.
    await chat.flag("plain", last)
    await examples.ingest_account(pg_pool, account, now=NOW)
    assert [(r["source"], r["verdict"]) for r in await stored(pg_pool)] == [(HAND, "plain")]

    # He came: the example knows how the chat ended.
    await booking(pg_pool, account, 901, "completed", BASE + timedelta(hours=3))
    await examples.ingest_account(pg_pool, account, now=NOW)
    assert [r["outcome"] for r in await stored(pg_pool)] == ["came"]

    # Judged bad: nothing from it stays.
    await chat.flag("bad", last)
    got = await examples.ingest_account(pg_pool, account, now=NOW)
    assert got["removed"] == 1 and await stored(pg_pool) == []


# --------------------------------------------------------------- vectors


async def seed_turns(pool, tenant, texts, *, chat_id=950, session="acct"):
    chat = await Chat(pool, tenant, chat_id, session=session).open()
    for client, reply in texts:
        await chat.say("client", client)
        await chat.say("hand", reply)
    return chat


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_with_the_embedder_down_the_pairs_are_kept_and_embedded_later(pg_pool, account, embedder):
    await seed_turns(pg_pool, account, [("how much for an hour?", "150 love"), ("free now?", "yes 😘")])
    await examples.ingest_account(pg_pool, account, now=NOW)
    embedder["down"] = True

    got = await examples.embed_pending(pg_pool, [account], client=None)

    assert got == {"embedded": 0, "model": None, "up": False}
    assert len(await stored(pg_pool)) == 2
    assert await pg_pool.fetchval("SELECT count(*) FROM example_queries") == 0

    embedder["down"] = False
    got = await examples.embed_pending(pg_pool, [account], client=None)
    assert got == {"embedded": 2, "model": MODEL, "up": True}
    queries = await pg_pool.fetch("SELECT text, generated, embed_model, embedding FROM example_queries ORDER BY id")
    assert [(q["text"], q["generated"], q["embed_model"]) for q in queries] == [
        ("how much for an hour?", False, MODEL), ("free now?", False, MODEL)]
    assert queries[0]["embedding"] == pytest.approx(vector("how much for an hour?"), abs=1e-6)
    # A batch gets the long timeout; nothing is embedded twice.
    assert embedder["calls"][-1][1] == examples.EMBED_TIMEOUT
    assert (await examples.embed_pending(pg_pool, [account], client=None))["embedded"] == 0

    # Another model: the old vectors no longer compare and are made again.
    embedder["model"] = "fake-minilm-v2"
    assert await examples.reembed_stale(pg_pool, [account], model="fake-minilm-v2", client=None) == 2
    assert {r["embed_model"] for r in await pg_pool.fetch("SELECT embed_model FROM example_queries")} == {
        "fake-minilm-v2"}


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_client_side_is_worded_again_on_the_platform_key(pg_pool, account, embedder, deepseek):
    await seed_turns(pg_pool, account, [("how much for an hour?", "150 love, the secret reply"),
                                        ("free now?", "yes 😘")])
    await examples.ingest_account(pg_pool, account, now=NOW)
    await examples.embed_pending(pg_pool, [account], client=None)

    assert (await examples.make_wordings(pg_pool, [account], api_key="", client=None))["error"] == "no_key"
    got = await examples.make_wordings(pg_pool, [account], api_key="pk", client=None)

    assert got == {"examples": 2, "wordings": 4, "calls": 1, "error": ""}
    [call] = deepseek["calls"]
    content = call["payload"]["messages"][0]["content"]
    assert call["key"] == "pk" and call["url"] == ai_responder.API_URL
    assert "how much for an hour?" in content and "free now?" in content
    assert "secret reply" not in content and "150 love" not in content  # never the business's side
    worded = await pg_pool.fetch("SELECT e.client_text, q.text FROM example_queries q JOIN reply_examples e "
                                 "ON e.id = q.example_id WHERE q.generated ORDER BY q.id")
    assert [(w["client_text"], w["text"]) for w in worded] == [
        ("how much for an hour?", "how much for an hour? pls"), ("how much for an hour?", "so how much for an hour?"),
        ("free now?", "free now? pls"), ("free now?", "so free now?"),
    ]  # "cik maksā tagad 50?" brought a figure the original did not have
    usage = await pg_pool.fetchrow("SELECT tenant_id, purpose, model FROM llm_usage")
    assert (usage["tenant_id"], usage["purpose"], usage["model"]) == (None, "reply_wordings", "deepseek-chat")
    # Worded once.
    assert (await examples.make_wordings(pg_pool, [account], api_key="pk", client=None))["calls"] == 0


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_failed_wording_call_hands_the_examples_back(pg_pool, account, embedder, deepseek):
    await seed_turns(pg_pool, account, [("free now?", "yes 😘")])
    await examples.ingest_account(pg_pool, account, now=NOW)
    await examples.embed_pending(pg_pool, [account], client=None)
    deepseek["fail"] = "DeepSeek API timed out."

    got = await examples.make_wordings(pg_pool, [account], api_key="pk", client=None)

    assert got["error"] == "DeepSeek API timed out." and got["examples"] == 0
    assert await pg_pool.fetchval("SELECT worded_at FROM reply_examples") is None


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_hourly_round_works_only_where_retrieval_is_on(pg_pool, account, embedder, deepseek, monkeypatch):
    await seed_turns(pg_pool, account, [("free now?", "yes 😘")])
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")

    await examples.tick(pg_pool, None)
    assert examples._task is None or examples._task.done()
    assert await stored(pg_pool) == []

    store = tenants.TenantStore(pg_pool)
    overrides = (await store.get(account))["config_json"]
    await store.save_config(account, {**overrides, "retrieval": {"enabled": True}}, actor="admin")
    monkeypatch.setattr(examples, "_task", None)
    await examples.tick(pg_pool, None)
    await examples._task
    [example] = await stored(pg_pool)
    assert example["worded_at"] is not None
    assert await pg_pool.fetchval("SELECT count(*) FROM example_queries") == 3  # the original and two wordings

    # Not again within the hour.
    first = examples._task
    await examples.tick(pg_pool, None)
    assert examples._task is first


# ------------------------------------------------------------- retrieval


_replies = iter(range(1, 100_000))


async def example(pool, tenant, client, reply, *, chat_id=900, phase=BEFORE_TIME, industry=1, wordings=(),
                  active=True):
    """A stored example and its vectors, as ingesting and embedding leave it."""
    example_id = await pool.fetchval(
        "INSERT INTO reply_examples (tenant_id, industry_id, chat_id, in_first_id, reply_last_id, client_text, "
        "reply_text, source, phase, has_figures, active) VALUES ($1, $2, $3, 1, $4, $5, $6, 'hand', $7, $8, $9) "
        "RETURNING id", tenant, industry, chat_id, next(_replies), client, reply, phase, any(c.isdigit() for c in reply),
        active)
    for text, generated in [(client, False), *((w, True) for w in wordings)]:
        await pool.execute("INSERT INTO example_queries (example_id, text, generated, embedding, embed_model) "
                           "VALUES ($1, $2, $3, $4, $5)", example_id, text, generated, vector(text), MODEL)
    return example_id


ASKING_PRICE = [{"role": "assistant", "content": "hey love"}, {"role": "user", "content": "hi"},
                {"role": "user", "content": "what's the price?"}]


def retrieval_on(app, **settings):
    app.config["retrieval"].update({"enabled": True, **settings})


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_switched_off_there_is_no_note(app, pg_pool, embedder):
    await example(pg_pool, app.tenant_id, "how much?", "150 love")

    assert await examples.note_for(app, 7, ASKING_PRICE) is examples.NOTHING
    assert embedder["calls"] == []


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_closest_moments_are_shown_best_first(app, pg_pool, embedder, caplog):
    price = await example(pg_pool, app.tenant_id, "how much for an hour?", "150 for an hour love")
    both = await example(pg_pool, app.tenant_id, "price now?", "for now 100, come")
    await example(pg_pool, app.tenant_id, "where are you?", "centre love")
    worded = await example(pg_pool, app.tenant_id, "can you send photo", "sure 😘", wordings=["cik maksā?"])
    retrieval_on(app)

    with caplog.at_level(logging.INFO, logger="examples"):
        shown = await examples.note_for(app, 7, ASKING_PRICE)

    # The price, then the wording that asks for it, then price-and-now; never "where".
    assert shown.ids[:1] == (price,) and set(shown.ids) == {price, worded, both}
    assert shown.text.startswith(NOTE_HEADER + "\n- Client: how much for an hour?\n  She: 150 for an hour love\n")
    assert "centre love" not in shown.text
    assert shown.replies[0] == "150 for an hour love"
    # The look-up waits for nothing, and its log has ids, never words.
    assert embedder["calls"][-1] == (["hi\nwhat's the price?"], examples.QUERY_TIMEOUT)
    assert f"#{price}" in caplog.text and "how much" not in caplog.text and "price?" not in caplog.text

    retrieval_on(app, k=1)
    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (price,)
    retrieval_on(app, k=3, min_similarity=0.85)
    assert set((await examples.note_for(app, 7, ASKING_PRICE)).ids) == {price, worded}


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_never_the_chat_being_answered_nor_a_switched_off_example(app, pg_pool, embedder):
    retrieval_on(app)
    assert await examples.note_for(app, 7, ASKING_PRICE) is examples.NOTHING
    assert embedder["calls"] == []  # nothing to compare with: no need to ask

    await example(pg_pool, app.tenant_id, "how much for an hour?", "150 love", chat_id=7)
    await example(pg_pool, app.tenant_id, "price?", "100 love", active=False)
    app.examples_cache.loaded_at = -math.inf
    assert await examples.note_for(app, 7, ASKING_PRICE) is examples.NOTHING
    assert len(app.examples_cache) == 1  # the one switched off is not even loaded


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_phase_must_match_and_a_time_with_the_owner_gets_nothing(app, pg_pool, embedder):
    before = await example(pg_pool, app.tenant_id, "how much?", "150 love")
    booked = await example(pg_pool, app.tenant_id, "how much again?", "as agreed love", phase=BOOKED)
    retrieval_on(app)
    now = app.utcnow()

    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (before,)

    await booking(pg_pool, app.tenant_id, 7, "confirmed", now + timedelta(hours=3), session="test")
    app.examples_cache.loaded_at = -math.inf
    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (booked,)

    await booking(pg_pool, app.tenant_id, 7, "pending", now + timedelta(hours=8), session="test")
    assert await examples.note_for(app, 7, ASKING_PRICE) is examples.NOTHING


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_client_naming_a_time_gets_no_examples(app, pg_pool, embedder):
    await example(pg_pool, app.tenant_id, "can I come over?", "yes love come")
    retrieval_on(app)

    naming = [{"role": "assistant", "content": "hey love"}, {"role": "user", "content": "can I come at 22?"}]
    assert await examples.note_for(app, 7, naming) is examples.NOTHING
    assert embedder["calls"] == []  # not even asked


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_industry_shares_only_examples_without_figures_and_never_another_industrys(app, pg_pool, embedder):
    other = await seed_session(pg_pool, "other", name="Leaui")
    elsewhere = await tenants.TenantStore(pg_pool).create_industry("Salons", actor="admin")
    far = await seed_session(pg_pool, "far", name="Salon", industry_id=elsewhere["id"])
    shared = await example(pg_pool, other, "how much love?", "depends on time, tell me first 😘")
    await example(pg_pool, other, "how much for an hour?", "150 for an hour")
    await example(pg_pool, far, "what's the price?", "a haircut is nice", industry=elsewhere["id"])
    retrieval_on(app)

    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (shared,)

    retrieval_on(app, share_in_industry=False)
    assert await examples.note_for(app, 7, ASKING_PRICE) is examples.NOTHING


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_vectors_are_kept_on_the_runtime_for_a_while(app, pg_pool, embedder, monkeypatch):
    first = await example(pg_pool, app.tenant_id, "how much for an hour?", "150 love")
    retrieval_on(app)
    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (first,)

    newer = await example(pg_pool, app.tenant_id, "what's the price?", "100 love")
    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (first,)  # not loaded again yet

    monkeypatch.setattr(examples, "CACHE_SECONDS", 0)
    assert (await examples.note_for(app, 7, ASKING_PRICE)).ids == (first, newer)


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_with_the_embedder_down_the_reply_goes_on_without_examples(app, pg_pool, embedder):
    await example(pg_pool, app.tenant_id, "how much for an hour?", "150 love")
    retrieval_on(app)
    embedder["down"] = True

    assert await examples.note_for(app, 7, ASKING_PRICE) is examples.NOTHING


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_scoring_thousands_of_vectors_is_quick(app, embedder):
    """The cap's worth of 384-dimension vectors, scored as a reply does."""
    import array
    import random

    cache = examples.Cache()
    count, dim = examples.CACHE_MAX, 384
    rng = random.Random(7)
    cache.vectors = [array.array("f", [rng.uniform(-1, 1) for _ in range(dim)]) for _ in range(count)]
    cache.example_ids = [i // 7 for i in range(count)]
    cache.query_ids = list(range(count))
    cache.tenants, cache.chats = [1] * count, [i % 50 for i in range(count)]
    cache.phases, cache.models = [BEFORE_TIME] * count, [MODEL] * count
    query = [rng.uniform(-1, 1) for _ in range(dim)]

    started = asyncio.get_running_loop().time()
    matches = await examples.best(cache, query, MODEL, phase=BEFORE_TIME, tenant_id=1, exclude_chat=3)
    took = asyncio.get_running_loop().time() - started

    assert len(matches) == len(set(cache.example_ids))
    assert matches == sorted(matches, key=lambda m: -m.similarity)
    assert took < 1.0, f"{took * 1000:.0f} ms for {count} vectors"


# ------------------------------------------------------------ copy guard


@pytest.fixture
def outbox(app, monkeypatch):
    sent: list[str] = []

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.append(text)
        return type("Sent", (), {"id": 1000 + len(sent)})()

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    return sent


@pytest.fixture
def model(app, monkeypatch):
    """The reply model, answering from a script in turn."""
    script: dict = {"replies": [], "calls": []}

    async def fake_generate_reply(**kwargs):
        script["calls"].append(kwargs)
        await kwargs["usage_sink"]("deepseek-chat", {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
                                                     "completion_tokens": 20})
        return script["replies"][len(script["calls"]) - 1]

    async def nothing(*a, **k):
        return None

    async def no_context(chat_id):
        return ""

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(ai_responder, "generate_reply", fake_generate_reply)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "deepseek_key", "k")
    monkeypatch.setattr(app, "quiet_seconds_left", lambda at=None: 0.0)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", no_sleep)
    return script


async def asks(db, text="Hi, are you free now? how much?"):
    await db.upsert_conversation(7, "Ann", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, text, telegram_id=1)


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_reply_that_copies_an_example_is_written_again_without_them(app, db, pg_pool, embedder, model,
                                                                            outbox):
    await example(pg_pool, app.tenant_id, "free now? how much", COPIED)
    retrieval_on(app)
    await asks(db)
    model["replies"] = ["Hey love! yes I'm free tonight, just tell me what time you want to come",
                        "hi babe, tonight works. what hour suits you?"]

    await app.draft_worker(7)

    first, again = model["calls"]
    assert "SIMILAR MOMENTS FROM EARLIER CHATS" in first["examples_note"] and COPIED in first["examples_note"]
    assert "examples_note" not in again and again["history"] == first["history"]
    [draft] = await db.pending_drafts(7)
    assert draft["text"] == "hi babe, tonight works. what hour suits you?"
    purposes = [r["purpose"] for r in await pg_pool.fetch("SELECT purpose FROM llm_usage ORDER BY id")]
    assert purposes == ["reply", "reply_rewrite"]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_reply_in_its_own_words_is_written_once(app, db, pg_pool, embedder, model, outbox):
    await example(pg_pool, app.tenant_id, "free now? how much", COPIED)
    retrieval_on(app)
    await asks(db)
    model["replies"] = ["hi babe, tonight works. what hour suits you?"]

    await app.draft_worker(7)

    assert len(model["calls"]) == 1
    [draft] = await db.pending_drafts(7)
    assert draft["text"] == model["replies"][0]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_with_the_copy_guard_off_a_copy_stays(app, db, pg_pool, embedder, model, outbox):
    await example(pg_pool, app.tenant_id, "free now? how much", COPIED)
    retrieval_on(app, copy_guard=False)
    await asks(db)
    model["replies"] = [COPIED]

    await app.draft_worker(7)

    assert len(model["calls"]) == 1
    assert [d["text"] for d in await db.pending_drafts(7)] == [COPIED]
