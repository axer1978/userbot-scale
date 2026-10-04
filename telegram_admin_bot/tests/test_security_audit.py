"""Security audit before the panel goes public (2026-09-29).

One regression test per finding, plus sweeping checks over every route:
- every /api/* route needs the right login (admin vs client), and the
  websocket too;
- cross-origin (incl. same-site: every *.sslip.io host is one "site")
  state-changing requests and websockets are refused, as are form-type
  bodies on the API;
- request bodies are size-limited;
- Secure cookies use the __Host- prefix, so a sibling domain can't plant one;
- a server error shows its text only to a logged-in admin;
- the API map (/docs, /openapi.json) is not served;
- the CSP lets the socket reach this host only.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.routing import APIRoute
from starlette.requests import Request

import owner_auth
import totp
import unanswered
from conftest import TEST_ADMIN_PASSWORD, seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"

# The only /api/* routes anyone may call without a login.
PUBLIC_API = {"/api/login", "/api/login-options", "/api/logout", "/api/owner/login", "/api/owner/logout",
              "/api/owner/signup", "/api/owner/signup-options", "/api/terms",
              "/api/manager/login", "/api/manager/logout"}
# Routes behind their own login rather than the admin's.
OWN_LOGIN = ("/api/owner/", "/api/manager/")


@pytest.fixture(autouse=True)
def fresh_owner_state(monkeypatch):
    import manager_api
    import manager_auth
    import owner_api

    monkeypatch.setattr(owner_auth, "_failures", {})
    monkeypatch.setattr(owner_auth, "_last_totp_step", {})
    monkeypatch.setattr(owner_api, "_pending_totp", {})
    monkeypatch.setattr(owner_api, "_signups", {})
    monkeypatch.setattr(manager_auth, "_last_totp_step", {})
    monkeypatch.setattr(manager_api, "_pending_totp", {})


def client(ip: str = "10.0.0.9", **kwargs) -> httpx.AsyncClient:
    import panel

    transport = httpx.ASGITransport(app=panel.app, client=(ip, 123), **kwargs)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@asynccontextmanager
async def owner_session(panel_client, tenant_ids=(), username: str = "anna"):
    """A logged-in client (password already changed)."""
    r = await panel_client.post("/api/owners", json={"username": username, "password": TEMP,
                                                     "tenant_ids": list(tenant_ids)})
    assert r.status_code == 200, r.text
    c = client()
    assert (await c.post("/api/owner/login", json={"username": username, "password": TEMP})).status_code == 200
    try:
        assert (await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})).status_code == 200
        yield c
    finally:
        await c.aclose()


def all_routes(routes=None):
    """Every route of the app, including those of included routers (newer
    FastAPI keeps those nested; none of them is mounted with a prefix)."""
    import panel

    for route in panel.app.routes if routes is None else routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from all_routes(nested.routes)
        else:
            yield route


def api_routes():
    for route in all_routes():
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            for method in sorted(route.methods - {"HEAD"}):
                yield method, route.path


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


# ------------------------------------------------------------ authn / authz


async def test_every_api_route_needs_a_login(panel_client):
    checked = 0
    async with client() as anon:
        for method, path in api_routes():
            if path in PUBLIC_API:
                continue
            r = await anon.request(method, concrete(path))
            assert r.status_code == 401, (method, path, r.status_code, r.text)
            checked += 1
    assert checked > 100  # the sweep really covered the app


async def test_a_client_login_opens_no_admin_route(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    async with owner_session(panel_client, [tenant]) as owner:
        assert (await owner.get("/api/owner/me")).status_code == 200  # the cookie is live
        for method, path in api_routes():
            if path in PUBLIC_API or path.startswith("/api/owner/"):
                continue
            r = await owner.request(method, concrete(path).replace("/1", f"/{tenant}", 1))
            assert r.status_code == 401, (method, path, r.status_code)


async def test_the_admin_login_opens_no_client_or_manager_route(panel_client):
    admin_only = httpx.AsyncClient(transport=panel_client._transport, base_url="http://test",
                                   cookies={"admin_token": panel_client.cookies.get("admin_token")})
    async with admin_only:
        assert (await admin_only.get("/api/sessions")).status_code == 200
        for method, path in api_routes():
            if path in PUBLIC_API or not path.startswith(OWN_LOGIN):
                continue
            r = await admin_only.request(method, concrete(path), json={})
            assert r.status_code == 401, (method, path, r.status_code)


async def manager_session(panel_client, username: str = "mia") -> httpx.AsyncClient:
    """A manager past the password change and the authenticator setup."""
    r = await panel_client.post("/api/managers", json={"username": username, "password": TEMP})
    assert r.status_code == 200, r.text
    c = client()
    assert (await c.post("/api/manager/login", json={"username": username, "password": TEMP})).status_code == 200
    assert (await c.post("/api/manager/password", json={"current": TEMP, "new": MINE})).status_code == 200
    setup = (await c.post("/api/manager/totp", json={})).json()
    r = await c.post("/api/manager/totp", json={"code": totp.code_at(setup["secret"], int(time.time()) // 30)})
    assert r.status_code == 200, r.text
    return c


async def test_a_manager_login_opens_no_admin_or_client_route(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    manager = await manager_session(panel_client)
    try:
        assert (await manager.get("/api/manager/overview")).status_code == 200  # the cookie is live
        for method, path in api_routes():
            if path in PUBLIC_API or path.startswith("/api/manager/"):
                continue
            r = await manager.request(method, concrete(path).replace("/1", f"/{tenant}", 1), json={})
            assert r.status_code == 401, (method, path, r.status_code)
    finally:
        await manager.aclose()


async def test_a_client_login_opens_no_manager_route(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    async with owner_session(panel_client, [tenant]) as owner:
        for method, path in api_routes():
            if path in PUBLIC_API or not path.startswith("/api/manager/"):
                continue
            r = await owner.request(method, concrete(path), json={})
            assert r.status_code == 401, (method, path, r.status_code)


async def ws_handshake(path: str, headers: dict[str, str]) -> list[dict]:
    """Drives panel.app's websocket route directly; returns what it sent."""
    import panel

    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in {"host": "test", **headers}.items()],
        "client": ("10.0.0.9", 1), "server": ("test", 80), "subprotocols": [],
    }
    inbox = [{"type": "websocket.connect"}]
    sent: list[dict] = []

    async def receive():
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await asyncio.wait_for(panel.app(scope, receive, send), 5)
    return sent


