"""The client dashboard's API (owner_api.py) and the admin's client-login
management (owner_admin_api.py).

The point of most of these: an owner sees exactly the businesses linked to
their login. Another business's id is "not found", whether it is a
dashboard, a queue listing or a queue item.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

import audit
import booking_store
import controls
import owner_auth
import stats
import unanswered
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"
ZONE = "Europe/Riga"  # the default config's timezone


@pytest.fixture(autouse=True)
def fresh_owner_state(monkeypatch):
    import owner_api

    monkeypatch.setattr(owner_auth, "_failures", {})
    monkeypatch.setattr(owner_auth, "_last_totp_step", {})
    monkeypatch.setattr(owner_api, "_pending_totp", {})


@pytest_asyncio.fixture
async def two(pg_pool):
    """Two businesses, each with an unanswered message."""
    a = await seed_session(pg_pool, "acct_a", name="Salon Anna")
    b = await seed_session(pg_pool, "acct_b", name="Barber Bob")
    qa = await unanswered.record(pg_pool, tenant_id=a, session_id="acct_a", chat_id=111, message_id=None,
                                 reason=unanswered.AI_ERROR)
    qb = await unanswered.record(pg_pool, tenant_id=b, session_id="acct_b", chat_id=222, message_id=None,
                                 reason=unanswered.FALLBACK)
    return {"a": a, "b": b, "qa": qa, "qb": qb}


def owner_client(ip: str = "10.0.0.1") -> httpx.AsyncClient:
    import panel

    transport = httpx.ASGITransport(app=panel.app, client=(ip, 123))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def create_owner(panel_client, username: str, tenant_ids, password: str = TEMP) -> dict:
    r = await panel_client.post("/api/owners", json={"username": username, "display_name": username.title(),
                                                     "password": password, "tenant_ids": list(tenant_ids)})
    assert r.status_code == 200, r.text
    return r.json()


async def ready(panel_client, client, username: str, tenant_ids) -> dict:
    owner = await create_owner(panel_client, username, tenant_ids)
    r = await client.post("/api/owner/login", json={"username": username, "password": TEMP})
    assert r.status_code == 200, r.text
    r = await client.post("/api/owner/password", json={"current": TEMP, "new": MINE})
    assert r.status_code == 200, r.text
    return owner


# ----------------------------------------------------------------- isolation


async def test_an_owner_sees_only_their_own_businesses(panel_client, pg_pool, two):
    a, b = two["a"], two["b"]
    async with owner_client() as c:
        owner = await ready(panel_client, c, "anna", [a])

        me = (await c.get("/api/owner/me")).json()
        assert [t["id"] for t in me["tenants"]] == [a]

        assert (await c.get(f"/api/owner/dashboard?tenant_id={a}")).status_code == 200
        r = await c.get(f"/api/owner/dashboard?tenant_id={b}")
        assert r.status_code == 404
        assert (await c.get("/api/owner/dashboard?tenant_id=999999")).json() == r.json()

        assert (await c.get(f"/api/owner/unanswered?tenant_id={b}")).status_code == 404
        items = (await c.get("/api/owner/unanswered")).json()["items"]
        assert [i["id"] for i in items] == [two["qa"]]
        assert (await c.post(f"/api/owner/unanswered/{two['qb']}/reviewed")).status_code == 404
        assert await pg_pool.fetchval("SELECT status FROM unanswered_queue WHERE id = $1", two["qb"]) == "open"

        overview = (await c.get("/api/owner/overview")).json()
        assert [t["id"] for t in overview["tenants"]] == [a]

        # Linked to both: sees both, the new link taking effect on the same session.
        r = await panel_client.patch(f"/api/owners/{owner['id']}", json={"tenant_ids": [a, b]})
        assert r.status_code == 200 and r.json()["tenant_ids"] == sorted([a, b])
        assert sorted(t["id"] for t in (await c.get("/api/owner/me")).json()["tenants"]) == sorted([a, b])
        assert (await c.get(f"/api/owner/dashboard?tenant_id={b}")).status_code == 200
        items = (await c.get("/api/owner/unanswered")).json()["items"]
        assert sorted(i["id"] for i in items) == sorted([two["qa"], two["qb"]])
        items = (await c.get(f"/api/owner/unanswered?tenant_id={b}")).json()["items"]
        assert [i["id"] for i in items] == [two["qb"]]
        assert len((await c.get("/api/owner/overview")).json()["tenants"]) == 2
        assert (await c.post(f"/api/owner/unanswered/{two['qb']}/reviewed")).status_code == 200

        # Unlinked again: gone at once.
        await panel_client.patch(f"/api/owners/{owner['id']}", json={"tenant_ids": [a]})
        assert (await c.get(f"/api/owner/dashboard?tenant_id={b}")).status_code == 404


async def test_two_owners_do_not_see_each_other(panel_client, two):
    async with owner_client("10.0.0.1") as anna, owner_client("10.0.0.2") as bob:
        await ready(panel_client, anna, "anna", [two["a"]])
        await ready(panel_client, bob, "bob", [two["b"]])
        assert (await anna.get(f"/api/owner/dashboard?tenant_id={two['b']}")).status_code == 404
        assert (await bob.get(f"/api/owner/dashboard?tenant_id={two['a']}")).status_code == 404
        assert [i["id"] for i in (await bob.get("/api/owner/unanswered")).json()["items"]] == [two["qb"]]


# ----------------------------------------------------------------- dashboard


async def test_the_dashboard_numbers_and_bookings(panel_client, pg_pool, two):
    a = two["a"]
    now = datetime.now(timezone.utc)
    today = stats.day_start(now, ZONE)
    store = booking_store.BookingStore(pg_pool, a, "acct_a")
    kw = dict(customer_name="Mia", customer_username=None, buffer_minutes=0, tz=ZONE)
    await store.create(chat_id=5, starts_at=today + timedelta(hours=12), ends_at=today + timedelta(hours=13), **kw)
    await store.create(chat_id=6, starts_at=today + timedelta(days=2, hours=10),
                       ends_at=today + timedelta(days=2, hours=11), **kw)
    await store.create(chat_id=7, starts_at=today + timedelta(days=20), ends_at=today + timedelta(days=20, hours=1),
                       **kw)

    async with owner_client() as c:
        await ready(panel_client, c, "anna", [a])
        d = (await c.get(f"/api/owner/dashboard?tenant_id={a}")).json()
    assert d["tenant"]["name"] == "Salon Anna" and d["tenant"]["timezone"] == ZONE
    assert "session_id" not in d["tenant"]
    assert [b["chat_id"] for b in d["bookings_today"]] == [5]
    assert [b["chat_id"] for b in d["bookings_upcoming"]] == [6]  # the one in 20 days is beyond the week
    for booking in d["bookings_today"] + d["bookings_upcoming"]:
        assert "customer_token" not in booking and "customer_ref" not in booking
        assert booking["customer_name"] == "Mia" and booking["state"] == "requested"
    assert d["summary"]["week"]["bookings"]["booked"] >= 1
    assert d["summary"]["unanswered_open"] == 1 == d["unanswered_open"]
    assert len(d["weeks"]) == 8
    assert d["flagged_customers"] == []


async def test_me_shows_whether_the_bot_is_sending(panel_client, pg_pool, two):
    a = two["a"]
    async with owner_client() as c:
        await ready(panel_client, c, "anna", [a])
        t = (await c.get("/api/owner/me")).json()["tenants"][0]
        assert t["bot"] == {"sending": True, "off_reason": ""}
        assert t["health"]["status"] == "unknown"
        await controls.add_hold(pg_pool, a, controls.MANUAL, "holiday", actor=audit.ADMIN)
        t = (await c.get("/api/owner/me")).json()["tenants"][0]
        assert t["bot"] == {"sending": False, "off_reason": "paused: holiday"}


async def test_marking_reviewed_is_scoped_and_audited(panel_client, pg_pool, two):
    a = two["a"]
    async with owner_client() as c:
        await ready(panel_client, c, "anna", [a])
        r = await c.post(f"/api/owner/unanswered/{two['qa']}/reviewed")
        assert r.status_code == 200
        assert r.json()["status"] == "reviewed" and r.json()["reviewed_by"] == "owner:anna"
        assert (await c.get("/api/owner/unanswered")).json()["items"] == []
        reviewed = (await c.get("/api/owner/unanswered?status=reviewed")).json()["items"]
        assert [i["id"] for i in reviewed] == [two["qa"]]
        assert len((await c.get("/api/owner/unanswered?status=all")).json()["items"]) == 1
        assert (await c.get("/api/owner/unanswered?status=bogus")).status_code == 400
        assert (await c.post("/api/owner/unanswered/999999/reviewed")).status_code == 404
    row = await pg_pool.fetchrow("SELECT * FROM audit_log WHERE event = 'unanswered_reviewed'")
    assert row["actor"] == "owner:anna" and row["tenant_id"] == a


async def test_the_owner_page_is_served(panel_client):
    async with owner_client() as c:
        r = await c.get("/owner")
        assert r.status_code in (301, 302, 307, 308) and r.headers["location"] == "/owner/"
        r = await c.get("/owner/")
        assert r.status_code == 200 and "owner.js" in r.text
        assert (await c.get("/owner/owner.js")).status_code == 200
        assert (await c.get("/owner/owner.css")).status_code == 200


# -------------------------------------------------------------- admin CRUD


async def test_admin_creates_lists_and_edits_client_logins(panel_client, pg_pool, two):
    a, b = two["a"], two["b"]
    owner = await create_owner(panel_client, "anna@example.com", [a])
    assert owner["must_change_password"] is True and owner["tenants"] == [{"id": a, "name": "Salon Anna"}]
    assert "password_hash" not in owner and "totp_secret_enc" not in owner

    # Usernames are unique whatever the case.
    r = await panel_client.post("/api/owners", json={"username": "ANNA@example.com", "password": TEMP,
                                                     "tenant_ids": []})
    assert r.status_code == 409
    r = await panel_client.post("/api/owners", json={"username": "x", "password": TEMP})
    assert r.status_code == 400
    r = await panel_client.post("/api/owners", json={"username": "shorty", "password": "short"})
    assert r.status_code == 400
    r = await panel_client.post("/api/owners", json={"username": "ghost", "password": TEMP,
                                                     "tenant_ids": [999999]})
    assert r.status_code == 400 and "999999" in r.json()["detail"]
    assert await pg_pool.fetchval("SELECT count(*) FROM owners") == 1  # nothing half-created

    listed = (await panel_client.get("/api/owners")).json()
    assert [o["username"] for o in listed] == ["anna@example.com"]
    assert "password_hash" not in listed[0]

    r = await panel_client.patch(f"/api/owners/{owner['id']}", json={"display_name": "Anna K", "tenant_ids": [b, a]})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Anna K" and r.json()["tenant_ids"] == sorted([a, b])
    assert (await panel_client.patch(f"/api/owners/{owner['id']}", json={"tenant_ids": [999999]})).status_code == 400
    assert (await panel_client.patch("/api/owners/999999", json={"disabled": True})).status_code == 404

    rows = await pg_pool.fetch("SELECT tenant_id, actor, event FROM audit_log WHERE event LIKE 'owner_%' ORDER BY id")
    assert [(r["tenant_id"], r["actor"], r["event"]) for r in rows] == [
        (a, "admin", "owner_created"), (a, "admin", "owner_updated"), (b, "admin", "owner_updated")]


async def test_a_login_with_no_business_is_audited_as_platform(panel_client, pg_pool):
    await create_owner(panel_client, "lonely", [])
    row = await pg_pool.fetchrow("SELECT tenant_id, actor FROM audit_log WHERE event = 'owner_created'")
    assert row["tenant_id"] is None and row["actor"] == "admin"


async def test_reset_password_ends_sessions_and_forces_a_change(panel_client, pg_pool, two):
    async with owner_client() as c:
        owner = await ready(panel_client, c, "anna", [two["a"]])
        assert (await c.get("/api/owner/me")).status_code == 200
        r = await panel_client.post(f"/api/owners/{owner['id']}/reset-password", json={"password": "short"})
        assert r.status_code == 400
        r = await panel_client.post(f"/api/owners/{owner['id']}/reset-password", json={"password": "another-temp-1"})
        assert r.status_code == 200 and r.json()["must_change_password"] is True and r.json()["sessions"] == 0
        assert (await c.get("/api/owner/me")).status_code == 401
        assert (await c.post("/api/owner/login", json={"username": "anna", "password": MINE})).status_code == 401
        r = await c.post("/api/owner/login", json={"username": "anna", "password": "another-temp-1"})
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        assert (await c.get("/api/owner/me")).status_code == 403
    assert await pg_pool.fetchval("SELECT count(*) FROM audit_log WHERE event = 'owner_password_reset'") == 1


async def test_deleting_a_login_ends_it(panel_client, pg_pool, two):
    async with owner_client() as c:
        owner = await ready(panel_client, c, "anna", [two["a"]])
        assert (await panel_client.delete(f"/api/owners/{owner['id']}")).json() == {"ok": True}
        assert (await c.get("/api/owner/me")).status_code == 401
        assert (await c.post("/api/owner/login", json={"username": "anna", "password": MINE})).status_code == 401
    assert await pg_pool.fetchval("SELECT count(*) FROM owner_sessions") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM owner_tenants") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM tenants") == 2  # the businesses stay
    assert (await panel_client.delete(f"/api/owners/{owner['id']}")).status_code == 404


async def test_the_admin_routes_need_the_admin_login(panel_client, two):
    await panel_client.post("/api/logout")
    for method, path in (("GET", "/api/owners"), ("POST", "/api/owners"), ("PATCH", "/api/owners/1"),
                         ("POST", "/api/owners/1/reset-password"), ("DELETE", "/api/owners/1/totp"),
                         ("DELETE", "/api/owners/1")):
        assert (await panel_client.request(method, path, json={})).status_code == 401, path
