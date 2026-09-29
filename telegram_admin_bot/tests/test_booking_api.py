"""The panel's booking routes: the calendar, actions passed to the running
account, opening hours, the waitlist, the calendar feed link, AI usage, and
that an account's routes never reach another tenant's bookings."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

import audit
import booking_states as bs
from booking_store import BookingStore
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

SOON = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)


@pytest_asyncio.fixture
async def accounts(pg_pool):
    a = await seed_session(pg_pool, "acc_a", name="Salon A")
    b = await seed_session(pg_pool, "acc_b", name="Salon B")
    return BookingStore(pg_pool, a, "acc_a"), BookingStore(pg_pool, b, "acc_b")


async def make(store, start=SOON, chat_id=42):
    return await store.create(chat_id=chat_id, customer_name="Anna", customer_username=None, starts_at=start,
                              ends_at=start + timedelta(hours=1), buffer_minutes=0, tz="Europe/Riga")


async def test_booking_routes_need_the_admin_login(panel_client, accounts):
    import panel

    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app), base_url="http://test")
    async with anonymous:
        for method, path in [("GET", "/api/sessions/acc_a/bookings"), ("GET", "/api/sessions/acc_a/availability"),
                             ("PUT", "/api/sessions/acc_a/availability"), ("GET", "/api/sessions/acc_a/waitlist"),
                             ("POST", "/api/sessions/acc_a/bookings/1/action"),
                             ("GET", "/api/sessions/acc_a/calendar-feed"), ("GET", "/api/sessions/acc_a/ai-usage")]:
            response = await anonymous.request(method, path, json={})
            assert response.status_code == 401, (method, path)


async def test_the_calendar_lists_only_this_accounts_bookings(panel_client, accounts):
    a, b = accounts
    mine = await make(a)
    await make(b)
    day = SOON.date().isoformat()
    body = (await panel_client.get(f"/api/sessions/acc_a/bookings?start={day}&days=3")).json()
    assert [x["id"] for x in body["bookings"]] == [mine["id"]]
    assert [x["id"] for x in body["awaiting"]] == [mine["id"]]
    assert body["timezone"] == "Europe/Riga"


async def test_one_account_cannot_see_or_act_on_anothers_booking(panel_client, accounts):
    a, b = accounts
    theirs = await make(b)
    assert (await panel_client.get(f"/api/sessions/acc_a/bookings/{theirs['id']}")).status_code == 404
    response = await panel_client.post(f"/api/sessions/acc_a/bookings/{theirs['id']}/action",
                                       json={"action": "cancel"})
    assert response.status_code == 404
    assert (await b.get(theirs["id"]))["state"] == bs.REQUESTED


async def test_an_action_goes_to_the_running_account_and_its_errors_come_back(panel_client, accounts):
    import panel

    a, _ = accounts
    booking = await make(a)
    calls = []
    stop = asyncio.Event()

    async def worker(action, args):
        calls.append((action, args))
        if args["action"] == "complete":
            raise bs.IllegalTransition("cannot mark completed booking #1: it is requested")
        return {"id": args["booking_id"], "state": "confirmed"}

    serving = asyncio.create_task(panel.bus.serve("acc_a", worker, stop))
    await asyncio.sleep(0.05)
    try:
        ok = await panel_client.post(f"/api/sessions/acc_a/bookings/{booking['id']}/action", json={"action": "confirm"})
        assert ok.status_code == 200 and ok.json()["state"] == "confirmed"
        refused = await panel_client.post(f"/api/sessions/acc_a/bookings/{booking['id']}/action",
                                          json={"action": "complete"})
        assert refused.status_code == 409 and "cannot mark completed" in refused.json()["detail"]
    finally:
        stop.set()
        await serving
    assert calls[0] == ("booking_action", {"booking_id": booking["id"], "action": "confirm", "starts_at": None,
                                           "minutes": None, "reason": ""})
    bad = await panel_client.post(f"/api/sessions/acc_a/bookings/{booking['id']}/action", json={"action": "delete"})
    assert bad.status_code == 422
    no_time = await panel_client.post(f"/api/sessions/acc_a/bookings/{booking['id']}/action", json={"action": "propose"})
    assert no_time.status_code == 400


async def test_an_action_on_an_account_nobody_runs_says_so(panel_client, accounts, monkeypatch):
    import booking_api

    a, _ = accounts
    booking = await make(a)
    monkeypatch.setattr(booking_api, "ACTION_TIMEOUT", 0.2)
    response = await panel_client.post(f"/api/sessions/acc_a/bookings/{booking['id']}/action", json={"action": "confirm"})
    assert response.status_code == 503


async def test_opening_hours_round_trip_and_are_validated(panel_client, accounts, pg_pool):
    a, b = accounts
    rules = [{"weekday": 0, "start_time": "09:00", "end_time": "13:00", "slot_minutes": 30, "buffer_minutes": 10},
             {"weekday": 0, "start_time": "14:00", "end_time": "18:00"}]
    saved = (await panel_client.put("/api/sessions/acc_a/availability", json={"rules": rules})).json()
    assert [(r["start_time"], r["end_time"]) for r in saved["rules"]] == [("09:00", "13:00"), ("14:00", "18:00")]
    assert (await panel_client.get("/api/sessions/acc_b/availability")).json()["rules"] == []
    bad = await panel_client.put("/api/sessions/acc_a/availability",
                                 json={"rules": [{"weekday": 0, "start_time": "13:00", "end_time": "09:00"}]})
    assert bad.status_code == 400
    worse = await panel_client.put("/api/sessions/acc_a/availability",
                                   json={"rules": [{"weekday": 9, "start_time": "09:00", "end_time": "10:00"}]})
    assert worse.status_code == 422
    assert len((await panel_client.get("/api/sessions/acc_a/availability")).json()["rules"]) == 2


async def test_free_slots_follow_the_hours_and_bookings(panel_client, accounts):
    a, _ = accounts
    await panel_client.put("/api/sessions/acc_a/availability", json={"rules": [
        {"weekday": d, "start_time": "10:00", "end_time": "13:00"} for d in range(7)]})
    day = (SOON + timedelta(days=1)).date()
    slots = (await panel_client.get(f"/api/sessions/acc_a/free-slots?start={day}&days=1")).json()["slots"]
    assert len(slots) == 3


async def test_the_waitlist_can_be_cleared_from_the_panel(panel_client, accounts):
    a, b = accounts
    entry = await a.add_waitlist(chat_id=5, customer_name="Ben", wanted_from=SOON, wanted_to=SOON + timedelta(hours=4))
    assert [e["id"] for e in (await panel_client.get("/api/sessions/acc_a/waitlist")).json()] == [entry["id"]]
    assert (await panel_client.delete(f"/api/sessions/acc_b/waitlist/{entry['id']}")).status_code == 404
    assert (await panel_client.delete(f"/api/sessions/acc_a/waitlist/{entry['id']}")).status_code == 200
    assert (await panel_client.get("/api/sessions/acc_a/waitlist")).json() == []


async def test_the_calendar_feed_link_and_replacing_it(panel_client, accounts, pg_pool, monkeypatch):
    a, _ = accounts
    assert (await panel_client.get("/api/sessions/acc_a/calendar-feed")).json() == {
        "url": "", "public_base_url_set": False}
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://book.example.com/")
    old = (await panel_client.get("/api/sessions/acc_a/calendar-feed")).json()["url"]
    assert old.startswith("https://book.example.com/cal/") and old.endswith(".ics")
    new = (await panel_client.post("/api/sessions/acc_a/calendar-feed/regenerate")).json()["url"]
    assert new != old
    assert any(e["reason"] == "calendar feed link replaced" for e in await audit.list_events(pg_pool, tenant_id=a.tenant_id))


async def test_ai_usage_against_the_limits(panel_client, accounts, pg_pool):
    a, _ = accounts
    await pg_pool.execute(
        "INSERT INTO llm_usage (tenant_id, purpose, model, prompt_cache_miss_tokens, completion_tokens, cost_eur) "
        "VALUES ($1, 'reply', 'deepseek-chat', 900, 100, 0.5)", a.tenant_id,
    )
    body = (await panel_client.get("/api/sessions/acc_a/ai-usage")).json()
    assert body["today"] == {"tokens": 1000, "eur": 0.5}
    assert body["reached"] == ""
    assert (await panel_client.get("/api/sessions/acc_b/ai-usage")).json()["today"]["tokens"] == 0


async def test_prices_are_edited_whole_and_audited(panel_client, pg_pool):
    prices = (await panel_client.get("/api/platform/prices")).json()
    assert "deepseek-chat" in prices["models"]
    prices["models"]["vision-model"] = {"input_cache_hit": 0.1, "input_cache_miss": 0.15, "output": 0.6}
    saved = await panel_client.put("/api/platform/prices", json=prices)
    assert saved.status_code == 200
    assert "vision-model" in (await panel_client.get("/api/platform/prices")).json()["models"]
    bad = await panel_client.put("/api/platform/prices", json={**prices, "usd_to_eur": -1})
    assert bad.status_code == 422
    assert any(e["reason"] == "LLM prices changed" for e in await audit.list_events(pg_pool))


async def test_a_photo_can_be_marked_as_the_entrance(panel_client, accounts, tmp_path):
    import panel

    library = await panel.media_library_for("acc_a")
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "door.jpg").write_bytes(b"\xff\xd8\xff")
    (library.dir / "clip.mp4").write_bytes(b"0000")
    library.refresh()
    photo = next(i for i in library.all() if i["file"] == "door.jpg")
    video = next(i for i in library.all() if i["file"] == "clip.mp4")
    marked = await panel_client.patch(f"/api/sessions/acc_a/media/{photo['id']}/role", json={"role": "arrival_reference"})
    assert marked.json()["role"] == "arrival_reference"
    assert (await panel_client.patch(f"/api/sessions/acc_a/media/{video['id']}/role",
                                     json={"role": "arrival_reference"})).status_code == 400
    assert (await panel_client.patch(f"/api/sessions/acc_a/media/{photo['id']}/role",
                                     json={"role": "something"})).status_code == 422
    unmarked = await panel_client.patch(f"/api/sessions/acc_a/media/{photo['id']}/role", json={"role": None})
    assert "role" not in unmarked.json()
