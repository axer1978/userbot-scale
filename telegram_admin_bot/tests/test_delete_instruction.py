"""The delete instruction (replies.delete_instruction): the reply writer may
mark the customer's latest messages with [DELETE]; with auto-send on they
are deleted in Telegram for both sides, kept in the panel as deleted, and
dropped from what the AI sees afterwards.

Messages enter through SessionRuntime.on_incoming like real ones; the model
and Telegram are faked (the fixture is test_unanswered.py's)."""

from __future__ import annotations

import pytest

import ai_responder
import audit
import unanswered
from database import DIR_IN, DIR_SYSTEM
from test_unanswered import CUSTOMER, customer, w  # noqa: F401  (w is a fixture)

pytestmark = [pytest.mark.requires_pg]

RULE = "Delete messages that contain a link."


# ---------------------------------------------------------------- the marker


@pytest.mark.parametrize("raw, wants, rest", [
    ("[DELETE]\nSure, see you then.", True, "Sure, see you then."),
    ("[DELETE]", True, ""),
    ("  `[delete]`  \nHi", True, "Hi"),
    ("Hi\n[DELETE]", True, "Hi"),
    ("Sure, see you then.", False, "Sure, see you then."),
    ("We never [DELETE] anything you send.", False, "We never [DELETE] anything you send."),
])
def test_take_delete_finds_the_marker_only_on_a_line_of_its_own(raw, wants, rest):
    assert ai_responder.take_delete(raw) == (wants, rest)


# ---------------------------------------------------------------- the runtime


class FakeTelegram:
    def __init__(self, fail=False):
        self.deleted: list[tuple[object, list[int], bool]] = []
        self.fail = fail

    async def __call__(self, request):
        from types import SimpleNamespace
        return SimpleNamespace(authorizations=[])

    def is_connected(self):
        return False

    async def delete_messages(self, peer, ids, revoke=False):
        if self.fail:
            raise ConnectionError("telegram unreachable")
        self.deleted.append((peer, list(ids), revoke))


@pytest.fixture
def tg(w):  # noqa: F811
    w.app.client = FakeTelegram()
    w.app.config["replies"]["delete_instruction"] = RULE
    return w.app.client


async def rows(w, direction=None):  # noqa: F811
    msgs = await w.db.get_messages(CUSTOMER, limit=100)
    return [m for m in msgs if direction is None or m["direction"] == direction]


@pytest.mark.asyncio
async def test_the_marked_messages_are_deleted_for_both_sides_and_the_reply_still_goes_out(
        w, tg, monkeypatch):  # noqa: F811
    seen = {}

    async def fake_reply(**kw):
        seen.update(kw)
        return "[DELETE]\nPlease don't send links here."

    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    await customer(w, "look at http://spam.example")

    assert seen["delete_instruction"] == RULE
    [incoming] = await rows(w, DIR_IN)
    assert tg.deleted == [(CUSTOMER, [incoming["telegram_id"]], True)]
    assert incoming["deleted_at"] is not None
    assert w.sent[CUSTOMER] == ["Please don't send links here."]           # marker never sent
    notes = [m["text"] for m in await rows(w, DIR_SYSTEM)]
    assert "Deleted 1 message under the delete instruction." in notes
    [event] = [e for e in await audit.list_events(w.pool, tenant_id=w.app.tenant_id)
               if e["event"] == audit.MESSAGES_DELETED]
    assert event["payload"]["count"] == 1 and event["payload"]["chat_id"] == CUSTOMER
    # The AI no longer sees the deleted message; the reply stays.
    history = await w.db.get_history_for_ai(CUSTOMER)
    assert [h["content"] for h in history] == ["Please don't send links here."]


@pytest.mark.asyncio
async def test_delete_only_sends_nothing_and_is_not_unanswered(w, tg):  # noqa: F811
    w.script["reply"] = "[DELETE]"
    await customer(w, "http://spam.example")
    assert len(tg.deleted) == 1
    assert CUSTOMER not in w.sent
    assert await unanswered.list_items(w.pool, [w.app.tenant_id]) == []


@pytest.mark.asyncio
async def test_only_the_messages_since_the_last_reply_are_deleted(w, tg):  # noqa: F811
    await customer(w, "Hello, are you open?")                      # answered normally
    w.script["reply"] = "[DELETE]\nNo links please."
    await customer(w, "http://spam.example")
    first, second = await rows(w, DIR_IN)
    assert tg.deleted == [(CUSTOMER, [second["telegram_id"]], True)]
    assert first["deleted_at"] is None and second["deleted_at"] is not None


@pytest.mark.asyncio
async def test_with_auto_send_off_nothing_is_deleted_only_noted(w, tg):  # noqa: F811
    w.app.config["auto_send"] = False
    w.script["reply"] = "[DELETE]\nNo links please."
    await customer(w, "http://spam.example")
    assert tg.deleted == []
    [incoming] = await rows(w, DIR_IN)
    assert incoming["deleted_at"] is None
    notes = [m["text"] for m in await rows(w, DIR_SYSTEM)]
    assert any("Auto-send is off, so nothing was deleted" in n for n in notes)
    drafts = [m for m in await rows(w) if m["status"] == "pending_approval"]
    assert [d["text"] for d in drafts] == ["No links please."]


@pytest.mark.asyncio
async def test_a_failed_deletion_is_noted_and_never_costs_the_reply(w, tg):  # noqa: F811
    tg.fail = True
    w.script["reply"] = "[DELETE]\nNo links please."
    await customer(w, "http://spam.example")
    assert w.sent[CUSTOMER] == ["No links please."]
    [incoming] = await rows(w, DIR_IN)
    assert incoming["deleted_at"] is None
    notes = [m["text"] for m in await rows(w, DIR_SYSTEM)]
    assert "Could not delete 1 message: ConnectionError." in notes


@pytest.mark.asyncio
async def test_without_an_instruction_the_model_is_not_told_and_nothing_is_deleted(
        w, tg, monkeypatch):  # noqa: F811
    w.app.config["replies"]["delete_instruction"] = ""
    seen = {}

    async def fake_reply(**kw):
        seen.update(kw)
        return "Sure."

    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    await customer(w, "http://spam.example")
    assert seen["delete_instruction"] == ""
    assert tg.deleted == []


def test_the_prompt_section_carries_the_instruction_and_the_marker():
    section = ai_responder.delete_section(RULE)
    assert RULE in section and ai_responder.DELETE_MARKER in section
