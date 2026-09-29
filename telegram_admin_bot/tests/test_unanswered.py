"""The unanswered queue: filled by a running account (session_runtime.py)
for every reason in unanswered.REASONS, and worked through by the admin
(unanswered_api.py): list, mark reviewed, reopen, promote an answer into
the industry template's FAQ.

Messages enter through SessionRuntime.on_incoming like real ones; the
model and Telegram are faked (the fixture follows test_safety_runtime.py).
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

import ai_responder
import alerts
import audit
import controls
import prompt_layers
import session_runtime
import tenants
import unanswered
import unanswered_api
from conftest import seed_session
from database import DIR_IN, STATUS_PENDING, STATUS_RECEIVED, Database

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

RIGA = ZoneInfo("Europe/Riga")
OWNER = 999
CUSTOMER = 42
NOW = datetime(2030, 3, 4, 12, 0, tzinfo=RIGA)
_ids = itertools.count(1)


class FakeEvent:
    def __init__(self, chat_id, text="", username="anna"):
        self.is_private = True
        self.chat_id = chat_id
        self.raw_text = text
        self.username = username
        self.message = SimpleNamespace(id=next(_ids), reply_to_msg_id=None, photo=None)

    async def get_sender(self):
        return SimpleNamespace(id=self.chat_id, first_name="Anna", last_name=None, username=self.username,
                               bot=False, access_hash=1)

    get_chat = get_sender


class FakeClient:
    async def __call__(self, request):
        return SimpleNamespace(authorizations=[])

    def is_connected(self):
        return False


@pytest_asyncio.fixture
async def w(app, db, monkeypatch):
    sent: dict[int, list[str]] = {}
    script = {"reply": "Sure, see you then.", "calls": 0}
    msg_ids = iter(range(5000, 90_000))

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.setdefault(chat_id, []).append(text)
        return SimpleNamespace(id=next(msg_ids))

    async def fake_reply(**kw):
        script["calls"] += 1
        reply = script["reply"]
        if isinstance(reply, Exception):
            raise reply
        return reply

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
    monkeypatch.setattr(app, "utcnow", lambda: NOW.astimezone(timezone.utc))
    monkeypatch.setattr(app.flow, "provider_chat_id", owner_chat)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", nothing)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    app.deepseek_key = "k"
    app.client = FakeClient()
    app.config["auto_send"] = True
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    app.config["api_spend_cap_eur"] = 0
    app.config["escalation_keywords"] = ["lawyer"]
    app.telegram_state["connected"] = True
    yield SimpleNamespace(app=app, db=db, sent=sent, script=script, pool=app.pool)
    await alerts.drain()


async def settle(app):
    for _ in range(10):
        tasks = [t for t in [*app.flow.scan_tasks.values(), *app.draft_tasks.values()] if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def customer(w, text, chat=CUSTOMER, username="anna"):
    await w.app.on_incoming(FakeEvent(chat, text, username))
    await settle(w.app)


async def queue(w, status=None):
    return await unanswered.list_items(w.pool, [w.app.tenant_id], status=status)


async def last_in(w, chat=CUSTOMER):
    return await unanswered.last_customer_message(w.pool, w.app.tenant_id, chat)


# ------------------------------------------------------- filling the queue


async def test_an_answered_message_is_not_queued(w):
    await customer(w, "Are you open tomorrow?")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]
    assert await queue(w) == []


async def test_a_reply_limit_queues_the_message_as_skipped(w):
    w.app.config["replies"]["max_messages_per_chat_per_hour"] = 1
    await customer(w, "Are you open tomorrow?")
    await customer(w, "And on Sunday?")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]
    [item] = await queue(w)
    assert (item["reason"], item["text"], item["chat_id"]) == ("skipped", "And on Sunday?", CUSTOMER)
    assert "limit 1" in item["detail"] and item["customer"] == "Anna" and item["status"] == "open"
    assert item["message_id"] == await last_in(w)
    assert "unanswered" in w.app.hub.types()


async def test_a_bare_acknowledgement_is_skipped_but_not_queued(w):
    w.app.config["replies"]["skip_acknowledgements"] = True
    await customer(w, "ok")
    assert CUSTOMER not in w.sent
    assert await queue(w) == []


async def test_the_no_reply_instruction_queues_as_skipped(w):
    w.app.config["replies"]["no_reply_instruction"] = "Do not answer questions about politics."
    w.script["reply"] = "[NO_REPLY]"
    await customer(w, "Who should I vote for?")
    assert CUSTOMER not in w.sent
    [item] = await queue(w)
    assert item["reason"] == "skipped" and "no-reply instruction" in item["detail"]


async def test_a_failed_or_empty_model_reply_queues_as_ai_error(w):
    w.script["reply"] = ai_responder.AIResponderError("DeepSeek timed out")
    await customer(w, "Hello?")
    w.script["reply"] = ""
    await customer(w, "Anyone there?", chat=43, username="bob")
    items = {i["chat_id"]: i for i in await queue(w)}
    assert items[CUSTOMER]["reason"] == "ai_error" and "timed out" in items[CUSTOMER]["detail"]
    assert items[43]["reason"] == "ai_error" and "empty" in items[43]["detail"]
    assert not w.sent


async def test_an_unexpected_drafting_failure_queues_as_ai_error(w):
    w.script["reply"] = RuntimeError("boom")
    await customer(w, "Hello?")
    [item] = await queue(w)
    assert item["reason"] == "ai_error" and "RuntimeError" in item["detail"]


async def test_soft_off_queues_every_message(w):
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "owner on holiday", actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    await customer(w, "Hello?")
    await customer(w, "Hello??")
    items = await queue(w)
    assert [(i["reason"], i["text"]) for i in items] == [("soft_off", "Hello??"), ("soft_off", "Hello?")]
    assert "owner on holiday" in items[0]["detail"]


async def test_a_draft_dropped_by_soft_off_is_queued(w, monkeypatch):
    """Switched off while a reply was being written: resuming replays
    nothing, so that customer is queued too."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_reply(**kw):
        started.set()
        await release.wait()
        return "late"

    monkeypatch.setattr(ai_responder, "generate_reply", slow_reply)
    await w.app.on_incoming(FakeEvent(CUSTOMER, "Hello?"))
    await asyncio.wait_for(started.wait(), 5)       # the model is writing the reply
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "stop", actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    await settle(w.app)
    [item] = await queue(w)
    assert item["reason"] == "soft_off" and CUSTOMER not in w.sent


