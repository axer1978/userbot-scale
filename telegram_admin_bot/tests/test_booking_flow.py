"""Bookings end to end through a running account: the customer asks, the
owner answers by text, the customer hears through the ordinary reply, and
the scheduler's tick sends reminders and lets unanswered requests lapse.

The model is scripted (what it extracts and replies); Telegram sends are
captured. Messages enter through SessionRuntime.on_incoming like real ones.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

import ai_responder
import audit
import booking_flow
import booking_states as bs
import bookings
import media
import session_runtime
import tenants
import unanswered
import vision

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

RIGA = ZoneInfo("Europe/Riga")
OWNER = 999
CUSTOMER = 42
# Monday 4 March 2030, 12:00 in Riga.
NOW = datetime(2030, 3, 4, 12, 0, tzinfo=RIGA)


_ids = itertools.count(1)


class FakeEvent:
    def __init__(self, chat_id, text="", reply_to=None, photo=False, message_id=None):
        message_id = message_id or next(_ids)
        self.is_private = True
        self.chat_id = chat_id
        self.raw_text = text
        self.message = SimpleNamespace(id=message_id, reply_to_msg_id=reply_to, photo=object() if photo else None)

    async def get_sender(self):
        name = "Owner" if self.chat_id == OWNER else "Anna"
        return SimpleNamespace(id=self.chat_id, first_name=name, last_name=None, username=name.lower(),
                               bot=False, access_hash=1)


@pytest_asyncio.fixture
async def world(app, db, monkeypatch):
    sent: dict[int, list[str]] = {}
    script = {"extract": None, "arrived": False, "reply": "ok", "notes": [], "calls": 0, "extract_calls": 0,
              "owner_answer": None, "owner_calls": []}
    clock = {"now": NOW}
    msg_ids = iter(range(500, 10_000))

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.setdefault(chat_id, []).append(text)
        return SimpleNamespace(id=next(msg_ids))

    async def fake_extract(**kw):
        script["extract_calls"] += 1
        script["state_note"] = kw.get("state_note", "")
        found, script["extract"] = script["extract"], None
        return found

    async def fake_arrival(**kw):
        return script["arrived"]

    async def fake_owner_answer(**kw):
        script["owner_calls"].append(kw)
        answer, script["owner_answer"] = script["owner_answer"], None
        if isinstance(answer, Exception):
            raise answer
        # The JSON the model wrote, read as the runtime reads it; nothing
        # scripted is an answer that can't be used.
        return bookings.parse_owner_answer(json.dumps(answer)) if answer is not None else None

    async def fake_reply(**kw):
        script["calls"] += 1
        script["notes"].append(kw.get("booking_note", ""))
        script["media_note"] = kw.get("media_note", "")
        script["no_reply_instruction"] = kw.get("no_reply_instruction", "")
        if kw.get("usage_sink"):
            await kw["usage_sink"]("deepseek-chat", {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000,
                                                     "completion_tokens": 100})
        return script["reply"]

    async def nothing(*a, **k):
        return None

    async def owner_chat():
        return OWNER

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "utcnow", lambda: clock["now"].astimezone(timezone.utc))
    monkeypatch.setattr(app.flow, "provider_chat_id", owner_chat)
    monkeypatch.setattr(app.flow, "calendar_client", lambda: None)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", nothing)
    monkeypatch.setattr(ai_responder, "extract_booking", fake_extract)
    monkeypatch.setattr(ai_responder, "extract_arrival", fake_arrival)
    monkeypatch.setattr(ai_responder, "read_owner_answer", fake_owner_answer)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    app.deepseek_key = "k"
    app.config["auto_send"] = True
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    app.config["booking"].update(enabled=True, provider="@owner", min_notice_minutes=0,
                                 arrival_instructions="Door code 4321, 2nd floor.")
    app.telegram_state["connected"] = True
    return SimpleNamespace(app=app, db=db, sent=sent, script=script, clock=clock, store=app.booking_store)


async def settle(app):
    for _ in range(10):
        tasks = [t for t in [*app.flow.scan_tasks.values(), *app.draft_tasks.values()] if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def customer(w, text, extract=None, chat=CUSTOMER, **event):
    w.script["extract"] = extract
    await w.app.on_incoming(FakeEvent(chat, text, **event))
    await settle(w.app)


async def owner(w, text, reply_to=None):
    await w.app.on_incoming(FakeEvent(OWNER, text, reply_to))
    await settle(w.app)


def book(start="2030-03-05T15:00", **extra):
    return {"intent": "book", "start": start, **extra}


async def only_booking(w):
    [b] = await w.store.between(NOW - timedelta(days=30), NOW + timedelta(days=60))
    return b


# ------------------------------------------------------------ requests


async def test_a_request_goes_to_the_owner_and_yes_confirms_it(world):
    w = world
    await customer(w, "tomorrow at 15?", book(service="haircut"))

    [request] = w.sent[OWNER]
    assert "Booking request #1" in request and "Tue 05 Mar 2030, 15:00–16:00" in request and "YES 1" in request
    b = await only_booking(w)
    assert b["state"] == bs.PENDING and b["number"] == 1
    # The customer's reply must not claim it is booked.
    assert "Do NOT say it is confirmed" in w.script["notes"][-1]

    await owner(w, "YES 1")

    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["decided_by"] == bs.OWNER
    assert w.sent[OWNER][-1].startswith("#1 confirmed")
    # The customer is told in a reply started by the confirmation itself.
    assert "has just been CONFIRMED" in w.script["notes"][-1]
    assert (await only_booking(w))["customer_notice"] is None


async def test_her_name_heads_the_owner_request_and_is_told_to_the_customer(world):
    w = world
    w.app.config["booking"]["name"] = "Lola"
    await customer(w, "hi")
    # No booking in the chat yet: no name.
    assert "Your name is" not in w.script["notes"][-1]

    await customer(w, "tomorrow at 15?", book())
    assert w.sent[OWNER][-1].startswith("📅 Booking request #1\nCustomer: Anna (@anna)\nGirl: Lola\n")

    await owner(w, "YES 1")
    assert "has just been CONFIRMED" in w.script["notes"][-1]
    assert "Your name is Lola" in w.script["notes"][-1]


async def test_without_her_name_the_owner_sees_the_account_name_and_the_customer_none(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    assert f"\nGirl: {w.app.bundle.tenant['name']}\n" in w.sent[OWNER][-1]

    await owner(w, "YES 1")
    assert "Your name is" not in w.script["notes"][-1]


async def test_the_accounts_own_confirmed_message_goes_to_the_owner(world):
    w = world
    w.app.config["booking"]["booker_messages"] = {"confirmed": "Done #{number}, {customer} {when}"}
    await customer(w, "tomorrow at 15?", book())
    # The request has no text of the account's: the default.
    assert w.sent[OWNER][-1].startswith("📅 Booking request #1\n")

    await owner(w, "YES 1")
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1] == "Done #1, Anna Tue 05 Mar 2030, 15:00–16:00"


async def test_an_own_message_that_cannot_be_used_falls_back_to_the_default(world):
    w = world
    # Stored without the panel's check: a blank the message does not have.
    w.app.config["booking"]["booker_messages"] = {"confirmed": "Done #{number} {nope}"}
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1] == "#1 confirmed ✅ for Tue 05 Mar 2030, 15:00–16:00 (1 hour) — Anna will be told."


async def test_the_address_follows_the_reply_that_says_it_is_confirmed(world):
    w = world
    w.app.config["booking"]["address"] = "Brivibas iela 1, Riga"
    w.script["reply"] = "checking, one moment"
    await customer(w, "tomorrow at 15?", book())

    # Not while the owner has not answered.
    assert "Brivibas iela 1, Riga" not in w.sent[CUSTOMER]
    assert "address is sent to them automatically" not in w.script["notes"][-1]

    w.script["reply"] = "booked for tomorrow 15:00"
    await owner(w, "YES 1")

    # The confirmation first, then the address word for word.
    assert w.sent[CUSTOMER][-2:] == ["booked for tomorrow 15:00", "Brivibas iela 1, Riga"]
    assert "address is sent to them automatically" in w.script["notes"][-1]

    # Once per booking, not again on the next message.
    await customer(w, "great")
    assert w.sent[CUSTOMER].count("Brivibas iela 1, Riga") == 1


async def test_the_address_goes_with_the_prices_and_not_again_after_the_confirmation(world):
    w = world
    w.app.config["booking"]["address"] = "Brivibas iela 1, Riga"
    w.script["reply"] = "hi love, 100 for 30 min\n[address]"
    await customer(w, "hi, prices?")
    # The reply is told about the tag; the address goes word for word, alone.
    assert "[address]" in w.script["notes"][-1]
    assert w.sent[CUSTOMER][-2:] == ["hi love, 100 for 30 min", "Brivibas iela 1, Riga"]

    w.script["reply"] = "checking, one moment"
    await customer(w, "tomorrow at 15?", book())
    w.script["reply"] = "booked for tomorrow 15:00"
    await owner(w, "YES 1")
    assert w.sent[CUSTOMER].count("Brivibas iela 1, Riga") == 1
    assert "address is sent to them automatically" not in w.script["notes"][-1]


async def test_with_address_only_when_asked_the_prices_go_without_it(world):
    w = world
    w.app.config["booking"].update(address="Brivibas iela 1, Riga", address_timing="when_asked")
    w.script["reply"] = "hi love, 100 for 30 min\n[address]"
    await customer(w, "hi, prices?")
    assert "only when they ask where you are" in w.script["notes"][-1]
    # Not asked where: the tag is dropped, the prices go alone.
    assert w.sent[CUSTOMER][-1] == "hi love, 100 for 30 min" and "Brivibas iela 1, Riga" not in w.sent[CUSTOMER]

    w.script["reply"] = "here love\n[address]"
    await customer(w, "where are you?")
    assert w.sent[CUSTOMER][-2:] == ["here love", "Brivibas iela 1, Riga"]


async def test_with_address_only_when_asked_a_confirmation_still_brings_it(world):
    w = world
    w.app.config["booking"].update(address="Brivibas iela 1, Riga", address_timing="when_asked")
    w.script["reply"] = "checking, one moment"
    await customer(w, "tomorrow at 15?", book())
    w.script["reply"] = "booked for tomorrow 15:00"
    await owner(w, "YES 1")
    assert w.sent[CUSTOMER][-2:] == ["booked for tomorrow 15:00", "Brivibas iela 1, Riga"]


async def test_quiet_until_answered_says_nothing_until_the_owner_answers(world):
    w = world
    w.app.config["booking"]["quiet_until_answered"] = True
    w.script["reply"] = "hi love, prices"
    await customer(w, "hi")
    assert w.sent[CUSTOMER] == ["hi love, prices"]

    # The time goes to the owner; nothing is written, not even to a nudge.
    replies = w.script["calls"]
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "??")
    assert w.sent[CUSTOMER] == ["hi love, prices"] and w.script["calls"] == replies
    assert len(w.sent[OWNER]) == 1

    # The owner's answer is the next message.
    w.script["reply"] = "yes love, booked for tomorrow 15:00"
    await owner(w, "YES 1")
    assert w.sent[CUSTOMER][-1] == "yes love, booked for tomorrow 15:00"
    assert "has just been CONFIRMED" in w.script["notes"][-1]


async def test_quiet_until_answered_still_sends_photos_they_ask_for(world, monkeypatch):
    w = world
    w.app.config["booking"]["quiet_until_answered"] = True
    library = w.app.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "me.jpg").write_bytes(b"\xff\xd8\xff me")
    photo = library.add_file("me.jpg", "me in a red dress")
    files = []
    file_ids = iter(range(9000, 9100))

    async def fake_deliver_file(peer, chat_id, item, path):
        files.append(item["id"])
        return SimpleNamespace(id=next(file_ids))

    check = {"says": False, "calls": 0}

    async def fake_photo_check(**kw):
        check["calls"] += 1
        return check["says"]

    monkeypatch.setattr(w.app, "deliver_file", fake_deliver_file)
    monkeypatch.setattr(ai_responder, "extract_photo_request", fake_photo_check)
    w.script["reply"] = "hi love, prices"
    await customer(w, "hi")
    await customer(w, "tomorrow at 15?", book())
    calls = w.script["calls"]

    # Talk that only mentions photos waits like anything else, unchecked.
    await customer(w, "nice pics 😍")
    assert check["calls"] == 0 and files == []

    # The photo check says it is no request for her photos: nothing.
    await customer(w, "a pic of the building?")
    assert check["calls"] == 1 and files == [] and w.sent[CUSTOMER] == ["hi love, prices"]

    # It says it is: the photos he has not had go out on their own, with
    # no text and no reply written.
    check["says"] = True
    await customer(w, "can I see a photo first?")
    assert files == [photo["id"]] and w.sent[CUSTOMER] == ["hi love, prices"]
    assert w.script["calls"] == calls

    # Once he has had them all, nothing more goes out while it waits.
    await customer(w, "more pics?")
    assert check["calls"] == 2 and files == [photo["id"]]

    # The owner's answer still comes next.
    w.script["reply"] = "yes love, booked for tomorrow 15:00"
    await owner(w, "YES 1")
    assert w.sent[CUSTOMER][-1] == "yes love, booked for tomorrow 15:00"


async def test_the_greeting_video_goes_with_the_first_reply_only(world, monkeypatch):
    w = world
    library = w.app.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "hi.mp4").write_bytes(b"video")
    (library.dir / "me.jpg").write_bytes(b"\xff\xd8\xff me")
    video = library.add_file("hi.mp4", "me saying hi")
    photo = library.add_file("me.jpg", "me in a red dress")
    library.set_flags(video["id"], greeting=True)
    files = []
    file_ids = iter(range(9000, 9100))

    async def fake_deliver_file(peer, chat_id, item, path):
        files.append(item["id"])
        w.sent.setdefault(chat_id, []).append(f"[file {item['id']}]")
        return SimpleNamespace(id=next(file_ids))

    monkeypatch.setattr(w.app, "deliver_file", fake_deliver_file)
    # Videos wait for approval, but not the greeting: the business chose it.
    assert w.app.config["media"]["videos_need_approval"]

    w.script["reply"] = "hi love 😘 what time do you want to come?"
    await customer(w, "hi")
    assert w.sent[CUSTOMER] == ["hi love 😘 what time do you want to come?", f"[file {video['id']}]"]
    assert "the system sends video #1: me saying hi by itself" in w.script["media_note"]

    # Not again; the photo he asks for goes as usual.
    w.script["reply"] = f"here love\n[send {photo['id']}]"
    await customer(w, "pics?")
    assert files == [video["id"], photo["id"]]
    assert "by itself" not in w.script["media_note"]


async def test_a_question_whether_it_is_a_bot_gets_no_reply_and_no_model_call(world):
    w = world
    w.app.config["replies"]["skip_bot_questions"] = True
    # A first message still gets the first reply.
    w.script["reply"] = "hi love, prices"
    await customer(w, "are you a bot?")
    assert w.sent[CUSTOMER] == ["hi love, prices"]

    calls = w.script["calls"]
    await customer(w, "r u a bot?")
    await customer(w, "ты бот?")
    assert w.script["calls"] == calls and w.sent[CUSTOMER] == ["hi love, prices"]
    assert await unanswered.list_items(w.app.pool, [w.app.tenant_id]) == []

    # Something else to answer: the model writes the reply.
    w.script["reply"] = "1 hour 200 love"
    await customer(w, "and how much for an hour?")
    assert w.sent[CUSTOMER][-1] == "1 hour 200 love"


async def test_quiet_until_answered_still_answers_a_first_message_with_a_time(world):
    w = world
    w.app.config["booking"]["quiet_until_answered"] = True
    w.script["reply"] = "hi love, prices"
    await customer(w, "hi, tomorrow at 15?", book())
    assert w.sent[CUSTOMER] == ["hi love, prices"]
    assert "Say nothing about that time now" in w.script["notes"][-1]

    # A cancel while it waits is answered: nothing waits any more.
    w.script["reply"] = "no problem love"
    await customer(w, "sorry, can't come", {"intent": "cancel"})
    assert w.sent[CUSTOMER][-1] == "no problem love"


async def test_no_address_for_a_time_that_is_not_free(world):
    w = world
    w.app.config["booking"]["address"] = "Brivibas iela 1, Riga"
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "tomorrow at 15?", book(), chat=CUSTOMER + 1)
    assert "Brivibas iela 1, Riga" not in w.sent.get(CUSTOMER + 1, [])


async def test_an_outcall_gets_no_address_and_the_owner_sees_it_is_an_outcall(world):
    w = world
    w.app.config["booking"]["address"] = "Brivibas iela 1, Riga"
    w.script["reply"] = "checking, one moment"
    await customer(w, "to my hotel tomorrow at 15?", book(outcall=True, notes="Hotel Bellevue"))
    [request] = w.sent[OWNER]
    assert "Notes: Outcall. Hotel Bellevue" in request

    await owner(w, "YES 1")
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert "Brivibas iela 1, Riga" not in w.sent[CUSTOMER]
    assert "address is sent to them automatically" not in w.script["notes"][-1]


async def test_an_outcall_customer_waiting_in_his_room_never_gets_the_way_in(world):
    w = world
    await customer(w, "to my hotel today at 12:30?", book("2030-03-04T12:30", outcall=True))
    await owner(w, "YES 1")
    w.script["arrived"] = True
    await customer(w, "I'm in my room waiting, 312")
    assert "Door code 4321, 2nd floor." not in w.sent[CUSTOMER]
    assert (await only_booking(w))["instructions_sent_at"] is None


async def tick(w, at=None):
    if at is not None:
        w.clock["now"] = at
    await w.app.handle_command("scheduler_tick", {})
    await settle(w.app)


async def confirmed_outcall(w, start="2030-03-04T13:00"):
    await customer(w, "to my hotel at 13?", book(start, outcall=True, notes="Hotel Bellevue"))
    await owner(w, "YES 1")
    return await only_booking(w)


async def test_an_outcall_runs_through_ready_taxi_and_the_way_to_him(world):
    w = world
    w.app.config["booking"].update(address="Brivibas iela 1, Riga", outcall_proof=False)
    await confirmed_outcall(w)
    # She is told how to go on; he hears it is booked and that she gets ready.
    assert "send READY 1" in w.sent[OWNER][-1]
    assert "outcall" in w.script["notes"][-1] and "about 20 minutes to get ready" in w.script["notes"][-1]
    assert "Brivibas iela 1, Riga" not in w.sent.get(CUSTOMER, [])

    # From the confirmation on, what he writes goes on to her word for word.
    await customer(w, "can't wait")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\ncan't wait"

    # READY: he is asked to order the taxi, and gets her address for it.
    w.script["reply"] = "ready love, order the taxi and send me the link"
    await owner(w, "READY 1")
    assert w.sent[OWNER][-1].startswith("#1 ready ✅")
    assert "you are ready for the outcall" in w.script["notes"][-1]
    assert w.sent[CUSTOMER][-2:] == ["ready love, order the taxi and send me the link", "Brivibas iela 1, Riga"]
    assert (await only_booking(w))["ready_at"] is not None

    # His taxi link goes on to her with what to answer.
    w.script["reply"] = "thank you"
    await customer(w, "here https://bolt.eu/ride/abc123")
    link = w.sent[OWNER][-1]
    assert "sent the taxi link" in link and "https://bolt.eu/ride/abc123" in link and "Send IN 1" in link
    assert "taxi link they sent has reached you" in w.script["notes"][-1]
    b = await only_booking(w)
    assert b["taxi_link"] == "https://bolt.eu/ride/abc123" and b["taxi_link_at"] is not None

    # IN: he is asked for the way to him; what he sends goes on to her.
    await owner(w, "IN 1")
    assert w.sent[OWNER][-1].startswith("#1 in the taxi ✅")
    assert "you are in the taxi now" in w.script["notes"][-1]
    assert (await only_booking(w))["in_taxi_by"] == bs.OWNER
    await customer(w, "Hotel Bellevue room 312, 3rd floor, lift on the left")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nHotel Bellevue room 312, 3rd floor, lift on the left"
    # And the replies keep knowing she is on her way.
    assert "You are in the taxi on your way to them" in w.script["notes"][-1]

    # Answered once: a second IN changes nothing.
    await owner(w, "IN 1")
    assert "already knows" in w.sent[OWNER][-1]


async def test_her_address_goes_once_even_when_the_ready_reply_writes_it(world):
    w = world
    w.app.config["booking"]["address"] = "Brivibas iela 1, Riga"
    await confirmed_outcall(w)
    w.script["reply"] = "ready love, order the taxi here\n[address]"
    await owner(w, "READY 1")
    assert w.sent[CUSTOMER][-2:] == ["ready love, order the taxi here", "Brivibas iela 1, Riga"]
    assert w.sent[CUSTOMER].count("Brivibas iela 1, Riga") == 1


async def test_he_is_told_she_is_in_the_taxi_when_her_in_does_not_come(world):
    w = world
    await confirmed_outcall(w)
    await owner(w, "READY")  # the only outcall under way: no number needed
    await customer(w, "https://bolt.eu/ride/abc")
    calls = w.script["calls"]

    await tick(w, NOW + timedelta(minutes=4))
    assert (await only_booking(w))["in_taxi_at"] is None and w.script["calls"] == calls

    await tick(w, NOW + timedelta(minutes=5))
    b = await only_booking(w)
    assert b["in_taxi_by"] == bs.SYSTEM
    assert "he was told you're in the taxi (no IN 1 within 5 min" in w.sent[OWNER][-1]
    assert w.script["calls"] == calls + 1 and "you are in the taxi now" in w.script["notes"][-1]

    # Once.
    await tick(w, NOW + timedelta(minutes=6))
    assert w.script["calls"] == calls + 1


async def test_with_no_wait_he_hears_she_is_in_the_taxi_with_the_link(world):
    w = world
    w.app.config["booking"]["outcall_in_taxi_minutes"] = 0
    await confirmed_outcall(w)
    await customer(w, "https://bolt.eu/ride/abc")
    b = await only_booking(w)
    assert b["in_taxi_by"] == bs.SYSTEM
    assert "you are in the taxi now" in w.script["notes"][-1]
    assert "Send IN" not in w.sent[OWNER][-2] and "sent the taxi link" in w.sent[OWNER][-2]


async def test_she_is_reminded_once_to_send_ready(world):
    w = world
    w.app.config["booking"]["outcall_proof"] = False
    await confirmed_outcall(w, "2030-03-04T12:30")  # confirmed at 12:00
    sent = len(w.sent[OWNER])
    await tick(w, NOW + timedelta(minutes=19))
    assert len(w.sent[OWNER]) == sent
    await tick(w, NOW + timedelta(minutes=20))
    assert w.sent[OWNER][-1].startswith("🚕 #1") and "send READY 1" in w.sent[OWNER][-1]
    await tick(w, NOW + timedelta(minutes=25))
    assert len(w.sent[OWNER]) == sent + 1


async def test_a_later_outcall_reminds_her_ahead_of_its_start(world):
    w = world
    w.app.config["booking"]["outcall_proof"] = False
    await confirmed_outcall(w, "2030-03-04T18:00")
    assert "when you are getting ready" in w.script["notes"][-1]
    sent = len(w.sent[OWNER])
    await tick(w, datetime(2030, 3, 4, 17, 39, tzinfo=RIGA))
    assert len(w.sent[OWNER]) == sent
    await tick(w, datetime(2030, 3, 4, 17, 40, tzinfo=RIGA))
    assert "send READY 1" in w.sent[OWNER][-1]


async def test_ready_asks_which_when_two_outcalls_are_under_way(world):
    w = world
    await confirmed_outcall(w)
    await customer(w, "me too at 15?", book("2030-03-04T15:00", outcall=True), chat=CUSTOMER + 1)
    await owner(w, "YES 2")
    await owner(w, "READY")
    assert w.sent[OWNER][-1].startswith("Which booking?") and "READY 7" in w.sent[OWNER][-1]
    await owner(w, "READY 2")
    assert w.sent[OWNER][-1].startswith("#2 ready")
    assert (await w.store.by_number(1))["ready_at"] is None
    # A bare IN is for the one she got ready for.
    await owner(w, "IN")
    assert w.sent[OWNER][-1].startswith("#2 in the taxi")
    assert (await w.store.by_number(1))["in_taxi_at"] is None


async def test_ready_for_an_incall_is_refused(world):
    w = world
    await confirmed_booking(w)
    await owner(w, "READY 1")
    assert "not a confirmed outcall" in w.sent[OWNER][-1]
    assert (await only_booking(w))["ready_at"] is None


async def test_an_incall_customer_is_not_passed_on(world):
    w = world
    await confirmed_booking(w)
    sent = len(w.sent[OWNER])
    await customer(w, "see you tomorrow https://bolt.eu/ride/abc")
    assert len(w.sent[OWNER]) == sent
    assert (await only_booking(w))["taxi_link"] is None


async def test_a_map_link_before_she_is_ready_is_passed_on_but_starts_nothing(world):
    w = world
    await confirmed_outcall(w)
    await customer(w, "I'm here https://maps.app.goo.gl/xyz")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nI'm here https://maps.app.goo.gl/xyz"
    assert (await only_booking(w))["taxi_link_at"] is None


async def test_with_the_taxi_steps_off_an_outcall_is_only_confirmed(world):
    w = world
    w.app.config["booking"]["outcall_taxi"] = False
    w.script["reply"] = "ok"
    await confirmed_outcall(w)
    assert "READY" not in w.sent[OWNER][-1]
    sent = len(w.sent[OWNER])
    await customer(w, "https://bolt.eu/ride/abc")
    assert len(w.sent[OWNER]) == sent
    # READY is then no command: read as her own words it changes nothing,
    # she is told so, and her chat gets no reply meant for a customer.
    w.script["owner_answer"] = said("ready", 1)
    await owner(w, "READY 1")
    assert len(w.sent[OWNER]) == sent + 1 and (await only_booking(w))["ready_at"] is None
    assert w.sent[OWNER][-1] == "#1 stays as it is: Mon 04 Mar 2030, 13:00–14:00."
    # Without the reading of her words it is an ordinary message (and gets a reply).
    w.app.config["booking"]["owner_free_text"] = False
    await owner(w, "READY 1")
    assert w.sent[OWNER][-1] == "ok" and (await only_booking(w))["ready_at"] is None


async def test_a_cancelled_outcall_stops_the_taxi_steps(world):
    w = world
    await confirmed_outcall(w, "2030-03-04T12:30")
    await owner(w, "CANCEL 1")
    sent = len(w.sent[OWNER])
    await customer(w, "why?")
    await tick(w, NOW + timedelta(minutes=30))
    assert len(w.sent[OWNER]) == sent


async def test_his_photos_of_the_place_go_on_to_her_as_photos(world, monkeypatch):
    w = world
    files = []
    file_ids = iter(range(9000, 9100))

    async def download_media(message, file=None):
        with open(file, "wb") as fh:
            fh.write(b"\xff\xd8\xff the entrance")
        return file

    async def fake_deliver_file(peer, chat_id, item, path):
        files.append((chat_id, item["kind"], path.read_bytes()))
        return SimpleNamespace(id=next(file_ids))

    async def no_vision(*a, **k):
        raise AssertionError("a photo that went on to her is not looked at")

    w.app.client = SimpleNamespace(download_media=download_media)
    monkeypatch.setattr(w.app, "deliver_file", fake_deliver_file)
    monkeypatch.setattr(w.app, "read_photo", no_vision)
    await confirmed_outcall(w)
    await owner(w, "IN 1")

    w.script["reply"] = "got it love"
    await customer(w, "this door", photo=True)
    assert files == [(OWNER, media.PHOTO, b"\xff\xd8\xff the entrance")]
    assert w.sent[OWNER][-1] == "📷 #1 Anna (@anna) sent a photo:\nthis door"
    # The reply knows a photo came, and answers it.
    history = await w.db.get_messages(CUSTOMER)
    assert any(m["text"] == "this door\n[photo for the outcall]" for m in history)
    assert w.sent[CUSTOMER][-1] == "got it love"
    owner_thread = await w.db.get_messages(OWNER)
    assert any(m["text"] == "[photo from booking #1]" for m in owner_thread)


async def test_a_photo_that_cannot_be_fetched_is_reported_to_her(world, monkeypatch):
    w = world

    async def download_media(message, file=None):
        return None  # nothing written: a view-once photo on WhatsApp, say

    w.app.client = SimpleNamespace(download_media=download_media)
    await confirmed_outcall(w)
    await customer(w, "", photo=True)
    assert "could not be fetched" in w.sent[OWNER][-1] and "#1" in w.sent[OWNER][-1]


async def test_the_panel_marks_her_ready_and_in_the_taxi(world):
    w = world
    b = await confirmed_outcall(w)

    async def panel(action):
        result = await w.app.handle_command("booking_action", {"booking_id": b["id"], "action": action})
        await settle(w.app)
        return result

    result = await panel("ready")
    assert result["ready_at"] is not None
    assert w.sent[OWNER][-1].startswith("#1 ready ✅") and w.sent[OWNER][-1].endswith("(from the panel)")
    assert "you are ready for the outcall" in w.script["notes"][-1]
    with pytest.raises(bs.IllegalTransition, match="ready already"):
        await panel("ready")

    result = await panel("in_taxi")
    assert result["in_taxi_by"] == bs.ADMIN
    assert "you are in the taxi now" in w.script["notes"][-1]
    with pytest.raises(bs.IllegalTransition, match="already knows"):
        await panel("in_taxi")


async def test_the_panel_refuses_taxi_steps_for_an_incall(world):
    w = world
    b = await confirmed_booking(w)
    with pytest.raises(bs.IllegalTransition, match="not a confirmed outcall"):
        await w.app.handle_command("booking_action", {"booking_id": b["id"], "action": "ready"})


@pytest_asyncio.fixture
async def his_photos(world, monkeypatch):
    """His photos download (or not: "fetch" False) and go on to her."""
    w = world
    got = {"files": [], "fetch": True}
    file_ids = iter(range(9000, 9100))

    async def download_media(message, file=None):
        if got["fetch"]:
            with open(file, "wb") as fh:
                fh.write(b"\xff\xd8\xff the hotel card")
            return file
        return None  # nothing written: a view-once photo on WhatsApp, say

    async def fake_deliver_file(peer, chat_id, item, path):
        got["files"].append(chat_id)
        return SimpleNamespace(id=next(file_ids))

    w.app.client = SimpleNamespace(download_media=download_media)
    monkeypatch.setattr(w.app, "deliver_file", fake_deliver_file)
    return got


async def test_an_outcall_with_the_proof_runs_photo_taxi_and_the_way_to_him(world, his_photos):
    w = world
    w.app.config["booking"]["address"] = "Brivibas iela 1, Riga"
    await confirmed_outcall(w)
    # She is told he is asked for the photo; he hears it is booked and is asked for it.
    assert "asked for a photo that shows he's at the place" in w.sent[OWNER][-1]
    assert "READY 1 skips the photo" in w.sent[OWNER][-1]
    assert "ask for a photo that shows they are at the place" in w.script["notes"][-1]
    assert "do not ask for the taxi yet" in w.script["notes"][-1]

    # Words are not the proof: they go on to her, and nothing starts.
    await customer(w, "I'm at Hotel Bellevue, room 312")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nI'm at Hotel Bellevue, room 312"
    assert (await only_booking(w))["proof_at"] is None
    assert "No taxi until it comes" in w.script["notes"][-1]

    # His photo goes on to her as the proof, and he is asked for the taxi at
    # once, with her address after the reply.
    w.script["reply"] = "thank you love, order me a taxi and send the link"
    await customer(w, "my key card", photo=True)
    assert his_photos["files"] == [OWNER]
    assert w.sent[OWNER][-1].startswith("📷 #1 Anna (@anna) sent his photo of the place (below). He's asked to order "
                                        "your taxi now")
    assert w.sent[OWNER][-1].endswith("\nmy key card")
    b = await only_booking(w)
    assert b["proof_at"] is not None and b["ready_at"] is not None
    assert "their photo of the place reached you" in w.script["notes"][-1]
    assert w.sent[CUSTOMER][-2:] == ["thank you love, order me a taxi and send the link", "Brivibas iela 1, Riga"]

    # A second photo is just a photo.
    await customer(w, "the entrance", photo=True)
    assert w.sent[OWNER][-1] == "📷 #1 Anna (@anna) sent a photo:\nthe entrance"

    # From here as before: his link goes to her, IN tells him she is coming.
    w.script["reply"] = "thank you"
    await customer(w, "https://bolt.eu/ride/abc123")
    assert "sent the taxi link" in w.sent[OWNER][-1] and "Send IN 1" in w.sent[OWNER][-1]
    await owner(w, "IN 1")
    assert w.sent[OWNER][-1].startswith("#1 in the taxi ✅")
    assert "you are in the taxi now" in w.script["notes"][-1]


async def test_a_proof_photo_that_does_not_come_through_is_asked_for_again(world, his_photos):
    w = world
    his_photos["fetch"] = False
    await confirmed_outcall(w)
    await customer(w, "", photo=True)
    assert "could not be fetched" in w.sent[OWNER][-1] and "he is asked to send it again" in w.sent[OWNER][-1]
    assert (await only_booking(w))["proof_at"] is None
    assert "did not come through to you" in w.script["notes"][-1]

    his_photos["fetch"] = True
    await customer(w, "again", photo=True)
    assert (await only_booking(w))["proof_at"] is not None


async def test_ready_skips_the_proof_photo(world, his_photos):
    w = world
    await confirmed_outcall(w)
    await owner(w, "READY 1")
    b = await only_booking(w)
    assert b["ready_at"] is not None and b["proof_at"] is None
    assert "you are ready for the outcall" in w.script["notes"][-1]
    # A photo now is no proof: it just goes on.
    await customer(w, "the hotel", photo=True)
    assert w.sent[OWNER][-1] == "📷 #1 Anna (@anna) sent a photo:\nthe hotel"
    assert (await only_booking(w))["proof_at"] is None


async def test_with_the_proof_she_is_not_reminded_to_send_ready(world):
    w = world
    await confirmed_outcall(w, "2030-03-04T12:30")
    sent = len(w.sent[OWNER])
    await tick(w, NOW + timedelta(minutes=25))
    assert len(w.sent[OWNER]) == sent


async def test_list_shows_the_outcalls_under_way(world):
    w = world
    await confirmed_outcall(w)
    await owner(w, "LIST")
    assert "Outcalls:" in w.sent[OWNER][-1] and "waiting for his photo of the place (READY 1 skips it)" in \
        w.sent[OWNER][-1]
    w.app.config["booking"]["outcall_proof"] = False
    await owner(w, "LIST")
    assert "send READY 1" in w.sent[OWNER][-1]


async def test_nothing_is_confirmed_without_the_owner(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "great, see you then!", {"intent": "confirm_attendance"})
    await customer(w, "so it's booked?", book())
    assert (await only_booking(w))["state"] == bs.PENDING
    assert len(w.sent[OWNER]) == 1


async def test_no_declines_and_the_customer_is_told(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "NO 1")
    assert (await only_booking(w))["state"] == bs.CANCELLED
    assert "NOT available" in w.script["notes"][-1]


async def test_a_bare_yes_answers_the_only_open_request_or_asks_which(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    await owner(w, "yes")
    assert "Which booking?" in w.sent[OWNER][-1]
    # A yes sent as a reply to the request message is unambiguous.
    b1 = await w.store.by_number(1)
    await owner(w, "yes", reply_to=b1["provider_message_id"])
    assert (await w.store.by_number(1))["state"] == bs.CONFIRMED
    assert (await w.store.by_number(2))["state"] == bs.PENDING


# ------------------------------------------------ the owner's own words


def said(action, number=None, start=None, in_minutes=None):
    """The reader's answer in the shape from before there could be several."""
    return {"action": action, "number": number, "start": start, "in_minutes": in_minutes}


