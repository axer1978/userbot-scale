"""Adding a Telegram account from the panel.

The real LoginFlow runs against real Postgres; only Telethon's client is
replaced, so no test ever talks to Telegram. What matters: a finished
sign-in leaves a session the manager will actually run (auth saved,
DeepSeek key stored, is_active), and an unfinished one leaves nothing
runnable behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from telethon import errors

import device_profiles
import login_flow
from database import SessionRegistry

PHONE = "+371 2000 0001"
SESSION_ID = "tg37120000001"


class SentCodeTypeApp:
    length = 5


class FakeTelegramClient:
    """Just the surface LoginFlow uses."""

    needs_password = False
    instances: list["FakeTelegramClient"] = []

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.api_id, self.api_hash, self.kwargs = api_id, api_hash, kwargs
        self.session = type("S", (), {
            "dc_id": 4, "server_address": "149.154.167.91", "port": 443,
            "auth_key": type("K", (), {"key": b"k" * 256})(),
        })()
        self.signed_in = False
        FakeTelegramClient.instances.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def send_code_request(self, phone):
        self.phone = phone
        return type("Sent", (), {
            "type": SentCodeTypeApp(), "next_type": None, "phone_code_hash": "hash", "timeout": 60,
        })()

    async def sign_in(self, phone=None, code=None, *, phone_code_hash=None, password=None):
        if password is not None:
            if password != "hunter2":
                raise errors.PasswordHashInvalidError(request=None)
        elif code != "12345":
            raise errors.PhoneCodeInvalidError(request=None)
        elif self.needs_password:
            raise errors.SessionPasswordNeededError(request=None)
        self.signed_in = True

    async def get_me(self):
        return type("Me", (), {"id": 777, "username": "anna", "phone": "37120000001"})()


@pytest.fixture(autouse=True)
def fake_telegram(monkeypatch):
    FakeTelegramClient.needs_password = False
    FakeTelegramClient.instances = []
    monkeypatch.setattr(login_flow, "TelegramClient", FakeTelegramClient)
    return FakeTelegramClient


def start_body(**overrides):
    body = {
        "api_id": "1234567", "api_hash": "0123456789abcdef0123456789abcdef",
        "phone": PHONE, "deepseek_api_key": "sk-test", "label": "",
    }
    return {**body, **overrides}


async def row(pg_pool, session_id=SESSION_ID):
    async with pg_pool.acquire() as con:
        return await con.fetchrow(
            "SELECT is_active, auth_key_enc IS NOT NULL AS has_auth, label, user_id "
            "FROM telegram_sessions WHERE session_id = $1",
            session_id,
        )


@pytest.mark.asyncio
async def test_signing_in_leaves_a_session_the_manager_will_run(panel_client, pg_pool):
    r = await panel_client.post("/api/auth/start", json=start_body(label="Front desk"))
    assert r.status_code == 200, r.text
    assert r.json()["step"] == "code"

    r = await panel_client.post("/api/auth/code", json={"code": "12-345"})
    assert r.status_code == 200, r.text
    assert (r.json()["step"], r.json()["session_id"]) == ("done", SESSION_ID)

    saved = await row(pg_pool)
    assert saved["is_active"] and saved["has_auth"]
    assert (saved["label"], saved["user_id"]) == ("Front desk", 777)
    registry = SessionRegistry(pg_pool)
    assert await registry.load_deepseek_key(SESSION_ID) == "sk-test"
    assert SESSION_ID in await registry.claimable()


@pytest.mark.asyncio
async def test_login_presents_the_device_the_runtime_will_use(panel_client):
    await panel_client.post("/api/auth/start", json=start_body())
    identity = device_profiles.derive(SESSION_ID)
    sent = FakeTelegramClient.instances[-1].kwargs
    assert sent["device_model"] == identity["device_model"]
    assert sent["app_version"] == identity["app_version"]
    assert sent["system_version"] == identity["system_version"]


@pytest.mark.asyncio
async def test_two_step_password_finishes_the_sign_in(panel_client, pg_pool, fake_telegram):
    fake_telegram.needs_password = True
    await panel_client.post("/api/auth/start", json=start_body())
    r = await panel_client.post("/api/auth/code", json={"code": "12345"})
    assert r.json()["step"] == "password"
    assert not (await row(pg_pool))["is_active"]

    r = await panel_client.post("/api/auth/password", json={"password": "wrong"})
    assert r.status_code == 400
    r = await panel_client.post("/api/auth/password", json={"password": "hunter2"})
    assert r.json()["step"] == "done"
    assert (await row(pg_pool))["is_active"]


@pytest.mark.asyncio
async def test_a_wrong_code_is_reported_and_can_be_retried(panel_client, pg_pool):
    await panel_client.post("/api/auth/start", json=start_body())
    r = await panel_client.post("/api/auth/code", json={"code": "00000"})
    assert r.status_code == 400 and "not right" in r.json()["detail"]
    assert not (await row(pg_pool))["is_active"]
    r = await panel_client.post("/api/auth/code", json={"code": "12345"})
    assert r.json()["step"] == "done"


@pytest.mark.asyncio
async def test_an_abandoned_sign_in_is_never_runnable(panel_client, pg_pool):
    await panel_client.post("/api/auth/start", json=start_body())
    r = await panel_client.post("/api/auth/cancel")
    assert r.json()["step"] == "credentials"
    saved = await row(pg_pool)
    assert not saved["is_active"] and not saved["has_auth"]
    assert await SessionRegistry(pg_pool).load_deepseek_key(SESSION_ID) is None


@pytest.mark.asyncio
async def test_a_deepseek_key_is_required_for_a_new_number(panel_client):
    r = await panel_client.post("/api/auth/start", json=start_body(deepseek_api_key=""))
    assert r.status_code == 400 and "DeepSeek" in r.json()["detail"]
    assert FakeTelegramClient.instances == []  # no code was requested


@pytest.mark.asyncio
async def test_signing_a_number_in_again_keeps_its_key(panel_client, pg_pool):
    await panel_client.post("/api/auth/start", json=start_body())
    await panel_client.post("/api/auth/code", json={"code": "12345"})

    r = await panel_client.post("/api/auth/start", json=start_body(deepseek_api_key=""))
    assert r.status_code == 200, r.text
    await panel_client.post("/api/auth/code", json={"code": "12345"})
    assert await SessionRegistry(pg_pool).load_deepseek_key(SESSION_ID) == "sk-test"


@pytest.mark.asyncio
async def test_a_running_number_is_not_signed_in_twice(panel_client, pg_pool):
    await panel_client.post("/api/auth/start", json=start_body())
    await panel_client.post("/api/auth/code", json={"code": "12345"})
    async with pg_pool.acquire() as con:
        await con.execute(
            "UPDATE telegram_sessions SET lease_worker_id = 'w1', lease_expires_at = $2 WHERE session_id = $1",
            SESSION_ID, datetime.now(timezone.utc) + timedelta(minutes=1),
        )
    r = await panel_client.post("/api/auth/start", json=start_body())
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_bad_api_id_is_refused_before_telegram_is_asked(panel_client):
    r = await panel_client.post("/api/auth/start", json=start_body(api_id="abc"))
    assert r.status_code == 400
    assert FakeTelegramClient.instances == []


@pytest.mark.asyncio
async def test_sign_in_routes_need_the_admin_password(panel_client):
    await panel_client.post("/api/logout")
    panel_client.cookies.clear()
    r = await panel_client.post("/api/auth/start", json=start_body())
    assert r.status_code == 401
    assert FakeTelegramClient.instances == []
