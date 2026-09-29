"""Staging mode (config `staging.*`): only the listed test chats get the
normal flow; every other customer message is stored, noted, queued as
unanswered and otherwise left alone. Escalation keywords still pause a
chat. Turning staging off is "go live".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import ai_responder
import alerts
import session_runtime
import unanswered
from database import STATUS_NOTE
from test_unanswered import CUSTOMER, OWNER, FakeClient, FakeEvent, settle  # noqa: F401  (fixtures below)

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TESTER = 77
TESTER_BY_ID = 78


@pytest_asyncio.fixture
async def w(app, db, monkeypatch):
    sent: dict[int, list[str]] = {}
    calls = {"reply": 0, "scan": 0}
    msg_ids = iter(range(5000, 90_000))

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.setdefault(chat_id, []).append(text)
        return SimpleNamespace(id=next(msg_ids))

    async def fake_reply(**kw):
        calls["reply"] += 1
        return "Hello from the bot."

    async def fake_scan(chat_id, text):
        calls["scan"] += 1

    async def nothing(*a, **k):
        return None

    async def owner_chat():
        return OWNER

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app.flow, "provider_chat_id", owner_chat)
    monkeypatch.setattr(app.flow, "on_customer_message", fake_scan)
    monkeypatch.setattr(app.flow, "enabled", lambda: True)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", nothing)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    app.deepseek_key = "k"
    app.client = FakeClient()
    app.config["auto_send"] = True
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    app.config["api_spend_cap_eur"] = 0
    app.config["escalation_keywords"] = ["lawyer"]
    app.config["staging"] = {"enabled": True, "test_chats": ["tester", str(TESTER_BY_ID)]}
    app.telegram_state["connected"] = True
    yield SimpleNamespace(app=app, db=db, sent=sent, calls=calls, pool=app.pool)
    await alerts.drain()


async def say(w, chat, text, username):
    await w.app.on_incoming(FakeEvent(chat, text, username))
    await settle(w.app)


async def notes(w, chat):
    return [m["text"] for m in await w.db.get_messages(chat) if m["status"] == STATUS_NOTE]


async def test_a_test_chat_gets_the_normal_flow(w):
    await say(w, TESTER, "Hi, can I book?", "Tester")          # username match ignores case
    await say(w, TESTER_BY_ID, "Hi from a chat listed by id", None)
    assert w.sent[TESTER] == ["Hello from the bot."] and w.sent[TESTER_BY_ID] == ["Hello from the bot."]
    assert w.calls["scan"] == 2
    assert await unanswered.list_items(w.pool, [w.app.tenant_id]) == []
    assert w.app.status()["staging"] is True


async def test_anyone_else_is_stored_noted_and_queued_not_answered(w):
    await say(w, CUSTOMER, "Hi, can I book?", "anna")
    await say(w, CUSTOMER, "Hello?", "anna")
    assert CUSTOMER not in w.sent
    assert w.calls == {"reply": 0, "scan": 0}                 # no booking scan, no model call
    history = [m["text"] for m in await w.db.get_messages(CUSTOMER) if m["direction"] == "in"]
    assert history == ["Hi, can I book?", "Hello?"]
    # One note an hour per chat, not one per message.
    assert await notes(w, CUSTOMER) == [session_runtime.STAGING_NOTE]
    items = await unanswered.list_items(w.pool, [w.app.tenant_id])
    assert [(i["reason"], i["text"]) for i in items] == [("staging", "Hello?"), ("staging", "Hi, can I book?")]


async def test_the_owner_chat_is_not_held_back_by_staging(w):
    await say(w, OWNER, "Just checking in", "owner")
    assert w.sent[OWNER] == ["Hello from the bot."]
    assert await unanswered.list_items(w.pool, [w.app.tenant_id]) == []


async def test_escalation_still_pauses_the_chat_in_staging(w):
    await say(w, CUSTOMER, "I will call my lawyer", "anna")
    conversation = await w.db.get_conversation(CUSTOMER)
    assert conversation["automation_paused"] and "lawyer" in conversation["paused_reason"]
    assert len(w.sent[OWNER]) == 1 and "needs a person" in w.sent[OWNER][0]   # the owner is told
    assert CUSTOMER not in w.sent
    [item] = await unanswered.list_items(w.pool, [w.app.tenant_id])
    assert item["reason"] == "escalated"


async def test_a_reply_scheduled_before_staging_went_on_is_not_sent(w):
    w.app.config["staging"]["enabled"] = False
    await say(w, CUSTOMER, "Hi", "anna")
    assert w.sent[CUSTOMER] == ["Hello from the bot."]
    w.app.config["staging"]["enabled"] = True
    await w.app.draft_worker(CUSTOMER)                          # e.g. held by quiet hours until now
    assert w.sent[CUSTOMER] == ["Hello from the bot."]
    [item] = await unanswered.list_items(w.pool, [w.app.tenant_id])
    assert item["reason"] == "staging"


async def test_turning_staging_off_answers_everyone(w):
    await say(w, CUSTOMER, "Hello?", "anna")
    assert CUSTOMER not in w.sent
    w.app.config["staging"]["enabled"] = False
    assert w.app.status()["staging"] is False
    await say(w, CUSTOMER, "Hello again?", "anna")
    await say(w, 55, "Me too", "bob")
    assert w.sent[CUSTOMER] == ["Hello from the bot."] and w.sent[55] == ["Hello from the bot."]
    assert w.calls["scan"] == 2
