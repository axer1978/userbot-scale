"""WhatsApp accounts in the runtime (build step 5): the transport that
drives a socket in wa-gateway over the bus, the wa_inbox handoff, the
wa_peers chat identity, and a lost session halting loudly.

The gateway itself is faked: FakeBus answers its commands. Everything needs
Postgres (the pg_pool fixture skips without PG_TEST_DSN).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

import audit
import commands
import controls
import crypto
import health
import session_runtime
import wa_store
from conftest import FakeHub, seed_session
from database import DIR_OUT, STATUS_PENDING, STATUS_RECEIVED, STATUS_SENT
from whatsapp_transport import GATEWAY, WhatsAppTransport

SID = "wa34600111222"
ANNA_PN = "34600123456@s.whatsapp.net"
ANNA_LID = "111222333@lid"


class FakeBus:
    """The bus as the WhatsApp transport uses it; `replies[action]` is what
    the gateway answers (an exception is raised)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.replies: dict[str, Any] = {"open": {"state": "open"}, "close": {"closed": True}}

    async def dispatch(self, target, action, args=None, *, timeout=30.0):
        self.calls.append((target, action, dict(args or {})))
        reply = self.replies.get(action)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def publish_event(self, session_id, payload):
        pass

    def actions(self) -> list[str]:
        return [a for _, a, _ in self.calls]


@pytest_asyncio.fixture
async def wa(pg_pool, tmp_path, monkeypatch):
    await seed_session(pg_pool, SID)
    await pg_pool.execute("UPDATE telegram_sessions SET channel = 'whatsapp' WHERE session_id = $1", SID)
    rt = session_runtime.SessionRuntime(pg_pool, SID, data_dir=tmp_path, redis_url="redis://unused")
    rt.hub = FakeHub()
    await rt.db.connect()
    await rt._choose_transport()
    await rt.bind_tenant()
    rt.bus = FakeBus()
    rt.lease_epoch = 7
    scheduled: list[int] = []
    monkeypatch.setattr(rt, "schedule_draft", lambda chat_id: scheduled.append(chat_id))
    rt.scheduled = scheduled
    yield rt
    await rt.db.close()


async def put_inbox(pool, wa_message_id: str, **payload) -> None:
    body = {"v": 1, "session_id": SID, "epoch": 7, "wa_message_id": wa_message_id, "jid": ANNA_PN,
            "phone_jid": ANNA_PN, "lid": None, "push_name": "Anna", "from_me": False, "type": "text",
            "text": "Hello", "quoted_id": None, "ts": 1_900_000_000, **payload}
    await pool.execute(
        "INSERT INTO wa_inbox (session_id, wa_message_id, payload) VALUES ($1, $2, $3::jsonb)",
        SID, wa_message_id, json.dumps(body),
    )


async def inbox_count(pool) -> int:
    return await pool.fetchval("SELECT count(*) FROM wa_inbox WHERE session_id = $1", SID)


async def pair(pool) -> None:
    """What wa-gateway leaves behind after pairing: an encrypted creds row."""
    blob = crypto.encrypt(b"{}", aad=crypto.aad_for(SID, "wa_auth:creds:"))
    await pool.execute("INSERT INTO wa_auth_state (session_id, kind, key_id, value_enc) VALUES ($1, 'creds', '', $2)",
                       SID, blob)


# ------------------------------------------------------------ the transport


