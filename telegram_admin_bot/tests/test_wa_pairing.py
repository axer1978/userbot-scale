"""Adding a WhatsApp account from the panel.

The wa-gateway (the Node service that really pairs) is played by a fake
that answers `cmd:@wa-gateway` on the test's fakeredis and publishes the
pairing events on `wa:pair:<pair_id>`, exactly the v1 wire contract. What
matters: the account row exists (channel whatsapp) before the gateway is
asked, a new client starts on the WhatsApp safety defaults, its browser
identity is chosen once and reused, a finished pairing leaves an account
the manager will run (key stored, active), and anything else leaves
nothing runnable behind.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import audit
import commands
import config_store
import controls
import tenant_config
import wa_device_profiles
import wa_pairing
from database import DIR_IN, STATUS_RECEIVED, Database, SessionRegistry

pytestmark = pytest.mark.requires_pg

PHONE = "+371 2000 0009"
DIGITS = "37120000009"
SESSION_ID = "wa37120000009"


class FakeGateway:
    """Answers the panel's RPCs the way wa-gateway does and publishes pairing
    events. `replies[action]` may be a dict (the whole response) or None to
    stay silent (a gateway that isn't running)."""

    def __init__(self, redis) -> None:
        self.redis = redis
        self.calls: list[tuple[str, dict]] = []
        self.replies: dict[str, dict] = {}
        self._task = None
        self._ready = asyncio.Event()

    async def __aenter__(self) -> "FakeGateway":
        self._task = asyncio.create_task(self._serve())
        await asyncio.wait_for(self._ready.wait(), 5)
        return self

    async def __aexit__(self, *exc) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _serve(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("cmd:@wa-gateway")
        self._ready.set()
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
                if message is None:
                    continue
                payload = json.loads(message["data"])
                action, args = payload["action"], payload["args"]
                self.calls.append((action, args))
                if action in self.replies and self.replies[action] is None:
                    continue
                reply = self.replies.get(action)
                if reply is None:
                    reply = {"ok": True, "result": {"started": True} if action == "pair" else {"cancelled": True}}
                await self.redis.publish(f"cmdresp:{payload['command_id']}", json.dumps(reply))
        finally:
            await pubsub.aclose()

    def args(self, action: str) -> dict:
        return [a for name, a in self.calls if name == action][-1]

    async def emit(self, pair_id: str, event: dict) -> None:
        await self.redis.publish(f"wa:pair:{pair_id}", json.dumps({"v": 1, **event}))


@pytest_asyncio.fixture
async def gateway(panel_client):
    import panel

    async with FakeGateway(panel.bus._redis) as gw:
        yield gw


def start_body(**overrides):
    return {"label": "Salon WA", "phone": PHONE, "deepseek_api_key": "sk-wa", "method": "qr", **overrides}


async def wait_for(panel_client, pair_id: str, status: str, **fields) -> dict:
    """Polls the pairing like the browser does until it reaches `status`
    (and the given fields match)."""
    for _ in range(100):
        state = (await panel_client.get(f"/api/wa/pair/{pair_id}")).json()
        if state.get("status") == status and all(state.get(k) == v for k, v in fields.items()):
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"pairing never reached {status} {fields}: {state}")


async def session_row(pg_pool, session_id=SESSION_ID):
    async with pg_pool.acquire() as con:
        return await con.fetchrow(
            "SELECT s.channel, s.label, s.is_active, t.channel AS tenant_channel, t.config_json, t.id AS tenant_id "
            "FROM telegram_sessions s JOIN tenants t ON t.session_id = s.session_id WHERE s.session_id = $1",
            session_id,
        )


def overrides(row) -> dict:
    value = row["config_json"]
    return json.loads(value) if isinstance(value, str) else value


# ------------------------------------------------------------- starting


@pytest.mark.asyncio
async def test_starting_creates_the_whatsapp_account_and_asks_the_gateway(panel_client, pg_pool, gateway):
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["status"] == "waiting" and state["session_id"] == SESSION_ID and state["method"] == "qr"
    assert "sk-wa" not in r.text

    row = await session_row(pg_pool)
    assert (row["channel"], row["tenant_channel"], row["label"]) == ("whatsapp", "whatsapp", "Salon WA")
    assert not row["is_active"]
    assert await SessionRegistry(pg_pool).load_deepseek_key(SESSION_ID) is None  # only once paired

    browser = wa_device_profiles.derive(SESSION_ID)
    assert gateway.args("pair") == {"session_id": SESSION_ID, "pair_id": state["pair_id"],
                                    "method": "qr", "browser": browser}
    identity = (await config_store.load(pg_pool, SESSION_ID))["identity"]
    assert identity["wa_browser"] == browser
    assert identity["device_model"] == ""  # the Telegram identity fields are left alone


@pytest.mark.asyncio
async def test_a_new_whatsapp_client_starts_on_the_safety_defaults(panel_client, pg_pool, gateway):
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 200, r.text
    row = await session_row(pg_pool)
    assert overrides(row) == tenant_config.WHATSAPP_CLIENT_DEFAULTS
    effective = (await panel_client.get(f"/api/sessions/{SESSION_ID}/status")).json()
    assert effective["auto_send"] is False
    assert effective["quiet_hours"] == {"enabled": True, "start": "21:00", "end": "09:00"}
    events = await pg_pool.fetch(
        "SELECT actor, reason FROM audit_log WHERE tenant_id = $1 AND event = $2", row["tenant_id"],
        audit.CONFIG_CHANGED,
    )
    assert [(e["actor"], e["reason"]) for e in events] == [(audit.ADMIN, "WhatsApp safety defaults")]

    # Starting again (a second attempt) doesn't seed a second time.
    await panel_client.post("/api/wa/pair/start", json=start_body())
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE tenant_id = $1 AND event = $2", row["tenant_id"],
        audit.CONFIG_CHANGED,
    ) == 1


