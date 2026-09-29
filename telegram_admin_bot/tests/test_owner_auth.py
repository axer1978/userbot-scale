"""Client logins (owner_auth.py, the login routes in owner_api.py).

The owner login is a world apart from the admin's: its own cookie, its own
sessions, and neither cookie opens the other's routes. Passwords are scrypt
hashes, failures are rate-limited per IP and per username, a disabled owner
is locked out at once, a temporary password must be changed before anything
else works, and an authenticator code works once.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import pytest_asyncio

import owner_auth
import totp
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TEMP = "temporary-pass-1"
MINE = "my-own-password-2"


class FakeClock:
    """Stands in for the `time` module inside totp.py, so codes are
    computed and checked at a time the test chooses."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def fresh_owner_state(monkeypatch):
    import owner_api

    monkeypatch.setattr(owner_auth, "_failures", {})
    monkeypatch.setattr(owner_auth, "_last_totp_step", {})
    monkeypatch.setattr(owner_api, "_pending_totp", {})


@pytest_asyncio.fixture
async def tenant(pg_pool):
    return await seed_session(pg_pool, "acct_a", name="Salon Anna")


def owner_client(ip: str = "10.0.0.1") -> httpx.AsyncClient:
    """A browser with no admin cookie, on the same app as panel_client."""
    import panel

    transport = httpx.ASGITransport(app=panel.app, client=(ip, 123))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def create_owner(panel_client, username: str = "anna", tenant_ids=(), password: str = TEMP) -> dict:
    r = await panel_client.post("/api/owners", json={"username": username, "display_name": "Anna",
                                                     "password": password, "tenant_ids": list(tenant_ids)})
    assert r.status_code == 200, r.text
    return r.json()


async def login(client, username: str = "anna", password: str = TEMP, code: str = "") -> httpx.Response:
    return await client.post("/api/owner/login", json={"username": username, "password": password, "code": code})


async def ready_owner(panel_client, client, tenant_ids=(), username: str = "anna") -> dict:
    """Created, logged in, temporary password already changed to MINE."""
    owner = await create_owner(panel_client, username, tenant_ids)
    assert (await login(client, username)).status_code == 200
    r = await client.post("/api/owner/password", json={"current": TEMP, "new": MINE})
    assert r.status_code == 200, r.text
    return owner


# ------------------------------------------------------------------ passwords


async def test_password_hash_round_trip():
    stored = owner_auth.hash_password("correct horse battery")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2 ** 14), "8", "1")
    assert owner_auth.verify_password("correct horse battery", stored)
    assert not owner_auth.verify_password("correct horse batterY", stored)
    assert not owner_auth.verify_password("", stored)
    # Salted: the same password never hashes the same twice.
    assert owner_auth.hash_password("correct horse battery") != stored
    # Garbage in the column is a failed check, not a crash.
    assert not owner_auth.verify_password("x", "not-a-hash")
    assert not owner_auth.verify_password("x", "bcrypt$1$2$3$AAAA$AAAA")


async def test_password_rules():
    with pytest.raises(ValueError):
        owner_auth.check_new_password("short")
    owner_auth.check_new_password("ten chars!")


# ---------------------------------------------------------------- login flow


async def test_login_change_password_and_logout(panel_client, pg_pool, tenant):
    owner = await create_owner(panel_client, tenant_ids=[tenant])
    assert owner["must_change_password"] is True
    async with owner_client() as c:
        r = await login(c)
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("owner_token=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Secure" not in cookie  # the panel listens on loopback in tests

        # Only the hash of the token is stored.
        token = c.cookies.get("owner_token")
        stored = await pg_pool.fetchval("SELECT token_hash FROM owner_sessions")
        assert stored == hashlib.sha256(token.encode()).hexdigest() != token

        # The temporary password opens nothing but the change itself.
        for path in ("/api/owner/me", f"/api/owner/dashboard?tenant_id={tenant}", "/api/owner/overview",
                     "/api/owner/unanswered"):
            r = await c.get(path)
            assert r.status_code == 403 and r.json()["detail"] == "change_password", path

        r = await c.post("/api/owner/password", json={"current": "wrong-password", "new": MINE})
        assert r.status_code == 400
        r = await c.post("/api/owner/password", json={"current": TEMP, "new": "short"})
        assert r.status_code == 400
        r = await c.post("/api/owner/password", json={"current": TEMP, "new": TEMP})
        assert r.status_code == 400
        assert (await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})).status_code == 200

        me = (await c.get("/api/owner/me")).json()
        assert me["username"] == "anna" and [t["id"] for t in me["tenants"]] == [tenant]
        assert "password_hash" not in me and "totp_secret_enc" not in me

        assert (await c.post("/api/owner/logout")).status_code == 200
        assert (await c.get("/api/owner/me")).status_code == 401
        assert await pg_pool.fetchval("SELECT count(*) FROM owner_sessions") == 0

        # The old password is gone, the new one works; usernames match in any case.
        assert (await login(c)).status_code == 401
        assert (await login(c, "ANNA", MINE)).status_code == 200

    events = [r["event"] for r in await pg_pool.fetch(
        "SELECT event FROM audit_log WHERE actor = 'owner:anna' ORDER BY id")]
    assert events == ["owner_login", "owner_password_changed", "owner_login"]


