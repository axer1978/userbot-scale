"""WhatsApp accounts in the runtime (build step 5): the transport that
drives a socket in wa-gateway over the bus, the wa_inbox handoff, the
wa_peers chat identity, and a lost session halting loudly.

The gateway itself is faked: FakeBus answers its commands. Everything needs
Postgres (the pg_pool fixture skips without PG_TEST_DSN).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import pytest_asyncio

import alerts
import audit
import commands
import controls
import crypto
import health
import media
import session_runtime
import wa_device_profiles
import wa_store
from conftest import FakeHub, seed_session
from transport import Inbound
from database import DIR_OUT, STATUS_ERROR, STATUS_PENDING, STATUS_RECEIVED, STATUS_SENT
from whatsapp_transport import GATEWAY, GatewayError, WhatsAppTransport, gateway_error, group_ref

SID = "wa34600111222"
ANNA_PN = "34600123456@s.whatsapp.net"
ANNA_LID = "111222333@lid"


class FakeBus:
    """The bus as the WhatsApp transport uses it; `replies[action]` is what
    the gateway answers (an exception is raised, a callable is called)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.sent = 0
        self.replies: dict[str, Any] = {
            "open": {"state": "open"}, "close": {"closed": True}, "presence": {"ok": True},
            "read": {"ok": True}, "delete": {"ok": True}, "logout": {"logged_out": True}, "send_text": self._sent,
            "send_media": self._sent,
            "download_media": {"data": base64.b64encode(b"\xff\xd8\xff door").decode(), "bytes": 8,
                               "mimetype": "image/jpeg"},
        }

    def _sent(self, args):
        self.sent += 1
        return {"message_id": f"3EB0OUT{self.sent}", "ts": 1_900_000_000}

    async def dispatch(self, target, action, args=None, *, timeout=30.0):
        self.calls.append((target, action, dict(args or {})))
        if target != GATEWAY:
            raise commands.CommandTimeout(f"no worker runs {target}")
        reply = self.replies.get(action)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(args)
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
    # Halts and hard-offs raise alerts, delivered in the background: let
    # them finish on this test's loop, or a later test's drain() waits on
    # a task whose loop is gone.
    await alerts.drain()
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
    assert args == {"session_id": SID, "epoch": 7, "browser": list(wa_device_profiles.derive(SID)), "groups": []}
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


async def a_chat(wa, pg_pool, *, connected: bool = True) -> int:
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, phone_jid=ANNA_PN, push_name="Anna")
    await wa.db.upsert_conversation(chat_id, "Anna", None, False, None)
    if connected:
        await wa.transport._open_once()
        wa.bus.calls.clear()
    return chat_id


