"""Security audit, WhatsApp era: every route of the panel app and of the
public booking app, checked against an explicit allow-list.

A route added without a login, or an owner-facing one mounted under the
admin's prefix, fails here by construction: the allow-lists below name
every route that may answer without a cookie, and everything else must be
401 without one, 401 with the wrong kind of cookie (owner vs admin), and
403 when a page on another site starts it. The pairing routes (the QR code
and pairing code are login credentials for the WhatsApp number) get their
own checks: admin only, never cacheable, and the credential is dropped the
moment the pairing is over.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount

import public_app
from conftest import TEST_ADMIN_PASSWORD, seed_session
from test_wa_pairing import FakeGateway, start_body

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

# The only routes of the panel app that answer without any login.
PUBLIC = {
    ("GET", "/api/login-options"), ("POST", "/api/login"), ("POST", "/api/logout"),
    ("POST", "/api/owner/login"), ("POST", "/api/owner/logout"),
    ("GET", "/owner"),  # a redirect to the dashboard page (static)
    # Sign-up and the terms of service, for people without a login yet.
    ("GET", "/api/terms"), ("GET", "/api/owner/signup-options"), ("POST", "/api/owner/signup"),
    ("POST", "/api/manager/login"), ("POST", "/api/manager/logout"),
    ("GET", "/manager"),  # a redirect to the moderator page (static)
}
# Owner (client dashboard) routes live here and nowhere else.
OWNER_PREFIX = "/api/owner/"
# Manager (moderator) routes likewise.
MANAGER_PREFIX = "/api/manager/"
# The static files (login page, scripts, the dashboard page): the one mount.
MOUNTS = {"/"}
WEBSOCKETS = {"/ws/{session_id}"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"


def all_routes(routes=None):
    import panel

    for route in panel.app.routes if routes is None else routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from all_routes(nested.routes)
        else:
            yield route


def api_routes() -> list[tuple[str, str]]:
    out = []
    for route in all_routes():
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, route.path))
    return out


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def client(ip: str = "10.0.0.9", cookies=None) -> httpx.AsyncClient:
    import panel

    transport = httpx.ASGITransport(app=panel.app, client=(ip, 123))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


async def owner_client(panel_client, tenant_ids=()) -> httpx.AsyncClient:
    """A logged-in client (password already changed). Close it with aclose()."""
    r = await panel_client.post("/api/owners", json={"username": "anna", "password": TEMP,
                                                     "tenant_ids": list(tenant_ids)})
    assert r.status_code == 200, r.text
    c = client()
    assert (await c.post("/api/owner/login", json={"username": "anna", "password": TEMP})).status_code == 200
    assert (await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})).status_code == 200
    return c


# ------------------------------------------------------------- the inventory


async def test_every_route_is_of_a_known_kind(panel_client):
    """Anything new must be an /api/ route (admin or owner), or be listed
    above. A page or endpoint of any other kind is refused here first."""
    mounts, sockets, other = set(), set(), []
    for route in all_routes():
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                if (method, route.path) in PUBLIC or route.path.startswith("/api/"):
                    continue
                other.append((method, route.path))
        elif isinstance(route, APIWebSocketRoute):
            sockets.add(route.path)
        elif isinstance(route, Mount):
            mounts.add(route.path or "/")
        else:
            other.append(type(route).__name__)
    assert other == [], other
    assert mounts == MOUNTS
    assert sockets == WEBSOCKETS
    assert len(api_routes()) > 100


async def test_anonymous_requests_get_401_on_every_non_public_route(panel_client):
    async with client() as anon:
        for method, path in api_routes():
            if (method, path) in PUBLIC:
                continue
            r = await anon.request(method, concrete(path), json={})
            assert r.status_code == 401, (method, path, r.status_code, r.text)
            # Nothing of the API may be cached by a browser or a proxy.
            assert r.headers.get("cache-control") == "no-store", (method, path)


async def test_the_owner_cookie_opens_no_admin_route(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "wa37120000001")
    owner = await owner_client(panel_client, [tenant])
    try:
        assert (await owner.get("/api/owner/me")).status_code == 200  # the cookie works
        for method, path in api_routes():
            if (method, path) in PUBLIC or path.startswith(OWNER_PREFIX):
                continue
            r = await owner.request(method, concrete(path).replace("/1", f"/{tenant}", 1), json={})
            assert r.status_code == 401, (method, path, r.status_code, r.text)
    finally:
        await owner.aclose()


async def test_the_admin_cookie_opens_no_owner_or_manager_route(panel_client):
    admin = client(cookies={"admin_token": panel_client.cookies.get("admin_token")})
    async with admin:
        assert (await admin.get("/api/sessions")).status_code == 200  # the cookie works
        for method, path in api_routes():
            if (method, path) in PUBLIC or not path.startswith((OWNER_PREFIX, MANAGER_PREFIX)):
                continue
            r = await admin.request(method, concrete(path), json={})
            assert r.status_code == 401, (method, path, r.status_code, r.text)


async def test_every_state_change_is_refused_from_another_site_even_when_logged_in(panel_client):
    """CSRF: the origin check runs before any route, so the WhatsApp pairing
    routes (and anything added later) are covered without opting in."""
    for method, path in api_routes():
        if method not in UNSAFE:
            continue
        for headers in ({"Origin": "https://evil.1-2-3-4.sslip.io"}, {"Sec-Fetch-Site": "same-site"}):
            r = await panel_client.request(method, concrete(path), json={}, headers=headers)
            assert r.status_code == 403, (method, path, headers, r.status_code)
    # The real form bodies a foreign page could post without a preflight.
    r = await panel_client.post("/api/wa/pair/start", content="phone=1", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


async def test_the_websocket_takes_only_the_admin_cookie(panel_client, pg_pool):
    from test_security_audit import ws_handshake

    tenant = await seed_session(pg_pool, "wa37120000001")
    owner = await owner_client(panel_client, [tenant])
    owner_cookie = f"owner_token={owner.cookies.get('owner_token')}"
    await owner.aclose()
    assert (await ws_handshake("/ws/wa37120000001", {}))[0]["code"] == 4401
    assert (await ws_handshake("/ws/wa37120000001", {"cookie": owner_cookie}))[0]["code"] == 4401
    admin = panel_client.cookies.get("admin_token")
    sent = await ws_handshake("/ws/wa37120000001", {"cookie": f"admin_token={admin}", "origin": "http://test"})
    assert sent[0]["type"] == "websocket.accept"


# -------------------------------------------------- the pairing credential


async def test_the_qr_code_is_admin_only_uncacheable_and_gone_once_the_pairing_ends(panel_client, pg_pool):
    import panel

    async with FakeGateway(panel.bus._redis) as gw, panel.bus.subscribe_events("wa37120000009") as live:
        r = await panel_client.post("/api/wa/pair/start", json=start_body())
        assert r.status_code == 200, r.text
        pair_id = r.json()["pair_id"]
        assert re.fullmatch(r"[0-9a-f]{32}", pair_id)  # 128 random bits, never sequential
        await gw.emit(pair_id, {"type": "qr", "session_id": "x", "pair_id": pair_id, "qr": "2@secret-qr"})
        for _ in range(100):
            state = (await panel_client.get(f"/api/wa/pair/{pair_id}")).json()
            if state["status"] == "qr":
                break
            await asyncio.sleep(0.05)
        r = await panel_client.get(f"/api/wa/pair/{pair_id}")
        assert r.json()["qr"] == "2@secret-qr"
        assert r.headers["cache-control"] == "no-store"

        # A client login never sees it; neither does anyone without a cookie.
        tenant = await seed_session(pg_pool, "acct_other")
        owner = await owner_client(panel_client, [tenant])
        try:
            assert (await owner.get(f"/api/wa/pair/{pair_id}")).status_code == 401
        finally:
            await owner.aclose()
        async with client() as anon:
            assert (await anon.get(f"/api/wa/pair/{pair_id}")).status_code == 401

        # Cancelled: the credential is dropped at once, the state stays readable.
        r = await panel_client.post(f"/api/wa/pair/{pair_id}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        state = (await panel_client.get(f"/api/wa/pair/{pair_id}")).json()
        assert state["qr"] is None and state["code"] is None
        # The account's live-events channel (every open panel tab's websocket)
        # never carried it: the pairing is polled by its id, not pushed.
        seen = []
        while (message := await live.get_message(ignore_subscribe_messages=True, timeout=0.2)) is not None:
            seen.append(message["data"])
        assert not [m for m in seen if "secret-qr" in m], seen


async def test_a_pairing_event_cannot_activate_another_account(panel_client, pg_pool):
    """The gateway's `paired` event names a session_id too; the panel must
    act on the pairing it started, never on what the event says."""
    import panel

    await seed_session(pg_pool, "wa37120000002", active=False)
    async with FakeGateway(panel.bus._redis) as gw:
        r = await panel_client.post("/api/wa/pair/start", json=start_body())
        pair_id = r.json()["pair_id"]
        await gw.emit(pair_id, {"type": "paired", "session_id": "wa37120000002", "pair_id": pair_id,
                                "jid": "1@s.whatsapp.net", "lid": None, "push_name": "<b>x</b>"})
        for _ in range(100):
            state = (await panel_client.get(f"/api/wa/pair/{pair_id}")).json()
            if state["status"] == "paired":
                break
            await asyncio.sleep(0.05)
        assert state["status"] == "paired" and state["session_id"] == "wa37120000009"
    rows = await pg_pool.fetch("SELECT session_id, is_active FROM telegram_sessions ORDER BY session_id")
    assert {r["session_id"]: r["is_active"] for r in rows} == {"wa37120000002": False, "wa37120000009": True}


# ----------------------------------------------------------- the public app


async def test_the_public_booking_app_serves_exactly_the_token_routes():
    app = public_app.create_app(pool=object(), bus=None)
    routes = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                routes.add((method, route.path))
        else:
            raise AssertionError(f"unexpected route kind on the public app: {route!r}")
    assert routes == {
        ("GET", "/healthz"),  # a bare "ok" for Docker's healthcheck
        ("GET", "/cal/{token}.ics"), ("GET", "/b/{token}"), ("GET", "/b/{token}/cancel"),
        ("POST", "/b/{token}/confirm"), ("POST", "/b/{token}/cancel"),
    }
    assert app.docs_url is None and app.openapi_url is None