@pytest.mark.asyncio
async def test_a_whatsapp_row_gets_the_whatsapp_transport(wa):
    assert isinstance(wa.transport, WhatsAppTransport)
    assert wa.db.channel == "whatsapp" and wa.booking_store.channel == "whatsapp"
    assert wa.status()["channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_no_login_means_needs_login(wa, pg_pool):
    with pytest.raises(session_runtime.NeedsLogin):
        await wa.transport.prepare()
    await pair(pg_pool)
    await wa.transport.prepare()


@pytest.mark.asyncio
async def test_open_carries_the_lease_epoch_and_connects(wa, pg_pool):
    await wa.transport._open_once()
    (target, action, args), = wa.bus.calls
    assert (target, action) == (GATEWAY, "open")
    assert args == {"session_id": SID, "epoch": 7}
    assert wa.transport.connected
    row = await wa.registry.get(SID)
    assert row["state"] == "running"


@pytest.mark.asyncio
async def test_stored_browser_identity_is_sent(wa):
    wa.account = {**wa.account, "identity": {**(wa.account.get("identity") or {}),
                                             "wa_browser": ["Mac OS", "Chrome", "14.4.1"]}}
    await wa.transport._open_once()
    assert wa.bus.calls[0][2]["browser"] == ["Mac OS", "Chrome", "14.4.1"]


@pytest.mark.asyncio
async def test_gateway_down_is_reported_not_fatal(wa):
    wa.bus.replies["open"] = commands.CommandTimeout("nobody answered")
    await wa.transport._open_once()
    assert not wa.transport.connected
    assert "not answering" in wa.transport.error
    # It comes back on the next keepalive, with no pairing.
    wa.bus.replies["open"] = {"state": "open"}
    await wa.transport._open_once()
    assert wa.transport.connected


@pytest.mark.asyncio
async def test_stop_closes_the_socket_with_the_epoch(wa):
    await wa.transport.disconnect()
    assert wa.bus.calls == [(GATEWAY, "close", {"session_id": SID, "epoch": 7})]


@pytest.mark.asyncio
async def test_sending_is_not_switched_on_yet(wa):
    assert wa.transport.can_send is False
    with pytest.raises(RuntimeError):
        await wa.transport.send_text(ANNA_PN, 1, "hi", None)


def test_prompt_names_whatsapp():
    t = WhatsAppTransport(object())
    assert t.adapt_prompt("You write replies in a Telegram chat.") == "You write replies in a WhatsApp chat."


# ----------------------------------------------------------- chat identity


@pytest.mark.asyncio
async def test_one_chat_per_person_and_the_pair_is_completed(pg_pool):
    await seed_session(pg_pool, SID)
    first, jid = await wa_store.chat_for(pg_pool, SID, lid=ANNA_LID, push_name="Anna")
    assert jid == ANNA_LID
    # Later WhatsApp says the LID and the phone number are one person.
    again, jid = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN, lid=ANNA_LID)
    assert again == first and jid == ANNA_PN          # replies now go to the number
    by_phone, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    assert by_phone == first
    row = await wa_store.peer(pg_pool, SID, first)
    assert row["push_name"] == "Anna"                 # not wiped by push_name=None
    other, _ = await wa_store.chat_for(pg_pool, SID, phone_jid="34600999999@s.whatsapp.net")
    assert other != first


@pytest.mark.asyncio
async def test_two_halves_seen_apart_resolve_to_the_phone_chat(pg_pool):
    await seed_session(pg_pool, SID)
    by_lid, _ = await wa_store.chat_for(pg_pool, SID, lid=ANNA_LID)
    by_phone, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    chat, jid = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN, lid=ANNA_LID)
    assert by_lid != by_phone and chat == by_phone and jid == ANNA_PN


def test_jid_helpers():
    assert wa_store.split_jid(ANNA_PN) == (ANNA_PN, None)
    assert wa_store.split_jid(ANNA_LID) == (None, ANNA_LID)
    assert wa_store.split_jid("123@g.us") == (None, None)
    assert wa_store.phone_of("34600123456:12@s.whatsapp.net") == "+34600123456"
    assert wa_store.phone_jid_for("+34 600 12 34 56") == "34600123456@s.whatsapp.net"
    with pytest.raises(ValueError):
        wa_store.phone_jid_for("hello")


# ------------------------------------------------------------------ inbox


@pytest.mark.asyncio
async def test_inbox_message_is_stored_acked_and_drafted(wa, pg_pool):
    await put_inbox(pg_pool, "3EB0AA01", text=" Are you open today? ")
    assert await wa.transport.drain() == 1
    assert await inbox_count(pg_pool) == 0
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    (row,) = await wa.db.get_messages(chat_id)
    assert (row["status"], row["text"], row["wa_message_id"], row["telegram_id"]) == \
        (STATUS_RECEIVED, "Are you open today?", "3EB0AA01", None)
    conversation = await wa.db.get_conversation(chat_id)
    assert conversation["display_name"] == "Anna"
    ref = await pg_pool.fetchval("SELECT customer_ref FROM conversations WHERE session_id = $1 AND chat_id = $2",
                                 SID, chat_id)
    assert ref == crypto.customer_ref(wa.tenant_id, "whatsapp", chat_id)
    assert wa.scheduled == [chat_id]


@pytest.mark.asyncio
async def test_redelivery_is_stored_once_and_handled_once(wa, pg_pool):
    await put_inbox(pg_pool, "3EB0AA02")
    await wa.transport.drain()
    await put_inbox(pg_pool, "3EB0AA02")      # the gateway hands it over again
    await wa.transport.drain()
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    assert len(await wa.db.get_messages(chat_id)) == 1
    assert wa.scheduled == [chat_id]
    assert await inbox_count(pg_pool) == 0


@pytest.mark.asyncio
async def test_a_failed_store_leaves_the_message_in_the_inbox(wa, pg_pool, monkeypatch):
    await put_inbox(pg_pool, "3EB0AA03")

    async def broken(message):
        raise RuntimeError("database went away")

    monkeypatch.setattr(wa, "handle_inbound", broken)
    with pytest.raises(RuntimeError):
        await wa.transport.drain()
    assert await inbox_count(pg_pool) == 1       # still there for the next drain


