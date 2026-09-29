"""The per-account Telegram proxy: parsing, what the panel may see (never the
password), the reachability check, and the account reconnecting through it."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import audit
import proxies
from conftest import seed_session
from database import SessionRegistry


def test_parse_accepts_socks5_and_http_and_keeps_the_credentials():
    p = proxies.parse("socks5://us%40er:p%3Ass@proxy.example.com:1080")
    assert (p["type"], p["host"], p["port"], p["username"], p["password"]) == \
        ("socks5", "proxy.example.com", 1080, "us@er", "p:ss")
    assert proxies.parse("socks5h://1.2.3.4:9050")["type"] == "socks5"
    assert proxies.parse("http://u:p@1.2.3.4:8080")["type"] == "http"
    assert proxies.telethon_tuple("socks5://u:p@1.2.3.4:1080") == ("socks5", "1.2.3.4", 1080, True, "u", "p")
    assert proxies.telethon_tuple("") is None and proxies.telethon_tuple(None) is None


@pytest.mark.parametrize("bad", ["", "1.2.3.4:1080", "ftp://1.2.3.4:21", "socks5://1.2.3.4", "socks5://:1080",
                                 "socks5://1.2.3.4:99999"])
def test_parse_refuses_what_cannot_work(bad):
    with pytest.raises(proxies.ProxyError):
        proxies.parse(bad)


def test_describe_never_includes_the_password():
    d = proxies.describe("socks5://alice:s3cret@1.2.3.4:1080")
    assert d == {"type": "socks5", "host": "1.2.3.4", "port": 1080, "username": "alice"}
    assert "s3cret" not in str(d)
    assert proxies.describe(None) is None


@pytest.mark.asyncio
async def test_reachable_says_yes_for_an_open_port_and_why_not_otherwise():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await proxies.reachable(f"socks5://127.0.0.1:{port}") == (True, "")
    finally:
        server.close()
        await server.wait_closed()
    ok, why = await proxies.reachable(f"socks5://127.0.0.1:{port}", timeout=2)
    assert not ok and "127.0.0.1" in why


@pytest.mark.requires_pg
@pytest.mark.asyncio
class TestPanel:
    @pytest_asyncio.fixture
    async def tenant(self, pg_pool):
        return await seed_session(pg_pool, "acct", name="Salon")

    async def test_setting_a_proxy_stores_it_encrypted_and_shows_no_password(self, panel_client, pg_pool, tenant):
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            r = await panel_client.put(f"/api/tenants/{tenant}/proxy",
                                       json={"proxy_url": f"socks5://bob:hunter22@127.0.0.1:{port}"})
        finally:
            server.close()
            await server.wait_closed()
        assert r.status_code == 200, r.text
        assert r.json()["proxy"] == {"type": "socks5", "host": "127.0.0.1", "port": port, "username": "bob"}
        assert r.json()["reconnected"] is False                      # nothing runs it in the test
        assert await SessionRegistry(pg_pool).load_proxy("acct") == f"socks5://bob:hunter22@127.0.0.1:{port}"
        raw = await pg_pool.fetchval("SELECT proxy_url_enc FROM telegram_sessions WHERE session_id = 'acct'")
        assert b"hunter22" not in bytes(raw)
        view = (await panel_client.get(f"/api/tenants/{tenant}/controls")).json()
        assert view["proxy"]["username"] == "bob" and "hunter22" not in str(view)
        [event] = [e for e in await audit.list_events(pg_pool, tenant_id=tenant) if e["event"] == audit.PROXY_CHANGED]
        assert "hunter22" not in str(event)

        r = await panel_client.put(f"/api/tenants/{tenant}/proxy", json={"proxy_url": ""})
        assert r.json()["proxy"] is None
        assert await SessionRegistry(pg_pool).load_proxy("acct") is None

    async def test_a_bad_or_unreachable_proxy_is_refused_and_nothing_changes(self, panel_client, pg_pool, tenant,
                                                                             monkeypatch):
        r = await panel_client.put(f"/api/tenants/{tenant}/proxy", json={"proxy_url": "ftp://x:21"})
        assert r.status_code == 400 and "socks5://" in r.json()["detail"]

        async def closed(url, timeout=6.0):
            return False, "cannot connect to 127.0.0.1:9 (Connection refused)"

        monkeypatch.setattr(proxies, "reachable", closed)
        r = await panel_client.put(f"/api/tenants/{tenant}/proxy", json={"proxy_url": "socks5://127.0.0.1:9"})
        assert r.status_code == 400 and "not reachable" in r.json()["detail"]
        assert await SessionRegistry(pg_pool).load_proxy("acct") is None


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_sign_in_goes_through_the_proxy_and_saves_it(pg_pool, monkeypatch):
    import login_flow

    seen = {}

    class FakeClient:
        def __init__(self, session, api_id, api_hash, **kw):
            seen["proxy"] = kw.get("proxy")

        async def connect(self):
            pass

        async def send_code_request(self, phone):
            from types import SimpleNamespace
            return SimpleNamespace(type=None, next_type=None, timeout=None, phone_code_hash="h")

        async def disconnect(self):
            pass

    monkeypatch.setattr(login_flow, "TelegramClient", FakeClient)
    flow = login_flow.LoginFlow(pg_pool)
    await flow.start("tg37100000001", 12345, "a" * 32, "+37100000001", proxy_url="socks5://u:p@10.0.0.1:1080")
    assert seen["proxy"] == ("socks5", "10.0.0.1", 1080, True, "u", "p")
    assert await SessionRegistry(pg_pool).load_proxy("tg37100000001") == "socks5://u:p@10.0.0.1:1080"
    with pytest.raises(login_flow.LoginError, match="socks5://"):
        await flow.start("tg37100000002", 12345, "a" * 32, "+37100000002", proxy_url="nonsense")
    await flow.reset()
