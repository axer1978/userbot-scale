"""Bookings in Postgres: numbered per tenant, never overlapping, changed
only through the state machine, and invisible to every other tenant."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import audit
import booking_states as bs
import booking_store
from booking_store import BookingStore, SlotTaken, StaleBooking
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

T0 = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2030, 3, 1, 9, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def stores(pg_pool):
    a = await seed_session(pg_pool, "acc_a", name="Salon A")
    b = await seed_session(pg_pool, "acc_b", name="Salon B")
    return BookingStore(pg_pool, a, "acc_a"), BookingStore(pg_pool, b, "acc_b")


async def make(store, start=T0, minutes=60, buffer=0, chat_id=42, name="Anna"):
    return await store.create(chat_id=chat_id, customer_name=name, customer_username="anna",
                              starts_at=start, ends_at=start + timedelta(minutes=minutes),
                              buffer_minutes=buffer, tz="Europe/Riga", service="haircut")


async def test_numbers_count_per_tenant_from_one(stores):
    a, b = stores
    assert [(await make(a, T0 + timedelta(hours=i)))["number"] for i in range(3)] == [1, 2, 3]
    assert (await make(b))["number"] == 1


async def test_two_live_bookings_of_a_tenant_cannot_overlap(stores):
    a, b = stores
    await make(a)
    with pytest.raises(SlotTaken):
        await make(a, T0 + timedelta(minutes=30), chat_id=43)
    # Back to back is fine, and another tenant's calendar is its own.
    await make(a, T0 + timedelta(hours=1), chat_id=43)
    await make(b, T0)


async def test_the_buffer_holds_the_slot_after_a_booking(stores):
    a, _ = stores
    await make(a, buffer=15)
    with pytest.raises(SlotTaken):
        await make(a, T0 + timedelta(minutes=70), chat_id=43)
    await make(a, T0 + timedelta(minutes=75), chat_id=43)


async def test_a_failed_insert_does_not_use_up_a_number(stores):
    a, _ = stores
    await make(a)
    with pytest.raises(SlotTaken):
        await make(a, chat_id=43)
    assert (await make(a, T0 + timedelta(hours=2), chat_id=43))["number"] == 2


async def test_two_customers_racing_for_one_slot_get_one_booking(stores, pg_pool):
    a, _ = stores
    results = await asyncio.gather(*(make(a, chat_id=100 + i) for i in range(5)), return_exceptions=True)
    won = [r for r in results if isinstance(r, dict)]
    assert len(won) == 1
    assert all(isinstance(r, SlotTaken) for r in results if not isinstance(r, dict))
    assert await pg_pool.fetchval("SELECT count(*) FROM bookings") == 1


async def test_a_cancelled_booking_frees_its_slot(stores):
    a, _ = stores
    first = await make(a)
    await a.apply(first, bs.cancel(first, actor=bs.CUSTOMER, now=NOW), actor=bs.CUSTOMER)
    await make(a, chat_id=43)


async def test_a_change_computed_from_an_old_read_is_refused(stores):
    a, _ = stores
    booking = await make(a)
    await a.apply(booking, bs.submitted(booking, provider_chat_id=9, provider_message_id=1), actor=bs.SYSTEM)
    pending = await a.get(booking["id"])
    await a.apply(pending, bs.confirm(pending, actor=bs.OWNER, now=NOW), actor=bs.OWNER)
    # The panel still shows it pending and an admin declines it: too late.
    with pytest.raises(StaleBooking):
        await a.apply(pending, bs.decline(pending, actor=bs.ADMIN, now=NOW), actor=bs.ADMIN)
    assert (await a.get(booking["id"]))["state"] == bs.CONFIRMED


async def test_every_change_writes_an_event_and_an_audit_row(stores, pg_pool):
    a, _ = stores
    booking = await make(a)
    await a.apply(booking, bs.submitted(booking, provider_chat_id=9, provider_message_id=1), actor=bs.SYSTEM)
    events = await a.events(booking["id"])
    assert [(e["from_state"], e["to_state"], e["action"]) for e in events] == [
        (None, "requested", "create"), ("requested", "pending", "submitted")]
    rows = await audit.list_events(pg_pool, tenant_id=a.tenant_id)
    assert {r["event"] for r in rows} >= {audit.BOOKING_CREATED, audit.BOOKING_CHANGED}


async def test_another_tenant_cannot_read_or_change_a_booking(stores, pg_pool):
    a, b = stores
    booking = await make(a)
    with pytest.raises(booking_store.BookingNotFound):
        await b.get(booking["id"])
    assert await b.by_number(booking["number"]) is None
    assert await b.for_chat(42) == []
    with pytest.raises(StaleBooking):
        await b.apply(booking, bs.cancel(booking, actor=bs.ADMIN, now=NOW), actor=bs.ADMIN)
    with pytest.raises(booking_store.BookingNotFound):
        await b.set_fields(booking["id"], customer_notice=None)
    assert (await a.get(booking["id"]))["state"] == bs.REQUESTED
    assert await b.busy(T0 - timedelta(days=1), T0 + timedelta(days=1)) == []


async def test_a_row_cannot_carry_one_tenants_id_and_anothers_account(stores, pg_pool):
    a, b = stores
    import asyncpg
    with pytest.raises(asyncpg.exceptions.RaiseError, match="does not own"):
        await pg_pool.execute(
            "INSERT INTO bookings (tenant_id, session_id, number, chat_id, customer_ref, starts_at, ends_at, "
            "blocked_until, tz, state) VALUES ($1, 'acc_b', 1, 1, 'x', $2, $3, $3, 'UTC', 'requested')",
            a.tenant_id, T0, T0 + timedelta(hours=1),
        )


async def test_customer_refs_differ_per_tenant(stores):
    a, b = stores
    assert (await make(a))["customer_ref"] != (await make(b))["customer_ref"]


async def test_a_reminder_is_claimed_exactly_once(stores):
    a, _ = stores
    booking = await make(a)
    claims = await asyncio.gather(*(a.claim_reminder(booking, 120) for _ in range(4)))
    assert sorted(claims) == [False, False, False, True]


async def test_due_reminders_skip_ones_already_past_at_confirmation(stores):
    a, _ = stores
    booking = await make(a)
    booking = await a.apply(booking, bs.confirm(booking, actor=bs.OWNER, now=NOW), actor=bs.OWNER)
    # Confirmed at NOW = 3 days ahead: both the 24 h and 2 h reminders will fall due.
    await a.set_fields(booking["id"], decided_at=T0 - timedelta(hours=5))
    reminders = [{"minutes_before": 1440, "instruction": ""}, {"minutes_before": 120, "instruction": ""}]
    # 3 hours before: the 24 h one had already passed when it was confirmed.
    assert await a.due_reminders(T0 - timedelta(hours=3), reminders) == []
    [(due, reminder, skipped)] = await a.due_reminders(T0 - timedelta(minutes=90), reminders)
    assert reminder["minutes_before"] == 120 and due["id"] == booking["id"] and skipped == []
    assert await a.claim_reminder(due, 120)
    assert await a.due_reminders(T0 - timedelta(minutes=90), reminders) == []


async def test_after_downtime_only_the_latest_due_reminder_is_sent(stores):
    a, _ = stores
    booking = await make(a)
    booking = await a.apply(booking, bs.confirm(booking, actor=bs.OWNER, now=NOW), actor=bs.OWNER)
    reminders = [{"minutes_before": 1440, "instruction": ""}, {"minutes_before": 120, "instruction": ""}]
    [(_, reminder, skipped)] = await a.due_reminders(T0 - timedelta(minutes=90), reminders)
    assert (reminder["minutes_before"], skipped) == (120, [1440])


async def test_a_moved_booking_gets_its_reminders_again(stores):
    a, _ = stores
    booking = await make(a)
    booking = await a.apply(booking, bs.confirm(booking, actor=bs.OWNER, now=NOW), actor=bs.OWNER)
    assert await a.claim_reminder(booking, 120)
    later = T0 + timedelta(days=1)
    moved = await a.apply(booking, bs.reschedule(booking, actor=bs.ADMIN, starts_at=later,
                                                 ends_at=later + timedelta(hours=1),
                                                 blocked_until=later + timedelta(hours=1), now=NOW), actor=bs.ADMIN)
    assert await a.claim_reminder(moved, 120)


async def test_waitlist_first_in_line_covers_the_slot(stores):
    a, b = stores
    first = await a.add_waitlist(chat_id=1, customer_name="A", wanted_from=T0 - timedelta(hours=2),
                                 wanted_to=T0 + timedelta(hours=4))
    await a.add_waitlist(chat_id=2, customer_name="B", wanted_from=T0, wanted_to=T0 + timedelta(hours=1))
    await a.add_waitlist(chat_id=3, customer_name="C", wanted_from=T0 + timedelta(hours=5),
                         wanted_to=T0 + timedelta(hours=8))
    assert (await a.first_in_line(T0, T0 + timedelta(hours=1)))["id"] == first["id"]
    assert await b.first_in_line(T0, T0 + timedelta(hours=1)) is None
    # A new wish from the same person replaces their old one.
    await a.add_waitlist(chat_id=1, customer_name="A", wanted_from=T0 + timedelta(days=3),
                         wanted_to=T0 + timedelta(days=4))
    assert (await a.first_in_line(T0, T0 + timedelta(hours=1)))["chat_id"] == 2


async def test_opening_hours_are_replaced_whole_and_audited(stores, pg_pool):
    a, b = stores
    await a.save_rules([{"weekday": 0, "start_time": "09:00", "end_time": "17:00", "slot_minutes": 60,
                         "buffer_minutes": 10}], actor=bs.ADMIN)
    rows = await a.save_rules([{"weekday": 1, "start_time": "10:00", "end_time": "12:00"}], actor=bs.ADMIN)
    assert [(r["weekday"], r["start_time"].isoformat()) for r in rows] == [(1, "10:00:00")]
    assert await b.rule_rows() == []
    with pytest.raises(ValueError):
        await a.save_rules([{"weekday": 1, "start_time": "12:00", "end_time": "10:00"}], actor=bs.ADMIN)
    assert len(await a.rule_rows()) == 1
    events = [r for r in await audit.list_events(pg_pool, tenant_id=a.tenant_id)
              if r["event"] == audit.AVAILABILITY_CHANGED]
    assert len(events) == 2


async def test_bookings_json_is_imported_once_with_its_numbers(stores, tmp_path, pg_pool):
    a, _ = stores
    path = tmp_path / "bookings.json"
    path.write_text(json.dumps({"bookings": [
        {"id": 5, "chat_id": 42, "client_name": "Anna", "client_username": "anna",
         "start": "2030-03-04T11:00:00+02:00", "end": "2030-03-04T12:00:00+02:00", "timezone": "Europe/Riga",
         "title": "haircut", "status": "confirmed", "client_notified": True, "decided_by": "provider"},
        # Overlapping old data is kept as history, not refused.
        {"id": 6, "chat_id": 43, "client_name": "Ben", "start": "2030-03-04T11:30:00+02:00",
         "end": "2030-03-04T12:30:00+02:00", "timezone": "Europe/Riga", "status": "pending"},
        {"id": 7, "chat_id": 44, "client_name": "Cid", "start": "2030-03-05T11:00:00+02:00",
         "end": "2030-03-05T12:00:00+02:00", "timezone": "Europe/Riga", "status": "declined",
         "decided_by": "panel", "client_notified": False},
        {"id": "junk"},
    ]}), encoding="utf-8")

    assert await a.import_legacy_file(path) == 3
    assert not path.exists() and (tmp_path / "bookings.json.imported").exists()
    five = await a.by_number(5)
    assert five["state"] == bs.CONFIRMED and five["legacy"] and five["decided_by"] == bs.OWNER
    seven = await a.by_number(7)
    assert (seven["state"], seven["cancelled_by"], seven["customer_notice"]) == (bs.CANCELLED, bs.ADMIN, bs.NOTICE_DECLINED)
    # New bookings continue after the highest imported number.
    assert (await make(a, T0 + timedelta(days=10)))["number"] == 8
    assert await a.import_legacy_file(path) == 0