@pytest.mark.asyncio
async def test_a_send_goes_to_the_gateway_fenced_by_the_epoch(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_as_me(chat_id, "See you at 3", guard=False)
    assert wa.bus.calls == [(GATEWAY, "send_text", {"session_id": SID, "epoch": 7, "jid": ANNA_PN,
                                                    "text": "See you at 3"})]
    assert (row["status"], row["wa_message_id"], row["telegram_id"]) == (STATUS_SENT, "3EB0OUT1", None)


@pytest.mark.asyncio
async def test_typing_shows_while_the_message_goes_out(wa, pg_pool, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(session_runtime.asyncio, "sleep", fake_sleep)
    chat_id = await a_chat(wa, pg_pool)
    wa.config["human"]["typing_indicator"] = True
    await wa.send_as_me(chat_id, "x" * 24, typing=True, guard=False)
    assert [(a, args.get("state")) for _, a, args in wa.bus.calls] == [
        ("presence", "composing"), ("send_text", None), ("presence", "paused")]
    assert len(slept) == 1 and slept[0] > 0


@pytest.mark.asyncio
async def test_a_failed_typing_indicator_never_stops_the_message(wa, pg_pool, monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(session_runtime.asyncio, "sleep", fake_sleep)
    chat_id = await a_chat(wa, pg_pool)
    wa.bus.replies["presence"] = commands.CommandError("not_connected: socket is reconnecting")
    wa.config["human"]["typing_indicator"] = True
    row = await wa.send_as_me(chat_id, "Hello", typing=True, guard=False)
    assert row["status"] == STATUS_SENT


@pytest.mark.asyncio
async def test_a_refused_send_stays_red_in_the_thread(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    wa.bus.replies["send_text"] = commands.CommandError("not_on_whatsapp: 34600123456 is not on WhatsApp")
    with pytest.raises(GatewayError) as caught:
        await wa.send_as_me(chat_id, "Are you there?", guard=False)
    assert caught.value.kind == "not_on_whatsapp"
    (row,) = [m for m in await wa.db.get_messages(chat_id) if m["direction"] == DIR_OUT]
    assert (row["status"], row["text"]) == (STATUS_ERROR, "Are you there?")
    # And the failure is acted on: this person can't be messaged, the chat pauses.
    assert await wa.handle_send_failure(chat_id, caught.value) is True
    assert (await wa.db.get_conversation(chat_id))["automation_paused"] is True


@pytest.mark.asyncio
async def test_a_refused_approved_draft_is_kept_not_dropped(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    draft = await wa.db.record_message(chat_id, DIR_OUT, STATUS_PENDING, "Draft reply", bump_preview=False)
    wa.bus.replies["send_text"] = commands.CommandError("blocked: the person blocked this number")
    with pytest.raises(GatewayError):
        await wa.handle_command("approve_draft", {"draft_id": draft["id"]})
    row = await wa.db.get_message(draft["id"])
    assert (row["status"], row["text"]) == (STATUS_ERROR, "Draft reply")


@pytest.mark.asyncio
async def test_a_rate_limit_halts_like_peer_flood(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    assert await wa.handle_send_failure(chat_id, GatewayError("rate_limited", "rate-overlimit")) is True
    assert [h["kind"] for h in await controls.holds(pg_pool, wa.tenant_id)] == [controls.WHATSAPP]
    assert "WhatsApp returned a rate limit" in wa.off_reason


@pytest.mark.asyncio
async def test_unknown_gateway_errors_are_not_classified(wa):
    assert wa.transport.classify(GatewayError("stale_epoch", "")) is None
    assert wa.transport.classify(ValueError("x")) is None


def test_gateway_error_strings_are_parsed():
    assert gateway_error(commands.CommandError("rate_limited: slow down")).kind == "rate_limited"
    assert gateway_error(commands.CommandTimeout("nobody")).kind == "not_connected"
    assert gateway_error(commands.CommandError("TypeError: boom here")).kind == "TypeError"
    assert gateway_error(commands.CommandError("something odd happened")).kind == "other"
    tagged = gateway_error(commands.CommandError("busy: pairing running", kind="busy"))
    assert (tagged.kind, tagged.detail) == ("busy", "pairing running")


@pytest.mark.asyncio
async def test_read_receipts_cover_new_messages_once(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    for wid in ("IN1", "IN2"):
        await wa.db.record_message(chat_id, "in", STATUS_RECEIVED, "hi", wa_message_id=wid)
    await wa.mark_read(chat_id)
    await wa.mark_read(chat_id)           # nothing new: nothing sent
    await wa.db.record_message(chat_id, "in", STATUS_RECEIVED, "again", wa_message_id="IN3")
    await wa.mark_read(chat_id)
    reads = [args for _, a, args in wa.bus.calls if a == "read"]
    assert [r["message_ids"] for r in reads] == [["IN1", "IN2"], ["IN3"]]
    assert reads[0]["jid"] == ANNA_PN and reads[0]["epoch"] == 7
    wa.config["human"]["mark_read"] = False
    await wa.db.record_message(chat_id, "in", STATUS_RECEIVED, "more", wa_message_id="IN4")
    await wa.mark_read(chat_id)
    assert len([a for _, a, _ in wa.bus.calls if a == "read"]) == 2


@pytest.mark.asyncio
async def test_sent_messages_are_deleted_for_everyone_and_keep_a_placeholder(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_as_me(chat_id, "Door code 4321", guard=False)
    await wa.delete_sent(chat_id, [row["id"]], placeholder="[deleted]", reason="test")
    [args] = [args for _, a, args in wa.bus.calls if a == "delete"]
    assert args["jid"] == ANNA_PN and args["message_ids"] == [row["wa_message_id"]] and args["epoch"] == 7
    assert (await wa.db.get_message(row["id"]))["text"] == "[deleted]"


@pytest.mark.asyncio
async def test_presence_online_and_offline(wa, pg_pool):
    await a_chat(wa, pg_pool)
    await wa.set_presence(True)
    await wa.set_presence(False)
    assert [(a, args["state"]) for _, a, args in wa.bus.calls] == [("presence", "available"),
                                                                    ("presence", "unavailable")]


@pytest.mark.asyncio
async def test_the_media_library_is_offered_in_whatsapp_replies(wa):
    wa.config["media"]["enabled"] = True
    library = wa.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "me.jpg").write_bytes(b"\xff\xd8\xff me")
    item = library.add_file("me.jpg", "me in a red dress")
    assert f"[send {item['id']}]" in wa.media_prompt()


@pytest.mark.asyncio
async def test_a_library_photo_goes_out_as_a_whatsapp_photo(wa, pg_pool):
    library = wa.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "me.jpg").write_bytes(b"\xff\xd8\xff me")
    item = library.add_file("me.jpg", "me in a red dress")
    library.set_flags(item["id"], view_once=True)
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_media_as_me(chat_id, item["id"], guard=False)
    [(target, action, args)] = [c for c in wa.bus.calls if c[1] == "send_media"]
    assert (target, args["session_id"], args["epoch"], args["jid"]) == (GATEWAY, SID, 7, ANNA_PN)
    assert (args["kind"], args["mimetype"], args["view_once"]) == ("image", "image/jpeg", True)
    assert base64.b64decode(args["data"]) == b"\xff\xd8\xff me"
    assert (row["status"], row["wa_message_id"]) == (STATUS_SENT, "3EB0OUT1")
    assert row["text"].startswith("[sent photo #")


@pytest.mark.asyncio
async def test_view_once_turned_off_in_the_panel_applies_to_the_next_send(wa, pg_pool):
    library = wa.media_library
    library.dir.mkdir(parents=True, exist_ok=True)
    (library.dir / "me.jpg").write_bytes(b"\xff\xd8\xff me")
    item = library.add_file("me.jpg", "me in a red dress")
    library.set_flags(item["id"], view_once=True)
    # The panel is another process with its own copy of the library.
    panel = media.MediaLibrary(library.dir)
    panel.set_flags(item["id"], view_once=False)
    chat_id = await a_chat(wa, pg_pool)
    await wa.send_media_as_me(chat_id, item["id"], guard=False)
    [(_, _, args)] = [c for c in wa.bus.calls if c[1] == "send_media"]
    assert args["view_once"] is False


@pytest.mark.asyncio
async def test_an_inbound_photo_is_fetched_through_the_gateway(wa, pg_pool, tmp_path):
    chat_id = await a_chat(wa, pg_pool)
    media = {"kind": "image", "mimetype": "image/jpeg", "url": None, "direct_path": "/v/t62/a",
             "media_key": base64.b64encode(b"k" * 32).decode(), "file_length": 8}

    async def peer():
        raise AssertionError("not needed")

    photo = Inbound(chat_id=chat_id, text="", external_id="P1", load_peer=peer, has_photo=True,
                    raw={"type": "image", "media": media})
    assert await wa.transport.download_photo(photo) == b"\xff\xd8\xff door"
    assert wa.bus.calls[-1] == (GATEWAY, "download_media", {"session_id": SID, "epoch": 7, "media": media})
    target = tmp_path / "entrance.jpg"
    assert await wa.transport.download_photo(photo, target) is None
    assert target.read_bytes() == b"\xff\xd8\xff door"

    # A view-once photo comes without a descriptor: nothing to fetch, no call.
    calls = len(wa.bus.calls)
    view_once = Inbound(chat_id=chat_id, text="", external_id="P2", load_peer=peer, has_photo=True,
                        raw={"type": "image"})
    assert await wa.transport.download_photo(view_once) is None
    assert len(wa.bus.calls) == calls


@pytest.mark.asyncio
async def test_hard_off_of_a_running_account_unlinks_the_device(wa, pg_pool, monkeypatch):
    await a_chat(wa, pg_pool)
    monkeypatch.setattr(wa, "spawn", lambda coro, what: coro.close())
    result = await wa.handle_command("hard_off", {"reason": "leaked phone"})
    assert result == {"logged_out": True}
    assert wa.bus.calls[-1] == (GATEWAY, "logout", {"session_id": SID, "epoch": 7})


@pytest.mark.asyncio
async def test_hard_off_of_an_idle_account_goes_through_the_gateway(wa, pg_pool):
    await pair(pg_pool)
    bus = FakeBus()
    result = await controls.hard_off(pg_pool, bus, wa.tenant_id, reason="phone stolen", actor=audit.ADMIN)
    # Nobody answered the account's own channel (FakeBus answers only the
    # gateway), so the gateway logged the device out under a hard-off lease.
    logout = [args for _, a, args in bus.calls if a == "logout"]
    assert logout and logout[0]["session_id"] == SID and logout[0]["epoch"] > 0
    assert result["logged_out"] is True
    assert not await wa_store.has_login(pg_pool, SID)
    row = await wa.registry.get(SID)
    assert (row["state"], row["is_active"]) == ("revoked", False)


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
    # A chat is labelled with the number; the name only when WhatsApp hides the number.
    assert wa_store.display_name({"chat_id": 5, "phone_jid": ANNA_PN, "push_name": "Anna"}) == "+34600123456"
    assert wa_store.display_name({"chat_id": 5, "phone_jid": None, "lid": ANNA_LID, "push_name": "Anna"}) == "Anna"
    assert wa_store.display_name({"chat_id": 5, "phone_jid": None, "lid": ANNA_LID, "push_name": ""}) ==         "WhatsApp chat 5"


@pytest.mark.asyncio
async def test_a_chat_without_a_known_number_keeps_the_name(wa, pg_pool):
    await put_inbox(pg_pool, "3EB0AA10", jid=ANNA_LID, phone_jid=None, lid=ANNA_LID, push_name="Anna")
    await wa.transport.drain()
    chat_id, _ = await wa_store.chat_for(pg_pool, SID, lid=ANNA_LID)
    assert (await wa.db.get_conversation(chat_id))["display_name"] == "Anna"
    # Later WhatsApp tells the number: the next message labels the chat with it.
    await put_inbox(pg_pool, "3EB0AA11", jid=ANNA_LID, phone_jid=ANNA_PN, lid=ANNA_LID, push_name="Anna")
    await wa.transport.drain()
    assert (await wa.db.get_conversation(chat_id))["display_name"] == "+34600123456"


@pytest.mark.asyncio
async def test_chats_and_bookings_stored_by_name_are_relabelled_at_start(wa, pg_pool):
    from datetime import datetime, timedelta, timezone

    anna, _ = await wa_store.chat_for(pg_pool, SID, phone_jid="34600123456:12@s.whatsapp.net", push_name="Anna")
    hidden, _ = await wa_store.chat_for(pg_pool, SID, lid="999888777@lid", push_name="Bea")
    await wa.db.upsert_conversation(anna, "Anna", None, False, None)
    await wa.db.upsert_conversation(hidden, "Bea", None, False, None)
    start = datetime(2030, 1, 7, 18, tzinfo=timezone.utc)
    await wa.booking_store.create(chat_id=anna, customer_name="Anna", customer_username=None, starts_at=start,
                                  ends_at=start + timedelta(hours=1), buffer_minutes=0, tz="Europe/Riga")
    await wa.booking_store.add_waitlist(chat_id=anna, customer_name="Anna", wanted_from=start,
                                        wanted_to=start + timedelta(hours=2))
    # Another account's chat with the same chat_id is not this account's to change.
    other = "wa34600999000"
    await seed_session(pg_pool, other)
    await pg_pool.execute("INSERT INTO wa_peers (session_id, chat_id, jid, phone_jid, push_name) "
                          "VALUES ($1, $2, $3, $3, 'Carl')", other, anna, "34600777888@s.whatsapp.net")
    await pg_pool.execute("INSERT INTO conversations (session_id, chat_id, display_name) VALUES ($1, $2, 'Carl')",
                          other, anna)

    await pair(pg_pool)
    await wa.transport.prepare()
    assert (await wa.db.get_conversation(anna))["display_name"] == "+34600123456"
    assert (await wa.db.get_conversation(hidden))["display_name"] == "Bea"   # no number known
    assert await pg_pool.fetchval("SELECT customer_name FROM bookings WHERE session_id = $1", SID) == "+34600123456"
    assert await pg_pool.fetchval("SELECT customer_name FROM waitlist WHERE session_id = $1", SID) == "+34600123456"
    assert await pg_pool.fetchval("SELECT display_name FROM conversations WHERE session_id = $1", other) == "Carl"
    assert await wa_store.relabel(pg_pool, SID) == 0      # nothing left the second time


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
    assert conversation["display_name"] == "+34600123456"     # her number, not her name
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
    assert conversation["display_name"] == "+34600123456"
    assert (await wa_store.peer(pg_pool, SID, chat_id))["push_name"] == "Anna"   # our own name never renames her
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


# ------------------------------------------------------------- auto-send


@pytest.mark.asyncio
async def test_auto_send_replies_through_the_gateway(wa, pg_pool, monkeypatch):
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
    wa.config["human"]["typing_indicator"] = False
    chat_id = await a_chat(wa, pg_pool)
    await wa.db.record_message(chat_id, "in", STATUS_RECEIVED, "Open today?", wa_message_id="X1")
    await wa.draft_worker(chat_id)
    sent = [m for m in await wa.db.get_messages(chat_id) if m["direction"] == DIR_OUT]
    assert [(m["status"], m["text"]) for m in sent] == [(STATUS_SENT, "Yes, until 7.")]
    assert "send_text" in wa.bus.actions() and "read" in wa.bus.actions()


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


# ------------------------------------------------- failed after sending


@pytest.mark.asyncio
async def test_a_message_refused_after_sending_turns_red_and_pauses(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_as_me(chat_id, "See you at 3", guard=False)
    await wa.transport._on_event(json.dumps({
        "v": 1, "type": "message_failed", "session_id": SID, "epoch": 7, "wa_message_id": row["wa_message_id"],
        "jid": ANNA_PN, "code": 403, "error_kind": "blocked",
    }))
    assert (await wa.db.get_message(row["id"]))["status"] == STATUS_ERROR
    assert (await wa.db.get_conversation(chat_id))["automation_paused"] is True


@pytest.mark.asyncio
async def test_a_rate_limit_after_sending_halts(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_as_me(chat_id, "Hello", guard=False)
    await wa.transport._on_event(json.dumps({
        "v": 1, "type": "message_failed", "session_id": SID, "epoch": 7, "wa_message_id": row["wa_message_id"],
        "jid": ANNA_PN, "code": 429, "error_kind": "rate_limited",
    }))
    assert [h["kind"] for h in await controls.holds(pg_pool, wa.tenant_id)] == [controls.WHATSAPP]


@pytest.mark.asyncio
async def test_an_unexplained_failure_is_shown(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_as_me(chat_id, "Hello", guard=False)
    await wa.transport._on_event(json.dumps({
        "v": 1, "type": "message_failed", "session_id": SID, "epoch": 7, "wa_message_id": row["wa_message_id"],
        "jid": ANNA_PN, "code": 500, "error_kind": "other",
    }))
    shown = [m["text"] for m in await wa.db.get_messages(chat_id) if m["status"] == STATUS_ERROR]
    assert "Hello" in shown and any("did not deliver" in t for t in shown)


@pytest.mark.asyncio
async def test_an_account_restriction_halts(wa, pg_pool):
    chat_id = await a_chat(wa, pg_pool)
    row = await wa.send_as_me(chat_id, "Hello", guard=False)
    await wa.transport._on_event(json.dumps({
        "v": 1, "type": "message_failed", "session_id": SID, "epoch": 7, "wa_message_id": row["wa_message_id"],
        "jid": ANNA_PN, "code": 463, "error_kind": "other",
    }))
    assert [h["kind"] for h in await controls.holds(pg_pool, wa.tenant_id)] == [controls.WHATSAPP]


# -------------------------------------------------- a group as the booker

GROUP = "120363001122334455@g.us"
FAMILY = "120363009988776655@g.us"
LINA_PN = "37120000001@s.whatsapp.net"


def gateway_groups(*groups):
    return {"groups": [{"jid": jid, "subject": name, "size": 3, "announce": False} for jid, name in groups]}


async def booker_group(wa, value=GROUP) -> int:
    """The account connected, the group set as the booker and found."""
    wa.bus.replies.setdefault("groups", gateway_groups((FAMILY, "Family"), (GROUP, "Riga bookings")))
    await wa.transport._open_once()
    wa.config["booking"].update(enabled=True, provider=value)
    chat_id = await wa.provider_chat_id()
    assert chat_id is not None
    return chat_id


async def put_group(pool, wa_message_id: str, text: str, *, group=GROUP, **payload) -> None:
    fields = {"jid": group, "group_jid": group, "phone_jid": LINA_PN, "push_name": "Lina", "text": text}
    await put_inbox(pool, wa_message_id, **{**fields, **payload})


def test_group_values_are_told_from_phone_numbers():
    assert group_ref(GROUP) == ("jid", GROUP)
    assert group_ref(" https://chat.whatsapp.com/AbCd1234XyZ ") == ("invite", "AbCd1234XyZ")
    assert group_ref("chat.whatsapp.com/invite/AbCd1234XyZ") == ("invite", "AbCd1234XyZ")
    assert group_ref("Group:  Riga bookings") == ("name", "Riga bookings")
    for phone in ("+371 2000 0000", "37120000000", "", "Riga bookings"):
        assert group_ref(phone) is None


@pytest.mark.asyncio
async def test_a_group_set_as_the_booker_is_found_stored_and_let_through(wa, pg_pool):
    chat_id = await booker_group(wa)
    row = await wa_store.peer(pg_pool, SID, chat_id)
    assert (row["jid"], row["group_jid"], row["push_name"], row["phone_jid"], row["lid"]) == \
        (GROUP, GROUP, "Riga bookings", None, None)
    assert (await wa.db.get_conversation(chat_id))["display_name"] == "Riga bookings"
    # The gateway is told at once (and on every keepalive after).
    opens = [args["groups"] for _, action, args in wa.bus.calls if action == "open"]
    assert opens == [[], [GROUP]]
    # Found once: the next rounds ask nothing.
    await wa.transport._find_booker_group()
    assert wa.bus.actions().count("groups") == 1
    # A restarted runtime names it in its very first open, so what the group
    # wrote meanwhile comes through.
    wa.transport.groups = []
    await pair(pg_pool)
    await wa.transport.prepare()
    assert wa.transport._open_args()["groups"] == [GROUP]


@pytest.mark.asyncio
async def test_a_group_by_its_invite_link_or_its_name(wa, pg_pool):
    wa.bus.replies["group_invite"] = lambda args: {"jid": GROUP, "subject": "Riga bookings"}
    chat_id = await booker_group(wa, " https://chat.whatsapp.com/AbCd1234 ")
    assert [args["code"] for _, action, args in wa.bus.calls if action == "group_invite"] == ["AbCd1234"]
    assert (await wa_store.peer(pg_pool, SID, chat_id))["group_jid"] == GROUP
    assert (await wa.transport.resolve_owner("group: riga BOOKINGS"))[0] == chat_id


@pytest.mark.asyncio
async def test_a_group_the_account_is_not_in_is_never_the_booker(wa, pg_pool):
    wa.bus.replies["groups"] = gateway_groups((FAMILY, "Family"), ("120363000000000001@g.us", "family"))
    await wa.transport._open_once()
    for value, why in ((GROUP, "not in that group"), ("group: Riga", "no group named"),
                       ("group: Family", "2 groups")):
        with pytest.raises(ValueError, match=why):
            await wa.transport.resolve_owner(value)
    assert await wa_store.group_jids(pg_pool, SID) == []
    assert wa.transport._open_args()["groups"] == []


@pytest.mark.asyncio
async def test_anyone_in_the_booker_group_answers_and_the_group_never_gets_a_reply(wa, pg_pool, monkeypatch):
    group_chat = await booker_group(wa)
    answers = []

    async def on_owner_message(chat_id, text, reply_to, message_id=None):
        answers.append((chat_id, text, reply_to))
        return text.startswith("YES")

    monkeypatch.setattr(wa.flow, "on_owner_message", on_owner_message)
    await put_group(pg_pool, "3EB0G1", "YES 1", quoted_id="3EB0OUT1")
    await put_group(pg_pool, "3EB0G2", "who takes the 9pm?", phone_jid="37120000002@s.whatsapp.net", push_name=None)
    assert await wa.transport.drain() == 2
    assert answers == [(group_chat, "YES 1", "3EB0OUT1"), (group_chat, "who takes the 9pm?", None)]
    assert sorted(m["text"] for m in await wa.db.get_messages(group_chat)) == \
        ["+37120000002: who takes the 9pm?", "Lina: YES 1"]
    assert wa.scheduled == []                   # never a customer's reply in the group
    # The members get no chats of their own from it.
    assert await pg_pool.fetchval("SELECT count(*) FROM wa_peers WHERE session_id = $1", SID) == 1


@pytest.mark.asyncio
async def test_other_groups_and_a_former_booker_group_are_never_read(wa, pg_pool):
    group_chat = await booker_group(wa)
    # A group never set as the booker (the gateway lets none through).
    await put_group(pg_pool, "3EB0G3", "YES 1", group=FAMILY)
    # The booker is a person now: the old group is no longer read.
    wa.config["booking"]["provider"] = "+34 600 555 666"
    await put_group(pg_pool, "3EB0G4", "YES 1")
    assert await wa.transport.drain() == 2
    # Bookings off: not read either.
    wa.config["booking"].update(provider=GROUP, enabled=False)
    await put_group(pg_pool, "3EB0G5", "YES 1")
    assert await wa.transport.drain() == 1
    assert await inbox_count(pg_pool) == 0
    assert await wa.db.get_messages(group_chat) == []
    assert await pg_pool.fetchval("SELECT count(*) FROM messages WHERE session_id = $1", SID) == 0
    assert await wa_store.group_chat(pg_pool, SID, FAMILY) is None


@pytest.mark.asyncio
async def test_typed_by_hand_in_the_booker_group_is_kept_without_a_takeover(wa, pg_pool):
    group_chat = await booker_group(wa)
    await put_group(pg_pool, "3EB0G6", "I'll take #1", from_me=True, push_name=None, phone_jid=None)
    await wa.transport.drain()
    [m] = await wa.db.get_messages(group_chat)
    assert (m["direction"], m["status"], m["text"]) == (DIR_OUT, STATUS_SENT, "I'll take #1")
    assert wa.takeover_until(await wa.db.get_conversation(group_chat)) is None


@pytest.mark.asyncio
async def test_requests_go_to_the_group_with_no_read_receipts_and_the_panel_lists_groups(wa, pg_pool, monkeypatch):
    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(session_runtime.asyncio, "sleep", nothing)
    group_chat = await booker_group(wa)
    assert await wa.flow.send_owner("📅 Booking request #1", reason="test") is not None
    assert [args["jid"] for _, action, args in wa.bus.calls if action == "send_text"] == [GROUP]
    await wa.db.record_message(group_chat, "in", STATUS_RECEIVED, "Lina: YES 1", wa_message_id="3EB0G7")
    await wa.transport.mark_read(group_chat)
    assert "read" not in wa.bus.actions()
    assert await wa.handle_command("list_groups", {}) == [
        {"id": FAMILY, "name": "Family", "members": 3, "admins_only": False},
        {"id": GROUP, "name": "Riga bookings", "members": 3, "admins_only": False},
    ]
