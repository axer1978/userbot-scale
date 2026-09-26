"""The optional second factor on the admin login (ADMIN_TOTP_SECRET).

With it set, the password alone gets nowhere, a code works once, and a
wrong code counts toward the same per-IP lockout as a wrong password.
"""

from __future__ import annotations

import time

import httpx
import pytest

import totp
from conftest import TEST_ADMIN_PASSWORD

SECRET = totp.new_secret()


@pytest.fixture
def panel(panel_client, monkeypatch):
    import panel as module

    monkeypatch.setattr(module, "ADMIN_TOTP_SECRET", SECRET)
    return module


def fresh_client(panel, ip="1.2.3.4") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=panel.app, client=(ip, 123))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def current_code() -> str:
    return totp.code_at(SECRET, int(time.time() // totp.STEP_SECONDS))


async def login(client, **body) -> httpx.Response:
    return await client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD, **body})


@pytest.mark.asyncio
async def test_the_sign_in_screen_is_told_to_ask_for_a_code(panel):
    async with fresh_client(panel) as c:
        assert (await c.get("/api/login-options")).json() == {"totp": True}


@pytest.mark.asyncio
async def test_the_password_alone_is_not_enough(panel):
    async with fresh_client(panel) as c:
        r = await login(c)
        assert r.status_code == 401 and r.json()["detail"] == "Wrong password or code"
        assert (await c.get("/api/sessions")).status_code == 401


@pytest.mark.asyncio
async def test_password_and_current_code_let_you_in(panel):
    async with fresh_client(panel) as c:
        assert (await login(c, code=current_code())).status_code == 200
        assert (await c.get("/api/sessions")).status_code == 200


@pytest.mark.asyncio
async def test_a_code_works_only_once(panel):
    code = current_code()
    async with fresh_client(panel, "1.1.1.1") as a, fresh_client(panel, "2.2.2.2") as b:
        assert (await login(a, code=code)).status_code == 200
        assert (await login(b, code=code)).status_code == 401


@pytest.mark.asyncio
async def test_a_right_code_with_a_wrong_password_fails(panel):
    async with fresh_client(panel) as c:
        r = await c.post("/api/login", json={"password": "nope", "code": current_code()})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_codes_count_toward_the_lockout(panel):
    async with fresh_client(panel) as c:
        for _ in range(panel.LOGIN_MAX_FAILURES):
            assert (await login(c, code="000000")).status_code == 401
        assert (await login(c, code=current_code())).status_code == 429


@pytest.mark.asyncio
async def test_without_a_secret_the_screen_asks_for_no_code(panel_client):
    import panel as module

    assert module.ADMIN_TOTP_SECRET == ""
    assert (await panel_client.get("/api/login-options")).json() == {"totp": False}


@pytest.mark.asyncio
async def test_a_non_ascii_password_is_just_wrong_not_a_crash(panel_client):
    r = await panel_client.post("/api/login", json={"password": "пароль"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_the_login_cookie_is_httponly_and_same_site_strict(panel):
    async with fresh_client(panel) as c:
        r = await login(c, code=current_code())
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
