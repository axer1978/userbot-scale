"""Staff roles and the approval queue (staff.py, staff_api.py, migration 0009).

The points: every route a manager could reach belongs to an action; a role
lets a manager do exactly its 'allow' actions; an 'approve' change is not
done but answered like a success, and runs when the admin approves; a
protective change runs at once even then; nobody but the admin reaches
roles, staff or the queue.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.routing import APIRoute

import controls
import owner_auth
import staff
import totp
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"
SIGNUP = {"password": MINE, "display_name": "Anna", "company": "Salon", "terms_version": 1,
          "accept_terms": True}
# Routes of the moderator panel that every manager has: their own login.
MANAGER_OWN = {"/api/manager/login", "/api/manager/logout", "/api/manager/account", "/api/manager/password",
               "/api/manager/totp"}
PUBLIC = {"/api/login", "/api/login-options", "/api/logout", "/api/terms", "/api/me"}


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


def client() -> httpx.AsyncClient:
    import panel

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app, client=("10.0.0.30", 1)),
                             base_url="http://test")


def code(secret: str) -> str:
    import manager_auth

    manager_auth._last_totp_step.clear()  # a test signs in twice within one 30 s step
    return totp.code_at(secret, int(time.time()) // 30)


async def role_id(pg_pool, name: str) -> int:
    return await pg_pool.fetchval("SELECT id FROM staff_roles WHERE name = $1", name)


@asynccontextmanager
async def staff_member(panel_client, pg_pool, role: str, *, admin_panel_login: bool):
    """A manager with this role, set up at /manager/; optionally signed in
    to the admin panel too (same client, both cookies)."""
    r = await panel_client.post("/api/managers", json={"username": "mia", "password": TEMP,
                                                       "role_id": await role_id(pg_pool, role)})
    assert r.status_code == 200, r.text
    async with client() as c:
        await c.post("/api/manager/login", json={"username": "mia", "password": TEMP})
        await c.post("/api/manager/password", json={"current": TEMP, "new": MINE})
        secret = (await c.post("/api/manager/totp", json={})).json()["secret"]
        assert (await c.post("/api/manager/totp", json={"code": code(secret)})).status_code == 200
        if admin_panel_login:
            r = await c.post("/api/login", json={"username": "mia", "password": MINE, "code": code(secret)})
            assert r.status_code == 200, r.text
        yield c


async def pending_signup(panel_client, pg_pool) -> int:
    await panel_client.post("/api/platform/terms", json={"title": "T", "body": "Rules."})
    await panel_client.put("/api/platform/signup", json={"enabled": True})
    async with client() as anon:
        r = await anon.post("/api/owner/signup", json={**SIGNUP, "email": "anna@salon.lv"})
        assert r.status_code == 200, r.text
    return await pg_pool.fetchval("SELECT id FROM owners")


# ---------------------------------------------------------------- coverage


def _routes():
    import panel

    def walk(rs):
        for r in rs:
            nested = getattr(r, "original_router", None)
            if nested is not None:
                yield from walk(nested.routes)
            else:
                yield r

    for r in walk(panel.app.routes):
        if isinstance(r, APIRoute) and r.path.startswith("/api/"):
            for method in r.methods - {"HEAD"}:
                yield method, r.path


async def test_every_staff_reachable_route_has_an_action(panel_client):
    unmapped = []
    for method, path in _routes():
        if path in PUBLIC or path in MANAGER_OWN or path.startswith(("/api/owner/",) + staff.ADMIN_ONLY_PREFIXES):
            continue
        if (method, path) not in staff.ROUTES:
            unmapped.append((method, path))
    assert unmapped == []
    real = set(_routes())
    assert [key for key in staff.ROUTES if key not in real] == []


# ------------------------------------------------------------ the admin panel


async def test_a_moderator_role_cannot_use_the_admin_panel(panel_client, pg_pool):
    async with staff_member(panel_client, pg_pool, "Moderator", admin_panel_login=False) as c:
        secret_row = await pg_pool.fetchval("SELECT id FROM managers")
        assert secret_row
        r = await c.post("/api/login", json={"username": "mia", "password": MINE, "code": "000000"})
        assert r.status_code == 401
        assert (await c.get("/api/sessions")).status_code == 401  # the manager cookie alone opens nothing here
        assert (await c.get("/api/manager/overview")).status_code == 200


async def test_a_senior_moderator_sees_only_what_the_role_allows(panel_client, pg_pool):
    await seed_session(pg_pool, "acct_a")
    async with staff_member(panel_client, pg_pool, "Senior moderator", admin_panel_login=True) as c:
        me = (await c.get("/api/me")).json()
        assert me["admin"] is False and me["role"] == "Senior moderator"
        assert (await c.get("/api/sessions")).status_code == 200
        assert (await c.get("/api/platform/terms")).status_code == 403  # view.terms is off
        assert (await c.put("/api/tenants/1/config", json={})).status_code == 403  # config.client is off
        for path in ("/api/managers", "/api/staff/roles", "/api/staff/requests", "/api/finetune/runs",
                     "/api/finetune/industries/1"):
            assert (await c.get(path)).status_code == 403, path
        assert (await c.post("/api/staff/roles", json={"name": "x"})).status_code == 403
        assert (await c.post("/api/finetune/runs", json={"tenant_id": 1, "images": []})).status_code == 403


async def test_a_change_for_approval_looks_done_and_waits(panel_client, pg_pool):
    owner_id = await pending_signup(panel_client, pg_pool)
    async with staff_member(panel_client, pg_pool, "Senior moderator", admin_panel_login=True) as c:
        r = await c.post(f"/api/owners/{owner_id}/approve", json={"reason": "looks fine"})
        assert r.status_code == 200 and r.json()["ok"] is True  # silent
        assert await pg_pool.fetchval("SELECT status FROM owners") == "pending"  # not done
        queue = (await panel_client.get("/api/staff/requests")).json()
        [item] = queue["requests"]
        assert queue["pending"] == 1 and item["action"] == "clients.approve" and item["username"] == "mia"
        assert "looks fine" in item["body"]

        r = await panel_client.post(f"/api/staff/requests/{item['id']}/approve", json={})
        assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["result_status"] == 200
        assert await pg_pool.fetchval("SELECT status FROM owners") == "active"
        assert (await panel_client.post(f"/api/staff/requests/{item['id']}/approve", json={})).status_code == 409


async def test_protective_changes_run_at_once_and_resuming_waits(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    async with staff_member(panel_client, pg_pool, "Senior moderator", admin_panel_login=True) as c:
        r = await c.post(f"/api/tenants/{tenant}/soft-off", json={"reason": "spam reports"})
        assert r.status_code == 200
        assert controls.MANUAL in [h["kind"] for h in await controls.holds(pg_pool, tenant)]
        r = await c.post(f"/api/tenants/{tenant}/resume", json={"kind": "manual", "reason": "ok now"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert controls.MANUAL in [h["kind"] for h in await controls.holds(pg_pool, tenant)]  # still paused
        log = (await panel_client.get("/api/staff/requests", params={"status": "all"})).json()["requests"]
        assert [(x["status"], x["note"]) for x in log] == [("pending", ""), ("applied", "protective: done at once")]
        r = await panel_client.post(f"/api/staff/requests/{log[0]['id']}/reject", json={"note": "keep it off"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        assert controls.MANUAL in [h["kind"] for h in await controls.holds(pg_pool, tenant)]


async def test_role_changes_apply_at_once(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    async with staff_member(panel_client, pg_pool, "Senior moderator", admin_panel_login=True) as c:
        roles = (await panel_client.get("/api/staff/roles")).json()
        senior = next(r for r in roles["roles"] if r["name"] == "Senior moderator")
        assert senior["members"] == 1 and roles["catalogue"]
        perms = {**senior["permissions"], "tenant.pause": "allow", "view.safety": "off", "bogus": "allow"}
        r = await panel_client.put(f"/api/staff/roles/{senior['id']}",
                                   json={**senior, "permissions": perms})
        assert r.status_code == 200
        saved = r.json()["permissions"]
        assert "bogus" not in saved and "view.safety" not in saved and saved["tenant.pause"] == "allow"
        assert (await c.get("/api/safety")).status_code == 403
        r = await c.post(f"/api/tenants/{tenant}/resume", json={"kind": "manual", "reason": "x"})
        assert r.status_code == 404  # allowed now: it really ran (and there was no hold to lift)
        assert (await panel_client.delete(f"/api/staff/roles/{senior['id']}")).status_code == 409


# --------------------------------------------------------- the moderator panel


async def test_the_moderator_panel_follows_the_role_too(panel_client, pg_pool):
    owner_id = await pending_signup(panel_client, pg_pool)
    moderator = await role_id(pg_pool, "Moderator")
    roles = (await panel_client.get("/api/staff/roles")).json()["roles"]
    role = next(r for r in roles if r["id"] == moderator)
    await panel_client.put(f"/api/staff/roles/{moderator}",
                           json={**role, "permissions": {**role["permissions"], "clients.approve": "approve",
                                                         "alerts.ack": "off"}})
    async with staff_member(panel_client, pg_pool, "Moderator", admin_panel_login=False) as c:
        account = (await c.get("/api/manager/account")).json()
        assert account["role"] == "Moderator" and account["permissions"]["clients.approve"] == "approve"
        assert (await c.post("/api/manager/alerts/1/ack")).status_code == 403
        r = await c.post(f"/api/manager/clients/{owner_id}/approve", json={"reason": "checked"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert await pg_pool.fetchval("SELECT status FROM owners") == "pending"
        [item] = (await panel_client.get("/api/staff/requests")).json()["requests"]
        r = await panel_client.post(f"/api/staff/requests/{item['id']}/approve", json={})
        assert r.json()["status"] == "approved", r.text
    row = await pg_pool.fetchrow("SELECT status, reviewed_by FROM owners")
    assert dict(row) == {"status": "active", "reviewed_by": "manager:mia"}  # done as the manager