def answer(number, action="none", **fields):
    """One booking's part of a reading (tell_customer, ask_customer,
    duration_minutes, extras_ok, extras_no, for_me, start, question)."""
    return {"number": number, "action": action, **fields}


def reading(*answers, standing=None):
    """What the reader found in her message: an answer per booking, and an
    instruction for every customer. Nothing given: about no booking."""
    return {"answers": list(answers), "standing": standing}


async def test_the_request_shows_the_extras_he_asked_for_and_keeps_them(world):
    w = world
    await customer(w, "tomorrow at 15, with domination", book(extras="domination  +50"))
    assert "\nExtras: domination +50\n" in w.sent[OWNER][-1]
    assert (await only_booking(w))["extras"] == "domination +50"
    # A new time before her answer: the extras he names then replace them.
    await customer(w, "17 better, and cum on face", book("2030-03-05T17:00", extras="domination +50, cum on face +50"))
    assert "\nExtras: domination +50, cum on face +50\n" in w.sent[OWNER][-1]


async def test_yes_i_confirm_in_her_words_confirms_the_request(world):
    w = world
    await customer(w, "tomorrow at 15?", book(extras="domination +50"))
    w.script["owner_answer"] = said("yes")
    await owner(w, "yes i confirm")
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1].startswith("#1 confirmed")
    [call] = w.script["owner_calls"]
    assert call["latest"] == "yes i confirm" and call["usage_sink"] is not None
    assert "#1 Anna: the customer asks for Tue 05 Mar 2030, 15:00–16:00 (1 hour)" in call["state_note"]
    assert "extras he asked for: domination +50" in call["state_note"]
    # The model sees her chat, the request in it, and when each message came.
    assert any("Booking request #1" in m["content"] for m in call["history"])
    assert all(isinstance(m["at"], datetime) for m in call["history"])