async def test_wrong_username_and_wrong_password_look_the_same(panel_client, tenant):
    await create_owner(panel_client, tenant_ids=[tenant])
    async with owner_client() as c:
        a = await login(c, "nobody", TEMP)
        b = await login(c, "anna", "wrong-password")
        assert a.status_code == b.status_code == 401
        assert a.json() == b.json() == {"detail": "Wrong username or password"}
        assert "owner_token" not in c.cookies


async def test_changing_the_password_ends_other_sessions(panel_client, tenant):
    await create_owner(panel_client, tenant_ids=[tenant])
    async with owner_client() as phone, owner_client() as laptop:
        await login(phone)
        await login(laptop)
        assert (await phone.post("/api/owner/password", json={"current": TEMP, "new": MINE})).status_code == 200
        assert (await phone.get("/api/owner/me")).status_code == 200
        assert (await laptop.get("/api/owner/me")).status_code == 401


# ---------------------------------------------------------------- rate limit


async def test_five_failures_per_ip_then_429(panel_client, tenant, monkeypatch):
    await create_owner(panel_client, tenant_ids=[tenant])
    clock = [1000.0]
    monkeypatch.setattr(owner_auth, "_now", lambda: clock[0])
    async with owner_client("10.0.0.9") as c:
        for i in range(5):
            # Different usernames: only the IP bucket fills up.
            assert (await login(c, f"guess{i}", "wrong-password")).status_code == 401
        r = await login(c)  # even the right password
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    async with owner_client("10.0.0.10") as other:
        assert (await login(other)).status_code == 200
    clock[0] += owner_auth.LOGIN_FAILURE_WINDOW_SECONDS + 1
    async with owner_client("10.0.0.9") as c:
        assert (await login(c)).status_code == 200


async def test_five_failures_per_username_then_429(panel_client, tenant):
    await create_owner(panel_client, tenant_ids=[tenant])
    for i in range(5):
        async with owner_client(f"10.1.0.{i}") as c:
            assert (await login(c, "Anna", "wrong-password")).status_code == 401
    async with owner_client("10.1.0.99") as c:
        r = await login(c)
        assert r.status_code == 429 and "Retry-After" in r.headers


# ------------------------------------------------------------------ disabled


async def test_a_disabled_owner_is_locked_out_at_once(panel_client, tenant):
    owner = await create_owner(panel_client, tenant_ids=[tenant])
    async with owner_client() as c:
        await login(c)
        await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})
        assert (await c.get("/api/owner/me")).status_code == 200

        r = await panel_client.patch(f"/api/owners/{owner['id']}", json={"disabled": True})
        assert r.status_code == 200 and r.json()["disabled"] is True
        assert (await c.get("/api/owner/me")).status_code == 401
        r = await login(c, password=MINE)
        assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password"

        await panel_client.patch(f"/api/owners/{owner['id']}", json={"disabled": False})
        assert (await login(c, password=MINE)).status_code == 200


async def test_a_disabled_owners_session_stops_even_if_its_row_survived(panel_client, pg_pool, tenant):
    """The session lookup itself checks `disabled`, not only the PATCH route."""
    owner = await create_owner(panel_client, tenant_ids=[tenant])
    async with owner_client() as c:
        await login(c)
        await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})
        await pg_pool.execute("UPDATE owners SET disabled = true WHERE id = $1", owner["id"])
        assert (await c.get("/api/owner/me")).status_code == 401


async def test_an_expired_session_is_refused(panel_client, pg_pool, tenant):
    await create_owner(panel_client, tenant_ids=[tenant])
    async with owner_client() as c:
        await login(c)
        await c.post("/api/owner/password", json={"current": TEMP, "new": MINE})
        await pg_pool.execute("UPDATE owner_sessions SET expires_at = now() - interval '1 second'")
        assert (await c.get("/api/owner/me")).status_code == 401


# ----------------------------------------------------------------------- TOTP