async def test_the_websocket_needs_the_admin_login_and_this_origin(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    admin = panel_client.cookies.get("admin_token")
    async with owner_session(panel_client, [tenant]) as owner:
        owner_cookie = f"owner_token={owner.cookies.get('owner_token')}"

    assert (await ws_handshake("/ws/acct_a", {}))[0] == {"type": "websocket.close", "code": 4401, "reason": ""}
    assert (await ws_handshake("/ws/acct_a", {"cookie": owner_cookie}))[0]["code"] == 4401
    # Another site, even a "same-site" sibling like another *.sslip.io host,
    # can't open the socket with the admin's cookie riding along.
    for bad in ({"origin": "https://evil.1-2-3-4.sslip.io"}, {"sec-fetch-site": "same-site"},
                {"origin": "null"}):
        sent = await ws_handshake("/ws/acct_a", {"cookie": f"admin_token={admin}", **bad})
        assert sent[0]["type"] == "websocket.close" and sent[0]["code"] == 4403, bad
    # This origin, logged in: accepted and greeted.
    sent = await ws_handshake("/ws/acct_a", {"cookie": f"admin_token={admin}", "origin": "http://test"})
    assert sent[0]["type"] == "websocket.accept"


# --------------------------------------------------------------------- CSRF


async def test_cross_origin_state_changes_are_refused(panel_client, pg_pool):
    body = {"username": "mallory", "password": TEMP, "tenant_ids": []}
    for headers in ({"Origin": "https://evil.1-2-3-4.sslip.io"}, {"Sec-Fetch-Site": "same-site"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
        r = await panel_client.post("/api/owners", json=body, headers=headers)
        assert r.status_code == 403, headers
        # Login CSRF: logging a victim in to the attacker's account.
        r = await panel_client.post("/api/owner/login", json={"username": "x", "password": "y"}, headers=headers)
        assert r.status_code == 403, headers
    assert await pg_pool.fetchval("SELECT count(*) FROM owners") == 0
    # The panel's own pages (same origin) work.
    r = await panel_client.post("/api/owners", json=body,
                                headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text
    # A plain GET from anywhere is still just a read (nothing to forge).
    assert (await panel_client.get("/api/owners", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 200


async def test_form_type_bodies_are_refused_on_the_api(panel_client, pg_pool):
    import json

    body = json.dumps({"username": "mallory", "password": TEMP, "tenant_ids": []})
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        r = await panel_client.post("/api/owners", content=body, headers={"Content-Type": ctype})
        assert r.status_code == 415, ctype
    assert await pg_pool.fetchval("SELECT count(*) FROM owners") == 0


async def test_state_changing_routes_do_not_run_on_get(panel_client):
    get_paths = {r.path for r in all_routes() if isinstance(r, APIRoute) and "GET" in r.methods}
    for method, path in api_routes():
        if method == "GET" or path in get_paths:
            continue
        r = await panel_client.get(concrete(path))
        assert r.status_code in (404, 405), (path, r.status_code)


async def test_no_cors_headers(panel_client):
    r = await panel_client.options("/api/sessions", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    r = await panel_client.get("/api/sessions", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------- body size


async def test_oversized_bodies_are_refused(panel_client, monkeypatch):
    import panel

    async with client() as anon:
        big = b"x" * (panel.MAX_BODY_BYTES + 1)
        r = await anon.post("/api/login", content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413

        async def chunks():  # no Content-Length: counted as it streams
            for _ in range(3):
                yield b" " * (panel.MAX_BODY_BYTES // 2)

        r = await anon.post("/api/owner/login", content=chunks(), headers={"Content-Type": "application/json"})
        assert r.status_code == 413


async def test_media_upload_size_and_name(panel_client, pg_pool, monkeypatch):
    import panel

    await seed_session(pg_pool, "acct_a")
    monkeypatch.setattr(panel, "MAX_UPLOAD_BYTES", 1000)
    url = "/api/sessions/acct_a/media/upload"
    headers = {"Content-Type": "application/octet-stream"}
    r = await panel_client.put(url, params={"name": "big.png"}, content=b"x" * 1001, headers=headers)
    assert r.status_code == 413

    async def chunks():
        for _ in range(3):
            yield b"x" * 600

    r = await panel_client.put(url, params={"name": "big.png"}, content=chunks(), headers=headers)
    assert r.status_code == 413
    # A name that only looks like a photo before it is cleaned.
    for name in ("x.png/", "..png", "../../x.png/"):
        r = await panel_client.put(url, params={"name": name}, content=b"x", headers=headers)
        assert r.status_code == 400, name
    # Path parts are stripped; the file lands inside the library.
    r = await panel_client.put(url, params={"name": "../../evil.png"}, content=b"x", headers=headers)
    assert r.status_code == 200 and r.json()["file"] == "evil.png"
    media_dir = (await panel.tenant_dir("acct_a")) / "media"
    assert (media_dir / "evil.png").is_file()
    assert not any(p.name.endswith(".part") for p in media_dir.iterdir())
    listed = await panel_client.get("/api/sessions/acct_a/media")
    assert [m["file"] for m in listed.json()] == ["evil.png"]


# ------------------------------------------------------------------ cookies


async def test_secure_cookies_use_the_host_prefix(panel_client, pg_pool, monkeypatch):
    import panel

    tenant = await seed_session(pg_pool, "acct_a")
    await panel_client.post("/api/owners", json={"username": "anna", "password": TEMP, "tenant_ids": [tenant]})
    monkeypatch.setattr(panel, "HOST", "0.0.0.0")
    monkeypatch.setenv("ADMIN_HOST", "0.0.0.0")
    async with client() as c:
        r = await c.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-admin_token=")
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie and "Domain" not in cookie
        token = cookie.split(";")[0].split("=", 1)[1]
        # The prefixed name opens the panel; the same token under the bare
        # name (which a sibling domain could have planted) opens nothing.
        assert (await c.get("/api/sessions", headers={"Cookie": f"__Host-admin_token={token}"})).status_code == 200
        c.cookies.clear()
        assert (await c.get("/api/sessions", headers={"Cookie": f"admin_token={token}"})).status_code == 401

        r = await c.post("/api/owner/login", json={"username": "anna", "password": TEMP})
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-owner_token=")
        assert "Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie
        otoken = cookie.split(";")[0].split("=", 1)[1]
        c.cookies.clear()
        assert (await c.get("/api/owner/me", headers={"Cookie": f"owner_token={otoken}"})).status_code == 401
        r = await c.get("/api/owner/me", headers={"Cookie": f"__Host-owner_token={otoken}"})
        assert r.status_code == 403 and r.json()["detail"] == owner_auth.CHANGE_PASSWORD  # logged in


async def test_logging_in_again_retires_the_old_session(panel_client, pg_pool):
    import panel

    old_admin = panel_client.cookies.get("admin_token")
    assert (await panel_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})).status_code == 200
    assert panel_client.cookies.get("admin_token") != old_admin
    assert old_admin not in panel._valid_tokens

    tenant = await seed_session(pg_pool, "acct_a")
    async with owner_session(panel_client, [tenant]) as owner:
        before = await pg_pool.fetchval("SELECT count(*) FROM owner_sessions")
        assert (await owner.post("/api/owner/login", json={"username": "anna", "password": MINE})).status_code == 200
        assert await pg_pool.fetchval("SELECT count(*) FROM owner_sessions") == before


# ------------------------------------------------------------ error details


def fake_request(cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/api/owner/me", "headers": headers,
                    "query_string": b"", "scheme": "http", "server": ("test", 80)})


async def test_server_errors_show_their_text_only_to_the_admin(panel_client):
    import json

    import panel

    exc = RuntimeError("connect to postgresql://userbot:s3cret@postgres/userbot failed")
    anon = await panel.unhandled(fake_request(), exc)
    assert anon.status_code == 500
    assert "s3cret" not in anon.body.decode() and "RuntimeError" not in anon.body.decode()
    owner = await panel.unhandled(fake_request("owner_token=whatever"), exc)
    assert "s3cret" not in owner.body.decode()
    admin = await panel.unhandled(fake_request(f"admin_token={panel_client.cookies.get('admin_token')}"), exc)
    assert json.loads(admin.body)["detail"].startswith("RuntimeError: ")


async def test_an_out_of_range_id_is_a_clean_answer_for_a_client(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    async with owner_session(panel_client, [tenant]) as owner:
        r = await owner.post("/api/owner/unanswered/99999999999/reviewed")
        assert r.status_code == 422


# ---------------------------------------------------------- tenant isolation


async def test_a_client_with_no_business_sees_nothing(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct_a")
    item = await unanswered.record(pg_pool, tenant_id=tenant, session_id="acct_a", chat_id=5,
                                   message_id=None, reason=unanswered.SKIPPED)
    async with owner_session(panel_client, []) as owner:
        for status in ("open", "reviewed", "all"):
            r = await owner.get("/api/owner/unanswered", params={"status": status})
            assert r.status_code == 200 and r.json() == {"items": []}
        assert (await owner.post(f"/api/owner/unanswered/{item}/reviewed")).status_code == 404
        assert (await owner.get("/api/owner/dashboard", params={"tenant_id": tenant})).status_code == 404
        assert (await owner.get("/api/owner/unanswered", params={"tenant_id": tenant})).status_code == 404
        assert (await owner.get("/api/owner/overview")).json() == {"tenants": []}
    assert await pg_pool.fetchval("SELECT status FROM unanswered_queue WHERE id = $1", item) == "open"


# ------------------------------------------------------------------ headers


async def test_the_api_map_is_not_served(panel_client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = await panel_client.get(path)
        assert r.status_code == 404, path


async def test_headers_on_static_files_and_the_api_and_a_host_bound_csp(panel_client):
    for path in ("/", "/js/core.js", "/owner/", "/owner/owner.js", "/api/sessions", "/api/login-options"):
        r = await panel_client.get(path)
        assert r.status_code == 200, path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
        # The socket may reach this host, and no other.
        assert "connect-src 'self' wss://test ws://test;" in csp
        assert " wss:;" not in csp and " ws:;" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert (await panel_client.get("/api/sessions")).headers["cache-control"] == "no-store"
    # A Host header that isn't a plain host name never makes it into the header.
    r = await panel_client.get("/", headers={"Host": "evil.example; script-src *"})
    assert "connect-src 'self';" in r.headers["content-security-policy"]
    assert "evil" not in r.headers["content-security-policy"]


# --------------------------------------------------------------------- TOTP


async def test_totp_ignores_non_ascii_digits():
    secret = totp.new_secret()
    code = totp.code_at(secret, 1000)
    arabic = "".join(chr(0x0660 + int(d)) for d in code)
    assert totp.matching_counter(secret, arabic, now=1000 * 30) is None
    assert totp.matching_counter(secret, "²" * 6, now=1000 * 30) is None
    assert totp.matching_counter(secret, code, now=1000 * 30) == 1000


async def test_admin_login_with_odd_digits_is_a_plain_failure(panel_client, monkeypatch):
    import panel

    monkeypatch.setattr(panel, "ADMIN_TOTP_SECRET", totp.new_secret())
    async with client() as c:
        r = await c.post("/api/login", json={"password": TEST_ADMIN_PASSWORD, "code": "١٢٣٤٥٦"})
        assert r.status_code == 401