async def test_ready_in_20_minutes_for_a_now_request_proposes_that_time(world):
    w = world
    await customer(w, "can I come now?", {"intent": "book", "asap": True, "start": None})
    assert (await only_booking(w))["starts_at"].astimezone(RIGA).strftime("%H:%M") == "12:05"
    w.script["owner_answer"] = said("ready_at", in_minutes=20)
    await owner(w, "I'm ready in 20 minutes")
    b = await only_booking(w)
    assert b["state"] == bs.PENDING and b["proposed_by"] == bs.OWNER
    assert b["proposed_starts_at"].astimezone(RIGA).strftime("%H:%M") == "12:20"
    assert "12:20" in w.sent[OWNER][-1] and "proposed to Anna" in w.sent[OWNER][-1]
    assert "proposes Mon 04 Mar 2030, 12:20–13:20" in w.script["notes"][-1]


async def test_ready_by_the_time_he_asked_for_is_a_yes(world):
    w = world
    await customer(w, "12:30 today?", book("2030-03-04T12:30"))
    w.script["owner_answer"] = said("ready_at", in_minutes=20)
    await owner(w, "I'll be ready in 20 min")
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["proposed_by"] is None
    assert b["starts_at"].astimezone(RIGA).strftime("%H:%M") == "12:30"


async def test_able_to_meet_in_an_hour_proposes_the_hour(world):
    w = world
    await customer(w, "12:30 today?", book("2030-03-04T12:30"))
    w.script["owner_answer"] = said("ready_at", in_minutes=60)
    await owner(w, "I will be able to meet in 1 hour")
    b = await only_booking(w)
    assert b["proposed_by"] == bs.OWNER and b["proposed_starts_at"].astimezone(RIGA).strftime("%H:%M") == "13:00"