async def test_totp_is_required_once_set_and_each_code_works_once(panel_client, pg_pool, tenant, monkeypatch):
    clock = FakeClock(1_800_000_000.0)
    monkeypatch.setattr(totp, "time", clock)
    async with owner_client() as c:
        owner = await ready_owner(panel_client, c, [tenant])
        setup = (await c.post("/api/owner/totp", json={})).json()
        secret = setup["secret"]
        assert setup["uri"].startswith("otpauth://totp/") and secret in setup["uri"]

        def code(offset: int = 0) -> str:
            return totp.code_at(secret, int(clock.now // 30) + offset)

        r = await c.post("/api/owner/totp", json={"code": "000000" if code() != "000000" else "111111"})
        assert r.status_code == 400
        assert (await c.post("/api/owner/totp", json={"code": code()})).status_code == 200
        assert (await c.get("/api/owner/me")).json()["totp"] is True
        # Stored encrypted, bound to this owner.
        blob = await pg_pool.fetchval("SELECT totp_secret_enc FROM owners WHERE id = $1", owner["id"])
        assert secret.encode() not in bytes(blob)
        assert owner_auth.decrypt_totp(owner["id"], blob) == secret
        # A second setup while it is on is refused.
        assert (await c.post("/api/owner/totp", json={})).status_code == 409
        await c.post("/api/owner/logout")

        r = await login(c, password=MINE)
        assert r.status_code == 401 and r.json()["detail"] == "code_required"
        # The code just used to confirm the setup is spent.
        assert (await login(c, password=MINE, code=code())).status_code == 401
        clock.now += 30
        assert (await login(c, password=MINE, code=code())).status_code == 200
        await c.post("/api/owner/logout")
        assert (await login(c, password=MINE, code=code())).status_code == 401  # replayed
        # The right code with a wrong password gets nowhere.
        clock.now += 30
        assert (await login(c, password="wrong-password", code=code())).status_code == 401
        assert (await login(c, password=MINE, code=code())).status_code == 200

        # Turning it off takes a fresh code.
        assert (await c.request("DELETE", "/api/owner/totp", json={"code": code()})).status_code == 400
        clock.now += 30
        r = await c.request("DELETE", "/api/owner/totp", json={"code": code()})
        assert r.status_code == 200 and r.json()["totp"] is False
        await c.post("/api/owner/logout")
        assert (await login(c, password=MINE)).status_code == 200

    events = [r["event"] for r in await pg_pool.fetch(
        "SELECT event FROM audit_log WHERE actor = 'owner:anna' AND event = 'owner_totp_changed'")]
    assert len(events) == 2


async def test_the_admin_can_remove_a_lost_authenticator(panel_client, tenant, monkeypatch):
    clock = FakeClock(1_800_000_000.0)
    monkeypatch.setattr(totp, "time", clock)
    async with owner_client() as c:
        owner = await ready_owner(panel_client, c, [tenant])
        secret = (await c.post("/api/owner/totp", json={})).json()["secret"]
        await c.post("/api/owner/totp", json={"code": totp.code_at(secret, int(clock.now // 30))})
        await c.post("/api/owner/logout")
        assert (await login(c, password=MINE)).json()["detail"] == "code_required"

        r = await panel_client.delete(f"/api/owners/{owner['id']}/totp")
        assert r.status_code == 200 and r.json()["totp"] is False
        assert (await login(c, password=MINE)).status_code == 200


# ------------------------------------------------------- the two worlds apart


async def test_an_owner_cookie_opens_no_admin_route(panel_client, tenant):
    async with owner_client() as c:
        await ready_owner(panel_client, c, [tenant])
        assert (await c.get("/api/owner/me")).status_code == 200
        for path in ("/api/sessions", "/api/safety", "/api/platform/tree", "/api/owners",
                     f"/api/tenants/{tenant}", "/api/audit"):
            assert (await c.get(path)).status_code == 401, path
        r = await c.post("/api/safety/global-stop", json={"on": True, "reason": "owner tries"})
        assert r.status_code == 401


async def test_the_admin_cookie_opens_no_owner_route(panel_client, tenant):
    # panel_client carries only the admin cookie.
    for path in ("/api/owner/me", "/api/owner/overview", f"/api/owner/dashboard?tenant_id={tenant}",
                 "/api/owner/unanswered"):
        assert (await panel_client.get(path)).status_code == 401, path
    r = await panel_client.post("/api/owner/password", json={"current": "x", "new": "y" * 12})
    assert r.status_code == 401


async def test_an_admin_token_in_the_owner_cookie_is_worthless(panel_client, tenant):
    admin_token = panel_client.cookies.get("admin_token")
    assert admin_token
    async with owner_client() as c:
        c.cookies.set("owner_token", admin_token)
        assert (await c.get("/api/owner/me")).status_code == 401
