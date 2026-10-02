"""Client sign-up with approval, the terms of service, and manager logins
(migration 0007: terms.py, owner_api.py, owner_admin_api.py, manager_*.py).

The points: sign-up is closed until the admin opens it and terms exist; a
signed-up login opens nothing until someone approves it; a required terms
version blocks every client until they accept it; a manager can do exactly
the moderation listed in manager_api.py and nothing that grants access.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest

import alerts
import controls
import owner_auth
import terms
import totp
from conftest import seed_session
from database import DIR_IN, STATUS_RECEIVED

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"
TERMS_BODY = "## 1. Rules\nBe nice.\n- no spam\n- no scams"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    import manager_api
    import manager_auth
    import owner_api

    monkeypatch.setattr(owner_auth, "_failures", {})
    monkeypatch.setattr(owner_auth, "_last_totp_step", {})
    monkeypatch.setattr(owner_api, "_pending_totp", {})
    monkeypatch.setattr(owner_api, "_signups", {})
    monkeypatch.setattr(manager_auth, "_last_totp_step", {})
    monkeypatch.setattr(manager_api, "_pending_totp", {})


def client(ip: str = "10.0.0.7") -> httpx.AsyncClient:
    import panel

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app, client=(ip, 123)), base_url="http://test")


def now_code(secret: str) -> str:
    return totp.code_at(secret, int(time.time()) // 30)


async def publish(panel_client, body: str = TERMS_BODY, **extra) -> dict:
    r = await panel_client.post("/api/platform/terms", json={"title": "Terms", "body": body, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def open_signup(panel_client) -> int:
    state = await publish(panel_client)
    r = await panel_client.put("/api/platform/signup", json={"enabled": True})
    assert r.status_code == 200 and r.json()["signup"]["open"], r.text
    return state["current"]["version"]


def signup_body(email: str = "anna@salon.lv", version: int = 1, **extra) -> dict:
    return {"email": email, "password": MINE, "display_name": "Anna", "company": "Salon Anna",
            "phone": "+371 2000 0000", "terms_version": version, "accept_terms": True, **extra}


# -------------------------------------------------------------------- terms


async def test_the_starter_terms_cannot_be_published_unfilled(panel_client):
    state = (await panel_client.get("/api/platform/terms")).json()
    assert state["current"] is None and state["signup"] == {"enabled": False, "open": False}
    r = await panel_client.post("/api/platform/terms", json={"title": "Terms", "body": state["starter"]["body"]})
    assert r.status_code == 400 and terms.PLACEHOLDER in r.json()["detail"]
    filled = state["starter"]["body"].replace(terms.PLACEHOLDER, "Acme")
    assert terms.PLACEHOLDER not in filled
    assert (await publish(panel_client, filled))["current"]["version"] == 1


async def test_sign_up_opens_only_once_terms_exist(panel_client):
    r = await panel_client.put("/api/platform/signup", json={"enabled": True})
    assert r.status_code == 400
    async with client() as anon:
        assert (await anon.get("/api/terms")).status_code == 404
        assert (await anon.post("/api/owner/signup", json=signup_body())).status_code == 403
        await open_signup(panel_client)
        options = (await anon.get("/api/owner/signup-options")).json()
        assert options == {"open": True, "terms": {"version": 1, "title": "Terms"}}
        assert (await anon.get("/api/terms")).json()["body"] == TERMS_BODY


async def test_terms_versions_and_acceptances_are_append_only(panel_client, pg_pool):
    await open_signup(panel_client)
    async with client() as anon:
        assert (await anon.post("/api/owner/signup", json=signup_body())).status_code == 200
    for sql in ("UPDATE terms_versions SET body = 'x'", "DELETE FROM terms_versions",
                "UPDATE terms_acceptances SET version = 1", "DELETE FROM terms_acceptances"):
        with pytest.raises(asyncpg.exceptions.RaiseError, match="append-only"):
            await pg_pool.execute(sql)
    # Deleting the login keeps the evidence that it accepted.
    owner_id = await pg_pool.fetchval("SELECT id FROM owners")
    assert (await panel_client.delete(f"/api/owners/{owner_id}")).status_code == 200
    row = await pg_pool.fetchrow("SELECT owner_id, username, version, ip FROM terms_acceptances")
    assert dict(row) == {"owner_id": owner_id, "username": "anna@salon.lv", "version": 1, "ip": "10.0.0.7"}


async def test_a_required_version_blocks_until_accepted(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    await panel_client.post("/api/owners", json={"username": "anna", "password": TEMP, "tenant_ids": [tenant]})
    async with client() as owner:
        await owner.post("/api/owner/login", json={"username": "anna", "password": TEMP})
        await owner.post("/api/owner/password", json={"current": TEMP, "new": MINE})
        assert (await owner.get("/api/owner/me")).status_code == 200  # no terms yet: nothing to accept

        await publish(panel_client)
        r = await owner.get("/api/owner/me")
        assert r.status_code == 403 and r.json()["detail"] == owner_auth.ACCEPT_TERMS
        assert (await owner.get("/api/owner/account")).json()["gate"] == owner_auth.ACCEPT_TERMS
        assert (await owner.post("/api/owner/terms/accept", json={"version": 0})).status_code == 409
        assert (await owner.post("/api/owner/terms/accept", json={"version": 1})).status_code == 200
        assert (await owner.get("/api/owner/me")).status_code == 200

        # A correction asks nobody to accept again ...
        await publish(panel_client, TERMS_BODY + "\nTypo fixed.", requires_acceptance=False)
        assert (await owner.get("/api/owner/me")).status_code == 200
        # ... a required version does.
        state = await publish(panel_client, TERMS_BODY + "\n- new rule", change_note="new rule")
        assert state["outstanding"] == 1
        assert (await owner.get("/api/owner/me")).status_code == 403
        assert (await owner.get("/api/owner/terms")).json()["terms"]["change_note"] == "new rule"
        assert (await owner.post("/api/owner/terms/accept", json={"version": 3})).status_code == 200
        assert (await owner.get("/api/owner/me")).status_code == 200
    assert (await panel_client.get("/api/platform/terms")).json()["outstanding"] == 0
    accepted = await pg_pool.fetch("SELECT tenant_id FROM audit_log WHERE event = 'terms_accepted'")
    assert [r["tenant_id"] for r in accepted] == [tenant, tenant]


# ------------------------------------------------------------------ sign-up


async def test_sign_up_waits_for_approval(panel_client, pg_pool):
    version = await open_signup(panel_client)
    tenant = await seed_session(pg_pool, "acct_a")
    async with client() as anna:
        r = await anna.post("/api/owner/signup", json=signup_body(version=version))
        assert r.status_code == 200 and r.json()["status"] == "pending"
        # Signed in at once, but nothing opens.
        for path in ("/api/owner/me", "/api/owner/overview", "/api/owner/unanswered"):
            r = await anna.get(path)
            assert r.status_code == 403 and r.json()["detail"] == owner_auth.PENDING_APPROVAL, path
        account = (await anna.get("/api/owner/account")).json()
        assert account["status"] == "pending" and account["gate"] == owner_auth.PENDING_APPROVAL
        assert account["terms"] == {"required": 1, "accepted": 1, "ok": True}

        listed = (await panel_client.get("/api/owners")).json()[0]
        assert (listed["status"], listed["email"], listed["company"], listed["terms_accepted"]) == \
            ("pending", "anna@salon.lv", "Salon Anna", 1)
        [alert] = await alerts.list_alerts(pg_pool, open_only=True)
        assert alert["kind"] == "signup_pending" and "anna@salon.lv" in alert["message"]

        r = await panel_client.post(f"/api/owners/{listed['id']}/approve", json={"tenant_ids": [tenant]})
        assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["tenant_ids"] == [tenant]
        me = await anna.get("/api/owner/me")
        assert me.status_code == 200 and [t["id"] for t in me.json()["tenants"]] == [tenant]
    assert await alerts.list_alerts(pg_pool, open_only=True) == []  # nobody waits any more
    events = [r["event"] for r in await pg_pool.fetch("SELECT event FROM audit_log ORDER BY id")]
    assert "owner_signup" in events and "owner_approved" in events


async def test_sign_up_refusals(panel_client, pg_pool):
    version = await open_signup(panel_client)
    async with client() as anon:
        for body, status in [
            (signup_body(accept_terms=False), 400),
            (signup_body(version=version + 1), 409),
            (signup_body(email="not-an-email"), 400),
            (signup_body(company=" "), 400),
            (signup_body(password="short"), 400),
        ]:
            r = await anon.post("/api/owner/signup", json=body)
            assert r.status_code == status, (body, r.text)
        assert (await anon.post("/api/owner/signup", json=signup_body(email="A@b.lv"))).status_code == 200
        r = await anon.post("/api/owner/signup", json=signup_body(email="a@B.lv"))
        assert r.status_code == 409 and "already exists" in r.json()["detail"]
    async with client() as anon:
        for i in range(2):
            assert (await anon.post("/api/owner/signup", json=signup_body(email=f"x{i}@b.lv"))).status_code == 200
        r = await anon.post("/api/owner/signup", json=signup_body(email="x9@b.lv"))
        assert r.status_code == 429  # three per address and hour
    async with client(ip="10.0.0.8") as other:
        await panel_client.put("/api/platform/signup", json={"enabled": False})
        assert (await other.post("/api/owner/signup", json=signup_body(email="y@b.lv"))).status_code == 403
    assert await pg_pool.fetchval("SELECT count(*) FROM owners") == 3


async def test_a_rejected_sign_up_sees_why(panel_client, pg_pool):
    version = await open_signup(panel_client)
    async with client() as anna:
        await anna.post("/api/owner/signup", json=signup_body(version=version))
        owner_id = await pg_pool.fetchval("SELECT id FROM owners")
        assert (await panel_client.post(f"/api/owners/{owner_id}/reject", json={"reason": " "})).status_code == 400
        r = await panel_client.post(f"/api/owners/{owner_id}/reject", json={"reason": "Not a business"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        assert (await anna.get("/api/owner/me")).json()["detail"] == owner_auth.REJECTED
        account = (await anna.get("/api/owner/account")).json()
        assert account["gate"] == owner_auth.REJECTED and account["review_reason"] == "Not a business"
        assert (await panel_client.post(f"/api/owners/{owner_id}/reject", json={"reason": "x"})).status_code == 409


# ----------------------------------------------------------------- managers


@asynccontextmanager
async def manager(panel_client, username: str = "mia"):
    """A manager through the first sign-in; yields the client and its TOTP secret."""
    r = await panel_client.post("/api/managers", json={"username": username, "display_name": "Mia",
                                                       "password": TEMP})
    assert r.status_code == 200, r.text
    async with client("10.0.0.20") as c:
        yield await _first_sign_in(c, username)


async def _first_sign_in(c: httpx.AsyncClient, username: str) -> tuple[httpx.AsyncClient, str]:
    r = await c.post("/api/manager/login", json={"username": username, "password": TEMP})
    assert r.json()["gate"] == "change_password"
    assert (await c.get("/api/manager/overview")).json()["detail"] == "change_password"
    assert (await c.post("/api/manager/totp", json={})).status_code == 403  # password first
    assert (await c.post("/api/manager/password", json={"current": TEMP, "new": MINE})).status_code == 200
    r = await c.get("/api/manager/overview")
    assert r.status_code == 403 and r.json()["detail"] == "setup_totp"
    setup = (await c.post("/api/manager/totp", json={})).json()
    assert (await c.post("/api/manager/totp", json={"code": "000000"})).status_code == 400
    assert (await c.post("/api/manager/totp", json={"code": now_code(setup["secret"])})).status_code == 200
    return c, setup["secret"]


async def test_a_manager_needs_a_new_password_and_an_authenticator(panel_client, pg_pool):
    import manager_auth

    async with manager(panel_client) as (c, secret):
        r = await c.get("/api/manager/overview")
        assert r.status_code == 200 and r.json()["me"]["username"] == "mia"
    manager_auth.forget_totp(await pg_pool.fetchval("SELECT id FROM managers"))
    async with client("10.0.0.21") as again:
        r = await again.post("/api/manager/login", json={"username": "mia", "password": MINE})
        assert r.status_code == 401 and r.json()["detail"] == "code_required"
        r = await again.post("/api/manager/login", json={"username": "mia", "password": MINE,
                                                         "code": now_code(secret)})
        assert r.status_code == 200 and r.json()["gate"] is None
        # Disabling the manager ends the session at once.
        manager_id = await pg_pool.fetchval("SELECT id FROM managers")
        assert (await panel_client.patch(f"/api/managers/{manager_id}", json={"disabled": True})).status_code == 200
        assert (await again.get("/api/manager/overview")).status_code == 401
    listed = (await panel_client.get("/api/managers")).json()
    assert listed[0]["disabled"] and listed[0]["totp"] and "password_hash" not in listed[0]


async def test_a_manager_pauses_and_resumes_only_what_is_theirs_to(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    async with manager(panel_client) as (c, _):
        path = f"/api/manager/tenants/{tenant}"
        assert (await c.post(path + "/pause", json={"reason": ""})).status_code == 400
        r = await c.post(path + "/pause", json={"reason": "customer complaints"})
        assert r.status_code == 200 and [h["kind"] for h in r.json()["holds"]] == [controls.MANUAL]
        assert (await c.post(path + "/pause", json={"reason": "again"})).status_code == 409
        assert await controls.off_reason(pg_pool, tenant)

        await controls.add_hold(pg_pool, tenant, controls.BILLING, "unpaid", actor="system")
        r = await c.post(path + "/resume", json={"kind": controls.BILLING, "reason": "paid, they say"})
        assert r.status_code == 403
        r = await c.post(path + "/resume", json={"kind": controls.MANUAL, "reason": "sorted out"})
        assert r.status_code == 200 and [h["kind"] for h in r.json()["holds"]] == [controls.BILLING]
        assert not r.json()["holds"][0]["resumable"]
        assert (await c.post(f"/api/manager/tenants/99999/pause", json={"reason": "x"})).status_code == 404
    actors = {r["actor"] for r in await pg_pool.fetch(
        "SELECT actor FROM audit_log WHERE event IN ('tenant_soft_off', 'tenant_resumed') AND tenant_id = $1",
        tenant)}
    assert "manager:mia" in actors


async def test_a_manager_reads_conversations_and_pauses_one(panel_client, pg_pool, db):
    tenant = await db.tenant_id()
    await db.upsert_conversation(42, "Bob", "bob", False, 1)
    await db.record_message(42, DIR_IN, STATUS_RECEIVED, "hello there", telegram_id=1)
    async with manager(panel_client) as (c, _):
        chats = (await c.get(f"/api/manager/tenants/{tenant}/conversations")).json()
        assert [x["chat_id"] for x in chats] == [42]
        thread = (await c.get(f"/api/manager/tenants/{tenant}/conversations/42/messages")).json()
        assert [m["text"] for m in thread["messages"]] == ["hello there"]
        r = await c.post(f"/api/manager/tenants/{tenant}/conversations/42/pause", json={"paused": True,
                                                                                      "reason": "abusive"})
        assert r.status_code == 200 and r.json()["automation_paused"]
        assert (await c.get(f"/api/manager/tenants/{tenant}/conversations/7/messages")).status_code == 404
    row = await pg_pool.fetchrow("SELECT actor, reason FROM audit_log WHERE event = 'chat_paused'")
    assert dict(row) == {"actor": "manager:mia", "reason": "abusive"}


async def test_a_manager_approves_but_cannot_link_a_business(panel_client, pg_pool):
    version = await open_signup(panel_client)
    tenant = await seed_session(pg_pool, "acct_a")
    async with client() as anna:
        await anna.post("/api/owner/signup", json=signup_body(version=version))
    owner_id = await pg_pool.fetchval("SELECT id FROM owners")
    async with manager(panel_client) as (c, _):
        overview = (await c.get("/api/manager/overview")).json()
        assert overview["pending_signups"] == 1
        r = await c.post(f"/api/manager/clients/{owner_id}/approve", json={"reason": "", "tenant_ids": [tenant]})
        assert r.status_code == 400  # a reason is required
        r = await c.post(f"/api/manager/clients/{owner_id}/approve",
                         json={"reason": "checked the business", "tenant_ids": [tenant]})
        assert r.status_code == 200 and r.json()["status"] == "active"
        assert r.json()["tenant_ids"] == [] and r.json()["reviewed_by"] == "manager:mia"
        r = await c.post(f"/api/manager/clients/{owner_id}/disabled", json={"disabled": True, "reason": "spam"})
        assert r.status_code == 200 and r.json()["disabled"]
        [alert] = await alerts.list_alerts(pg_pool, open_only=False, limit=5)
        r = await c.post(f"/api/manager/alerts/{alert['id']}/ack")
        assert r.status_code == 200
    assert await pg_pool.fetchval("SELECT count(*) FROM owner_sessions WHERE owner_id = $1", owner_id) == 0


async def test_the_pages_are_served(panel_client):
    for page, script in (("/owner/", "/owner/owner.js"), ("/manager/", "/manager/manager.js"),
                         ("/terms/", "/terms/terms.js")):
        r = await panel_client.get(page)
        assert r.status_code == 200 and script in r.text, page
        assert (await panel_client.get(script)).status_code == 200, script
    r = await panel_client.get("/manager", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/manager/"