async def test_a_time_she_names_in_her_words_is_proposed(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = said("time", start="2030-03-05T17:00")
    await owner(w, "can he come at 5 instead?")
    b = await only_booking(w)
    assert b["proposed_starts_at"].astimezone(RIGA).hour == 17
    # The very time he asked for: a yes.
    w.script["owner_answer"] = said("time", start="2030-03-05T15:00")
    await owner(w, "ok actually 15 is fine")
    assert (await only_booking(w))["state"] == bs.CONFIRMED


async def test_her_words_answer_the_request_she_replies_to(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    b2 = await w.store.by_number(2)
    w.script["owner_answer"] = said("yes")
    await owner(w, "this one yes, I confirm", reply_to=b2["provider_message_id"])
    assert w.script["owner_calls"][-1]["replying_to"] == 2
    assert (await w.store.by_number(2))["state"] == bs.CONFIRMED
    assert (await w.store.by_number(1))["state"] == bs.PENDING


async def test_unclear_words_get_one_short_question(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = said("unclear")
    await owner(w, "hmm maybe, who is he?")
    assert w.sent[OWNER][-1] == "#1 Anna, Tue 15:00: yes or no? Or give another time."
    assert (await only_booking(w))["state"] == bs.PENDING
    # The reader's own question, when it has one.
    w.script["owner_answer"] = reading(answer(1, "unclear", question="Should he come at 15 or later?"))
    await owner(w, "hmm, later maybe")
    assert w.sent[OWNER][-1] == "#1: Should he come at 15 or later?"


async def test_small_talk_from_her_gets_no_reply_at_all(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    sent, calls = len(w.sent[OWNER]), w.script["calls"]
    w.script["owner_answer"] = reading()
    await owner(w, "how are you?")
    # Read, about no booking: nothing to her, and never a customer's reply.
    assert len(w.script["owner_calls"]) == 1
    assert len(w.sent[OWNER]) == sent and w.script["calls"] == calls
    assert (await only_booking(w))["state"] == bs.PENDING


async def test_no_model_call_for_a_strict_command(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    assert w.script["owner_calls"] == []
    assert (await only_booking(w))["state"] == bs.CONFIRMED


async def test_with_nothing_open_her_words_are_still_read_so_nothing_is_lost(world):
    w = world
    # Small talk: read, about nothing, so no answer at all.
    w.script["owner_answer"] = reading()
    await owner(w, "hi there")
    assert len(w.script["owner_calls"]) == 1 and OWNER not in w.sent
    assert w.script["owner_calls"][0]["state_note"] == ""
    # An instruction for every customer, with no booking open: saved for the admin.
    w.script["owner_answer"] = reading(standing="from now on cash only")
    await owner(w, "from now on cash only")
    assert w.sent[OWNER] == ['Saved for the admin to add to your rules: "from now on cash only". Nothing changes '
                             "until they accept it."]
    [rule] = await w.app.pool.fetch("SELECT source, status FROM prompt_rules")
    assert (rule["source"], rule["status"]) == ("booker", "proposed")
    # Words for "him" with no booking to put them on: she is asked which booking.
    w.script["owner_answer"] = reading(answer(None, tell_customer="bring cash"))
    await owner(w, "tell him to bring cash")
    assert w.sent[OWNER][-1] == ("Which booking is that about? Send LIST to see them. With your answer: tell him "
                                 "bring cash.")


async def test_a_yes_with_words_stands_when_the_model_cannot_be_asked(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "ok see you")
    assert len(w.script["owner_calls"]) == 1
    assert (await only_booking(w))["state"] == bs.CONFIRMED


async def test_words_that_cannot_be_read_get_the_commands_back(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "who is he? from where?")
    assert w.sent[OWNER][-1] == "I couldn't read that just now — answer with YES 1 / NO 1, or a time like 1 15:30."
    # An answer from the model that can't be used: the same.
    sent = len(w.sent[OWNER])
    await owner(w, "hmm where is he from")
    assert len(w.sent[OWNER]) == sent + 1 and w.sent[OWNER][-1].startswith("I couldn't read that just now")
    assert (await only_booking(w))["state"] == bs.PENDING

    # Out of the AI budget: the model is not asked; a loose yes still stands.
    async def limit():
        return "the daily token limit is reached"

    monkeypatch.setattr(w.app, "ai_limit_reason", limit)
    calls = len(w.script["owner_calls"])
    await owner(w, "who is he?")
    assert w.sent[OWNER][-1].startswith("I couldn't read that just now")
    await owner(w, "ok see you")
    assert len(w.script["owner_calls"]) == calls
    assert (await only_booking(w))["state"] == bs.CONFIRMED and w.sent[OWNER][-1].startswith("#1 confirmed")


async def test_her_words_read_as_ok_in_20_are_not_a_plain_yes(world):
    w = world
    await customer(w, "can I come now?", {"intent": "book", "asap": True, "start": None})
    w.script["owner_answer"] = said("ready_at", 1, in_minutes=20)
    await owner(w, "ok in 20 min")
    b = await only_booking(w)
    assert b["state"] == bs.PENDING and b["proposed_starts_at"].astimezone(RIGA).strftime("%H:%M") == "12:20"


async def test_a_yes_to_a_confirmed_booking_changes_nothing(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    w.script["owner_answer"] = said("yes", 1)
    await owner(w, "yes yes he is coming")
    assert w.sent[OWNER][-1] == "#1 stays as it is: Tue 05 Mar 2030, 15:00–16:00."


async def test_reading_her_words_can_be_switched_off(world):
    w = world
    w.app.config["booking"]["owner_free_text"] = False
    await customer(w, "tomorrow at 15?", book())
    # As before there was any reading: her words get an ordinary reply...
    await owner(w, "I'm ready in 20 minutes")
    assert w.script["owner_calls"] == [] and w.sent[OWNER][-1] == "ok"
    assert (await only_booking(w))["proposed_by"] is None
    # ...and a loose yes is a yes.
    await owner(w, "ok see you")
    assert w.script["owner_calls"] == [] and w.sent[OWNER][-1].startswith("#1 confirmed")


async def test_her_ready_in_words_starts_the_outcall_taxi_steps(world):
    w = world
    await customer(w, "come to my hotel tomorrow at 15", book(outcall=True))
    await owner(w, "YES 1")
    w.clock["now"] = datetime(2030, 3, 5, 14, 20, tzinfo=RIGA)
    w.script["owner_answer"] = said("ready")
    await owner(w, "I'm ready, he can order the taxi")
    assert (await only_booking(w))["ready_at"] is not None
    assert "is asked to order the taxi" in w.sent[OWNER][-1]


async def test_the_owner_proposes_a_time_and_the_customer_takes_it(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "1 16:30")
    b = await only_booking(w)
    assert b["state"] == bs.PENDING and b["proposed_by"] == bs.OWNER
    assert "proposes Tue 05 Mar 2030, 16:30–17:30" in w.script["notes"][-1]

    await customer(w, "yes that works", {"intent": "accept_proposal"})
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["starts_at"].astimezone(RIGA).hour == 16
    assert "took the time you proposed" in w.sent[OWNER][-1]


async def test_a_customer_changing_the_time_before_an_answer_keeps_the_number(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "actually 17 is better", book("2030-03-05T17:00"))
    b = await only_booking(w)
    assert b["number"] == 1 and b["state"] == bs.PENDING and b["starts_at"].astimezone(RIGA).hour == 17
    assert "Booking request #1 (new time)" in w.sent[OWNER][-1]


async def test_a_taken_time_is_refused_in_code_with_alternatives(world):
    w = world
    await w.store.save_rules([{"weekday": d, "start_time": "09:00", "end_time": "18:00"} for d in range(7)],
                             actor=bs.ADMIN)
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    owner_messages = len(w.sent[OWNER])

    await customer(w, "can I come tomorrow at 15:30?", book("2030-03-05T15:30"), chat=43)

    assert len(w.sent[OWNER]) == owner_messages  # never put to the owner
    note = w.script["notes"][-1]
    assert "already taken" in note and "Offer these free times" in note and "waitlist" in note
    assert "Tue 05 Mar 16:00" in note


async def test_outside_opening_hours_is_refused(world):
    w = world
    await w.store.save_rules([{"weekday": 1, "start_time": "09:00", "end_time": "12:00"}], actor=bs.ADMIN)
    await customer(w, "tomorrow at 15?", book())
    assert OWNER not in w.sent
    assert "outside opening hours" in w.script["notes"][-1]
    assert "Tue 05 Mar 11:00" in w.script["notes"][-1]


# ------------------------------------ her words: every part of her message


async def test_several_bookings_in_one_message_get_one_answer(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    sent = len(w.sent[OWNER])
    w.script["owner_answer"] = reading(answer(1, "yes"), answer(2, "no"))
    await owner(w, "yes 1, no 2")
    assert (await w.store.by_number(1))["state"] == bs.CONFIRMED
    assert (await w.store.by_number(2))["state"] == bs.CANCELLED
    # One message back to her, a line for each booking, in her order.
    assert len(w.sent[OWNER]) == sent + 1
    assert w.sent[OWNER][-1].split("\n") == [
        "#1 confirmed ✅ for Tue 05 Mar 2030, 15:00–16:00 (1 hour) — Anna will be told.",
        "#2 declined ❌ — Anna will be told.",
    ]
    # Each customer hears his own news.
    assert any("has just been CONFIRMED" in note for note in w.script["notes"])
    assert "NOT available" in w.script["notes"][-1] or "NOT available" in w.script["notes"][-2]


async def test_her_messages_are_read_one_at_a_time_in_order(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    first_read, release, seen = asyncio.Event(), asyncio.Event(), []

    async def slow_reader(**kw):
        seen.append(kw)
        if len(seen) == 1:
            first_read.set()
            await release.wait()
            return bookings.parse_owner_answer(json.dumps(reading(answer(1, "yes"))))
        return bookings.parse_owner_answer(json.dumps(reading(answer(2, "yes"))))

    monkeypatch.setattr(ai_responder, "read_owner_answer", slow_reader)
    first = asyncio.create_task(w.app.on_incoming(FakeEvent(OWNER, "the first one is fine")))
    await first_read.wait()
    second = asyncio.create_task(w.app.on_incoming(FakeEvent(OWNER, "the second one too")))
    # While her first message is being read, the second waits its turn.
    done, _ = await asyncio.wait({second}, timeout=0.5)
    assert not done and len(seen) == 1
    release.set()
    await asyncio.gather(first, second)
    await settle(w.app)
    # The second was read knowing what the first did.
    assert [kw["latest"] for kw in seen] == ["the first one is fine", "the second one too"]
    assert "#1 Anna: confirmed for" in seen[1]["state_note"]
    assert {(await w.store.by_number(n))["state"] for n in (1, 2)} == {bs.CONFIRMED}


async def test_her_words_for_him_go_with_his_next_reply_once(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "yes", tell_customer="bring cash, no cards"))
    await owner(w, "yes, tell him to bring cash, no cards")
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED
    # She sees what he will be told, so she can catch a misreading.
    assert w.sent[OWNER][-1] == ("#1 confirmed ✅ for Tue 05 Mar 2030, 15:00–16:00 (1 hour) — Anna will be told. "
                                 "I'll tell him: bring cash, no cards.")
    # His reply, started by her answer, carries the confirmation and her words.
    note = w.script["notes"][-1]
    assert "has just been CONFIRMED" in note
    assert 'NEWS TO PASS ON IN THIS REPLY: tell them "bring cash, no cards"' in note
    assert "never 'let me check'" in note
    # Once: the reply that carried them cleared them.
    assert b["booker_news"] == []
    await customer(w, "great, see you")
    assert "bring cash" not in w.script["notes"][-1]


async def test_a_decline_with_words_passes_them_on(world):
    w = world
    await customer(w, "today at 15?", book("2030-03-04T15:00"))
    w.script["owner_answer"] = reading(answer(1, "no", tell_customer="I'm busy today, maybe tomorrow"))
    await owner(w, "no, tell him I'm busy today, maybe tomorrow")
    assert (await only_booking(w))["state"] == bs.CANCELLED
    assert w.sent[OWNER][-1] == "#1 declined ❌ — Anna will be told. I'll tell him: I'm busy today, maybe tomorrow."
    note = w.script["notes"][-1]
    assert "NOT available" in note and 'tell them "I\'m busy today, maybe tomorrow"' in note


async def test_her_words_wait_for_his_next_reply_across_a_restart(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    # His chat is paused, so no reply can carry her words yet.
    await w.db.set_paused(CUSTOMER, True)
    calls = w.script["calls"]
    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="I'm 10 min late"))
    await owner(w, "tell him I'm 10 min late")
    # She is told it waits (and would be dropped if it can't go in time).
    assert w.sent[OWNER][-1] == "#1 Anna: I'll tell him: I'm 10 min late. " + bookings.WORDS_WAIT
    assert w.script["calls"] == calls

    # A restarted process: the same database, a flow with nothing in memory.
    async def owner_chat():
        return OWNER

    w.app.flow = booking_flow.BookingFlow(w.app)
    w.app.flow.provider_chat_id = owner_chat
    w.app.flow.calendar_client = lambda: None
    await w.db.set_paused(CUSTOMER, False)
    await customer(w, "ok?")
    assert 'tell them "I\'m 10 min late"' in w.script["notes"][-1]
    assert (await only_booking(w))["booker_news"] == []


async def test_her_question_goes_to_him_and_his_answers_come_back_until_she_acts(world, his_photos):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="does he want 2 hours"))
    await owner(w, "ask him if he wants 2 hours")
    assert w.sent[OWNER][-1] == "#1 Anna: I'll ask him: does he want 2 hours? His answer will come here."
    assert 'ask them "does he want 2 hours"' in w.script["notes"][-1]
    assert (await only_booking(w))["booker_asked_at"] == NOW

    # What he writes goes on to her word for word, photos as photos; he is
    # answered as usual.
    calls = w.script["calls"]
    await customer(w, "yes 2 hours please")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nyes 2 hours please"
    await customer(w, "me", photo=True)
    assert his_photos["files"] == [OWNER] and w.sent[OWNER][-1] == "📷 #1 Anna (@anna) sent a photo:\nme"
    assert any(m["text"] == "me\n[photo]" for m in await w.db.get_messages(CUSTOMER))
    assert w.script["calls"] == calls + 2

    # Her yes ends it: what he writes after that stays in his chat.
    await owner(w, "YES 1")
    assert (await only_booking(w))["booker_asked_at"] is None
    sent = len(w.sent[OWNER])
    await customer(w, "see you then")
    assert len(w.sent[OWNER]) == sent


async def test_his_answers_stop_going_to_her_after_three_hours(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="is he coming alone"))
    await owner(w, "ask him if he comes alone")
    w.clock["now"] = NOW + timedelta(hours=2, minutes=59)
    await customer(w, "yes alone")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nyes alone"
    sent = len(w.sent[OWNER])
    w.clock["now"] = NOW + timedelta(hours=3)
    await customer(w, "so?")
    assert len(w.sent[OWNER]) == sent


async def test_her_question_about_a_booking_is_answered_to_her_alone(world):
    w = world
    await customer(w, "tomorrow at 15 at my hotel, with domination?",
                   book(extras="domination +50", outcall=True, notes="Hotel Bellevue"))
    calls = w.script["calls"]
    w.script["owner_answer"] = reading(answer(1, "none", for_me=True))
    await owner(w, "what extras did he want? at his place?")
    facts = w.sent[OWNER][-1].split("\n")
    assert facts[:3] == ["#1 Anna (@anna) — Tue 05 Mar 2030, 15:00–16:00 (1 hour), outcall.",
                         "Waiting for your answer.", "Extras he asked for: domination +50."]
    # Nothing to him.
    assert w.script["calls"] == calls and (await only_booking(w))["booker_news"] == []


async def test_her_remarks_about_him_stay_with_her(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    # The reader keeps "he's weird, he didn't show last time" out of
    # everything that goes to him.
    w.script["owner_answer"] = reading(answer(1, "yes"))
    await owner(w, "ok yes, but he is weird, he didn't show last time")
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert "I'll tell him" not in w.sent[OWNER][-1]
    note = w.script["notes"][-1]
    assert "has just been CONFIRMED" in note and "tell them" not in note and "weird" not in note


async def test_a_yes_for_a_shorter_time_confirms_that_length(world):
    w = world
    await customer(w, "tomorrow at 15 for 2 hours?", book(duration_minutes=120))
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=60))
    await owner(w, "yes but only 1 hour")
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["ends_at"].astimezone(RIGA).strftime("%H:%M") == "16:00"
    assert b["blocked_until"] == b["ends_at"]
    assert w.sent[OWNER][-1] == "#1 confirmed ✅ for Tue 05 Mar 2030, 15:00–16:00 (1 hour) — Anna will be told."
    assert "the appointment on Tue 05 Mar 2030, 15:00–16:00 has just been CONFIRMED" in w.script["notes"][-1]


async def test_a_yes_for_longer_than_is_free_confirms_nothing(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 16?", book("2030-03-05T16:00"), chat=43)
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=120, tell_customer="bring cash"))
    await owner(w, "yes 1, 2 hours, tell him bring cash")
    b = await w.store.by_number(1)
    assert b["state"] == bs.PENDING and b["booker_news"] == []
    assert w.sent[OWNER][-1] == ("#1: Tue 05 Mar 2030, 15:00–17:00 (2 hours) is not possible (conflict). Nothing "
                                 "confirmed; YES 1 takes it as he asked: Tue 05 Mar 2030, 15:00–16:00.")


async def test_she_makes_a_confirmed_booking_longer_or_shorter(world):
    w = world
    await confirmed_booking(w)
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=120))
    await owner(w, "he can stay 2 hours")
    b = await only_booking(w)
    assert b["ends_at"].astimezone(RIGA).hour == 17 and b["blocked_until"] == b["ends_at"]
    assert w.sent[OWNER][-1] == "#1 now Tue 05 Mar 2030, 15:00–17:00 (2 hours) ✅ — Anna will be told."
    assert "now lasts a different time: it is Tue 05 Mar 2030, 15:00–17:00" in w.script["notes"][-1]
    assert [e["action"] for e in await w.store.events(b["id"])][-1] == "change_length"

    # Longer than is free: it stays.
    await customer(w, "me at 18?", book("2030-03-05T18:00"), chat=43)
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=240))
    await owner(w, "make #1 4 hours")
    assert w.sent[OWNER][-1] == ("#1: Tue 05 Mar 2030, 15:00–19:00 (4 hours) is not possible (conflict). It stays "
                                 "Tue 05 Mar 2030, 15:00–17:00.")
    # Shorter is always free.
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=30))
    await owner(w, "only 30 min for #1 actually")
    assert (await w.store.by_number(1))["ends_at"].astimezone(RIGA).strftime("%H:%M") == "15:30"


