"""The admin password gate, now that the panel may face the internet.

Wrong passwords are limited per client IP (5 per sliding 15 minutes, then
429 even for the right password), a success clears that IP's count, other
IPs are never affected, and a login token stops working after 12 hours.
"""

from __future__ import annotations

import time

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from conftest import TEST_ADMIN_PASSWORD

WRONG = {"password": "not-it"}
RIGHT = {"password": TEST_ADMIN_PASSWORD}


class Clock:
    """Stands in for panel._now; starts at the real monotonic time so tokens
    issued before it was installed (the panel_client fixture's own login)
    still make sense on it."""

    def __init__(self) -> None:
        self.t = time.monotonic()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def panel(panel_client):
    """The panel module — imported only once panel_client has set the env
    vars it reads at import time."""
    import panel as module

    return module


@pytest.fixture
def clock(panel, monkeypatch):
    fake = Clock()
    monkeypatch.setattr(panel, "_now", fake)
    return fake


def client_from(ip: str, app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 123))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def fail(client: httpx.AsyncClient, times: int) -> None:
    for _ in range(times):
        assert (await client.post("/api/login", json=WRONG)).status_code == 401


@pytest.mark.asyncio
async def test_sixth_attempt_is_refused_even_with_the_right_password(panel_client, panel, clock):
    await fail(panel_client, panel.LOGIN_MAX_FAILURES)

    r = await panel_client.post("/api/login", json=RIGHT)
    assert r.status_code == 429
    assert "Too many wrong passwords" in r.json()["detail"]
    retry_after = int(r.headers["Retry-After"])
    assert 0 < retry_after <= panel.LOGIN_FAILURE_WINDOW_SECONDS + 1


@pytest.mark.asyncio
async def test_lockout_lifts_once_the_window_has_passed(panel_client, panel, clock):
    await fail(panel_client, panel.LOGIN_MAX_FAILURES)
    assert (await panel_client.post("/api/login", json=RIGHT)).status_code == 429

    clock.advance(panel.LOGIN_FAILURE_WINDOW_SECONDS + 1)
    assert (await panel_client.post("/api/login", json=RIGHT)).status_code == 200


@pytest.mark.asyncio
async def test_another_ip_is_unaffected(panel_client, panel, clock):
    await fail(panel_client, panel.LOGIN_MAX_FAILURES)
    assert (await panel_client.post("/api/login", json=RIGHT)).status_code == 429

    async with client_from("1.2.3.4", panel.app) as other:
        assert (await other.post("/api/login", json=RIGHT)).status_code == 200


@pytest.mark.asyncio
async def test_a_successful_login_resets_the_count(panel_client, panel, clock):
    await fail(panel_client, panel.LOGIN_MAX_FAILURES - 1)
    assert (await panel_client.post("/api/login", json=RIGHT)).status_code == 200

    # Had the earlier failures still counted, this would be 8 in the window.
    await fail(panel_client, panel.LOGIN_MAX_FAILURES - 1)
    assert (await panel_client.post("/api/login", json=RIGHT)).status_code == 200


@pytest.mark.asyncio
async def test_forwarded_client_ip_is_what_gets_limited(panel_client, panel, clock):
    """Behind Caddy every request arrives from the same container address;
    the limit must key on X-Forwarded-For (via uvicorn's proxy headers, as
    panel.py's uvicorn.run enables), or one attacker would lock everyone out."""
    behind_proxy = ProxyHeadersMiddleware(panel.app, trusted_hosts="*")
    async with client_from("172.18.0.5", behind_proxy) as caddy:
        attacker = {"X-Forwarded-For": "203.0.113.9"}
        operator = {"X-Forwarded-For": "198.51.100.7"}
        for _ in range(panel.LOGIN_MAX_FAILURES):
            r = await caddy.post("/api/login", json=WRONG, headers=attacker)
            assert r.status_code == 401
        assert (await caddy.post("/api/login", json=RIGHT, headers=attacker)).status_code == 429
        assert (await caddy.post("/api/login", json=RIGHT, headers=operator)).status_code == 200


@pytest.mark.asyncio
async def test_a_login_token_expires(panel_client, panel, clock):
    assert (await panel_client.get("/api/sessions")).status_code == 200

    clock.advance(panel.SESSION_TTL_SECONDS - 60)
    assert (await panel_client.get("/api/sessions")).status_code == 200

    clock.advance(120)
    assert (await panel_client.get("/api/sessions")).status_code == 401
    assert panel._valid_tokens == {}

    assert (await panel_client.post("/api/login", json=RIGHT)).status_code == 200
    assert (await panel_client.get("/api/sessions")).status_code == 200
