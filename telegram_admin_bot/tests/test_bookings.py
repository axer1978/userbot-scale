"""Appointments: a client agrees a time, the provider says yes or no.

The extraction itself is the model's job and is stubbed here; what is tested
is everything around it — that a found time becomes a request to the
provider, that the provider's reply is read correctly and tied to the right
booking, that the client is told through the ordinary reply pipeline, and
that a request the client changes their mind about is withdrawn.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import ai_responder
import bookings
import session_runtime
from database import DIR_SYSTEM, STATUS_NOTE, STATUS_PENDING

TZ = "Europe/Madrid"


def booking(store, chat_id=7, start="2030-03-04T15:00", **extra):
    fields = bookings.build_booking(
        {"booked": True, "start": start}, tz_name=TZ, default_duration=60,
        now=datetime(2030, 3, 1, tzinfo=ZoneInfo(TZ)),
    )
    return store.add(chat_id=chat_id, client_name="Anna", client_username="anna",
                     **{**fields, **extra})


# ------------------------------------------------------ the extraction

def test_only_a_positive_answer_with_a_time_counts():
    assert bookings.parse_extraction('{"booked": false}') is None
    assert bookings.parse_extraction('{"booked": true}') is None
    assert bookings.parse_extraction("no json here") is None
    found = bookings.parse_extraction('```json\n{"booked": true, "start": "2030-03-04T15:00"}\n```')
    assert found["start"] == "2030-03-04T15:00"


def test_the_time_lands_in_the_configured_zone_with_a_default_length():
    now = datetime(2030, 3, 1, tzinfo=ZoneInfo(TZ))
    fields = bookings.build_booking(
        {"booked": True, "start": "2030-03-04T15:00", "title": "haircut"},
        tz_name=TZ, default_duration=45, now=now,
    )
    assert fields["start"] == "2030-03-04T15:00+01:00"
    assert fields["end"] == "2030-03-04T15:45+01:00"
    assert fields["title"] == "haircut"


def test_a_time_in_the_past_is_not_a_booking():
    now = datetime(2030, 3, 5, tzinfo=ZoneInfo(TZ))
    assert bookings.build_booking(
        {"booked": True, "start": "2030-03-04T15:00"}, tz_name=TZ,
        default_duration=60, now=now,
    ) is None


def test_garbage_from_the_model_is_not_a_booking():
    assert bookings.build_booking(
        {"booked": True, "start": "Tuesday-ish"}, tz_name=TZ, default_duration=60,
    ) is None


# --------------------------------------------------- the provider's reply

def test_a_number_picks_the_booking(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a, b = booking(store), booking(store, chat_id=8, start="2030-03-05T10:00")
    d = bookings.parse_provider_reply("yes 2", store.pending())
    assert d.confirmed and d.booking is b
    d = bookings.parse_provider_reply("NO #1", store.pending())
    assert not d.confirmed and d.booking is a


def test_a_bare_answer_is_fine_when_only_one_thing_is_waiting(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store)
    assert bookings.parse_provider_reply("ok", store.pending()).booking is a
    assert bookings.parse_provider_reply("Да", store.pending()).confirmed


def test_a_bare_answer_with_several_waiting_is_ambiguous(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    booking(store); booking(store, chat_id=8, start="2030-03-05T10:00")
    d = bookings.parse_provider_reply("yes", store.pending())
    assert d.ambiguous and d.booking is None


def test_replying_to_the_request_message_disambiguates(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store, provider_message_id=501)
    booking(store, chat_id=8, start="2030-03-05T10:00", provider_message_id=502)
    d = bookings.parse_provider_reply("no", store.pending(), reply_to_message_id=501)
    assert d.booking is a and not d.confirmed


def test_ordinary_chat_from_the_provider_is_not_a_decision(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    booking(store)
    assert bookings.parse_provider_reply("how was your weekend", store.pending()) is None
    assert bookings.parse_provider_reply("", store.pending()) is None


def test_an_unknown_number_is_not_silently_applied_elsewhere(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    booking(store)
    d = bookings.parse_provider_reply("yes 99", store.pending())
    assert d.ambiguous


# -------------------------------------------------------------- the store

def test_bookings_survive_a_restart(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store)
    store.update(a, status=bookings.CONFIRMED)

    again = bookings.BookingStore(tmp_path / "b.json")
    assert again.get(1).status == bookings.CONFIRMED
    assert booking(again, chat_id=9).id == 2  # ids keep counting


# ------------------------------------------------------- prompt context

def test_a_pending_request_is_never_presented_as_booked(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    booking(store)
    note = bookings.context_for_reply(store.for_chat(7))
    assert "Do NOT say it is confirmed" in note


def test_news_goes_first_and_only_once(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store)
    store.update(a, status=bookings.CONFIRMED)
    note = bookings.context_for_reply(store.for_chat(7), news=a)
    assert note.count("Mon 04 Mar 2030") == 1
    assert "NEWS TO PASS ON" in note


# ------------------------------------------------------ the whole flow

PROVIDER = 999


@pytest.fixture
def flow(app, db, monkeypatch, tmp_path):
    """Provider resolved, sends captured, drafting instant, model scripted."""
    sent: dict[int, list[str]] = {}
    script: dict[str, object] = {"extract": None, "reply": "sure thing"}

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.setdefault(chat_id, []).append(text)
        return type("Sent", (), {"id": 500 + sum(len(v) for v in sent.values())})()

    async def fake_provider():
        return PROVIDER

    async def fake_extract(**kw):
        return script["extract"]

    async def fake_reply(**kw):
        script["last_note"] = kw.get("booking_note", "")
        return script["reply"]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(app, "booking_store", bookings.BookingStore(tmp_path / "b.json"))
    monkeypatch.setattr(app, "booking_scan_tasks", {})
    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "provider_chat_id", fake_provider)
    monkeypatch.setattr(app, "calendar_client", lambda: None)
    monkeypatch.setattr(app, "go_online_for", no_sleep)
    monkeypatch.setattr(app, "mark_read", no_sleep)
    monkeypatch.setattr(app, "schedule_go_offline", lambda _: None)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ai_responder, "extract_booking", fake_extract)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    app.config["booking"]["enabled"] = True
    app.config["booking"]["provider"] = "@provider"
    app.config["timing"]["timezone"] = TZ
    app.deepseek_key = "k"
    return app, sent, script


async def client_writes(app, db, chat_id, text):
    await db.upsert_conversation(chat_id, "Anna", "anna", False, 1)
    await db.record_message(chat_id, "in", "received", text)


@pytest.mark.asyncio
async def test_an_agreed_time_is_put_to_the_provider(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm works for me")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00", "title": "haircut"}

    await app.booking_scan_worker(7)

    request = sent[PROVIDER][0]
    assert "Booking request #1" in request
    assert "Anna (@anna)" in request
    assert "Mon 04 Mar 2030, 15:00–16:00" in request
    assert "haircut" in request
    assert "YES 1" in request and "NO 1" in request
    b = app.booking_store.get(1)
    assert b.status == bookings.PENDING
    assert b.provider_message_id == 501
    notes = [m for m in await db.get_messages(7) if m["direction"] == DIR_SYSTEM]
    assert notes and notes[0]["status"] == STATUS_NOTE
    assert "booking" in app.hub.types()


@pytest.mark.asyncio
async def test_the_same_slot_is_not_asked_twice(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)
    await client_writes(app, db, 7, "great, see you then")
    await app.booking_scan_worker(7)

    assert len(sent[PROVIDER]) == 1
    assert len(app.booking_store.all()) == 1


@pytest.mark.asyncio
async def test_a_changed_time_replaces_the_open_request(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)
    await client_writes(app, db, 7, "actually make it 4pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T16:00"}
    await app.booking_scan_worker(7)

    assert app.booking_store.get(1).status == bookings.SUPERSEDED
    assert app.booking_store.get(2).status == bookings.PENDING
    assert "(replaces #1)" in sent[PROVIDER][1]
    assert app.booking_store.pending() == [app.booking_store.get(2)]


@pytest.mark.asyncio
async def test_nothing_found_means_nothing_sent(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "hi, how much is a haircut?")
    script["extract"] = None
    await app.booking_scan_worker(7)
    assert sent == {} and app.booking_store.all() == []


@pytest.mark.asyncio
async def test_a_yes_from_the_provider_reaches_the_client(flow, db):
    """Confirmed → note in the thread, thanks to the provider, and the client
    gets a reply drafted with the news in the prompt (approval mode here)."""
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)

    await app.handle_provider_reply(PROVIDER, "yes 1", None)
    await app.draft_tasks[7]

    b = app.booking_store.get(1)
    assert b.status == bookings.CONFIRMED and b.decided_by == "provider"
    assert "confirmed ✅" in sent[PROVIDER][-1]
    assert "CONFIRMED" in script["last_note"]
    drafts = await db.pending_drafts(7)
    assert drafts and drafts[0]["status"] == STATUS_PENDING
    assert b.client_notified


@pytest.mark.asyncio
async def test_a_no_asks_the_client_for_another_time(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)

    await app.handle_provider_reply(PROVIDER, "no", None)
    await app.draft_tasks[7]

    assert app.booking_store.get(1).status == bookings.DECLINED
    assert "NOT available" in script["last_note"]
    assert "declined ❌" in sent[PROVIDER][-1]


@pytest.mark.asyncio
async def test_an_ambiguous_answer_is_asked_about(flow, db):
    app, sent, script = flow
    for chat_id, start in ((7, "2030-03-04T15:00"), (8, "2030-03-05T10:00")):
        await client_writes(app, db, chat_id, "book me")
        script["extract"] = {"booked": True, "start": start}
        await app.booking_scan_worker(chat_id)

    await app.handle_provider_reply(PROVIDER, "yes", None)

    assert "Which one?" in sent[PROVIDER][-1]
    assert "#1" in sent[PROVIDER][-1] and "#2" in sent[PROVIDER][-1]
    assert all(b.status == bookings.PENDING for b in app.booking_store.all())


@pytest.mark.asyncio
async def test_the_provider_chatting_is_left_alone(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)

    assert await app.handle_provider_reply(PROVIDER, "thanks, busy day today", None) is False
    assert len(sent[PROVIDER]) == 1


@pytest.mark.asyncio
async def test_a_yes_with_nothing_waiting_is_just_conversation(flow, db):
    app, sent, script = flow
    assert await app.handle_provider_reply(PROVIDER, "yes", None) is False
    assert sent == {}


@pytest.mark.asyncio
async def test_news_is_carried_by_whichever_draft_runs_next(flow, db):
    """The client writes before the news-draft ran; that newer draft still
    carries the confirmation instead of losing it."""
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)
    b = app.booking_store.get(1)
    await app.decide_booking(b, True, by="panel")
    app.cancel_draft(7)  # as a newer incoming message would

    await client_writes(app, db, 7, "any news?")
    await app.draft_worker(7)

    assert "CONFIRMED" in script["last_note"]
    assert app.booking_store.get(1).client_notified


@pytest.mark.asyncio
async def test_the_pending_request_shapes_ordinary_replies(flow, db):
    app, sent, script = flow
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)

    await client_writes(app, db, 7, "so is it booked?")
    await app.draft_worker(7)

    assert "Do NOT say it is confirmed" in script["last_note"]


@pytest.mark.asyncio
async def test_a_missing_provider_is_reported_not_swallowed(flow, db, monkeypatch):
    app, sent, script = flow

    async def nobody():
        return None

    monkeypatch.setattr(app, "provider_chat_id", nobody)
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)

    assert "error" in app.hub.types()
    assert app.booking_store.get(1).status == bookings.PENDING  # kept for the panel


@pytest.mark.asyncio
async def test_an_unsent_request_is_retried_once_the_provider_exists(flow, db, monkeypatch):
    """Provider not set when the time was agreed: the request must still
    reach them later, not be lost to the same-slot dedup."""
    app, sent, script = flow

    async def nobody():
        return None

    real_provider = app.provider_chat_id
    monkeypatch.setattr(app, "provider_chat_id", nobody)
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)
    assert sent == {}

    monkeypatch.setattr(app, "provider_chat_id", real_provider)
    await client_writes(app, db, 7, "so, is it ok?")
    await app.booking_scan_worker(7)

    assert len(sent[PROVIDER]) == 1 and "Booking request #1" in sent[PROVIDER][0]
    assert app.booking_store.get(1).provider_message_id is not None
    assert len(app.booking_store.all()) == 1


@pytest.mark.asyncio
async def test_saving_the_provider_setting_delivers_what_is_waiting(flow, db, monkeypatch):
    app, sent, script = flow

    async def nobody():
        return None

    real_provider = app.provider_chat_id
    monkeypatch.setattr(app, "provider_chat_id", nobody)
    await client_writes(app, db, 7, "tuesday 3pm")
    script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
    await app.booking_scan_worker(7)

    monkeypatch.setattr(app, "provider_chat_id", real_provider)
    await app.resend_unsent_bookings()

    assert len(sent[PROVIDER]) == 1


# -------------------------------------------------- reminder and arrival

def test_the_check_in_is_due_inside_the_window_only(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store)  # 2030-03-04 15:00 Madrid
    store.update(a, status=bookings.CONFIRMED)
    tz = ZoneInfo(TZ)
    assert store.due_for_reminder(datetime(2030, 3, 4, 12, 0, tzinfo=tz), 120) == []
    assert store.due_for_reminder(datetime(2030, 3, 4, 13, 30, tzinfo=tz), 120) == [a]
    assert store.due_for_reminder(datetime(2030, 3, 4, 15, 30, tzinfo=tz), 120) == []  # started
    assert store.due_for_reminder(datetime(2030, 3, 4, 13, 30, tzinfo=tz), 0) == []  # off
    store.update(a, reminder_requested_at="x")
    assert store.due_for_reminder(datetime(2030, 3, 4, 13, 30, tzinfo=tz), 120) == []


def test_arrival_is_only_expected_around_the_slot(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store)
    store.update(a, status=bookings.CONFIRMED)
    tz = ZoneInfo(TZ)
    assert store.awaiting_arrival(7, datetime(2030, 3, 3, 15, 0, tzinfo=tz), 120) is None
    assert store.awaiting_arrival(7, datetime(2030, 3, 4, 14, 50, tzinfo=tz), 120) is a
    assert store.awaiting_arrival(7, datetime(2030, 3, 4, 16, 20, tzinfo=tz), 120) is a
    assert store.awaiting_arrival(7, datetime(2030, 3, 4, 17, 0, tzinfo=tz), 120) is None
    store.update(a, instructions_sent_at="x")
    assert store.awaiting_arrival(7, datetime(2030, 3, 4, 14, 50, tzinfo=tz), 120) is None


def test_the_reminder_wording_never_leaks_the_address(tmp_path):
    store = bookings.BookingStore(tmp_path / "b.json")
    a = booking(store)
    store.update(a, status=bookings.CONFIRMED)
    now = datetime(2030, 3, 4, 13, 0, tzinfo=ZoneInfo(TZ))
    note = bookings.context_for_reply([a], news=a, news_kind="reminder", now=now)
    assert "in about 2 hours" in note and "still coming" in note
    assert "Never give the address" in note


def test_arrival_json_is_read_strictly():
    assert bookings.parse_arrival('{"arrived": true}')
    assert not bookings.parse_arrival('{"arrived": false}')
    assert not bookings.parse_arrival("yes")


@pytest.fixture
def confirmed(flow, db, monkeypatch):
    """A confirmed booking for chat 7 at 15:00, with the clock under control."""
    app, sent, script = flow
    clock = {"now": datetime(2030, 3, 4, 12, 0, tzinfo=ZoneInfo(TZ))}
    monkeypatch.setattr(app, "booking_now", lambda: clock["now"])
    app.config["booking"]["reminder_minutes_before"] = 120
    app.config["booking"]["arrival_instructions"] = "Calle Mayor 5, 3rd floor. Code 1234#."

    async def seed():
        await client_writes(app, db, 7, "tuesday 3pm")
        script["extract"] = {"booked": True, "start": "2030-03-04T15:00"}
        await app.booking_scan_worker(7)
        b = app.booking_store.get(1)
        app.booking_store.update(b, status=bookings.CONFIRMED, client_notified=True)
        script["extract"] = None
        return b

    return app, sent, script, clock, seed


@pytest.mark.asyncio
async def test_the_client_is_asked_whether_they_are_coming(confirmed, db):
    app, sent, script, clock, seed = confirmed
    b = await seed()

    await app.check_reminders()
    assert b.reminder_requested_at is None  # 3 hours out: too early

    clock["now"] = datetime(2030, 3, 4, 13, 5, tzinfo=ZoneInfo(TZ))
    await app.check_reminders()
    assert b.reminder_requested_at is not None
    await app.draft_tasks[7]

    assert "still coming" in script["last_note"]
    assert b.reminder_sent
    assert not b.instructions_sent_at
    # Asked once, not every minute.
    await app.check_reminders()
    assert len(await db.pending_drafts(7)) == 1


@pytest.mark.asyncio
async def test_arriving_gets_the_instructions_word_for_word(confirmed, db, monkeypatch):
    app, sent, script, clock, seed = confirmed
    b = await seed()
    app.booking_store.update(b, reminder_requested_at="x", reminder_sent=True)
    clock["now"] = datetime(2030, 3, 4, 14, 55, tzinfo=ZoneInfo(TZ))

    async def fake_arrival(**kw):
        return script.get("arrived", False)

    monkeypatch.setattr(ai_responder, "extract_arrival", fake_arrival)

    await client_writes(app, db, 7, "on my way, 5 min")
    script["arrived"] = False
    await app.booking_scan_worker(7)
    assert 7 not in sent

    await client_writes(app, db, 7, "I'm here")
    script["arrived"] = True
    await app.booking_scan_worker(7)

    assert sent[7] == ["Calle Mayor 5, 3rd floor. Code 1234#."]
    assert b.instructions_sent_at and b.arrived_at
    # Sent once: a second "here" does not repeat the code.
    await client_writes(app, db, 7, "here!!")
    await app.booking_scan_worker(7)
    assert len(sent[7]) == 1


@pytest.mark.asyncio
async def test_no_instructions_configured_is_reported_not_silent(confirmed, db, monkeypatch):
    app, sent, script, clock, seed = confirmed
    b = await seed()
    app.config["booking"]["arrival_instructions"] = ""
    clock["now"] = datetime(2030, 3, 4, 15, 2, tzinfo=ZoneInfo(TZ))

    async def arrived(**kw):
        return True

    monkeypatch.setattr(ai_responder, "extract_arrival", arrived)
    await client_writes(app, db, 7, "я на месте")
    await app.booking_scan_worker(7)

    assert 7 not in sent
    notes = [m["text"] for m in await db.get_messages(7) if m["status"] == STATUS_NOTE]
    assert any("no arrival instructions" in n for n in notes)