async def test_an_extra_she_refuses_is_never_offered_to_him_again(world):
    w = world
    await customer(w, "tomorrow at 15, with anal?", book(extras="anal +100"))
    w.script["owner_answer"] = reading(answer(1, "yes", extras_no=["anal"]))
    await owner(w, "yes but no anal")
    assert w.sent[OWNER][-1].endswith("— Anna will be told. I'll tell him: no anal.")
    note = w.script["notes"][-1]
    assert "these extras are NOT on offer for them: anal" in note
    assert "Extras NOT on offer for them, whatever your lists say: anal." in note
    b = await only_booking(w)
    assert (b["extras_no"], b["booker_news"]) == ("anal", [])

    # Every later reply in his chat knows; the news itself went once.
    await customer(w, "what about anal then?")
    assert "Extras NOT on offer for them" in w.script["notes"][-1]
    assert "NEWS TO PASS ON" not in w.script["notes"][-1]
    # Also for a new booking of his, after this one is over.
    await customer(w, "can't come, sorry", {"intent": "cancel"})
    await customer(w, "thursday at 15?", book("2030-03-07T15:00"))
    assert "Extras NOT on offer for them, whatever your lists say: anal." in w.script["notes"][-1]
    # Never in anyone else's chat.
    await customer(w, "hi", chat=43)
    assert "NOT on offer" not in w.script["notes"][-1]


async def test_with_quiet_until_answered_her_words_still_go_while_his_time_waits(world):
    w = world
    w.app.config["booking"]["quiet_until_answered"] = True
    w.script["reply"] = "hi love, prices"
    await customer(w, "hi")
    await customer(w, "tomorrow at 15?", book())
    assert w.sent[CUSTOMER] == ["hi love, prices"]

    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="I'm in the shower, 10 minutes"))
    w.script["reply"] = "give me 10 min love, I'm in the shower"
    await owner(w, "tell him I'm in the shower, 10 min")
    assert w.sent[CUSTOMER][-1] == "give me 10 min love, I'm in the shower"
    note = w.script["notes"][-1]
    assert 'tell them "I\'m in the shower, 10 minutes"' in note and "Say nothing about that time now" in note
    assert (await only_booking(w))["state"] == bs.PENDING

    # His next message waits for her answer again.
    calls = w.script["calls"]
    await customer(w, "??")
    assert w.script["calls"] == calls


async def test_a_time_she_names_with_a_length_is_proposed_for_that_long(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "time", start="2030-03-05T18:00", duration_minutes=30))
    await owner(w, "not at 15, 18:00 for half an hour")
    b = await only_booking(w)
    assert b["proposed_by"] == bs.OWNER
    assert [t.astimezone(RIGA).strftime("%H:%M") for t in (b["proposed_starts_at"], b["proposed_ends_at"])] == \
        ["18:00", "18:30"]


async def test_words_that_fit_two_requests_get_asked_which_one(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    w.script["owner_answer"] = reading(answer(None, "yes"))
    await owner(w, "ok fine")
    assert w.sent[OWNER][-1] == "Which one: #1 Anna, Tue 15:00, or #2 Anna, Tue 17:00? Answer with the number."
    assert {(await w.store.by_number(n))["state"] for n in (1, 2)} == {bs.PENDING}


async def test_a_standing_instruction_is_proposed_as_a_rule(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(standing="from now on cash only")
    await owner(w, "from now on cash only, for everyone")
    assert w.sent[OWNER][-1] == ('Saved for the admin to add to your rules: "from now on cash only". Nothing '
                                 "changes until they accept it.")
    [rule] = await w.app.pool.fetch("SELECT * FROM prompt_rules")
    assert (rule["source"], rule["status"], rule["created_by"], rule["tenant_id"], rule["section"], rule["text"]) == \
        ("booker", "proposed", "booker", w.app.tenant_id, "booking", "from now on cash only")
    assert json.loads(rule["source_ref"]) == {"tenant_id": w.app.tenant_id, "chat_id": OWNER}
    assert (await only_booking(w))["state"] == bs.PENDING

    # One that can't be saved is told to her, and the rest still happens.
    w.script["owner_answer"] = reading(answer(1, "yes"), standing="cash")
    await owner(w, "yes. and cash")
    assert w.sent[OWNER][-1].split("\n")[1] == \
        'I couldn\'t save "cash" for the admin just now — please tell them yourself.'
    assert (await only_booking(w))["state"] == bs.CONFIRMED


# ------------------------------------------------- cancel, move, waitlist


async def test_a_cancel_frees_the_slot_for_the_first_on_the_waitlist(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await customer(w, "tomorrow at 15?", book(), chat=43)
    await customer(w, "put me on the waitlist", {"intent": "waitlist"}, chat=43)
    [entry] = await w.store.waitlist()
    assert entry["chat_id"] == 43

    await customer(w, "sorry, I can't make it", {"intent": "cancel"})

    assert (await w.store.by_number(1))["state"] == bs.CANCELLED
    assert "cancelled by the customer" in w.sent[OWNER][-1]
    [entry] = await w.store.waitlist()
    assert entry["state"] == "offered"
    assert "has freed up" in w.script["notes"][-1]

    await customer(w, "yes please!", {"intent": "accept_proposal"}, chat=43)
    two = await w.store.by_number(2)
    assert two["chat_id"] == 43 and two["state"] == bs.PENDING  # still the owner's call
    assert "Booking request #2" in w.sent[OWNER][-1]


async def test_the_customer_moves_a_confirmed_booking_with_the_owners_yes(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await customer(w, "can we move it to thursday 10?", book("2030-03-07T10:00"))
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["proposed_by"] == bs.CUSTOMER
    assert "asks to move" in w.sent[OWNER][-1]

    await owner(w, "YES 1")
    b = await only_booking(w)
    assert b["number"] == 1 and b["starts_at"].astimezone(RIGA).day == 7 and b["proposed_by"] is None
    assert "MOVED" in w.script["notes"][-1]
    events = [e["action"] for e in await w.store.events(b["id"])]
    assert events[-1] == "reschedule" and "cancel" not in events


# ---------------------------------------------------- the scheduler tick


async def confirmed_booking(w, start="2030-03-05T15:00"):
    await customer(w, "time?", book(start))
    await owner(w, "YES 1")
    return await only_booking(w)


async def test_reminders_go_once_and_1_confirms_attendance(world):
    w = world
    await confirmed_booking(w)
    replies_before = w.script["calls"]
    w.clock["now"] = datetime(2030, 3, 5, 13, 30, tzinfo=RIGA)  # 90 minutes before

    await w.app.handle_command("scheduler_tick", {})
    await settle(w.app)
    await w.app.handle_command("scheduler_tick", {})
    await settle(w.app)

    assert w.script["calls"] == replies_before + 1
    assert "SEND NOW" in w.script["notes"][-1] and "reply 1" in w.script["notes"][-1]
    [event] = [e for e in await audit.list_events(w.app.pool, tenant_id=w.app.tenant_id)
               if e["event"] == audit.REMINDER_SENT]
    assert event["payload"]["minutes_before"] == 120

    extract_calls = w.script["extract_calls"]
    await customer(w, "1")
    assert (await only_booking(w))["attendance_confirmed_at"] is not None
    assert w.script["extract_calls"] == extract_calls  # decided in code, no model call


async def test_2_after_a_reminder_cancels(world):
    w = world
    await confirmed_booking(w)
    w.clock["now"] = datetime(2030, 3, 5, 13, 30, tzinfo=RIGA)
    await w.app.handle_command("scheduler_tick", {})
    await settle(w.app)
    await customer(w, "2")
    assert (await only_booking(w))["state"] == bs.CANCELLED


async def test_an_unanswered_request_lapses_at_its_start(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.clock["now"] = datetime(2030, 3, 5, 15, 1, tzinfo=RIGA)
    await w.app.handle_command("scheduler_tick", {})
    await settle(w.app)
    b = await only_booking(w)
    assert (b["state"], b["cancelled_by"]) == (bs.CANCELLED, bs.SYSTEM)
    assert "lapsed" in w.sent[OWNER][-1]


# ------------------------------------------------------- panel and page


async def test_a_panel_decision_is_passed_on_to_the_owner(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    b = await only_booking(w)
    result = await w.app.handle_command("booking_action", {"booking_id": b["id"], "action": "confirm"})
    assert result["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1].endswith("(from the panel)")
    with pytest.raises(bs.IllegalTransition):
        await w.app.handle_command("booking_action", {"booking_id": b["id"], "action": "confirm"})


async def test_the_customer_page_cancel_tells_both_sides(world):
    w = world
    b = await confirmed_booking(w)
    await w.app.handle_command("booking_customer_action", {"booking_id": b["id"], "action": "cancel"})
    await settle(w.app)
    assert (await only_booking(w))["state"] == bs.CANCELLED
    assert "cancelled by the customer" in w.sent[OWNER][-1]
    assert "CANCELLED" in w.script["notes"][-1]


# ------------------------------------------------------------- arrival


async def test_arrival_sends_the_instructions_word_for_word_once(world):
    w = world
    await confirmed_booking(w, "2030-03-04T12:30")
    w.script["arrived"] = True
    w.script["reply"] = "one sec, coming"
    replies = w.script["calls"]
    await customer(w, "I'm at the door")
    assert w.sent[CUSTOMER].count("Door code 4321, 2nd floor.") == 1
    assert (await only_booking(w))["instructions_sent_at"] is not None
    # The way in is the whole answer: no reply is written to "I'm here".
    assert w.sent[CUSTOMER][-1] == "Door code 4321, 2nd floor." and w.script["calls"] == replies
    # A message after that is answered as usual; the way in is not sent again.
    await customer(w, "I'm here!!")
    assert w.sent[CUSTOMER].count("Door code 4321, 2nd floor.") == 1
    assert w.script["calls"] == replies + 1


async def test_she_meets_him_outside_and_the_booking_phone_hears_he_is_here(world, his_photos):
    w = world
    w.app.config["booking"].update(arrival_instructions="", name="Isabela")
    await confirmed_booking(w, "2030-03-04T12:30")
    w.script["arrived"] = True
    w.script["reply"] = "one sec love, I'm coming"
    await customer(w, "I'm here, by the green door")
    # His words go to the booking phone, once, and he is answered as her.
    assert w.sent[OWNER][-1].startswith("Isabela — #1 Anna (@anna)")
    assert "he's here — go and meet him.\nI'm here, by the green door" in w.sent[OWNER][-1]
    assert "coming out to meet them" in w.script["notes"][-1]
    assert w.sent[CUSTOMER][-1] == "one sec love, I'm coming"
    assert (await only_booking(w))["arrived_at"] is not None

    # What he sends after that goes on to her: words, and photos as photos.
    calls = w.script["calls"]
    await customer(w, "red jacket")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nred jacket"
    await customer(w, "here", photo=True)
    assert his_photos["files"] == [OWNER] and w.sent[OWNER][-1] == "📷 #1 Anna (@anna) sent a photo:\nhere"
    # No new arrival message, and no more arrival checks once she knows.
    assert sum("go and meet him" in m for m in w.sent[OWNER]) == 1
    assert w.script["calls"] == calls + 2


async def test_with_a_way_in_the_booking_phone_hears_it_was_sent(world):
    w = world
    await confirmed_booking(w, "2030-03-04T12:30")
    w.script["arrived"] = True
    await customer(w, "at the door")
    assert w.sent[CUSTOMER][-1] == "Door code 4321, 2nd floor."
    assert "he's here — the way in was sent to him.\nat the door" in w.sent[OWNER][-1]


async def test_the_arrival_message_to_the_owner_can_be_switched_off(world):
    w = world
    w.app.config["booking"].update(arrival_instructions="", arrival_to_owner=False)
    await confirmed_booking(w, "2030-03-04T12:30")
    sent = len(w.sent[OWNER])
    w.script["arrived"] = True
    await customer(w, "I'm here")
    await customer(w, "red jacket")
    assert len(w.sent[OWNER]) == sent


async def test_arrival_messages_are_deleted_for_both_sides_after_the_set_minutes(world, monkeypatch):
    w = world
    library = w.app.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "stairs.jpg").write_bytes(b"\xff\xd8\xff stairs")
    stairs = library.add_file("stairs.jpg", "the stairs to the flat")
    library.set_flags(stairs["id"], send_on_arrival=True, view_once=True)
    files, deleted = [], []
    file_ids = iter(range(9000, 9100))

    async def fake_deliver_file(peer, chat_id, item, path):
        files.append(item["id"])
        return SimpleNamespace(id=next(file_ids))

    async def fake_delete(peer, chat_id, ids):
        if w.script.get("fail_on") in ids:
            w.script["fail_on"] = None
            raise RuntimeError("network down")
        deleted.append((chat_id, list(ids)))

    monkeypatch.setattr(w.app, "deliver_file", fake_deliver_file)
    monkeypatch.setattr(w.app.transport, "delete_messages", fake_delete)
    w.app.config["booking"]["arrival_cleanup_minutes"] = 15

    # The way in is never something the AI can send.
    assert f"[send {stairs['id']}]" not in w.app.media_prompt()

    await confirmed_booking(w, "2030-03-04T12:30")
    w.script["arrived"] = True
    await customer(w, "I'm at the door")
    assert w.sent[CUSTOMER].count("Door code 4321, 2nd floor.") == 1 and files == [stairs["id"]]
    booking = await only_booking(w)
    assert len(booking["instructions_message_ids"]) == 2
    assert booking["instructions_cleanup_at"] == NOW + timedelta(minutes=15)

    w.clock["now"] = NOW + timedelta(minutes=10)
    await w.app.flow.tick()
    assert deleted == []

    # The photo fails after the text went: the next tick retries only the photo.
    w.script["fail_on"] = 9000
    w.clock["now"] = NOW + timedelta(minutes=16)
    await w.app.flow.tick()
    assert len(deleted) == 1 and (await only_booking(w))["instructions_cleaned_at"] is None
    await w.app.flow.tick()
    assert [chat for chat, _ in deleted] == [CUSTOMER, CUSTOMER]
    assert [len(ids) for _, ids in deleted] == [1, 1] and deleted[-1][1] == [9000]
    assert (await only_booking(w))["instructions_cleaned_at"] is not None

    # The rows keep only a placeholder: no door code for the AI to repeat.
    history = await w.db.get_messages(CUSTOMER)
    assert not any("4321" in m["text"] for m in history)
    assert sum(m["text"] == media.DELETED_PLACEHOLDER for m in history) == 2
    await w.app.flow.tick()
    assert len(deleted) == 2


async def test_without_a_cleanup_time_nothing_is_deleted(world, monkeypatch):
    w = world
    deleted = []

    async def fake_delete(peer, chat_id, ids):
        deleted.append(ids)

    monkeypatch.setattr(w.app.transport, "delete_messages", fake_delete)
    await confirmed_booking(w, "2030-03-04T12:30")
    w.script["arrived"] = True
    await customer(w, "I'm at the door")
    assert (await only_booking(w))["instructions_cleanup_at"] is None
    w.clock["now"] = NOW + timedelta(days=1)
    await w.app.flow.tick()
    assert deleted == []


@pytest_asyncio.fixture
async def photos(world, monkeypatch):
    w = world
    w.app.config["vision"].update(enabled=True, model="vision-model")
    w.app.config["booking"].update(arrival_photo_check=True, arrival_requires_photo=True)
    monkeypatch.setenv("VISION_API_URL", "https://vision.example/v1/chat/completions")
    monkeypatch.setenv("VISION_API_KEY", "vk")
    library = w.app.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "door.jpg").write_bytes(b"\xff\xd8\xff reference")
    library.add_file("door.jpg", "our door", role=media.ARRIVAL_REFERENCE)
    verdict = {"match": vision.PhotoMatch(True, 0.9)}

    async def fake_compare(photo, references, **kw):
        assert references == [b"\xff\xd8\xff reference"]
        return verdict["match"]

    async def fake_describe(image, **kw):
        return "a haircut picture"

    async def download_media(message, file=None):
        if file is bytes:
            return b"\xff\xd8\xff customer"
        with open(file, "wb") as fh:
            fh.write(b"\xff\xd8\xff owner")
        return file

    monkeypatch.setattr(vision, "compare_to_reference", fake_compare)
    monkeypatch.setattr(vision, "describe_photo", fake_describe)
    w.app.client = SimpleNamespace(download_media=download_media)
    return verdict


async def test_saying_im_here_waits_for_a_matching_photo(world, photos):
    w = world
    await confirmed_booking(w, "2030-03-04T12:30")
    w.script["arrived"] = True
    await customer(w, "I'm here")
    assert "Door code 4321, 2nd floor." not in w.sent[CUSTOMER]
    assert "send a photo of the entrance" in w.script["notes"][-1]

    await customer(w, "", photo=True)
    assert w.sent[CUSTOMER].count("Door code 4321, 2nd floor.") == 1
    assert (await only_booking(w))["arrival_photo_match"] is True
    assert "photo matches the entrance" in w.sent[OWNER][-1]


async def test_a_photo_of_the_wrong_door_gets_no_instructions(world, photos):
    w = world
    photos["match"] = vision.PhotoMatch(True, 0.4)  # below the configured 0.7
    await confirmed_booking(w, "2030-03-04T12:30")
    await customer(w, "", photo=True)
    assert "Door code 4321, 2nd floor." not in w.sent.get(CUSTOMER, [])
    assert (await only_booking(w))["arrival_photo_match"] is False
    assert "does not look like the entrance" in w.sent[OWNER][-1]
    [checked] = [e for e in await audit.list_events(w.app.pool, tenant_id=w.app.tenant_id)
                 if e["event"] == audit.ARRIVAL_PHOTO_CHECKED]
    assert checked["payload"] == {"booking": 1, "same_place": True, "confidence": 0.4}


async def test_other_photos_are_described_for_the_reply(world, photos):
    w = world
    await customer(w, "like this?", photo=True)
    history = await w.db.get_messages(CUSTOMER)
    assert any("[photo] a haircut picture" in m["text"] for m in history)


async def test_the_owner_sends_the_entrance_photo(world, photos):
    w = world
    await w.app.on_incoming(FakeEvent(OWNER, "door", photo=True, message_id=77))
    refs = w.app.media_library.references()
    assert len(refs) == 2 and refs[-1]["file"] == "entrance-77.jpg"
    assert "entrance reference photo" in w.sent[OWNER][-1]


# ---------------------------------------------------------- AI and reply limits


async def test_the_ai_stops_at_the_clients_token_limit(world, pg_pool):
    w = world
    w.app.config["limits"]["daily_tokens"] = 1000
    await customer(w, "hello", None)
    assert w.script["calls"] == 1  # this reply used 1,100 tokens
    await customer(w, "hello again", None)
    assert w.script["calls"] == 1
    events = [e for e in await audit.list_events(pg_pool, tenant_id=w.app.tenant_id)
              if e["event"] == audit.AI_LIMIT_REACHED]
    assert len(events) == 1 and "daily token limit" in events[0]["reason"]


async def test_the_monthly_spend_cap_is_enforced(world, pg_pool):
    w = world
    w.app.config["api_spend_cap_eur"] = 0.0001
    await customer(w, "hello", None)
    await customer(w, "hello again", None)
    assert w.script["calls"] == 1


async def test_reply_limits_and_acknowledgements(world, pg_pool):
    w = world
    w.app.config["replies"].update(max_messages_per_chat_per_hour=2, skip_acknowledgements=True)
    await customer(w, "hello", None)
    await customer(w, "thanks!", None)
    assert w.script["calls"] == 1
    await customer(w, "one more question", None)
    await customer(w, "and another", None)
    assert w.script["calls"] == 2
    skipped = [e["reason"] for e in await audit.list_events(pg_pool, tenant_id=w.app.tenant_id)
               if e["event"] == audit.REPLY_SKIPPED]
    assert "the message is only an acknowledgement" in skipped
    assert any("limit 2" in r for r in skipped)


async def test_booking_news_is_delivered_even_to_an_acknowledgement(world):
    w = world
    w.app.config["replies"]["skip_acknowledgements"] = True
    await customer(w, "tomorrow at 15?", book())
    # The confirmation arrives while the customer's reply is paused, and
    # their next message is just "ok": the news still has to go out.
    await w.db.set_paused(CUSTOMER, True)
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, False)
    calls = w.script["calls"]
    await customer(w, "ok", None)
    assert w.script["calls"] == calls + 1
    assert "has just been CONFIRMED" in w.script["notes"][-1]


async def test_the_no_reply_instruction_lets_the_model_stay_silent(world, pg_pool):
    w = world
    w.app.config["replies"]["no_reply_instruction"] = "Don't answer spam."
    w.script["reply"] = "[NO_REPLY]"
    await customer(w, "BUY CHEAP WATCHES", None)
    assert w.script["no_reply_instruction"] == "Don't answer spam."
    assert CUSTOMER not in w.sent
    assert any(e["event"] == audit.REPLY_SKIPPED for e in await audit.list_events(pg_pool, tenant_id=w.app.tenant_id))


async def test_the_owners_ordinary_message_gets_no_customer_reply(world):
    w = world
    # Her chat is for bookings: "hi" (read as about nothing) gets nothing,
    # a command its answer.
    w.script["owner_answer"] = reading()
    await owner(w, "hi, how is it going?")
    assert OWNER not in w.sent and w.script["calls"] == 0
    await owner(w, "LIST")
    assert w.sent[OWNER] == ["Nothing is waiting for your answer right now."]
    # Without the reading of her words, it is answered like anyone's.
    w.app.config["booking"]["owner_free_text"] = False
    await owner(w, "hi, how is it going?")
    assert w.sent[OWNER][-1] == "ok"


# ------------------------------------- nothing she writes is lost (review)


async def test_her_length_and_refusal_survive_until_her_plain_yes(world):
    w = world
    await customer(w, "tomorrow at 15 for 2 hours, with anal?", book(duration_minutes=120, extras="anal"))
    # "max 1 hour and no anal": not a yes yet, so she is asked — with what
    # she said repeated — and the refusal holds at once.
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=60, extras_no=["anal"]))
    await owner(w, "max 1 hour and no anal")
    assert w.sent[OWNER][-1] == ("#1 Anna, Tue 15:00: yes or no? Or give another time. With your answer: "
                                 "for 1 hour; no anal.")
    held = await only_booking(w)
    assert (held["state"], held["extras_no"]) == (bs.PENDING, "anal")
    # Her plain YES answers that question: it is read together with it.
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=60, extras_no=["anal"]))
    await owner(w, "YES 1")
    assert len(w.script["owner_calls"]) == 2
    b = await only_booking(w)
    assert (b["state"], b["ends_at"] - b["starts_at"], b["extras_no"]) == (bs.CONFIRMED, timedelta(hours=1), "anal")
    assert w.sent[OWNER][-1].endswith("I'll tell him: no anal.")
    # The question is answered: the next plain command costs no model call.
    await customer(w, "and next week?", book("2030-03-11T15:00"), chat=43)
    await owner(w, "YES 2")
    assert len(w.script["owner_calls"]) == 2


