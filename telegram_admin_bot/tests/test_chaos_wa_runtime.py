"""Crash tests for the WhatsApp path of the runtime (audit lane B).

What the Telegram-era crash tests (test_chaos.py) do not reach: the
keepalive that re-opens the socket through the gateway, a lost session
whose event never arrived, sends the gateway never answers, the wa_inbox
handoff under concurrent drains and a failing ack, bursts of fifty
contacts, the event subscription without Valkey, and per-chat memory.

The gateway is FakeBus from test_whatsapp_runtime.py; Postgres is real
(the pg_pool fixture); nothing here touches WhatsApp or Valkey.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import ai_responder
import alerts
import commands
import controls
import health
import session_runtime
import wa_store
import whatsapp_transport
from database import DIR_OUT, STATUS_ERROR, STATUS_RECEIVED, STATUS_SENT
from test_whatsapp_runtime import ANNA_PN, GATEWAY, SID, a_chat, inbox_count, pair, put_inbox, wa  # noqa: F401
from transport import Inbound
from whatsapp_transport import GatewayError

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

REAL_SLEEP = asyncio.sleep


def stop_after(n: int):
    """An asyncio.sleep for the transport's loops: yields, and ends the loop
    (CancelledError, as a stop would) after n calls."""
    calls = {"n": 0}

    async def sleep(_seconds=0, *a, **k):
        calls["n"] += 1
        if calls["n"] >= n:
            raise asyncio.CancelledError
        await REAL_SLEEP(0)

    return sleep, calls


# ------------------------------------------------------------ keepalive


async def test_the_keepalive_survives_a_failing_round(wa, monkeypatch):
    """A database blip inside on_connected used to end the keepalive task
    for good: no reconnect after a gateway restart, no inbox drains."""
    boom = {"left": 1}

    async def flaky_on_connected():
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("database went away")

    monkeypatch.setattr(wa, "on_connected", flaky_on_connected)
    sleep, calls = stop_after(3)
    monkeypatch.setattr(whatsapp_transport.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await wa.transport._keep_open()
    assert wa.bus.actions().count("open") == 3, "the loop went on after the failing round"
    assert wa.transport.connected


async def test_a_gateway_restart_is_ridden_out_by_the_keepalive(wa, pg_pool):
    answers = [commands.CommandTimeout("no worker runs @wa-gateway"), {"state": "opening"}, {"state": "open"}]

    def open_reply(args):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    wa.bus.replies["open"] = open_reply
    await wa.transport._open_once()
    assert not wa.transport.connected and "not answering" in wa.transport.error
    assert "not answering" in (await health.overview(pg_pool, wa.tenant_id))["last_error"]
    await wa.transport._open_once()                      # gateway back, socket connecting
    assert not wa.transport.connected
    await wa.transport._open_once()                      # open: connected again, no pairing
    assert wa.transport.connected and wa.transport.error is None
    assert (await wa.registry.get(SID))["state"] == "running"


# --------------------------------------------------------- session lost


async def test_a_lost_session_is_never_reopened_by_the_keepalive(wa, pg_pool, monkeypatch):
    """connectionReplaced (440): if the keepalive kept sending `open`, the
    gateway would open a socket again and WhatsApp would bounce the two
    sessions off each other (ping-pong, then a ban)."""
    await pair(pg_pool)
    await wa.transport._open_once()
    wa.bus.calls.clear()
    await wa.transport._on_event(json.dumps({"v": 1, "type": "session_lost", "session_id": SID,
                                             "reason": "connectionReplaced", "code": 440}))
    sleep, _ = stop_after(3)
    monkeypatch.setattr(whatsapp_transport.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await wa.transport._keep_open()
    assert "open" not in wa.bus.actions()
    # A second lost event (the gateway restarting and failing again) halts nothing twice.
    await wa.transport._on_event(json.dumps({"v": 1, "type": "session_lost", "session_id": SID,
                                             "reason": "loggedOut", "code": 401}))
    assert len([h for h in await controls.holds(pg_pool, wa.tenant_id) if h["kind"] == controls.WHATSAPP]) == 1


async def test_open_refused_as_session_lost_halts_even_when_the_event_was_missed(wa, pg_pool, caplog):
    """The gateway lost the login while the bus dropped its event (or we
    were restarting): the keepalive's next `open` is refused with
    session_lost. That must halt like the event would have, not log a
    refusal every 15 s for ever."""
    await pair(pg_pool)
    wa.bus.replies["open"] = commands.CommandError("session_lost: loggedOut; re-pair the number", kind="session_lost")
    await wa.transport._open_once()
    assert wa.transport.lost
    assert "HALTING ALL AUTOMATION: WhatsApp session lost (loggedOut)" in caplog.text
    assert [h["kind"] for h in await controls.holds(pg_pool, wa.tenant_id)] == [controls.WHATSAPP]
    assert (await wa.registry.get(SID))["state"] == "needs_login"
    assert not await wa_store.has_login(pg_pool, SID)


async def test_a_lost_session_is_loud_in_the_panel(wa, pg_pool):
    await pair(pg_pool)
    await wa.transport._open_once()
    await wa.transport.session_lost("badSession", 500)
    status = wa.status()
    assert status["telegram_connected"] is False and "session lost" in status["telegram_error"]
    assert "corrupt" in status["telegram_error"]
    open_alerts = await alerts.list_alerts(pg_pool, open_only=True, tenant_id=wa.tenant_id)
    assert [(a["kind"], a["severity"]) for a in open_alerts] == [(controls.WHATSAPP, alerts.CRITICAL)]
    assert "session lost (badSession)" in open_alerts[0]["message"]
    # The watchdog's own row: status logged_out, with its own alert.
    assert (await health.check_all(pg_pool))[wa.tenant_id] == health.LOGGED_OUT
    overview = await health.overview(pg_pool, wa.tenant_id)
    assert overview["status"] == health.LOGGED_OUT and "session lost" in overview["last_error"]
    kinds = {a["kind"] for a in await alerts.list_alerts(pg_pool, open_only=True, tenant_id=wa.tenant_id)}
    assert kinds == {controls.WHATSAPP, "health:logged_out"}
    # Nothing can send meanwhile.
    with pytest.raises(session_runtime.SendBlocked):
        await wa.ensure_may_send("bot")


# ------------------------------------------------------------- sending


async def auto_send(wa, monkeypatch):
    async def reply(**kw):
        return "Yes, until 7."

    async def nothing(*a, **k):
        return None

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(ai_responder, "generate_reply", reply)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", nothing)
    monkeypatch.setattr(wa, "borrowed_context", no_context)
    wa.deepseek_key = "k"
    wa.config["auto_send"] = True
    wa.config["quiet_hours"]["enabled"] = False
    wa.config["api_spend_cap_eur"] = 0
    wa.config["human"]["typing_indicator"] = False


async def test_a_send_the_gateway_never_answers_is_red_and_never_sent_twice(wa, pg_pool, monkeypatch):
    """The bus round trip times out although the gateway may have sent the
    message: it is stored red (visible), and never retried."""
    await auto_send(wa, monkeypatch)
    wa.bus.replies["send_text"] = commands.CommandTimeout("send_text: no reply from @wa-gateway in 45s")
    chat_id = await a_chat(wa, pg_pool)
    await wa.db.record_message(chat_id, "in", STATUS_RECEIVED, "Open today?", wa_message_id="X1")
    await wa.draft_worker(chat_id)
    await REAL_SLEEP(0.05)
    assert wa.bus.actions().count("send_text") == 1
    out = [(m["status"], m["text"]) for m in await wa.db.get_messages(chat_id) if m["direction"] == DIR_OUT]
    assert (STATUS_ERROR, "Yes, until 7.") in out
    assert not any(s == STATUS_SENT for s, _ in out)
    assert wa.draft_tasks == {}, "no retry was scheduled"


@pytest.mark.parametrize("error", [
    commands.CommandError("bad_request: jid must be a 1:1 chat jid", kind="bad_request"),
    commands.CommandError("not_connected: socket is connecting", kind="not_connected"),
    commands.CommandTimeout("no worker runs @wa-gateway"),
])
async def test_a_refused_send_is_red_not_dropped(wa, pg_pool, error):
    wa.bus.replies["send_text"] = error
    chat_id = await a_chat(wa, pg_pool)
    with pytest.raises(GatewayError):
        await wa.send_as_me(chat_id, "See you at 3", guard=False)
    rows = [(m["status"], m["text"]) for m in await wa.db.get_messages(chat_id) if m["direction"] == DIR_OUT]
    assert rows == [(STATUS_ERROR, "See you at 3")]
    assert wa.in_flight_sends == {}


async def test_feature_gaps_fail_cleanly(wa, pg_pool, tmp_path, monkeypatch):
    """Not features yet on WhatsApp: no photo download, no file sends. They
    must say so, not crash the handler."""
    chat_id = await a_chat(wa, pg_pool)

    async def peer():
        raise AssertionError("not needed")

    message = Inbound(chat_id=chat_id, text="", external_id="P1", load_peer=peer, has_photo=True)
    wa.config["vision"]["enabled"] = True
    wa.config["vision"]["model"] = "m"
    monkeypatch.setattr(session_runtime.vision, "endpoint_from_env", lambda: ("http://x", "k"))
    assert await wa.read_photo(chat_id, message) is None
    with pytest.raises(GatewayError) as info:
        await wa.transport.send_file(ANNA_PN, chat_id, Path(tmp_path / "x.jpg"), False, False)
    assert info.value.kind == "bad_request"
    assert wa.transport.can_send_files is False


# ------------------------------------------------------------- inbox


async def test_two_drains_at_once_handle_a_message_once(wa, pg_pool):
    """The inbox event and the keepalive can both drain at the same moment."""
    await put_inbox(pg_pool, "3EB0CC01")
    await asyncio.gather(wa.transport.drain(), wa.transport.drain(), wa.transport.drain())
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    assert len(await wa.db.get_messages(chat_id)) == 1
    assert wa.scheduled == [chat_id]
    assert await inbox_count(pg_pool) == 0


async def test_an_ack_that_fails_does_not_handle_the_message_twice(wa, pg_pool, monkeypatch):
    """Stored, then the DELETE of the handoff row fails (database gone for
    a moment): the row is seen again on the next drain and recognised."""
    real_ack = wa_store.inbox_ack
    fail = {"left": 1}

    async def flaky_ack(pool, session_id, row_id):
        if fail["left"]:
            fail["left"] -= 1
            raise ConnectionError("database went away")
        await real_ack(pool, session_id, row_id)

    monkeypatch.setattr(wa_store, "inbox_ack", flaky_ack)
    await put_inbox(pg_pool, "3EB0CC02", text="Hello?")
    with pytest.raises(ConnectionError):
        await wa.transport.drain()
    assert await inbox_count(pg_pool) == 1
    await wa.transport.drain()
    assert await inbox_count(pg_pool) == 0
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    assert len(await wa.db.get_messages(chat_id)) == 1
    assert wa.scheduled == [chat_id], "one reply drafted"


async def test_a_burst_of_fifty_contacts(wa, pg_pool):
    for n in range(50):
        jid = f"3460000{n:04d}@s.whatsapp.net"
        await put_inbox(pg_pool, f"3EB0B{n:04d}", jid=jid, phone_jid=jid, push_name=f"P{n}", text=f"hi {n}")
    assert await wa.transport.drain() == 50
    assert await inbox_count(pg_pool) == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM wa_peers WHERE session_id = $1", SID) == 50
    assert len(set(wa.scheduled)) == 50
    assert wa.in_flight_sends == {} and wa.in_flight_media == {}


async def test_a_burst_of_fifty_messages_from_one_contact_schedules_one_reply_at_a_time(wa, pg_pool, monkeypatch):
    # The real schedule_draft: a newer message replaces the pending draft.
    monkeypatch.setattr(wa, "schedule_draft", session_runtime.SessionRuntime.schedule_draft.__get__(wa))
    started: list[int] = []

    async def slow_draft(chat_id, attempt=0):
        started.append(chat_id)
        await REAL_SLEEP(10)

    monkeypatch.setattr(wa, "draft_worker", slow_draft)
    for n in range(50):
        await put_inbox(pg_pool, f"3EB0S{n:04d}", text=f"line {n}")
    assert await wa.transport.drain() == 50
    await REAL_SLEEP(0)
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    assert len(await wa.db.get_messages(chat_id)) == 50
    live = [t for t in wa.draft_tasks.values() if not t.done()]
    assert len(live) == 1, "one draft task per chat, the earlier ones cancelled"
    for task in wa.draft_tasks.values():
        task.cancel()
    await asyncio.gather(*wa.draft_tasks.values(), return_exceptions=True)


# ------------------------------------------------------------- memory


async def test_read_receipt_memory_is_bounded(wa, pg_pool):
    await wa.transport._open_once()
    for n in range(whatsapp_transport.READ_UPTO_MAX + 200):
        chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=f"3461{n:07d}@s.whatsapp.net")
        await wa.transport.mark_read(chat_id, f"3EB0R{n}")
    assert len(wa.transport._read_upto) == whatsapp_transport.READ_UPTO_MAX


# ------------------------------------------------------------- valkey


async def test_the_event_subscription_keeps_retrying_without_valkey(wa, monkeypatch):
    def refused(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(whatsapp_transport.aioredis, "from_url", refused)
    sleep, calls = stop_after(4)
    monkeypatch.setattr(whatsapp_transport.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await wa.transport._listen()
    assert calls["n"] == 4
    # Messages still flow meanwhile: the keepalive drains the inbox without events.
    await put_inbox(wa.pool, "3EB0V001")
    assert await wa.transport.drain() == 1