async def test_a_paused_or_taken_over_chat_queues_as_paused(w):
    await customer(w, "First")                       # makes the conversation
    await w.db.set_paused(CUSTOMER, True)
    await customer(w, "Hello?")
    [item] = await queue(w)
    assert item["reason"] == "paused" and item["detail"] == "paused in the panel"


async def test_an_escalation_queues_as_escalated(w):
    await customer(w, "I will call my lawyer")
    [item] = await queue(w)
    assert item["reason"] == "escalated" and "lawyer" in item["detail"]
    # The chat is paused now: the next message is queued as paused.
    await customer(w, "Hello?")
    assert [i["reason"] for i in await queue(w)] == ["paused", "escalated"]


async def test_a_policy_hold_queues_as_policy_hold_and_keeps_the_draft(w):
    w.app.config["banned_topics"] = ["crypto"]
    w.script["reply"] = "We accept crypto payments."
    await customer(w, "Can I pay in bitcoin?")
    assert CUSTOMER not in w.sent
    drafts = [m for m in await w.db.get_messages(CUSTOMER) if m["status"] == STATUS_PENDING]
    assert len(drafts) == 1
    [item] = await queue(w)
    assert item["reason"] == "policy_hold" and "crypto" in item["detail"]


async def test_a_draft_waiting_for_approval_is_not_unanswered(w):
    w.app.config["auto_send"] = False
    await customer(w, "Hi")
    assert await queue(w) == []


async def test_a_fallback_phrase_queues_the_message_but_the_reply_still_goes_out(w):
    w.app.config["unanswered"]["fallback_phrases"] = ["let me check with"]
    w.script["reply"] = "Good question! LET ME CHECK WITH the team and get back to you."
    await customer(w, "Do you do hair extensions?")
    assert w.sent[CUSTOMER] == ["Good question! LET ME CHECK WITH the team and get back to you."]
    [item] = await queue(w)
    assert item["reason"] == "fallback" and "let me check with" in item["detail"]