async def test_a_plain_yes_after_a_question_still_counts_when_her_words_cannot_be_read(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    w.script["owner_answer"] = reading(answer(None, "yes", tell_customer="bring cash"))
    await owner(w, "yes, tell him to bring cash")
    assert w.sent[OWNER][-1].startswith("Which one: #1 Anna, Tue 15:00, or #2 Anna, Tue 17:00?")
    assert w.sent[OWNER][-1].endswith("With your answer: tell him bring cash.")
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "YES 1")
    assert (await w.store.by_number(1))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1].split("\n")[-1] == bookings.READ_EARLIER_FAILED
    assert (await w.store.by_number(1))["booker_news"] == []


async def test_words_for_the_other_one_never_land_on_the_quoted_request(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    first = await w.store.by_number(1)
    w.script["owner_answer"] = reading(answer(1, "yes"), answer(None, "none", tell_customer="I'm 10 min late"))
    await owner(w, "yes. tell the other one I'm 10 min late", reply_to=first["provider_message_id"])
    assert (await w.store.by_number(1))["state"] == bs.CONFIRMED
    assert (await w.store.by_number(1))["booker_news"] == [] and (await w.store.by_number(2))["booker_news"] == []
    assert not any("10 min late" in note for note in w.script["notes"])
    assert w.sent[OWNER][-1].split("\n")[-1].startswith("Which one: #1 Anna")


async def test_a_cancel_with_words_passes_the_words_on(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    w.script["owner_answer"] = reading(answer(1, "cancel", tell_customer="sorry, I'm sick"))
    await owner(w, "CANCEL 1 tell him sorry I'm sick")
    b = await only_booking(w)
    assert b["state"] == bs.CANCELLED and len(w.script["owner_calls"]) == 1
    assert w.sent[OWNER][-1].endswith("I'll tell him: sorry, I'm sick.")
    assert 'tell them "sorry, I\'m sick"' in w.script["notes"][-1]


async def test_a_cancel_with_words_stands_when_they_cannot_be_read_and_she_is_told(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "CANCEL 1 tell him sorry I'm sick")
    assert (await only_booking(w))["state"] == bs.CANCELLED
    assert w.sent[OWNER][-1].split("\n")[-1] == bookings.READ_REST_FAILED


@pytest.mark.parametrize("text, confirmed", [("yes, see you", True), ("yes, no anal", False),
                                             ("ok, not today", False), ("claro que no", False),
                                             ("yes 1 at 18:00", False)])
async def test_when_her_words_cannot_be_read_a_yes_stands_only_when_nothing_can_turn_it(world, text, confirmed):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, text)
    state = (await only_booking(w))["state"]
    assert (state == bs.CONFIRMED) is confirmed
    if confirmed:
        assert w.sent[OWNER][-1].split("\n")[-1] == bookings.READ_REST_FAILED
    else:
        assert state == bs.PENDING and w.sent[OWNER][-1] == bookings.read_failed([1])


async def test_a_loose_no_without_its_number_never_declines_unread(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "no problem babe")
    assert (await only_booking(w))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == bookings.read_failed([1])


async def test_a_loose_yes_or_no_read_as_nothing_gets_her_a_question_while_a_request_waits(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading()
    await owner(w, "no problem babe")
    assert (await only_booking(w))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == "#1 Anna, Tue 15:00: yes or no? Or give another time."


async def test_a_number_she_typed_that_the_reading_misses_is_asked_about(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 17?", book("2030-03-05T17:00"), chat=43)
    w.script["owner_answer"] = reading(answer(2, "yes", tell_customer="bring cash"))
    await owner(w, "YES 1 and tell him to bring cash")
    assert (await w.store.by_number(1))["state"] == bs.PENDING
    assert (await w.store.by_number(2))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == "#1 Anna, Tue 15:00: yes or no? Or give another time."


async def test_the_models_other_word_for_yes_still_confirms(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = {"answers": [{"number": 1, "action": "confirm"}], "standing": None}
    await owner(w, "ok thanks, confirm")
    assert (await only_booking(w))["state"] == bs.CONFIRMED


async def test_his_entrance_photo_while_arriving_goes_to_the_arrival_check_not_to_her(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "yes", ask_customer="is he coming alone"))
    await owner(w, "yes, ask him if he comes alone")
    to_her = []

    async def passed(booking, photo, caption, *, proof=False):
        to_her.append(booking["number"])
        return True

    monkeypatch.setattr(w.app.flow, "pass_on_photo", passed)
    booking_id = (await only_booking(w))["id"]
    # Three hours before: his photo is his answer to her.
    w.clock["now"] = datetime(2030, 3, 5, 12, 0, tzinfo=RIGA)
    await w.store.set_fields(booking_id, booker_asked_at=w.clock["now"])
    assert await w.app.flow.pass_on(CUSTOMER, "me", photo=object()) == "[photo]"
    assert to_her == [1]
    # At the door with no photo check set up: still his answer to her.
    w.clock["now"] = datetime(2030, 3, 5, 14, 55, tzinfo=RIGA)
    await w.store.set_fields(booking_id, booker_asked_at=w.clock["now"])
    assert await w.app.flow.pass_on(CUSTOMER, "", photo=object()) == "[photo]"
    assert to_her == [1, 1]
    # With the check set up, it is the entrance photo for the way in.
    monkeypatch.setattr(w.app.flow, "photo_check_ready", lambda: True)
    assert await w.app.flow.pass_on(CUSTOMER, "", photo=object()) is None
    assert to_her == [1, 1]


async def test_a_photo_that_did_not_reach_her_is_not_stored_as_passed_on(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="send a photo"))
    await owner(w, "ask him for a photo first")

    async def failed(booking, photo, caption, *, proof=False):
        return False

    monkeypatch.setattr(w.app.flow, "pass_on_photo", failed)
    assert await w.app.flow.pass_on(CUSTOMER, "", photo=object()) is None


async def test_a_panel_decision_ends_her_question_to_him(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="is he coming alone"))
    await owner(w, "ask him if he comes alone")
    b = await only_booking(w)
    assert b["booker_asked_at"] is not None
    await w.app.flow.admin_action(b["id"], "confirm", {})
    assert (await only_booking(w))["booker_asked_at"] is None


# ------------------------------------------- round 2: what she quotes, says, means


async def _asked_about_length(w):
    """#1 (chat 42) confirmed, #2 (chat 43) waiting; she asked #1 about 2
    hours and his answer came to her. Returns the id of that message."""
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await customer(w, "and me at 18?", book("2030-03-05T18:00"), chat=43)
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="does he want 2 hours"))
    await owner(w, "ask the 15:00 guy if he wants 2 hours")
    await customer(w, "yes 2 hours please")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nyes 2 hours please"
    return next(m for m in reversed(await w.db.get_messages(OWNER)) if m["direction"] == "out")