@pytest.mark.asyncio
async def test_typed_on_the_phone_is_stored_and_takes_over(wa, pg_pool):
    wa.config["takeover_hours"] = 12
    await put_inbox(pg_pool, "3EB0AA04", text="Hi, customer")
    await wa.transport.drain()
    await put_inbox(pg_pool, "3EB0AA05", from_me=True, push_name="The Salon", text="I'll take this one")
    await wa.transport.drain()
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN)
    sent = [m for m in await wa.db.get_messages(chat_id) if m["direction"] == DIR_OUT]
    assert [(m["status"], m["wa_message_id"]) for m in sent] == [(STATUS_SENT, "3EB0AA05")]
    conversation = await wa.db.get_conversation(chat_id)
    assert conversation["display_name"] == "Anna"          # our own push name never renames the chat
    assert wa.takeover_until(conversation) is not None


@pytest.mark.asyncio
async def test_messages_without_a_usable_jid_are_dropped(wa, pg_pool):
    await put_inbox(pg_pool, "3EB0AA06", jid="120363@g.us", phone_jid=None, lid=None)
    assert await wa.transport.drain() == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM messages WHERE session_id = $1", SID) == 0


@pytest.mark.asyncio
async def test_inbox_event_triggers_a_drain(wa, pg_pool):
    await put_inbox(pg_pool, "3EB0AA07")
    await wa.transport._on_event(json.dumps({"v": 1, "type": "inbox", "session_id": SID}))
    assert await inbox_count(pg_pool) == 0


# ---------------------------------------------------- nothing is sent yet


@pytest.mark.asyncio
async def test_auto_send_still_drafts_for_approval(wa, pg_pool, monkeypatch):
    import ai_responder

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
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN, push_name="Anna")
    await wa.db.upsert_conversation(chat_id, "Anna", None, False, None)
    await wa.db.record_message(chat_id, "in", STATUS_RECEIVED, "Open today?", wa_message_id="X1")
    await wa.draft_worker(chat_id)
    drafts = [m for m in await wa.db.get_messages(chat_id) if m["status"] == STATUS_PENDING]
    assert [d["text"] for d in drafts] == ["Yes, until 7."]
    assert "send_text" not in wa.bus.actions()


# --------------------------------------------------------- session lost


@pytest.mark.asyncio
async def test_session_lost_halts_loudly_and_never_reopens(wa, pg_pool, caplog):
    await pair(pg_pool)
    await wa.transport._open_once()
    await wa.transport._on_event(json.dumps({"v": 1, "type": "session_lost", "session_id": SID,
                                             "reason": "loggedOut", "code": 401}))
    assert "HALTING ALL AUTOMATION: WhatsApp session lost (loggedOut)" in caplog.text
    holds = await controls.holds(pg_pool, wa.tenant_id)
    assert [h["kind"] for h in holds] == [controls.WHATSAPP]
    assert wa.off_reason and "stopped after a WhatsApp error" in wa.off_reason
    row = await wa.registry.get(SID)
    assert row["state"] == "needs_login" and "Pair the account again" in row["state_reason"]
    assert not await wa_store.has_login(pg_pool, SID)
    events = [e["event"] for e in await audit.list_events(pg_pool, tenant_id=wa.tenant_id)]
    assert audit.ACCOUNT_HALTED in events
    assert "halted" in wa.hub.types()
    assert not wa.transport.connected and wa.finished is False   # keeps its lease: the dot shows red
    # The keepalive skips a lost session: nothing asks the gateway to reopen it.
    assert wa.transport.lost
    # And a restarted worker can't start it: the login is gone.
    with pytest.raises(session_runtime.NeedsLogin):
        await wa.transport.prepare()


@pytest.mark.asyncio
async def test_a_ban_marks_the_account_revoked(wa, pg_pool):
    await pair(pg_pool)
    await wa.transport.session_lost("forbidden", 403)
    row = await wa.registry.get(SID)
    assert row["state"] == "revoked"
    assert "may be banned" in row["state_reason"]


@pytest.mark.asyncio
async def test_health_watchdog_sees_paired_whatsapp_accounts(wa, pg_pool):
    before = await health.check_all(pg_pool)
    assert wa.tenant_id not in before                # never paired: not watched
    await pair(pg_pool)
    after = await health.check_all(pg_pool)
    assert wa.tenant_id in after


# -------------------------------------------------------------- bookings


@pytest.mark.asyncio
async def test_owner_is_a_phone_number_and_replies_match_by_quoted_id(wa, pg_pool):
    await wa.transport._open_once()
    chat_id, peer = await wa.transport.resolve_owner("+34 600 555 666")
    assert peer.name == "+34600555666"
    assert (await wa_store.peer(pg_pool, SID, chat_id))["jid"] == "34600555666@s.whatsapp.net"
    assert wa.flow.owner_message_ref({"wa_message_id": "3EB0OWN", "telegram_id": None}) == \
        {"provider_wa_message_id": "3EB0OWN"}