async def test_a_fallback_in_a_drafted_reply_is_queued_too(w):
    w.app.config["auto_send"] = False
    w.app.config["unanswered"]["fallback_phrases"] = ["I'm not sure"]
    w.script["reply"] = "i'm not sure, sorry."
    await customer(w, "Is parking free?")
    [item] = await queue(w)
    assert item["reason"] == "fallback"


async def test_one_entry_per_message_the_first_reason_wins(w):
    w.app.config["banned_topics"] = ["crypto"]
    w.app.config["unanswered"]["fallback_phrases"] = ["let me check"]
    w.script["reply"] = "Let me check about crypto."
    await customer(w, "Crypto?")
    [item] = await queue(w)
    assert item["reason"] == "policy_hold"


async def test_the_owner_chat_is_never_queued(w):
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "off", actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    await customer(w, "hello, it's me", chat=OWNER, username="owner")
    assert await queue(w) == []


async def test_a_failure_to_record_never_costs_the_reply(w, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("queue table is gone")

    monkeypatch.setattr(unanswered, "record", broken)
    w.app.config["unanswered"]["fallback_phrases"] = ["let me check"]
    w.script["reply"] = "Let me check and come back to you."
    await customer(w, "Is it open?")
    assert w.sent[CUSTOMER] == ["Let me check and come back to you."]
    # And an early exit still exits cleanly.
    await w.db.set_paused(CUSTOMER, True)
    await customer(w, "Again?")
    assert len(w.sent[CUSTOMER]) == 1


# ---------------------------------------------------------------- admin API


@pytest_asyncio.fixture
async def items(pg_pool):
    """Two clients in the default industry, one open item each."""
    a = await seed_session(pg_pool, "acct-a", name="Salon A")
    b = await seed_session(pg_pool, "acct-b", name="Salon B")
    out = {}
    for tid, session, text in ((a, "acct-a", "Do you do beards?"), (b, "acct-b", "Is there parking?")):
        db = Database(pg_pool, session)
        await db.connect()
        await db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
        row = await db.record_message(CUSTOMER, DIR_IN, STATUS_RECEIVED, text)
        out[tid] = await unanswered.record(pg_pool, tenant_id=tid, session_id=session, chat_id=CUSTOMER,
                                           message_id=row["id"], reason=unanswered.FALLBACK, detail="x")
        await db.close()
    yield SimpleNamespace(pool=pg_pool, a=a, b=b, item_a=out[a], item_b=out[b])
    await alerts.drain()


async def test_the_queue_needs_the_admin_login(panel_client, items):
    await panel_client.post("/api/logout")
    assert (await panel_client.get("/api/unanswered")).status_code == 401
    assert (await panel_client.post(f"/api/unanswered/{items.item_a}/reviewed")).status_code == 401


async def test_list_all_clients_or_one(panel_client, items):
    r = (await panel_client.get("/api/unanswered")).json()
    assert r["open"] == 2
    assert {(i["tenant_name"], i["text"]) for i in r["items"]} == {("Salon A", "Do you do beards?"),
                                                                   ("Salon B", "Is there parking?")}
    assert {t["name"] for t in r["tenants"]} >= {"Salon A", "Salon B"}
    r = (await panel_client.get(f"/api/unanswered?tenant_id={items.b}")).json()
    assert [i["id"] for i in r["items"]] == [items.item_b]
    assert (await panel_client.get("/api/unanswered?status=nope")).status_code == 400
    assert (await panel_client.get("/api/unanswered?tenant_id=9999")).status_code == 404
    assert (await panel_client.get("/api/unanswered/count")).json() == {"open": 2}


async def test_review_and_reopen(panel_client, items):
    r = await panel_client.post(f"/api/unanswered/{items.item_a}/reviewed")
    assert r.status_code == 200 and r.json()["status"] == "reviewed" and r.json()["reviewed_by"] == "admin"
    assert [i["id"] for i in (await panel_client.get("/api/unanswered")).json()["items"]] == [items.item_b]
    reviewed = (await panel_client.get("/api/unanswered?status=reviewed")).json()["items"]
    assert [i["id"] for i in reviewed] == [items.item_a]
    assert len((await panel_client.get("/api/unanswered?status=all")).json()["items"]) == 2

    r = await panel_client.post(f"/api/unanswered/{items.item_a}/reopen")
    assert r.json()["status"] == "open" and r.json()["reviewed_by"] is None
    events = [e["event"] for e in await audit.list_events(items.pool, tenant_id=items.a)]
    assert events[:2] == [unanswered_api.UNANSWERED_REOPENED, unanswered_api.UNANSWERED_REVIEWED]
    assert (await panel_client.post("/api/unanswered/99999/reviewed")).status_code == 404


async def test_promote_appends_to_the_industry_faq_as_a_new_version(panel_client, items):
    store = tenants.TenantStore(items.pool)
    industry = await store.get_industry(1)
    before = (await store.version(tenants.INDUSTRY, 1, industry["template_version"]))["content"]["sections"]
    await store.save_industry_template(
        1, {"sections": {**before, "faq": "Q: Do you take cards?\nA: Yes."}}, actor="admin")
    version_before = (await store.get_industry(1))["template_version"]

    r = await panel_client.post(f"/api/unanswered/{items.item_a}/promote",
                                json={"answer": "Yes, beard trims are 15 EUR.\n"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["status"] == "added_to_template"
    assert body["industry"]["template_version"] == version_before + 1

    live = await store.get_industry(1)
    assert live["template_version"] == version_before + 1
    version = await store.version(tenants.INDUSTRY, 1, live["template_version"])
    assert version["content"]["sections"]["faq"] == (
        "Q: Do you take cards?\nA: Yes.\n\nQ: Do you do beards?\nA: Yes, beard trims are 15 EUR.")
    assert version["note"] == f"From the unanswered queue #{items.item_a}"
    # The other sections are carried over unchanged.
    assert {k: v for k, v in version["content"]["sections"].items() if k != "faq"} == \
        {k: v for k, v in before.items() if k != "faq"}
    [event] = [e for e in await audit.list_events(items.pool, tenant_id=items.a)
               if e["event"] == unanswered_api.UNANSWERED_PROMOTED]
    assert event["payload"]["version"] == version_before + 1 and event["actor"] == "admin"
    # Every client in the industry renders it.
    bundle = await store.bundle(items.b)
    assert "beard trims are 15 EUR" in bundle.prompt.text

    # An explicit question wins over the customer's words.
    r = await panel_client.post(f"/api/unanswered/{items.item_b}/promote",
                                json={"question": "Is there\nparking?  ", "answer": "Yes, behind the building."})
    faq = (await store.version(tenants.INDUSTRY, 1, (await store.get_industry(1))["template_version"]))
    assert faq["content"]["sections"]["faq"].endswith("Q: Is there parking?\nA: Yes, behind the building.")


async def test_promote_refuses_an_empty_answer_and_an_overflowing_faq(panel_client, items):
    store = tenants.TenantStore(items.pool)
    r = await panel_client.post(f"/api/unanswered/{items.item_a}/promote", json={"answer": "   "})
    assert r.status_code == 400
    industry = await store.get_industry(1)
    sections = (await store.version(tenants.INDUSTRY, 1, industry["template_version"]))["content"]["sections"]
    await store.save_industry_template(
        1, {"sections": {**sections, "faq": "x" * (prompt_layers.MAX_SECTION_CHARS - 20)}}, actor="admin")
    version = (await store.get_industry(1))["template_version"]

    r = await panel_client.post(f"/api/unanswered/{items.item_a}/promote", json={"answer": "A long enough answer."})
    assert r.status_code == 400 and "limit" in r.json()["detail"]
    assert (await store.get_industry(1))["template_version"] == version        # nothing saved
    [item] = [i for i in (await panel_client.get("/api/unanswered")).json()["items"] if i["id"] == items.item_a]
    assert item["status"] == "open"