async def test_ok_to_his_answer_she_quoted_is_about_his_booking_never_another_request(world):
    w = world
    his_answer = await _asked_about_length(w)
    calls = len(w.script["owner_calls"])
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=120))
    await owner(w, "ok", reply_to=his_answer["telegram_id"])
    # Read with the model (not a strict yes to the only waiting request).
    assert len(w.script["owner_calls"]) == calls + 1
    assert w.script["owner_calls"][-1]["replying_to"] == 1
    assert (await w.store.by_number(2))["state"] == bs.PENDING
    first = await w.store.by_number(1)
    assert first["ends_at"] - first["starts_at"] == timedelta(hours=2)


async def test_a_quoted_ok_never_confirms_another_request_when_her_words_cannot_be_read(world):
    w = world
    his_answer = await _asked_about_length(w)
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "ok", reply_to=his_answer["telegram_id"])
    assert (await w.store.by_number(2))["state"] == bs.PENDING


async def test_a_bare_ok_right_after_his_answer_is_read_not_taken_for_the_waiting_request(world):
    w = world
    await _asked_about_length(w)
    calls = len(w.script["owner_calls"])
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=120))
    await owner(w, "ok")
    assert len(w.script["owner_calls"]) == calls + 1
    assert (await w.store.by_number(2))["state"] == bs.PENDING


async def test_a_bare_yes_right_after_the_request_costs_no_model_call(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "yes")
    assert w.script["owner_calls"] == [] and (await only_booking(w))["state"] == bs.CONFIRMED


async def test_a_length_after_yes_is_her_length_not_booking_number_two(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 18?", book("2030-03-05T18:00"), chat=43)
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=120))
    await owner(w, "yes 2 hours", reply_to=(await w.store.by_number(1))["provider_message_id"])
    first, second = await w.store.by_number(1), await w.store.by_number(2)
    assert first["state"] == bs.CONFIRMED and first["ends_at"] - first["starts_at"] == timedelta(hours=2)
    assert second["state"] == bs.PENDING


@pytest.mark.parametrize("text", ["yes at 9", "ok 9pm", "yes but at 21", "ok, half an hour later",
                                  "да, через полчаса"])
async def test_model_down_a_yes_naming_another_time_never_confirms_his(world, text):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, text)
    assert (await only_booking(w))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == bookings.read_failed([1])


async def test_model_down_a_no_with_another_time_after_it_never_declines(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "NO 1 tomorrow at 18:00 instead")
    assert (await only_booking(w))["state"] == bs.PENDING
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "NO 1 sorry not today")
    assert (await only_booking(w))["state"] == bs.CANCELLED


async def test_the_model_answering_nonsense_never_lets_a_loose_yes_stand(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = None  # an answer that can't be used
    await owner(w, "ok see you")
    assert (await only_booking(w))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == bookings.read_failed([1])


async def test_model_down_with_nothing_open_she_is_told_nothing_was_done(world):
    w = world
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "from now on cash only")
    assert w.sent[OWNER] == [bookings.READ_NOTHING_FAILED]


async def test_a_slow_model_never_holds_her_messages_past_the_budget(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    monkeypatch.setattr(booking_flow, "OWNER_READ_BUDGET", 0.05)
    never = asyncio.Event()

    async def stuck(**kw):
        w.script["owner_calls"].append(kw)
        await never.wait()

    monkeypatch.setattr(ai_responder, "read_owner_answer", stuck)
    await owner(w, "ok see you")
    # Read as "down": the loose yes stands, and she is told the rest did nothing.
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1].split("\n")[-1] == bookings.READ_REST_FAILED


async def test_her_later_messages_are_not_in_the_reading_of_an_earlier_one(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    first = await w.db.record_message(OWNER, "in", "received", "fine by me")
    await w.db.record_message(OWNER, "in", "received", "and tell him to bring cash")
    history = await w.db.get_history_for_ai(OWNER, limit=30, upto_id=first["id"])
    assert [m["content"] for m in history if m["role"] == "user"][-1] == "fine by me"
    # The bot's own lines after it stay.
    await w.db.record_message(OWNER, "out", "sent", "#1 confirmed")
    history = await w.db.get_history_for_ai(OWNER, limit=30, upto_id=first["id"])
    assert history[-1] == {"role": "assistant", "content": "#1 confirmed"}


async def test_her_question_after_he_cancelled_still_brings_his_answer(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await customer(w, "sorry I cancel", {"intent": "cancel"})
    assert (await only_booking(w))["state"] == bs.CANCELLED
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="why did you cancel"))
    await owner(w, "ask him why he cancelled")
    assert w.sent[OWNER][-1] == "#1 Anna: I'll ask him: why did you cancel? His answer will come here."
    await customer(w, "my wife came home")
    assert w.sent[OWNER][-1] == "💬 #1 Anna (@anna):\nmy wife came home"


async def test_her_words_no_reply_carried_in_time_are_dropped_and_she_is_told(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, True)
    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="I'm in the shower, 10 min"))
    await owner(w, "tell him I'm in the shower, 10 min")
    assert w.sent[OWNER][-1].endswith(bookings.WORDS_WAIT)
    # A day later, never told: dropped, and she hears which words.
    w.clock["now"] = NOW + timedelta(hours=25)
    await w.app.flow.tick()
    assert (await only_booking(w))["booker_news"] == []
    assert w.sent[OWNER][-1] == ('#1 Anna: this did not reach him in time and was dropped: "I\'m in the shower, '
                                 '10 min". Tell him yourself if it still matters.')
    await w.db.set_paused(CUSTOMER, False)
    await customer(w, "hello?")
    assert "shower" not in w.script["notes"][-1]


async def test_the_same_words_twice_go_to_him_once(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, True)
    for _ in range(2):
        w.script["owner_answer"] = reading(answer(1, "none", tell_customer="bring cash"))
        await owner(w, "tell him to bring cash")
    assert len((await only_booking(w))["booker_news"]) == 1


async def test_a_question_stays_open_through_small_talk_until_she_answers_it(world):
    w = world
    await customer(w, "tomorrow at 15 for 2 hours?", book(duration_minutes=120))
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=60, tell_customer="bring cash"))
    await owner(w, "max 1 hour, and tell him to bring cash")
    w.script["owner_answer"] = reading()
    await owner(w, "brb")
    # Her plain YES 1 still answers the question: read with what she said.
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=60, tell_customer="bring cash"))
    await owner(w, "YES 1")
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["ends_at"] - b["starts_at"] == timedelta(hours=1)
    assert w.sent[OWNER][-1].endswith("I'll tell him: bring cash.")


async def test_her_typed_no_answering_the_question_never_becomes_a_yes(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 18?", book("2030-03-05T18:00"), chat=43)
    w.script["owner_answer"] = reading(answer(None, "yes", tell_customer="bring cash"))
    await owner(w, "yes, tell him to bring cash")
    assert w.sent[OWNER][-1].startswith("Which one:")
    # The reading (with her earlier yes) says yes #2; she typed NO 2.
    w.script["owner_answer"] = reading(answer(2, "yes", tell_customer="bring cash"))
    await owner(w, "NO 2")
    assert (await w.store.by_number(2))["state"] == bs.CANCELLED
    assert (await w.store.by_number(2))["booker_news"] == []
    assert w.sent[OWNER][-1].split("\n")[-1] == bookings.EARLIER_NOT_APPLIED


async def test_the_yes_the_bot_offers_after_a_length_that_is_not_free_is_taken_as_typed(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 16?", book("2030-03-05T16:00"), chat=43)
    await owner(w, "YES 2")
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=120))
    await owner(w, "yes, 2 hours", reply_to=(await w.store.by_number(1))["provider_message_id"])
    assert "Nothing confirmed; YES 1 takes it as he asked" in w.sent[OWNER][-1]
    calls = len(w.script["owner_calls"])
    await owner(w, "YES 1")
    assert len(w.script["owner_calls"]) == calls  # not read again: no loop
    assert (await w.store.by_number(1))["state"] == bs.CONFIRMED


async def test_a_length_change_keeps_the_confirmation_he_has_not_heard_yet(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await w.db.set_paused(CUSTOMER, True)
    await owner(w, "YES 1")
    assert (await only_booking(w))["customer_notice"] == bs.NOTICE_CONFIRMED
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=120))
    await owner(w, "make it 2 hours")
    b = await only_booking(w)
    assert b["customer_notice"] == bs.NOTICE_CONFIRMED and b["ends_at"] - b["starts_at"] == timedelta(hours=2)


async def test_her_words_go_even_when_the_longer_length_is_not_free(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await customer(w, "and me at 16?", book("2030-03-05T16:00"), chat=43)
    await owner(w, "YES 2")
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=120, tell_customer="bring cash"))
    await owner(w, "make it 2 hours, and tell him to bring cash")
    line = w.sent[OWNER][-1]
    assert "is not possible" in line and line.endswith("I'll tell him: bring cash.")
    assert 'tell them "bring cash"' in w.script["notes"][-1]
    assert (await w.store.by_number(1))["ends_at"].astimezone(RIGA).hour == 16


async def test_a_longer_length_into_another_bookings_buffer_is_refused_by_the_check(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    first = await only_booking(w)
    await w.store.set_fields(first["id"], blocked_until=first["ends_at"] + timedelta(minutes=15))
    await customer(w, "and me at 16:20?", book("2030-03-05T16:20"), chat=43)
    await owner(w, "YES 2")
    # 15:00-16:10 plus its 15 min buffer runs into 16:20.
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=70))
    await owner(w, "make it 70 min")
    assert "is not possible (conflict)" in w.sent[OWNER][-1]