def test_the_whatsapp_defaults_are_a_valid_client_layer():
    config = tenant_config.resolve(None, tenant_config.WHATSAPP_CLIENT_DEFAULTS).as_dict()
    assert config["daily_message_cap"] == 60 and config["hourly_message_cap"] == 15
    assert config["safety"]["daily_peer_cap"] == 15
    assert config["reply_delay"] == {"min_s": 45, "max_s": 180, "distribution": "lognormal"}
    assert config["burst"] == {"max_messages": 3, "gap_ms": {"min": 1200, "max": 3500}}
    assert config["quiet_hours"] == {"enabled": True, "start": "21:00", "end": "09:00"}
    assert config["auto_send"] is False and config["outreach"]["enabled"] is False


# ---------------------------------------------------------------- events


@pytest.mark.asyncio
async def test_the_qr_rotates_and_a_paired_number_is_left_running(panel_client, pg_pool, gateway):
    pair_id = (await panel_client.post("/api/wa/pair/start", json=start_body())).json()["pair_id"]

    await gateway.emit(pair_id, {"type": "qr", "qr": "2@first,qr"})
    await wait_for(panel_client, pair_id, "qr", qr="2@first,qr")
    await gateway.emit(pair_id, {"type": "qr", "qr": "2@second,qr"})
    await wait_for(panel_client, pair_id, "qr", qr="2@second,qr")

    await gateway.emit(pair_id, {"type": "paired", "jid": "37120000009:3@s.whatsapp.net", "lid": "1@lid",
                                 "push_name": "Anna"})
    state = await wait_for(panel_client, pair_id, "paired")
    assert state["qr"] is None and state["push_name"] == "Anna"

    registry = SessionRegistry(pg_pool)
    assert await registry.load_deepseek_key(SESSION_ID) == "sk-wa"
    assert (await session_row(pg_pool))["is_active"]
    assert SESSION_ID in await registry.claimable()
    r = await panel_client.get(f"/api/wa/pair/{pair_id}")
    assert "sk-wa" not in r.text and "deepseek" not in r.text


@pytest.mark.asyncio
async def test_a_pairing_code_is_requested_for_the_number_and_shown(panel_client, gateway):
    r = await panel_client.post("/api/wa/pair/start", json=start_body(method="code"))
    assert r.status_code == 200, r.text
    pair_id = r.json()["pair_id"]
    assert gateway.args("pair")["phone"] == DIGITS and gateway.args("pair")["method"] == "code"

    await gateway.emit(pair_id, {"type": "code", "code": "ABCD1234"})
    state = await wait_for(panel_client, pair_id, "code", code="ABCD1234")
    assert state["qr"] is None


