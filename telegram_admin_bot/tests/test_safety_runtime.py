"""Safety and control on a running account: soft-off, the global stop, the
spend cap, escalation keywords, human takeover and the three anomaly
triggers (a new Telegram login, a send-volume spike, the outbound
trip-wire).

Messages enter through SessionRuntime.on_incoming / on_outgoing like real
ones; the model and Telegram are faked.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

import ai_responder
import alerts
import audit
import controls
import health
import scheduler
import session_runtime
from database import DIR_OUT, STATUS_PENDING, STATUS_SENT

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

RIGA = ZoneInfo("Europe/Riga")
OWNER = 999
CUSTOMER = 42
NOW = datetime(2030, 3, 4, 12, 0, tzinfo=RIGA)
_ids = itertools.count(1)


class FakeEvent:
    def __init__(self, chat_id, text="", message_id=None):
        self.is_private = True
        self.chat_id = chat_id
        self.raw_text = text
        self.message = SimpleNamespace(id=message_id or next(_ids), reply_to_msg_id=None, photo=None)

    async def get_sender(self):
        return SimpleNamespace(id=self.chat_id, first_name="Anna", last_name=None, username="anna",
                               bot=False, access_hash=1)

    get_chat = get_sender


class FakeClient:
    """Enough of a TelegramClient for the login check and a hard-off."""

    def __init__(self):
        self.authorizations = [SimpleNamespace(hash=0, current=True, device_model="Server", platform="Linux",
                                               app_name="bot", app_version="1", country="LV", date_created=None)]
        self.logged_out = False

    async def __call__(self, request):
        return SimpleNamespace(authorizations=list(self.authorizations))

    async def log_out(self):
        self.logged_out = True
        return True

    def is_connected(self):
        return False


def login(hash_, device="iPhone 15"):
    return SimpleNamespace(hash=hash_, current=False, device_model=device, platform="iOS", app_name="Telegram",
                           app_version="11", country="Latvia", date_created=datetime(2030, 3, 4, tzinfo=timezone.utc))


@pytest_asyncio.fixture
async def w(app, db, monkeypatch):
    sent: dict[int, list[str]] = {}
    script = {"reply": "Sure, see you then.", "calls": 0}
    clock = {"now": NOW}
    msg_ids = iter(range(5000, 90_000))

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.setdefault(chat_id, []).append(text)
        return SimpleNamespace(id=next(msg_ids))

    async def fake_reply(**kw):
        script["calls"] += 1
        return script["reply"]

    async def nothing(*a, **k):
        return None

    async def owner_chat():
        return script.get("owner", OWNER)

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "utcnow", lambda: clock["now"].astimezone(timezone.utc))
    monkeypatch.setattr(app.flow, "provider_chat_id", owner_chat)
    monkeypatch.setattr(session_runtime.asyncio, "sleep", nothing)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_reply)
    app.deepseek_key = "k"
    app.client = FakeClient()
    app.config["auto_send"] = True
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    app.config["api_spend_cap_eur"] = 0
    app.config["escalation_keywords"] = ["lawyer", "refund"]
    app.telegram_state["connected"] = True
    yield SimpleNamespace(app=app, db=db, sent=sent, script=script, clock=clock, pool=app.pool)
    await alerts.drain()


async def settle(app):
    for _ in range(10):
        tasks = [t for t in [*app.flow.scan_tasks.values(), *app.draft_tasks.values()] if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def customer(w, text, chat=CUSTOMER):
    await w.app.on_incoming(FakeEvent(chat, text))
    await settle(w.app)


async def audit_events(w, event):
    return [e for e in await audit.list_events(w.pool, tenant_id=w.app.tenant_id) if e["event"] == event]


# ------------------------------------------------------------------ soft-off


async def test_a_normal_message_is_answered(w):
    await customer(w, "Hi, are you open tomorrow?")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]


async def test_soft_off_receives_but_sends_nothing_and_resume_replays_nothing(w):
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "owner on holiday", actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    assert w.app.paused() and "owner on holiday" in w.app.off_reason

    await customer(w, "Hello?")
    assert CUSTOMER not in w.sent and w.script["calls"] == 0          # no AI call either
    history = await w.db.get_messages(CUSTOMER)
    assert [m["text"] for m in history] == ["Hello?"]                 # but it is stored

    # A bot send is refused at the last step too.
    with pytest.raises(session_runtime.SendBlocked, match="owner on holiday"):
        await w.app.send_as_me(CUSTOMER, "anything")
    # The operator can still write by hand from the panel.
    await w.app.handle_command("send", {"chat_id": CUSTOMER, "text": "Back on Monday."})
    assert w.sent[CUSTOMER] == ["Back on Monday."]

    await controls.remove_hold(w.pool, w.app.tenant_id, controls.MANUAL, actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    await settle(w.app)
    # Nothing that arrived while off is answered after resuming.
    assert w.sent[CUSTOMER] == ["Back on Monday."] and w.script["calls"] == 0


async def test_entering_soft_off_drops_replies_waiting_for_quiet_hours(w):
    due = w.app.utcnow() + timedelta(hours=8)
    await scheduler.defer_reply(w.pool, w.app.tenant_id, w.app.session_id, CUSTOMER, due)
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "pause", actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    assert await scheduler.deferred_for(w.pool, w.app.tenant_id) == []


async def test_the_switch_is_rechecked_right_before_a_send(w):
    """A hold added elsewhere (another process) stops the next send even
    before this runtime heard about it."""
    assert not w.app.paused()
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "from elsewhere", actor=audit.ADMIN)
    with pytest.raises(session_runtime.SendBlocked):
        await w.app.send_as_me(CUSTOMER, "hello", guard=False)
    assert w.app.paused()


async def test_global_stop_stops_every_tenant_and_only_the_admin_lifts_it(w):
    with pytest.raises(ValueError):
        await controls.set_global_stop(w.pool, True, reason=" ", actor=audit.ADMIN)
    await controls.set_global_stop(w.pool, True, reason="provider outage", actor=audit.ADMIN)
    await customer(w, "Hello?")
    assert CUSTOMER not in w.sent
    assert w.app.off_reason == "global stop: provider outage"
    [event] = [e for e in await audit.list_events(w.pool) if e["event"] == audit.GLOBAL_STOP]
    assert event["tenant_id"] is None and event["reason"] == "provider outage"

    await controls.set_global_stop(w.pool, False, reason="", actor=audit.ADMIN)
    await customer(w, "Hello again?")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]


async def test_spend_cap_switches_off_with_an_alert_and_lifts_itself(w, monkeypatch):
    w.app.config["api_spend_cap_eur"] = 1.0
    reached = {"reason": "monthly AI spend limit reached (€1.02 of €1.00)"}

    async def limit(pool, tenant_id, config, now_local):
        return reached["reason"]

    monkeypatch.setattr(session_runtime.ai_limits, "limit_reached", limit)
    await customer(w, "Hi")
    assert CUSTOMER not in w.sent
    assert w.app.paused() and "AI limit" in w.app.off_reason
    [alert] = await alerts.list_alerts(w.pool, open_only=True)
    assert alert["kind"] == "spend_cap"

    # The next period: the tick lifts the hold by itself.
    reached["reason"] = ""
    await w.app.handle_command("scheduler_tick", {})
    assert not w.app.paused()
    assert await alerts.list_alerts(w.pool, open_only=True) == []
    [resumed] = await audit_events(w, audit.TENANT_RESUMED)
    assert resumed["payload"] == {"kind": "spend_cap"} and resumed["actor"] == audit.SYSTEM


async def test_an_hourly_cap_holds_messages_back_and_alerts(w):
    w.app.config["hourly_message_cap"] = 2
    await w.app.send_as_me(CUSTOMER, "one")
    await w.app.send_as_me(CUSTOMER, "two")
    with pytest.raises(session_runtime.SendBlocked, match="Hourly send limit"):
        await w.app.send_as_me(CUSTOMER, "three")
    [alert] = await alerts.list_alerts(w.pool, open_only=True)
    assert alert["kind"] == "send_cap"


# ---------------------------------------------------------------- escalation


async def test_an_escalation_keyword_pauses_the_chat_and_pings_the_owner(w):
    await customer(w, "I will call my LAWYER about this")
    assert CUSTOMER not in w.sent and w.script["calls"] == 0
    [ping] = w.sent[OWNER]
    assert "Anna needs a person" in ping and "“lawyer”" in ping and "call my LAWYER" in ping
    conversation = await w.db.get_conversation(CUSTOMER)
    assert conversation["automation_paused"] and "lawyer" in conversation["paused_reason"]
    [event] = await audit_events(w, audit.ESCALATED)
    assert event["payload"]["keyword"] == "lawyer" and event["payload"]["owner_told"] is True
    assert "escalation" in w.app.hub.types()

    # Later messages in that chat are stored, not answered, not re-escalated.
    await customer(w, "Hello? Refund please")
    assert CUSTOMER not in w.sent and len(w.sent[OWNER]) == 1

    # Switching the chat back on in the panel clears the reason.
    conversation = await w.db.set_paused(CUSTOMER, False)
    assert conversation["paused_reason"] == ""


async def test_a_keyword_inside_another_word_does_not_escalate(w):
    await customer(w, "Is the paralawyer course free?")     # "lawyer" not at a word start
    assert w.sent[CUSTOMER] == ["Sure, see you then."]


async def test_an_escalation_nobody_can_be_told_about_alerts_the_operator(w):
    w.script["owner"] = None
    await customer(w, "I want a refund")
    assert OWNER not in w.sent
    [alert] = await alerts.list_alerts(w.pool, open_only=True)
    assert alert["kind"] == "escalation_unrouted"
    notes = [m["text"] for m in await w.db.get_messages(CUSTOMER) if m["direction"] == "system"]
    assert any("could NOT be told" in n for n in notes)


async def test_escalation_still_pauses_the_chat_while_soft_off(w):
    await controls.add_hold(w.pool, w.app.tenant_id, controls.MANUAL, "pause", actor=audit.ADMIN)
    await w.app.handle_command("reload_controls", {})
    await customer(w, "refund now")
    assert (await w.db.get_conversation(CUSTOMER))["automation_paused"]
    assert OWNER not in w.sent                      # sending is off: the owner can't be pinged
    [alert] = await alerts.list_alerts(w.pool, open_only=True)
    assert alert["kind"] == "escalation_unrouted"


# ------------------------------------------------------------ human takeover


async def test_writing_by_hand_silences_the_bot_in_that_chat_until_takeover_hours_pass(w):
    await customer(w, "Hi")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]
    # The owner answers on their phone.
    await w.app.on_outgoing(FakeEvent(CUSTOMER, "I'll handle this one personally"))
    conversation = await w.db.get_conversation(CUSTOMER)
    assert conversation["human_takeover_until"]
    [event] = await audit_events(w, audit.HUMAN_TAKEOVER)
    assert event["actor"] == audit.OWNER and event["payload"]["chat_id"] == CUSTOMER

    await customer(w, "Great, thanks! One more question")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]           # the bot stays quiet

    # takeover_hours (12 by default) later it carries on by itself.
    w.clock["now"] += timedelta(hours=12, minutes=1)
    await customer(w, "Are you there?")
    assert w.sent[CUSTOMER][-1] == "Sure, see you then." and len(w.sent[CUSTOMER]) == 2


async def test_the_bots_own_messages_do_not_count_as_a_takeover(w):
    row = await w.app.send_as_me(CUSTOMER, "Hello from the bot")
    # Telegram echoes the same message back as an outgoing event.
    await w.app.on_outgoing(FakeEvent(CUSTOMER, "Hello from the bot", message_id=row["telegram_id"]))
    assert (await w.db.get_conversation(CUSTOMER))["human_takeover_until"] is None


async def test_a_hand_sent_panel_message_is_a_takeover_too(w):
    await customer(w, "Hi")
    await w.app.handle_command("send", {"chat_id": CUSTOMER, "text": "Hi, it's Anna from the salon."})
    assert (await w.db.get_conversation(CUSTOMER))["human_takeover_until"]
    await customer(w, "Oh hi Anna")
    assert w.sent[CUSTOMER] == ["Sure, see you then.", "Hi, it's Anna from the salon."]


async def test_takeover_can_be_switched_off_in_config(w):
    w.app.config["takeover_hours"] = 0
    await w.app.on_outgoing(FakeEvent(CUSTOMER, "typed by hand"))
    await customer(w, "Hi")
    assert w.sent[CUSTOMER] == ["Sure, see you then."]


async def test_messages_to_the_owner_chat_are_not_a_takeover(w):
    await w.app.on_outgoing(FakeEvent(OWNER, "note to self"))
    assert (await w.db.get_conversation(OWNER))["human_takeover_until"] is None


# ------------------------------------------------------------------ anomalies


async def test_a_new_telegram_login_switches_the_client_off(w):
    w.app.client.authorizations.append(login(111, "Pixel 8"))
    assert await w.app.check_logins() == []            # the first check only records
    assert not w.app.paused()

    w.app.client.authorizations.append(login(222, "Unknown PC"))
    [fresh] = await w.app.check_logins()
    assert fresh["hash"] == 222
    assert w.app.paused() and "anomaly" in w.app.off_reason and "Unknown PC" in w.app.off_reason
    [event] = await audit_events(w, audit.ANOMALY_DETECTED)
    assert event["payload"]["trigger"] == "new_login"
    [alert] = await alerts.list_alerts(w.pool, open_only=True)
    assert alert["kind"] == "anomaly:new_login" and alert["severity"] == alerts.CRITICAL
    logins = await health.known_logins(w.pool, w.app.tenant_id)
    assert {entry["hash"] for entry in logins} == {0, 111, 222}

    # Seen once: the next check does not trip again.
    await controls.remove_hold(w.pool, w.app.tenant_id, controls.ANOMALY, actor=audit.ADMIN)
    assert await w.app.check_logins() == []


async def test_a_message_from_telegrams_service_account_is_never_answered_and_checks_logins(w):
    w.app.client.authorizations.append(login(111))
    await w.app.check_logins()
    w.app.client.authorizations.append(login(333, "Hijacker"))
    await customer(w, "New login. Dear user, we detected a login from a new device", chat=777000)
    assert 777000 not in w.sent and w.script["calls"] == 0
    assert "Hijacker" in w.app.off_reason


async def test_new_logins_only_alert_when_suspending_is_turned_off(w):
    w.app.config["anomaly"]["new_login_suspend"] = False
    await w.app.check_logins()
    w.app.client.authorizations.append(login(444))
    await w.app.check_logins()
    assert not w.app.paused()
    [alert] = await alerts.list_alerts(w.pool, open_only=True)
    assert alert["kind"] == "anomaly:new_login" and alert["severity"] == alerts.WARNING


async def seed_sent(w, count, hours_ago):
    tid = w.app.tenant_id
    await w.pool.executemany(
        "INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text, created_at) "
        "VALUES ($1, $2, $3, $4, $5, 'x', now() - make_interval(hours => $6))",
        [(w.app.session_id, tid, CUSTOMER, DIR_OUT, STATUS_SENT, float(hours_ago))] * count,
    )


async def test_a_send_volume_spike_switches_the_client_off(w):
    await w.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    # A normal week: about 2 messages an hour.
    for day in range(1, 7):
        await seed_sent(w, 48, day * 24)
    await seed_sent(w, 29, 0.5)                 # this hour: 29, under the floor of 30
    await w.app.send_as_me(CUSTOMER, "30th this hour", guard=False)

    with pytest.raises(session_runtime.SendBlocked, match="messages sent in the last hour"):
        await w.app.send_as_me(CUSTOMER, "31st", guard=False)
    assert "anomaly" in w.app.off_reason
    [event] = await audit_events(w, audit.ANOMALY_DETECTED)
    assert event["payload"]["trigger"] == "volume"


async def test_a_busy_account_is_measured_against_its_own_average(w):
    await w.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    for day in range(1, 7):
        await seed_sent(w, 24 * 20, day * 24)    # 20 an hour, every hour
    await seed_sent(w, 60, 0.5)                  # 60 this hour: busy, but under 5x
    await w.app.send_as_me(CUSTOMER, "fine", guard=False)
    assert not w.app.paused()


async def test_the_trip_wire_holds_the_reply_and_switches_the_client_off(w):
    w.script["reply"] = "Pay here: https://evil.example.ru/pay or to 0x52908400098527886E0F7030069857D2E4169EE7"
    await customer(w, "How do I pay?")
    assert CUSTOMER not in w.sent
    [draft] = [m for m in await w.db.get_messages(CUSTOMER) if m["status"] == STATUS_PENDING]
    assert "evil.example.ru" in draft["text"]
    assert "anomaly" in w.app.off_reason
    [event] = await audit_events(w, audit.ANOMALY_DETECTED)
    assert event["payload"]["trigger"] == "tripwire" and len(event["payload"]["reasons"]) == 2


async def test_other_policy_holds_do_not_switch_the_client_off(w):
    w.script["reply"] = "We can give you a discount!"
    await customer(w, "Any deals?")
    assert CUSTOMER not in w.sent and not w.app.paused()


# ------------------------------------------------------------ kill switches


async def test_hard_off_logs_the_session_out_and_finishes_the_runtime(w, monkeypatch):
    stopped = []

    async def fake_stop():
        stopped.append(True)

    monkeypatch.setattr(w.app, "stop", fake_stop)
    result = await w.app.handle_command("hard_off", {"reason": "hijack"})
    assert result == {"logged_out": True} and w.app.client.logged_out
    assert w.app.finished
    await asyncio.sleep(0)


async def test_health_records_what_the_account_reports(w):
    await health.seen(w.pool, w.app.tenant_id, w.app.session_id)
    await health.rate_limited(w.pool, w.app.tenant_id, w.app.session_id, 600)
    view = await health.overview(w.pool, w.app.tenant_id)
    assert view["last_seen_at"] and view["rate_limited_until"]


async def test_reminders_are_not_sent_into_a_taken_over_chat(w, monkeypatch):
    """The booking tick claims a due reminder but does not send it while a
    person handles the chat, and says so."""
    w.app.config["booking"].update(enabled=True, provider="@owner", min_notice_minutes=0)
    await w.db.upsert_conversation(CUSTOMER, "Anna", "anna", False, 1)
    await w.db.set_takeover(CUSTOMER, w.app.utcnow() + timedelta(hours=5))
    start = w.app.utcnow() + timedelta(minutes=100)
    booking = await w.app.booking_store.create(
        chat_id=CUSTOMER, customer_name="Anna", customer_username="anna", service="Cut",
        notes="", starts_at=start, ends_at=start + timedelta(hours=1), buffer_minutes=0, tz="Europe/Riga",
    )
    await w.pool.execute("UPDATE bookings SET state = 'confirmed' WHERE id = $1", booking["id"])
    await w.app.handle_command("scheduler_tick", {})
    await settle(w.app)
    assert CUSTOMER not in w.sent
    notes = [m["text"] for m in await w.db.get_messages(CUSTOMER) if m["direction"] == "system"]
    assert any("not sent: a person is handling this chat" in n for n in notes)