async def test_a_notice_set_while_his_reply_went_out_is_not_cleared_by_it(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    b = await only_booking(w)
    await w.store.set_fields(b["id"], customer_notice=bs.NOTICE_CONFIRMED)
    told = {key: b.get(key) for key in ("starts_at", "ends_at", "proposed_starts_at", "proposed_ends_at")}
    await w.store.clear_notice(b["id"], {**told, "customer_notice": bs.NOTICE_REQUESTED})
    assert (await only_booking(w))["customer_notice"] == bs.NOTICE_CONFIRMED
    # The same notice, but about other times than the reply told ("make it
    # 2 hours" while "it's booked" went out): kept for the next reply.
    await w.store.set_fields(b["id"], ends_at=b["ends_at"] + timedelta(hours=1),
                             blocked_until=b["blocked_until"] + timedelta(hours=1))
    await w.store.clear_notice(b["id"], {**told, "customer_notice": bs.NOTICE_CONFIRMED})
    assert (await only_booking(w))["customer_notice"] == bs.NOTICE_CONFIRMED
    await w.store.clear_notice(b["id"], {**told, "customer_notice": bs.NOTICE_CONFIRMED,
                                         "ends_at": b["ends_at"] + timedelta(hours=1)})
    assert (await only_booking(w))["customer_notice"] is None


async def test_a_new_reply_waits_for_one_still_going_out(world):
    w = world
    w.app.begin_sending(CUSTOMER)
    await w.app.on_incoming(FakeEvent(CUSTOMER, "hi"))
    tasks = [t for t in w.app.draft_tasks.values() if not t.done()]
    done, _ = await asyncio.wait(tasks, timeout=0.2)
    assert not done and CUSTOMER not in w.sent
    w.app.end_sending(CUSTOMER)
    await settle(w.app)
    assert w.sent[CUSTOMER] == ["ok"]


async def test_ready_with_a_length_does_both(world):
    w = world
    await confirmed_outcall(w)
    w.script["owner_answer"] = reading(answer(1, "ready", duration_minutes=120))
    await owner(w, "ready, and make it 2 hours")
    b = await only_booking(w)
    assert b["ready_at"] is not None and b["ends_at"] - b["starts_at"] == timedelta(hours=2)


# ------------------------------------------- round 3: the second review


async def _two_waiting(w):
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 18?", book("2030-03-05T18:00"), chat=43)


async def test_ok_after_a_panel_notice_never_confirms_the_waiting_request(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 18?", book("2030-03-05T18:00"), chat=43)
    await w.app.flow.admin_action((await w.store.by_number(1))["id"], "confirm", {})
    assert "(from the panel)" in w.sent[OWNER][-1] or "confirmed" in w.sent[OWNER][-1]
    calls = len(w.script["owner_calls"])
    w.script["owner_answer"] = reading()
    await owner(w, "ok")
    # Read (the last message was not #2's request), about nothing: asked.
    assert len(w.script["owner_calls"]) == calls + 1
    assert (await w.store.by_number(2))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == "#2 Anna, Tue 18:00: yes or no? Or give another time."


async def test_after_a_restart_a_bare_ok_after_his_answer_is_read_not_guessed(world):
    w = world
    await _asked_about_length(w)

    async def owner_chat():
        return OWNER

    w.app.flow = booking_flow.BookingFlow(w.app)
    w.app.flow.provider_chat_id = owner_chat
    w.app.flow.calendar_client = lambda: None
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "ok")
    # Model down and its booking in doubt: nothing is put on #2.
    assert (await w.store.by_number(2))["state"] == bs.PENDING
    assert w.sent[OWNER][-1] == bookings.read_failed([2])


async def test_the_yes_n_the_bot_offers_after_a_held_question_is_not_read_again(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 16?", book("2030-03-05T16:00"), chat=43)
    w.script["owner_answer"] = reading(answer(None, "yes", duration_minutes=120))
    await owner(w, "yes, 2 hours")
    assert w.sent[OWNER][-1].startswith("Which one:")
    w.script["owner_answer"] = reading(answer(1, "yes", duration_minutes=120))
    await owner(w, "YES 1")
    assert "Nothing confirmed; YES 1 takes it as he asked" in w.sent[OWNER][-1]
    calls = len(w.script["owner_calls"])
    await owner(w, "YES 1")
    assert len(w.script["owner_calls"]) == calls
    first = await w.store.by_number(1)
    assert first["state"] == bs.CONFIRMED and first["ends_at"] - first["starts_at"] == timedelta(hours=1)


async def test_two_questions_in_one_answer_close_one_by_one(world):
    w = world
    flow = w.app.flow
    flow._held = {1: booking_flow._Held(NOW, 9, (1,)), 2: booking_flow._Held(NOW, 9, (2,))}
    flow._close_held(1)
    assert set(flow._held) == {2}
    flow._held = {1: booking_flow._Held(NOW, 9, (1, 2)), 2: booking_flow._Held(NOW, 9, (1, 2))}
    flow._close_held(1)
    assert flow._held == {}


async def test_ok_thanks_to_the_bots_confirmation_gets_no_reply(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    ack = next(m for m in reversed(await w.db.get_messages(OWNER)) if m["direction"] == "out")
    sent = len(w.sent[OWNER])
    w.script["owner_answer"] = reading()
    await owner(w, "ok thanks", reply_to=ack["telegram_id"])
    assert len(w.sent[OWNER]) == sent


async def test_with_commands_only_a_quote_changes_nothing_of_the_old_behaviour(world):
    w = world
    w.app.config["booking"]["owner_free_text"] = False
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    ack = next(m for m in reversed(await w.db.get_messages(OWNER)) if m["direction"] == "out")
    await owner(w, "CANCEL", reply_to=ack["telegram_id"])
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1].startswith("Which booking?")


async def test_her_words_held_by_a_question_she_answers_too_late_are_flagged(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await customer(w, "and me at 18?", book("2030-03-05T18:00"), chat=43)
    w.script["owner_answer"] = reading(answer(None, "yes", tell_customer="bring cash"))
    await owner(w, "yes, tell him to bring cash")
    w.clock["now"] = NOW + timedelta(minutes=31)
    await owner(w, "YES 1")
    assert (await w.store.by_number(1))["state"] == bs.CONFIRMED
    assert w.sent[OWNER][-1].split("\n")[-1] == bookings.format_held_lost(
        1, bookings.OwnerCommand("yes", tell="bring cash"))


async def test_asked_again_her_held_words_are_repeated_and_kept(world):
    w = world
    await customer(w, "tomorrow at 15 for 2 hours, with anal?", book(duration_minutes=120, extras="anal"))
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=60, extras_no=["anal"]))
    await owner(w, "max 1 hour and no anal")
    w.script["owner_answer"] = reading()
    await owner(w, "YES")
    assert w.sent[OWNER][-1] == ("#1 Anna, Tue 15:00: yes or no? Or give another time. With your answer: "
                                 "for 1 hour; no anal.")


async def test_a_length_change_while_his_taxi_step_is_untold_also_tells_the_new_end(world):
    w = world
    await confirmed_outcall(w)
    await w.db.set_paused(CUSTOMER, True)
    await owner(w, "READY 1")
    assert (await only_booking(w))["customer_notice"] == bs.NOTICE_READY
    w.script["owner_answer"] = reading(answer(1, "none", duration_minutes=120))
    await owner(w, "make it 2 hours")
    b = await only_booking(w)
    assert b["customer_notice"] == bs.NOTICE_READY
    assert b["booker_news"][-1]["tell"].startswith("it is now Mon 04 Mar 2030, 13:00–15:00 (2 hours)")


async def test_old_words_are_never_dropped_while_sending_is_off(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, True)
    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="bring cash"))
    await owner(w, "tell him to bring cash")
    w.clock["now"] = NOW + timedelta(hours=25)
    monkeypatch.setattr(w.app, "paused", lambda: True)
    await w.app.flow.tick()
    assert len((await only_booking(w))["booker_news"]) == 1
    monkeypatch.setattr(w.app, "paused", lambda: False)
    await w.app.flow.tick()
    assert (await only_booking(w))["booker_news"] == []
    assert "did not reach him in time" in w.sent[OWNER][-1]


async def test_telling_her_old_words_are_dropped_is_retried_not_repeated_every_tick(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, True)
    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="bring cash"))
    await owner(w, "tell him to bring cash")
    errors = []

    async def push_error(chat_id, text):
        errors.append(text)

    async def unreachable(text, *, reason):
        return None

    reachable = w.app.flow.send_owner
    monkeypatch.setattr(w.app, "push_error", push_error)
    monkeypatch.setattr(w.app.flow, "send_owner", unreachable)
    w.clock["now"] = NOW + timedelta(hours=25)
    await w.app.flow.tick()
    w.clock["now"] = NOW + timedelta(hours=25, minutes=1)
    await w.app.flow.tick()
    # Her chat out of reach: kept, and the admin told once.
    assert len((await only_booking(w))["booker_news"]) == 1
    assert len(errors) == 1 and "could not reach the owner" in errors[0]
    w.clock["now"] = NOW + timedelta(hours=25, minutes=31)
    await w.app.flow.tick()
    assert len((await only_booking(w))["booker_news"]) == 1 and len(errors) == 1
    monkeypatch.setattr(w.app.flow, "send_owner", reachable)
    w.clock["now"] = NOW + timedelta(hours=26, minutes=2)
    await w.app.flow.tick()
    # Reachable again: told, then dropped.
    assert (await only_booking(w))["booker_news"] == []
    assert "did not reach him in time" in w.sent[OWNER][-1]


async def test_old_words_she_cant_be_told_about_for_a_day_are_dropped_anyway(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, True)
    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="bring cash"))
    await owner(w, "tell him to bring cash")

    async def unreachable(text, *, reason):
        return None

    monkeypatch.setattr(w.app.flow, "send_owner", unreachable)
    w.clock["now"] = NOW + timedelta(hours=25)
    await w.app.flow.tick()
    assert len((await only_booking(w))["booker_news"]) == 1
    w.clock["now"] = NOW + timedelta(hours=49, minutes=1)
    await w.app.flow.tick()
    assert (await only_booking(w))["booker_news"] == []
    assert w.app.flow._drop_failed == {}


async def test_her_words_during_quiet_hours_say_when_they_go(world, monkeypatch):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    monkeypatch.setattr(w.app, "quiet_seconds_left", lambda *a, **k: 3600)
    w.script["owner_answer"] = reading(answer(1, "none", tell_customer="bring cash"))
    await owner(w, "tell him to bring cash")
    assert w.sent[OWNER][-1].endswith("His replies wait for the quiet hours to end (about 13:00), so this goes to "
                                      "him then.")


async def test_words_with_a_decline_are_kept_past_the_time_he_had_asked_for(world):
    w = world
    await customer(w, "today at 13?", book("2030-03-04T13:00"))
    await w.db.set_paused(CUSTOMER, True)
    w.script["owner_answer"] = reading(answer(1, "no", tell_customer="I'm away this week, next week is fine"))
    await owner(w, "no, tell him I'm away this week, next week is fine")
    assert (await only_booking(w))["state"] == bs.CANCELLED
    w.clock["now"] = datetime(2030, 3, 4, 15, 0, tzinfo=RIGA)  # past 13:00-14:00 + 30 min
    await w.app.flow.tick()
    assert len((await only_booking(w))["booker_news"]) == 1


async def test_a_stale_question_dropped_keeps_her_newer_one_open(world):
    w = world
    await customer(w, "tomorrow at 15?", book("2030-03-06T15:00"))
    await owner(w, "YES 1")
    await w.db.set_paused(CUSTOMER, True)
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="does he want 2 hours"))
    await owner(w, "ask him if he wants 2 hours")
    w.clock["now"] = NOW + timedelta(hours=23)
    w.script["owner_answer"] = reading(answer(1, "none", ask_customer="is he coming by car"))
    await owner(w, "ask him if he comes by car")
    w.clock["now"] = NOW + timedelta(hours=24, minutes=1)
    await w.app.flow.tick()
    b = await only_booking(w)
    assert [i["ask"] for i in b["booker_news"]] == ["is he coming by car"]
    assert b["booker_asked_at"] == NOW + timedelta(hours=23)


async def test_a_draft_only_waiting_for_a_send_is_replaced_not_doubled(world):
    w = world
    w.app.begin_sending(CUSTOMER)
    await w.app.on_incoming(FakeEvent(CUSTOMER, "hi"))
    first = w.app.draft_tasks[CUSTOMER]
    await w.app.on_incoming(FakeEvent(CUSTOMER, "you there?"))
    await asyncio.wait([first], timeout=0.5)
    assert first.cancelled()
    w.app.end_sending(CUSTOMER)
    await settle(w.app)
    assert w.sent[CUSTOMER] == ["ok"]


async def test_model_down_with_only_finished_bookings_she_is_told_nothing_was_done(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "NO 1")
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "tell him sorry")
    assert w.sent[OWNER][-1] == bookings.READ_NOTHING_FAILED


# ------------------------------------------------- a group as the booker

GROUP = -1001234567890
OTHER_GROUP = -1009999999999


class GroupEvent(FakeEvent):
    """A message in a Telegram group the account is in, by `sender`."""

    def __init__(self, text, reply_to=None, chat_id=GROUP, sender="Lina", outgoing=False):
        super().__init__(chat_id, text, reply_to)
        self.is_private = False
        self.sender_id = 77
        self.sender = sender
        self.outgoing = outgoing

    async def get_sender(self):
        return SimpleNamespace(id=77, first_name=self.sender, last_name=None, username=None, bot=False,
                               access_hash=2)

    async def get_chat(self):
        return SimpleNamespace(id=self.chat_id, title="Riga bookings", username=None, access_hash=555)


@pytest_asyncio.fixture
async def group_world(world, monkeypatch):
    async def booker_group():
        return GROUP

    monkeypatch.setattr(world.app.flow, "provider_chat_id", booker_group)
    world.app.config["booking"]["provider"] = "https://t.me/+RigaBookings"
    return world


async def in_group(w, text, reply_to=None, chat_id=GROUP, **event):
    await w.app.on_incoming(GroupEvent(text, reply_to, chat_id, **event))
    await settle(w.app)


async def group_texts(w, chat_id=GROUP):
    return [m["text"] for m in await w.db.get_messages(chat_id)]


async def test_a_group_as_the_booker_gets_the_request_and_anyone_in_it_answers(group_world):
    w = group_world
    await customer(w, "tomorrow at 15?", book())
    [request] = w.sent[GROUP]
    assert "Booking request #1" in request and "YES 1" in request
    assert OWNER not in w.sent

    await in_group(w, "YES 1", sender="Lina")
    b = await only_booking(w)
    assert b["state"] == bs.CONFIRMED and b["decided_by"] == bs.OWNER
    assert w.sent[GROUP][-1].startswith("#1 confirmed")
    assert "has just been CONFIRMED" in w.script["notes"][-1]
    # The group's thread says who wrote what.
    assert "Lina: YES 1" in await group_texts(w)
    assert (await w.db.get_conversation(GROUP))["display_name"] == "Riga bookings"


async def test_a_member_answers_in_her_own_words_by_quoting_the_request(group_world):
    w = group_world
    await customer(w, "tomorrow at 15?", book())
    request_id = (await w.store.by_number(1))["provider_message_id"]
    w.script["owner_answer"] = reading(answer(1, "yes", tell_customer="bring cash"))
    await in_group(w, "ok, he should bring cash", reply_to=request_id, sender="Driver Andris")
    assert (await only_booking(w))["state"] == bs.CONFIRMED
    [call] = w.script["owner_calls"]
    assert call["latest"] == "ok, he should bring cash"
    assert "Driver Andris: ok, he should bring cash" in await group_texts(w)


async def test_talk_in_the_booker_group_gets_no_reply_and_other_groups_are_never_read(group_world):
    w = group_world
    await customer(w, "tomorrow at 15?", book())
    sent, calls = len(w.sent[GROUP]), w.script["calls"]
    # Another group the account is in: not read, not stored, even a YES.
    await in_group(w, "YES 1", chat_id=OTHER_GROUP)
    assert (await only_booking(w))["state"] == bs.PENDING
    assert await w.db.get_messages(OTHER_GROUP) == [] and OTHER_GROUP not in w.sent
    # Talk in the booker's group about no booking: read, and nothing said back.
    w.script["owner_answer"] = reading()
    await in_group(w, "who drives tonight?")
    assert len(w.sent[GROUP]) == sent and w.script["calls"] == calls
    # Without the reading of her words a private booker chat gets an ordinary
    # reply; a group never does.
    w.app.config["booking"]["owner_free_text"] = False
    await in_group(w, "who drives tonight?")
    assert len(w.sent[GROUP]) == sent and w.script["calls"] == calls
    assert (await only_booking(w))["state"] == bs.PENDING


async def test_a_group_that_is_no_longer_the_booker_is_not_read(group_world, monkeypatch):
    w = group_world
    await customer(w, "tomorrow at 15?", book())

    async def person():
        return OWNER

    monkeypatch.setattr(w.app.flow, "provider_chat_id", person)
    await in_group(w, "YES 1")
    assert (await only_booking(w))["state"] == bs.PENDING
    assert "Lina: YES 1" not in await group_texts(w)


async def test_typed_by_hand_in_the_booker_group_is_kept_and_takes_nothing_over(group_world):
    w = group_world
    await customer(w, "tomorrow at 15?", book())
    await w.app.on_outgoing(GroupEvent("I'll take #1", outgoing=True))
    await settle(w.app)
    assert "I'll take #1" in await group_texts(w)
    assert w.app.takeover_until(await w.db.get_conversation(GROUP)) is None
    # The same in another group: nothing stored.
    await w.app.on_outgoing(GroupEvent("hi all", chat_id=OTHER_GROUP, outgoing=True))
    assert await w.db.get_messages(OTHER_GROUP) == []
