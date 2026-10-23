"""Bookings end to end through a running account: the customer asks, the
owner answers by text, the customer hears through the ordinary reply, and
the scheduler's tick sends reminders and lets unanswered requests lapse.

The model is scripted (what it extracts and replies); Telegram sends are
captured. Messages enter through SessionRuntime.on_incoming like real ones.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

import ai_responder
import audit
import booking_states as bs
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
        return answer

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
    # READY is then an ordinary message to the account (and gets a reply).
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
    return {"action": action, "number": number, "start": start, "in_minutes": in_minutes}


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
    assert "#1 Anna: the customer asks for Tue 05 Mar 2030, 15:00–16:00" in call["state_note"]
    assert "extras: domination +50" in call["state_note"]
    # The model sees her chat, the request in it.
    assert any("Booking request #1" in m["content"] for m in call["history"])


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


async def test_unclear_words_get_the_commands_back(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = said("unclear")
    await owner(w, "hmm maybe, who is he?")
    assert w.sent[OWNER][-1].startswith("#1: I did not get that. Reply YES 1 or NO 1")
    assert (await only_booking(w))["state"] == bs.PENDING


async def test_words_not_about_a_booking_get_an_ordinary_reply(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    calls = w.script["calls"]
    await owner(w, "how are you?")
    assert len(w.script["owner_calls"]) == 1 and w.script["calls"] == calls + 1
    assert (await only_booking(w))["state"] == bs.PENDING


async def test_no_model_call_for_a_command_or_when_nothing_is_open(world):
    w = world
    await owner(w, "hi there")
    assert w.script["owner_calls"] == []
    await customer(w, "tomorrow at 15?", book())
    await owner(w, "YES 1")
    assert w.script["owner_calls"] == []
    assert (await only_booking(w))["state"] == bs.CONFIRMED


async def test_a_yes_with_words_stands_when_the_model_cannot_be_asked(world):
    w = world
    await customer(w, "tomorrow at 15?", book())
    w.script["owner_answer"] = ai_responder.AIResponderError("DeepSeek is down")
    await owner(w, "ok see you")
    assert len(w.script["owner_calls"]) == 1
    assert (await only_booking(w))["state"] == bs.CONFIRMED


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
    await owner(w, "I'm ready in 20 minutes")
    assert w.script["owner_calls"] == []
    assert (await only_booking(w))["proposed_by"] is None


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


async def test_the_owners_ordinary_message_gets_an_ordinary_reply(world):
    w = world
    await owner(w, "hi, how is it going?")
    assert w.sent[OWNER] == ["ok"]
    await owner(w, "LIST")
    assert w.sent[OWNER][-1] == "Nothing is waiting for your answer right now."
