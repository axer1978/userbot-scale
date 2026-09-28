"""public_app.py: the calendar feed and the customer's booking page.

Two tenants with bookings each, so every test that reads data also proves
one tenant's token never reaches the other's rows. Button presses go over a
CommandBus on fakeredis to a fake worker that records what it was asked.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import fakeredis
import httpx
import pytest
import pytest_asyncio

import commands
import public_app
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

RIGA = ZoneInfo("Europe/Riga")
CAL_A = "calA_" + "a" * 27
CAL_B = "calB_" + "b" * 27
# A far-future fixed slot, so the rendered date is known: Friday.
A1_START = datetime(2030, 10, 4, 14, 0, tzinfo=RIGA)

HTML_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex, nofollow",
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
}


def tok(label: str) -> str:
    return f"tok_{label}_".ljust(32, "x")


async def add_booking(pool, session_id, number, token, starts_at, *, hours=1, state="confirmed",
                      customer_name="", updated_at=None, attendance=None, service="Haircut",
                      tz="Europe/Riga"):
    ends_at = starts_at + timedelta(hours=hours)
    async with pool.acquire() as con:
        return await con.fetchval(
            """
            INSERT INTO bookings (session_id, number, chat_id, customer_ref, customer_name,
                                  customer_username, notes, service, starts_at, ends_at,
                                  blocked_until, tz, state, customer_token, updated_at,
                                  attendance_confirmed_at)
            VALUES ($1, $2, $3, $4, $5, 'secret_username', 'secret notes', $6, $7, $8, $8,
                    $9, $10, $11, COALESCE($12, now()), $13)
            RETURNING id
            """,
            session_id, number, 1000 + number, f"ref{number}", customer_name, service,
            starts_at, ends_at, tz, state, token, updated_at, attendance,
        )


@pytest_asyncio.fixture
async def world(pg_pool):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ta = await seed_session(pg_pool, "acct_a", name="Salon <Anna>")
    tb = await seed_session(pg_pool, "acct_b", name="Barber Bob")
    async with pg_pool.acquire() as con:
        await con.execute("UPDATE tenants SET calendar_token = $1 WHERE id = $2", CAL_A, ta)
        await con.execute("UPDATE tenants SET calendar_token = $1 WHERE id = $2", CAL_B, tb)
    ids = {
        "a1": await add_booking(pg_pool, "acct_a", 1, tok("a1"), A1_START,
                                customer_name="Alice Secret"),
        "a2": await add_booking(pg_pool, "acct_a", 2, tok("a2"), now + timedelta(days=3),
                                state="pending", customer_name="Pat Pending"),
        "a3": await add_booking(pg_pool, "acct_a", 3, tok("a3"), now + timedelta(days=4),
                                state="cancelled", customer_name="Recent Cancel"),
        "a4": await add_booking(pg_pool, "acct_a", 4, tok("a4"), now + timedelta(days=5),
                                state="cancelled", updated_at=now - timedelta(days=40)),
        "a5": await add_booking(pg_pool, "acct_a", 5, tok("a5"), now - timedelta(days=100),
                                state="completed", updated_at=now - timedelta(days=99)),
        "a6": await add_booking(pg_pool, "acct_a", 6, tok("a6"), now - timedelta(days=1)),
        "a7": await add_booking(pg_pool, "acct_a", 7, tok("a7"), now + timedelta(days=6),
                                attendance=now),
        # Same time as a1 — another tenant, so no overlap conflict.
        "b1": await add_booking(pg_pool, "acct_b", 1, tok("b1"), A1_START,
                                customer_name="Bob Client", service="Beard trim"),
    }
    return {"tenant_a": ta, "tenant_b": tb, "ids": ids}


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def bus(redis):
    return commands.CommandBus(redis)


@pytest_asyncio.fixture
async def client(pg_pool, bus):
    application = public_app.create_app(pool=pg_pool, bus=bus)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        yield c


@asynccontextmanager
async def worker(bus, redis, session_id, calls, *, fail=False):
    """A stand-in for the worker running `session_id`: records each command
    and answers {} (or raises, with fail=True)."""
    stop = asyncio.Event()

    async def handler(action, args):
        calls.append((session_id, action, args))
        if fail:
            raise RuntimeError("booking can't be changed")
        return {}

    task = asyncio.create_task(bus.serve(session_id, handler, stop))
    for _ in range(200):  # until it is subscribed, or a dispatch could miss it
        if dict(await redis.pubsub_numsub(f"cmd:{session_id}")).get(f"cmd:{session_id}"):
            break
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        stop.set()
        await task


def uids(text: str) -> set[int]:
    lines = text.replace("\r\n ", "").split("\r\n")
    return {int(line.split("-", 1)[1].split("@")[0]) for line in lines if line.startswith("UID:")}


async def state_of(pool, booking_id):
    async with pool.acquire() as con:
        return await con.fetchrow(
            "SELECT state, attendance_confirmed_at, updated_at FROM bookings WHERE id = $1", booking_id
        )


# ------------------------------------------------------------------ feed


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


async def test_calendar_returns_only_that_tenants_bookings(client, world):
    ids = world["ids"]
    r = await client.get(f"/cal/{CAL_A}.ics")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert r.headers["cache-control"] == "private, max-age=300"
    # Live, recent past, recently cancelled — not the old cancel, not the
    # completed one from 100 days ago, and nothing of tenant B.
    assert uids(r.text) == {ids["a1"], ids["a2"], ids["a3"], ids["a6"], ids["a7"]}
    assert "X-WR-CALNAME:Salon <Anna>" in r.text
    assert "X-WR-TIMEZONE:Europe/Riga" in r.text
    assert f"UID:booking-{ids['a1']}@localhost" in r.text
    assert "#1 Haircut – Alice Secret" in r.text
    assert "Bob Client" not in r.text and "Beard trim" not in r.text
    assert "STATUS:CANCELLED" in r.text

    r = await client.get(f"/cal/{CAL_B}.ics")
    assert uids(r.text) == {ids["b1"]}
    assert "X-WR-CALNAME:Barber Bob" in r.text
    assert "Alice Secret" not in r.text


async def test_calendar_unknown_and_malformed_tokens_404(client, world):
    for path in [
        "/cal/" + "z" * 32 + ".ics",   # well-formed, unknown
        f"/cal/{tok('a1')}.ics",       # a customer token is not a calendar token
        "/cal/short.ics",
        "/cal/" + "a" * 129 + ".ics",
        "/cal/abc%27%20OR%201=1--aaaaaaaaaa.ics",
        f"/cal/{CAL_A}",                # no .ics
    ]:
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.text == "Not found"
        assert "BEGIN:VCALENDAR" not in r.text


class ExplodingPool:
    def acquire(self):
        raise AssertionError("the database was touched")


async def test_malformed_tokens_never_touch_the_database(bus):
    application = public_app.create_app(pool=ExplodingPool(), bus=bus)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as c:
        for method, path in [
            ("GET", "/cal/short.ics"),
            ("GET", "/cal/has.dots.in.it.aaaaaaaaaaa.ics"),
            ("GET", "/b/short"),
            ("GET", "/b/" + "a" * 129),
            ("GET", "/b/bad!chars~aaaaaaaaaaaaaaa"),
            ("GET", "/b/short/cancel"),
            ("POST", "/b/short/confirm"),
            ("POST", "/b/bad!chars~aaaaaaaaaaaaaaa/cancel"),
        ]:
            r = await c.request(method, path)
            assert r.status_code == 404, path


# ------------------------------------------------------------------ page


async def test_booking_page_shows_the_booking_and_nothing_about_the_customer(client, world):
    r = await client.get(f"/b/{tok('a1')}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Salon &lt;Anna&gt;" in body and "<Anna>" not in body  # escaped
    assert "#1" in body
    assert "Haircut" in body
    assert "Fri 04 Oct 2030, 14:00–15:00 (Europe/Riga)" in body
    assert "Confirmed" in body
    for secret in ("Alice Secret", "secret_username", "secret notes", "ref1", "Bob Client", "Beard trim"):
        assert secret not in body
    assert f'action="/b/{tok("a1")}/confirm"' in body
    assert f'href="/b/{tok("a1")}/cancel"' in body
    assert "<script" not in body.lower() and "http://" not in body and "https://" not in body
    for name, value in HTML_HEADERS.items():
        assert r.headers[name] == value


async def test_booking_page_other_tenant(client, world):
    r = await client.get(f"/b/{tok('b1')}")
    assert "Barber Bob" in r.text and "Beard trim" in r.text
    assert "Salon" not in r.text and "Bob Client" not in r.text


@pytest.mark.parametrize("label, text, can_confirm, can_cancel", [
    ("a2", "Waiting for confirmation", False, True),
    ("a3", "Cancelled", False, False),
    ("a5", "Completed", False, False),
    ("a6", "Confirmed", False, False),       # confirmed but already started
    ("a7", "Confirmed", False, True),        # attendance already confirmed
])
async def test_booking_page_states(client, world, label, text, can_confirm, can_cancel):
    r = await client.get(f"/b/{tok(label)}")
    assert r.status_code == 200
    assert text in r.text
    assert ("/confirm" in r.text) is can_confirm
    assert ("/cancel" in r.text) is can_cancel
    if label == "a7":
        assert "You have confirmed that you are coming" in r.text
        assert "I&#x27;m coming" not in r.text


async def test_no_show_label(client, world, pg_pool):
    async with pg_pool.acquire() as con:
        await con.execute("UPDATE bookings SET state = 'no_show' WHERE id = $1", world["ids"]["a6"])
    r = await client.get(f"/b/{tok('a6')}")
    assert "Missed" in r.text


async def test_booking_page_unknown_token_404(client, world):
    r = await client.get("/b/" + "q" * 40)
    assert r.status_code == 404 and r.text == "Not found"
    # A calendar token is not a booking token.
    assert (await client.get(f"/b/{CAL_A}")).status_code == 404
    assert (await client.get("/b/" + "q" * 40 + "/cancel")).status_code == 404
    assert (await client.post("/b/" + "q" * 40 + "/confirm")).status_code == 404
    assert (await client.post("/b/" + "q" * 40 + "/cancel")).status_code == 404


async def test_cancel_page(client, world):
    r = await client.get(f"/b/{tok('a1')}/cancel")
    assert r.status_code == 200
    assert f'<form method="post" action="/b/{tok("a1")}/cancel">' in r.text
    for name, value in HTML_HEADERS.items():
        assert r.headers[name] == value
    # Not cancellable (already cancelled / in the past) → back to the page.
    for label in ("a3", "a6"):
        r = await client.get(f"/b/{tok(label)}/cancel")
        assert r.status_code == 303
        assert r.headers["location"] == f"/b/{tok(label)}"


async def test_get_never_dispatches_or_changes_anything(client, world, bus, redis, pg_pool):
    calls: list = []
    before = {k: await state_of(pg_pool, v) for k, v in world["ids"].items()}
    async with worker(bus, redis, "acct_a", calls), worker(bus, redis, "acct_b", calls):
        for label in ("a1", "a2", "a7", "b1"):
            await client.get(f"/b/{tok(label)}")
            await client.get(f"/b/{tok(label)}/cancel")
            # GET on the POST-only routes is refused, not treated as a press.
            assert (await client.get(f"/b/{tok(label)}/confirm")).status_code == 405
        await client.get(f"/cal/{CAL_A}.ics")
    assert calls == []
    after = {k: await state_of(pg_pool, v) for k, v in world["ids"].items()}
    assert after == before


# --------------------------------------------------------------- actions


async def test_confirm_dispatches_to_the_bookings_own_session(client, world, bus, redis):
    calls: list = []
    async with worker(bus, redis, "acct_a", calls), worker(bus, redis, "acct_b", calls):
        r = await client.post(f"/b/{tok('a1')}/confirm")
    assert r.status_code == 303
    assert r.headers["location"] == f"/b/{tok('a1')}"
    assert calls == [("acct_a", "booking_customer_action",
                      {"booking_id": world["ids"]["a1"], "action": "confirm_attendance", "via": "page"})]


async def test_cancel_dispatches_to_the_bookings_own_session(client, world, bus, redis):
    calls: list = []
    async with worker(bus, redis, "acct_a", calls), worker(bus, redis, "acct_b", calls):
        r = await client.post(f"/b/{tok('b1')}/cancel")
        assert r.status_code == 303 and r.headers["location"] == f"/b/{tok('b1')}"
        r = await client.post(f"/b/{tok('a2')}/cancel")  # a pending one can be withdrawn
        assert r.status_code == 303
    assert calls == [
        ("acct_b", "booking_customer_action",
         {"booking_id": world["ids"]["b1"], "action": "cancel", "via": "page"}),
        ("acct_a", "booking_customer_action",
         {"booking_id": world["ids"]["a2"], "action": "cancel", "via": "page"}),
    ]


async def test_actions_on_ineligible_bookings_do_not_dispatch(client, world, bus, redis):
    calls: list = []
    async with worker(bus, redis, "acct_a", calls):
        for path in (
            f"/b/{tok('a2')}/confirm",  # pending: nothing to attend yet
            f"/b/{tok('a3')}/confirm",  # cancelled
            f"/b/{tok('a3')}/cancel",   # already cancelled
            f"/b/{tok('a6')}/confirm",  # already started
            f"/b/{tok('a6')}/cancel",
            f"/b/{tok('a7')}/confirm",  # attendance already confirmed
        ):
            r = await client.post(path)
            assert r.status_code == 303, path
    assert calls == []


async def test_timeout_shows_a_friendly_503(client, world, monkeypatch):
    monkeypatch.setattr(public_app, "ACTION_TIMEOUT", 0.2)
    r = await client.post(f"/b/{tok('a1')}/confirm")  # no worker is running acct_a
    assert r.status_code == 503
    assert "We couldn't do that right now" in r.text
    assert "message Salon &lt;Anna&gt; directly" in r.text
    for name, value in HTML_HEADERS.items():
        assert r.headers[name] == value


async def test_worker_error_shows_a_friendly_503(client, world, bus, redis):
    calls: list = []
    async with worker(bus, redis, "acct_a", calls, fail=True):
        r = await client.post(f"/b/{tok('a1')}/cancel")
    assert r.status_code == 503
    assert "directly" in r.text
    assert "RuntimeError" not in r.text  # the worker's error text is not shown
    assert len(calls) == 1


async def test_unknown_routes_are_bare_404s(client):
    for path in ("/", "/docs", "/openapi.json", "/admin"):
        r = await client.get(path)
        assert r.status_code == 404 and r.text == "Not found", path
