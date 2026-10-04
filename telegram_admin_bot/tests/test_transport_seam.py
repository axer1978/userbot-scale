"""The transport seam (transport.py): the runtime's business logic talks to
the network only through `SessionRuntime.transport`, and the Telegram
transport keeps doing exactly what session_runtime.py did before the split.

Pure tests first (no Postgres); the ones that drive a runtime need the
`app` fixture and are skipped without PG_TEST_DSN.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from telethon import errors

import session_runtime
import transport
from database import DIR_OUT, STATUS_RECEIVED, STATUS_SENT
from telegram_transport import TelegramTransport, describe_sender
from transport import (
    PEER_FLOOD,
    RATE_LIMITED,
    SESSION_REJECTED,
    UNREACHABLE,
    Failure,
    Inbound,
    PeerInfo,
    Transport,
)

HERE = Path(__file__).resolve().parent.parent


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


# ---------------------------------------------------------------- the seam


@pytest.mark.parametrize("name", ["session_runtime.py", "booking_flow.py", "transport.py"])
def test_business_logic_never_imports_telethon(name):
    """Only telegram_transport.py (and the sign-in flow) may talk Telethon."""
    assert "telethon" not in _imported_modules(HERE / name)


def test_make_transport_by_channel():
    rt = SimpleNamespace()
    made = transport.make_transport(transport.TELEGRAM, rt)
    assert isinstance(made, TelegramTransport) and made.rt is rt
    assert made.channel == "telegram" and made.network == "Telegram"
    with pytest.raises(ValueError):
        transport.make_transport("fax", rt)


def test_runtime_defaults_to_the_telegram_transport(tmp_path):
    rt = session_runtime.SessionRuntime(None, "s1", data_dir=tmp_path, redis_url="redis://unused")
    assert isinstance(rt.transport, TelegramTransport)
    # The pre-split names still reach the transport's state.
    fake = object()
    rt.client = fake
    assert rt.transport.client is fake
    rt.telegram_state["connected"] = True
    assert rt.transport.connected is True
    rt.me_info = {"id": 5, "name": "Me"}
    assert rt.transport.me == {"id": 5, "name": "Me"}
    assert rt.lease_epoch == 0


def test_needs_login_is_one_class():
    """manager.py catches session_runtime.NeedsLogin; the transport raises
    transport.NeedsLogin. They must be the same class."""
    assert session_runtime.NeedsLogin is transport.NeedsLogin


# ------------------------------------------------------- classification


@pytest.mark.parametrize("exc, kind", [
    (errors.PeerFloodError(None), PEER_FLOOD),
    (errors.UserDeactivatedBanError(None), SESSION_REJECTED),
    (errors.AuthKeyUnregisteredError(None), SESSION_REJECTED),
    (errors.SessionRevokedError(None), SESSION_REJECTED),
    (errors.UserIsBlockedError(None), UNREACHABLE),
    (errors.UserPrivacyRestrictedError(None), UNREACHABLE),
    (errors.InputUserDeactivatedError(None), UNREACHABLE),
    (errors.ChatWriteForbiddenError(None), UNREACHABLE),
])
def test_telegram_errors_classify(exc, kind):
    failure = TelegramTransport(SimpleNamespace()).classify(exc)
    assert failure == Failure(kind, type(exc).__name__)


def test_flood_waits_carry_their_seconds():
    t = TelegramTransport(SimpleNamespace())
    flood = errors.FloodWaitError(None)
    flood.seconds = 42
    slow = errors.SlowModeWaitError(None)
    slow.seconds = 7
    assert t.classify(flood) == Failure(RATE_LIMITED, "FloodWaitError", 42)
    assert t.classify(slow) == Failure(RATE_LIMITED, "SlowModeWaitError", 7)


def test_unknown_errors_are_not_classified():
    t = TelegramTransport(SimpleNamespace())
    assert t.classify(ValueError("x")) is None
    assert t.classify(RuntimeError("Telegram is not connected.")) is None


# ------------------------------------------------ Telegram send semantics


class StubClient:
    """Records what the Telegram transport asks Telethon to do."""

    def __init__(self, *, action_fails: bool = False, send_fails: bool = False):
        self.calls: list[tuple] = []
        self.action_fails = action_fails
        self.send_fails = send_fails

    @asynccontextmanager
    async def action(self, chat_id, kind):
        if self.action_fails:
            raise RuntimeError("no typing here")
        self.calls.append(("action", chat_id, kind))
        yield SimpleNamespace(progress=None)
        self.calls.append(("action_end", chat_id, kind))

    async def send_message(self, peer, text):
        self.calls.append(("send", peer, text))
        if self.send_fails:
            raise errors.PeerFloodError(None)
        return SimpleNamespace(id=900)


def _telegram(client) -> TelegramTransport:
    t = TelegramTransport(SimpleNamespace(session_id="s1"))
    t.client = client
    return t


@pytest.mark.asyncio
async def test_send_without_typing_goes_straight_out():
    client = StubClient()
    sent = await _telegram(client).send_text("peer", 5, "hi", None)
    assert client.calls == [("send", "peer", "hi")]
    assert sent.id == 900


@pytest.mark.asyncio
async def test_send_with_typing_sends_while_the_indicator_shows(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = StubClient()
    await _telegram(client).send_text("peer", 5, "hi", 3.5)
    assert slept == [3.5]
    assert client.calls == [("action", 5, "typing"), ("send", "peer", "hi"), ("action_end", 5, "typing")]


@pytest.mark.asyncio
async def test_typing_indicator_failure_still_sends_once(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = StubClient(action_fails=True)
    await _telegram(client).send_text("peer", 5, "hi", 2.0)
    assert client.calls == [("send", "peer", "hi")]


@pytest.mark.asyncio
async def test_a_failed_send_is_never_repeated(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = StubClient(send_fails=True)
    with pytest.raises(errors.PeerFloodError):
        await _telegram(client).send_text("peer", 5, "hi", 2.0)
    assert [c for c in client.calls if c[0] == "send"] == [("send", "peer", "hi")]


@pytest.mark.asyncio
async def test_resolve_peer_refuses_while_disconnected():
    t = _telegram(StubClient())
    with pytest.raises(RuntimeError, match="Telegram is not connected."):
        await t.resolve_peer(5)


# ------------------------------------------------- Telegram normalisation


class Event:
    def __init__(self, chat_id=42, text=" hello ", private=True, photo=None, reply_to=None):
        self.is_private = private
        self.chat_id = chat_id
        self.raw_text = text
        self.message = SimpleNamespace(id=77, photo=photo, reply_to_msg_id=reply_to)
        self.lookups = 0

    async def get_sender(self):
        self.lookups += 1
        return SimpleNamespace(first_name="Anna", last_name="K", username="anna", bot=False, access_hash=9)

    get_chat = get_sender


class Sink:
    session_id = "s1"

    def __init__(self, fail: bool = False):
        self.inbound: list[Inbound] = []
        self.echoes: list[Inbound] = []
        self.fail = fail

    async def handle_inbound(self, message):
        if self.fail:
            raise RuntimeError("boom")
        self.inbound.append(message)

    async def handle_own_echo(self, message):
        self.echoes.append(message)


@pytest.mark.asyncio
async def test_incoming_event_is_normalised():
    sink = Sink()
    event = Event(photo=object(), reply_to=12)
    await TelegramTransport(sink).on_incoming(event)
    (msg,) = sink.inbound
    assert (msg.chat_id, msg.text, msg.external_id, msg.from_me) == (42, "hello", 77, False)
    assert msg.has_photo and msg.reply_to == 12 and not msg.is_service and msg.raw is event
    # Who it is from is looked up only when the runtime asks.
    assert event.lookups == 0
    assert await msg.load_peer() == PeerInfo("Anna K", "anna", False, 9)


@pytest.mark.asyncio
async def test_service_account_and_outgoing_flags():
    sink = Sink()
    t = TelegramTransport(sink)
    await t.on_incoming(Event(chat_id=777000, text="Login code: 1"))
    await t.on_outgoing(Event(text="typed on the phone"))
    assert sink.inbound[0].is_service is True
    assert sink.echoes[0].from_me is True and sink.echoes[0].text == "typed on the phone"


@pytest.mark.asyncio
async def test_group_messages_never_reach_the_runtime():
    sink = Sink()
    t = TelegramTransport(sink)
    await t.on_incoming(Event(private=False))
    await t.on_outgoing(Event(private=False))
    assert sink.inbound == [] and sink.echoes == []


@pytest.mark.asyncio
async def test_a_failing_message_is_logged_not_raised(caplog):
    sink = Sink(fail=True)
    await TelegramTransport(sink).on_incoming(Event())
    assert "Could not handle a message in chat 42" in caplog.text


def test_describe_sender_unchanged():
    assert describe_sender(None, 5) == ("Chat 5", None, False, None)
    assert describe_sender(SimpleNamespace(title="Group"), 5) == ("Group", None, False, None)


# ------------------------------------------ the runtime over any transport


class FakeTransport(Transport):
    """A network that records everything and never touches one."""

    channel = "fake"
    network = "FakeNet"

    def __init__(self, rt):
        super().__init__(rt)
        self.sent: list[tuple] = []
        self.read: list[tuple] = []
        self.presence: list[bool] = []
        self._connected = True
        self.next_id = 500

    @property
    def connected(self):
        return self._connected

    @property
    def error(self):
        return None

    @property
    def me(self):
        return {"id": 1, "name": "Me", "username": None}

    async def resolve_peer(self, chat_id):
        return f"peer-{chat_id}"

    async def send_text(self, peer, chat_id, text, typing_seconds):
        self.sent.append((peer, chat_id, text, typing_seconds))
        self.next_id += 1
        return self.next_id

    def message_id(self, sent):
        return sent

    async def mark_read(self, chat_id, message_id=None):
        self.read.append((chat_id, message_id))

    async def set_presence(self, online):
        self.presence.append(online)

    def classify(self, exc):
        if isinstance(exc, LookupError):
            return Failure(UNREACHABLE, "Blocked")
        return None


def _peer_loader(calls: Optional[list] = None, name: str = "Bea"):
    async def load():
        if calls is not None:
            calls.append(1)
        return PeerInfo(name, "bea")
    return load


@pytest.fixture
def fake_net(app, monkeypatch):
    net = FakeTransport(app)
    app.transport = net
    scheduled: list[Any] = []
    monkeypatch.setattr(app, "schedule_draft", lambda chat_id: scheduled.append(chat_id))
    app.scheduled = scheduled
    return net


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_inbound_from_any_transport_is_stored_and_drafted(app, db, fake_net):
    await app.handle_inbound(Inbound(chat_id=31, text="Hi there", external_id=4001, load_peer=_peer_loader()))
    conversation = await db.get_conversation(31)
    assert conversation["display_name"] == "Bea" and conversation["username"] == "bea"
    (row,) = await db.get_messages(31)
    assert (row["status"], row["text"], row["telegram_id"]) == (STATUS_RECEIVED, "Hi there", 4001)
    assert app.scheduled == [31]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_service_message_is_stored_but_never_answered(app, db, fake_net, monkeypatch):
    checked = []

    async def check_logins():
        checked.append(1)
        return []

    monkeypatch.setattr(app, "check_logins", check_logins)
    await app.handle_inbound(Inbound(chat_id=32, text="code 1", external_id=1, load_peer=_peer_loader(),
                                     is_service=True))
    assert checked == [1] and app.scheduled == []
    assert len(await db.get_messages(32)) == 1


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_own_send_echo_is_ignored_without_a_lookup(app, db, fake_net):
    calls: list[int] = []
    app.in_flight_sends[33] = ["on its way"]
    await app.handle_own_echo(Inbound(chat_id=33, text="on its way", external_id=5, load_peer=_peer_loader(calls),
                                      from_me=True))
    assert calls == [] and await db.get_messages(33) == []


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_hand_written_echo_takes_the_chat_over(app, db, fake_net, monkeypatch):
    async def no_owner():
        return None

    monkeypatch.setattr(app.flow, "provider_chat_id", no_owner)
    await db.upsert_conversation(34, "Cy", None, False, None)
    await app.handle_own_echo(Inbound(chat_id=34, text="I'll handle this", external_id=6,
                                      load_peer=_peer_loader(name="Cy"), from_me=True))
    (row,) = [m for m in await db.get_messages(34) if m["direction"] == DIR_OUT]
    assert row["status"] == STATUS_SENT and row["telegram_id"] == 6
    assert app.takeover_until(await db.get_conversation(34)) is not None
    notes = [m["text"] for m in await db.get_messages(34) if m["status"] == "note"]
    assert any("Someone wrote here by hand in FakeNet" in n for n in notes)


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_send_as_me_goes_through_the_transport(app, db, fake_net):
    await db.upsert_conversation(35, "Di", None, False, None)
    row = await app.send_as_me(35, "Hello", guard=False)
    assert fake_net.sent == [("peer-35", 35, "Hello", None)]
    assert row["telegram_id"] == 501 and row["status"] == STATUS_SENT


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_typing_time_is_decided_by_the_runtime(app, db, fake_net):
    await db.upsert_conversation(36, "Ed", None, False, None)
    app.config["human"]["typing_indicator"] = True
    await app.send_as_me(36, "x" * 24, typing=True, guard=False)
    app.config["human"]["typing_indicator"] = False
    await app.send_as_me(36, "y" * 24, typing=True, guard=False)
    (_, _, _, first), (_, _, _, second) = fake_net.sent
    assert first is not None and first > 0      # 24 chars at 12 cps, jittered
    assert second is None                        # indicator off: no typing


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_read_receipts_and_presence_obey_the_config(app, fake_net):
    app.config["human"]["mark_read"] = False
    await app.mark_read(37)
    app.config["human"]["mark_read"] = True
    await app.mark_read(37, 12)
    assert fake_net.read == [(37, 12)]
    await app.set_presence(True)
    await app.set_presence(True)   # cached: not sent twice
    await app.set_presence(False)
    assert fake_net.presence == [True, False]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_send_failures_are_acted_on_by_kind(app, db, fake_net):
    await db.upsert_conversation(38, "Flo", None, False, None)
    assert await app.handle_send_failure(38, LookupError("blocked")) is True
    conversation = await db.get_conversation(38)
    assert conversation["automation_paused"] is True
    errors_shown = [m["text"] for m in await db.get_messages(38) if m["status"] == "error"]
    assert errors_shown == ["Cannot message this person (Blocked); this conversation is now paused. "
                            "They may have blocked the account."]
    assert await app.handle_send_failure(38, ValueError("other")) is False


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_telegram_failure_texts_are_unchanged(app, db):
    """The operator-facing wording from before the split, for Telegram."""
    await db.upsert_conversation(39, "Gus", None, False, None)
    await app.handle_send_failure(39, errors.UserIsBlockedError(None))
    shown = [m["text"] for m in await db.get_messages(39) if m["status"] == "error"]
    assert shown == ["Cannot message this person (UserIsBlockedError); this conversation is now paused. "
                     "They may have blocked the account."]