@pytest.mark.asyncio
async def test_a_failed_pairing_leaves_nothing_runnable(panel_client, pg_pool, gateway):
    pair_id = (await panel_client.post("/api/wa/pair/start", json=start_body())).json()["pair_id"]
    await gateway.emit(pair_id, {"type": "failed", "reason": "The QR code was not scanned in time."})
    state = await wait_for(panel_client, pair_id, "failed")
    assert state["error"] == "The QR code was not scanned in time."
    assert not (await session_row(pg_pool))["is_active"]
    assert await SessionRegistry(pg_pool).load_deepseek_key(SESSION_ID) is None


@pytest.mark.asyncio
async def test_cancelling_tells_the_gateway_and_ignores_later_events(panel_client, pg_pool, gateway):
    pair_id = (await panel_client.post("/api/wa/pair/start", json=start_body())).json()["pair_id"]
    r = await panel_client.post(f"/api/wa/pair/{pair_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert gateway.args("pair_cancel") == {"pair_id": pair_id}

    await gateway.emit(pair_id, {"type": "paired", "jid": "x@s.whatsapp.net"})
    await asyncio.sleep(0.3)
    assert (await panel_client.get(f"/api/wa/pair/{pair_id}")).json()["status"] == "cancelled"
    assert not (await session_row(pg_pool))["is_active"]
    assert (await panel_client.post("/api/wa/pair/nope/cancel")).status_code == 404
    assert (await panel_client.get("/api/wa/pair/nope")).status_code == 404


# ---------------------------------------------------------------- refusals


@pytest.mark.asyncio
async def test_no_gateway_running_is_said_plainly(panel_client, pg_pool, monkeypatch):
    import panel

    monkeypatch.setattr(panel, "WA_GATEWAY_TIMEOUT", 0.3)
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 503
    assert "WhatsApp gateway is not running" in r.json()["detail"]
    assert panel.wa_pairings.active() == []


@pytest.mark.asyncio
async def test_a_busy_number_is_refused(panel_client, gateway):
    gateway.replies["pair"] = {"ok": False, "error": "Busy: socket already open", "error_kind": "busy"}
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 409 and "already has a WhatsApp connection" in r.json()["detail"]
    import panel

    assert panel.wa_pairings.active() == []


@pytest.mark.asyncio
async def test_a_running_number_is_not_paired_again(panel_client, pg_pool, gateway):
    await SessionRegistry(pg_pool).create(SESSION_ID, label="x", channel="whatsapp")
    async with pg_pool.acquire() as con:
        await con.execute(
            "UPDATE telegram_sessions SET lease_worker_id = 'w1', lease_expires_at = $2 WHERE session_id = $1",
            SESSION_ID, datetime.now(timezone.utc) + timedelta(minutes=1),
        )
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 409 and "Stop it before pairing it again" in r.json()["detail"]
    assert gateway.calls == []


async def hold_lease(pg_pool, *, state: str, reason: str = "") -> None:
    async with pg_pool.acquire() as con:
        await con.execute(
            "UPDATE telegram_sessions SET lease_worker_id = 'w1', lease_expires_at = $2, state = $3, "
            "state_reason = $4, is_active = true WHERE session_id = $1",
            SESSION_ID, datetime.now(timezone.utc) + timedelta(minutes=1), state, reason,
        )


@pytest.mark.asyncio
async def test_a_number_whose_session_was_lost_is_released_and_paired_again(panel_client, pg_pool, gateway):
    """Halted after a session loss, the account still holds its lease (red
    dot). Pairing it again deactivates it, waits for its runtime to let go,
    then pairs — no SQL by hand."""
    import asyncio

    await SessionRegistry(pg_pool).create(SESSION_ID, label="x", channel="whatsapp")
    await hold_lease(pg_pool, state="needs_login", reason="WhatsApp session lost (loggedOut)")

    async def worker_lets_go():
        # What the halted runtime does once its renewal fails on is_active.
        for _ in range(100):
            if not await pg_pool.fetchval("SELECT is_active FROM telegram_sessions WHERE session_id = $1",
                                          SESSION_ID):
                break
            await asyncio.sleep(0.05)
        await pg_pool.execute("UPDATE telegram_sessions SET lease_worker_id = NULL, lease_expires_at = NULL "
                              "WHERE session_id = $1", SESSION_ID)

    letting_go = asyncio.create_task(worker_lets_go())
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    await letting_go
    assert r.status_code == 200, r.text
    assert [c for c in gateway.calls if c[0] == "pair"]
    assert (await session_row(pg_pool))["is_active"] is False
    pair_id = r.json()["pair_id"]
    await gateway.emit(pair_id, {"type": "paired", "jid": "34600123456@s.whatsapp.net", "lid": None,
                                 "push_name": "Salon"})
    await wait_for(panel_client, pair_id, "paired")
    assert (await session_row(pg_pool))["is_active"] is True    # the manager picks it up again


@pytest.mark.asyncio
async def test_a_lost_session_that_does_not_let_go_is_refused_for_now(panel_client, pg_pool, gateway, monkeypatch):
    import panel

    monkeypatch.setattr(panel, "WA_RELEASE_WAIT_SECONDS", 0.3)
    await SessionRegistry(pg_pool).create(SESSION_ID, label="x", channel="whatsapp")
    await hold_lease(pg_pool, state="needs_login")
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 409 and "try again in half a minute" in r.json()["detail"]
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_a_healthy_running_number_is_never_deactivated(panel_client, pg_pool, gateway):
    await SessionRegistry(pg_pool).create(SESSION_ID, label="x", channel="whatsapp")
    await hold_lease(pg_pool, state="running")
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 409 and "Stop it before pairing it again" in r.json()["detail"]
    assert (await session_row(pg_pool))["is_active"] is True


@pytest.mark.asyncio
async def test_a_telegram_row_under_that_id_is_never_turned_into_whatsapp(panel_client, pg_pool, gateway):
    await SessionRegistry(pg_pool).create(SESSION_ID, label="tg", channel="telegram")
    r = await panel_client.post("/api/wa/pair/start", json=start_body())
    assert r.status_code == 409
    assert (await session_row(pg_pool))["channel"] == "telegram"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_a_deepseek_key_and_a_real_number_are_required(panel_client, pg_pool, gateway):
    r = await panel_client.post("/api/wa/pair/start", json=start_body(deepseek_api_key=""))
    assert r.status_code == 400 and "DeepSeek" in r.json()["detail"]
    for phone in ("", "12345", "0037120000009", "+371 2000 0009 ext 2", "+1234567890123456"):
        r = await panel_client.post("/api/wa/pair/start", json=start_body(phone=phone))
        assert r.status_code == 400, phone
    r = await panel_client.post("/api/wa/pair/start", json=start_body(method="sms"))
    assert r.status_code == 400
    assert gateway.calls == []
    assert await session_row(pg_pool) is None


@pytest.mark.asyncio
async def test_the_pairing_routes_need_the_admin_login(panel_client):
    await panel_client.post("/api/logout")
    panel_client.cookies.clear()
    assert (await panel_client.post("/api/wa/pair/start", json=start_body())).status_code == 401
    assert (await panel_client.get("/api/wa/pair/x")).status_code == 401
    assert (await panel_client.post("/api/wa/pair/x/cancel")).status_code == 401


# ---------------------------------------------------------------- re-pairing


@pytest.mark.asyncio
async def test_pairing_a_number_again_keeps_its_history_settings_and_identity(
        panel_client, pg_pool, gateway, monkeypatch):
    pair_id = (await panel_client.post("/api/wa/pair/start", json=start_body())).json()["pair_id"]
    await gateway.emit(pair_id, {"type": "paired", "jid": "a@s.whatsapp.net"})
    await wait_for(panel_client, pair_id, "paired")
    browser = gateway.args("pair")["browser"]

    db = Database(pg_pool, SESSION_ID)
    await db.upsert_conversation(501, "Customer", None, False)
    await db.record_message(501, DIR_IN, STATUS_RECEIVED, "hello")
    row = await session_row(pg_pool)
    import tenants

    await tenants.TenantStore(pg_pool).save_config(
        row["tenant_id"], {**overrides(row), "daily_message_cap": 40}, actor=audit.ADMIN, reason="test")
    # Stopped (no lease) and unlinked on the phone: the operator pairs it again,
    # with no new key and no name.
    async with pg_pool.acquire() as con:
        await con.execute("UPDATE telegram_sessions SET is_active = false WHERE session_id = $1", SESSION_ID)
    monkeypatch.setattr(wa_device_profiles, "derive", lambda _sid: ["Windows", "Edge", "1"])

    r = await panel_client.post("/api/wa/pair/start", json=start_body(deepseek_api_key="", label=""))
    assert r.status_code == 200, r.text
    assert gateway.args("pair")["browser"] == browser  # the stored one, not a fresh derive
    await gateway.emit(r.json()["pair_id"], {"type": "paired", "jid": "a@s.whatsapp.net"})
    await wait_for(panel_client, r.json()["pair_id"], "paired")

    after = await session_row(pg_pool)
    assert after["tenant_id"] == row["tenant_id"] and after["label"] == "Salon WA" and after["is_active"]
    assert overrides(after)["daily_message_cap"] == 40  # not re-seeded
    assert [m["text"] for m in await db.get_messages(501)] == ["hello"]
    assert await SessionRegistry(pg_pool).load_deepseek_key(SESSION_ID) == "sk-wa"


# ------------------------------------------------------- the rest of the panel


@pytest.mark.asyncio
async def test_outreach_is_refused_for_whatsapp_accounts(panel_client, pg_pool):
    registry = SessionRegistry(pg_pool)
    await registry.create(SESSION_ID, label="wa", channel="whatsapp")
    await registry.create("tg37120000001", label="tg", channel="telegram")

    r = await panel_client.get(f"/api/sessions/{SESSION_ID}/contacts")
    assert r.status_code == 400 and r.json()["detail"] == "Outreach is not available for WhatsApp accounts."
    r = await panel_client.post(f"/api/sessions/{SESSION_ID}/outreach", json={"chat_ids": [1], "goal": "hi"})
    assert r.status_code == 400 and r.json()["detail"] == "Outreach is not available for WhatsApp accounts."
    # A Telegram account still gets as far as asking its (absent) worker.
    import panel

    monkeypatch_timeout = panel.LIVE_ACTION_TIMEOUT
    panel.LIVE_ACTION_TIMEOUT = 0.2
    try:
        r = await panel_client.get("/api/sessions/tg37120000001/contacts")
    finally:
        panel.LIVE_ACTION_TIMEOUT = monkeypatch_timeout
    assert r.status_code != 400


@pytest.mark.asyncio
async def test_the_session_list_and_status_carry_the_channel(panel_client, pg_pool):
    registry = SessionRegistry(pg_pool)
    await registry.create(SESSION_ID, label="wa", channel="whatsapp")
    await registry.create("tg37120000001", label="tg", channel="telegram")
    channels = {s["session_id"]: s["channel"] for s in (await panel_client.get("/api/sessions")).json()}
    assert channels == {SESSION_ID: "whatsapp", "tg37120000001": "telegram"}
    status = (await panel_client.get(f"/api/sessions/{SESSION_ID}/status")).json()
    assert status["channel"] == "whatsapp" and status["telegram_connected"] is False


@pytest.mark.asyncio
async def test_saving_account_settings_keeps_the_whatsapp_browser(panel_client, pg_pool, gateway):
    await panel_client.post("/api/wa/pair/start", json=start_body())
    stored = await config_store.load(pg_pool, SESSION_ID)
    form = {k: v for k, v in stored.items() if k != "identity"}
    r = await panel_client.put(f"/api/sessions/{SESSION_ID}/config", json=form)
    assert r.status_code == 200, r.text
    assert r.json()["identity"]["wa_browser"] == wa_device_profiles.derive(SESSION_ID)


def test_the_whatsapp_hold_has_a_label():
    assert controls.LABELS[controls.WHATSAPP] == "stopped after a WhatsApp error"


# ------------------------------------------------------------- identity


def test_the_browser_identity_is_deterministic_and_realistic():
    ids = [f"wa3712000{n:04d}" for n in range(40)]
    first = [wa_device_profiles.derive(i) for i in ids]
    assert first == [wa_device_profiles.derive(i) for i in ids]
    assert all(tuple(b) in wa_device_profiles.PROFILES for b in first)
    assert len({tuple(b) for b in first}) > 2  # spread across the list, not one for everyone
    for os_name, browser, version in wa_device_profiles.PROFILES:
        assert os_name in ("Mac OS", "Windows", "Ubuntu") and browser in ("Chrome", "Edge", "Safari") and version


def test_config_store_keeps_wa_browser_beside_the_telegram_identity():
    telegram = {"device_model": "iPhone 13", "system_version": "17.5.1", "app_version": "10.14",
                "lang_code": "lv", "system_lang_code": "lv-LV", "lang_pack": "", "tz_offset": 10800}
    clean = config_store.normalize({"identity": {**telegram, "wa_browser": ["Mac OS", "Chrome", "14.4.1"]}})
    assert clean["identity"] == {**telegram, "wa_browser": ["Mac OS", "Chrome", "14.4.1"]}
    assert config_store.normalize({"identity": telegram})["identity"] == {**telegram, "wa_browser": []}
    assert config_store.normalize({})["identity"]["wa_browser"] == []
    for bad in (["Mac OS", "Chrome"], ["Mac OS", "", "1"], "Mac OS,Chrome,1", [1, 2, None], None):
        assert config_store.normalize({"identity": {"wa_browser": bad}})["identity"]["wa_browser"] == [], bad


# --------------------------------------------------------- Pairings itself


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _noop(_pairing, _event) -> None:
    return None


@pytest_asyncio.fixture
async def fake_bus():
    import fakeredis

    bus = commands.CommandBus(fakeredis.FakeAsyncRedis(decode_responses=True))
    yield bus
    await bus.close()


@pytest.mark.asyncio
async def test_a_pairing_nobody_finishes_expires_and_is_cancelled_at_the_gateway(fake_bus):
    clock = Clock()
    pairings = wa_pairing.Pairings(ttl=60, clock=clock)
    async with FakeGateway(fake_bus._redis) as gw:
        pairing = await pairings.open(fake_bus, session_id="wa1", method="qr", deepseek_key="k", on_paired=_noop)
        clock.now += 61
        for _ in range(60):
            if pairing.finished:
                break
            await asyncio.sleep(0.05)
        assert pairing.status == "expired" and pairing.error
        for _ in range(60):
            if gw.calls:
                break
            await asyncio.sleep(0.05)
        assert gw.args("pair_cancel") == {"pair_id": pairing.pair_id}
    await pairings.close()


@pytest.mark.asyncio
async def test_pairings_are_capped_and_one_number_has_one(fake_bus):
    pairings = wa_pairing.Pairings(max_active=2)
    async with FakeGateway(fake_bus._redis) as gw:
        first = await pairings.open(fake_bus, session_id="wa1", method="qr", deepseek_key="", on_paired=_noop)
        again = await pairings.open(fake_bus, session_id="wa1", method="code", deepseek_key="", on_paired=_noop)
        assert first.status == "cancelled" and gw.args("pair_cancel") == {"pair_id": first.pair_id}
        await pairings.open(fake_bus, session_id="wa2", method="qr", deepseek_key="", on_paired=_noop)
        with pytest.raises(wa_pairing.TooManyPairings):
            await pairings.open(fake_bus, session_id="wa3", method="qr", deepseek_key="", on_paired=_noop)
        assert len(pairings.active()) == 2 and again in pairings.active()
    await pairings.close()


@pytest.mark.asyncio
async def test_a_gateway_error_kind_reaches_the_caller(fake_bus):
    async with FakeGateway(fake_bus._redis) as gw:
        gw.replies["pair"] = {"ok": False, "error": "Busy: lease held", "error_kind": "busy"}
        with pytest.raises(commands.CommandError) as info:
            await fake_bus.dispatch("@wa-gateway", "pair", {}, timeout=2)
        assert info.value.kind == "busy" and str(info.value) == "Busy: lease held"


@pytest.mark.asyncio
async def test_subscribe_channel_hears_any_channel(fake_bus):
    async with fake_bus.subscribe_channel("wa:pair:abc") as pubsub:
        await fake_bus._redis.publish("wa:pair:abc", "hello")
        for _ in range(20):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is not None:
                break
        assert message["data"] == "hello"
