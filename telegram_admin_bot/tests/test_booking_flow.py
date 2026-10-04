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
    script = {"extract": None, "arrived": False, "reply": "ok", "notes": [], "calls": 0, "extract_calls": 0}
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

    async def fake_reply(**kw):
        script["calls"] += 1
        script["notes"].append(kw.get("booking_note", ""))
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
    await customer(w, "I'm at the door")
    assert w.sent[CUSTOMER].count("Door code 4321, 2nd floor.") == 1
    assert (await only_booking(w))["instructions_sent_at"] is not None
    await customer(w, "I'm here!!")
    assert w.sent[CUSTOMER].count("Door code 4321, 2nd floor.") == 1


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
