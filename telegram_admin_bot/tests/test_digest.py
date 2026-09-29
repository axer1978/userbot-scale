"""The weekly digest (digest.py): due at the configured weekday and hour in
the client's own timezone, the previous full week's numbers, sent once per
week whatever the scheduler does, by Telegram and (with SMTP) e-mail, and
an alert when the owner could not be reached at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

import alerts
import booking_store
import commands
import digest
import mailer
import scheduler
import stats
import unanswered
from conftest import seed_session
from database import DIR_IN, DIR_OUT, STATUS_RECEIVED, STATUS_SENT, Database

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

NY = ZoneInfo("America/New_York")
RIGA = ZoneInfo("Europe/Riga")
# A Monday.
MONDAY = datetime(2030, 3, 4, tzinfo=RIGA)


class FakeBus:
    """Records dispatches; answers from a table, or times out."""

    def __init__(self, answers=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.answers = answers or {}

    async def dispatch(self, session_id, action, args=None, *, timeout=30):
        self.calls.append((session_id, action, args or {}))
        answer = self.answers.get(action)
        if answer is None:
            raise commands.CommandTimeout("nobody home")
        return answer

    async def publish_event(self, session_id, payload):
        pass

    def notices(self):
        return [args["text"] for _, action, args in self.calls if action == "owner_notice"]


@pytest_asyncio.fixture
async def t(pg_pool, monkeypatch):
    for name in ("SMTP_HOST", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    tid = await seed_session(pg_pool, "acct", name="Salon Anna")
    # The account is running (a live lease), so the digest goes over the bus.
    await pg_pool.execute(
        "UPDATE telegram_sessions SET lease_expires_at = now() + interval '1 hour' WHERE session_id = 'acct'")
    yield SimpleNamespace(pool=pg_pool, tid=tid)
    await alerts.drain()


async def configure(t, overrides):
    await t.pool.execute("UPDATE tenants SET config_json = $2::jsonb WHERE id = $1", t.tid, json.dumps(overrides))


async def log_rows(t):
    return [dict(r) for r in await t.pool.fetch(
        "SELECT week_start, sent_via FROM digest_log WHERE tenant_id = $1 ORDER BY week_start", t.tid)]


SENT = {"owner_notice": {"sent": True, "error": ""}}


async def test_sent_once_per_week_however_often_the_scheduler_ticks(t):
    await configure(t, {"timezone": "Europe/Riga"})             # Monday 09:00 by default
    bus = FakeBus(SENT)
    now = MONDAY + timedelta(hours=9, minutes=1)
    assert await digest.tick(t.pool, bus, now) == {t.tid: "telegram"}
    assert await digest.tick(t.pool, bus, now) == {}
    assert await digest.tick(t.pool, bus, now + timedelta(days=3)) == {}
    [notice] = bus.notices()
    assert notice.startswith("Weekly summary for Salon Anna, 25.02–03.03:")
    [(session, _, args)] = bus.calls
    assert session == "acct" and args["reason"] == "weekly digest"
    assert await log_rows(t) == [{"week_start": (MONDAY - timedelta(days=7)).date(), "sent_via": "telegram"}]

    # The next week, the next one.
    assert await digest.tick(t.pool, bus, now + timedelta(days=7)) == {t.tid: "telegram"}
    assert len(bus.notices()) == 2 and bus.notices()[1].startswith("Weekly summary for Salon Anna, 04.03–10.03:")


async def test_not_before_the_weekday_and_hour_in_the_clients_timezone(t):
    await configure(t, {"timezone": "America/New_York", "digest": {"weekday": 2, "hour": 18}})
    bus = FakeBus(SENT)
    wednesday_ny = datetime(2030, 3, 6, 18, 0, tzinfo=NY)
    for early in (datetime(2030, 3, 4, 12, 0, tzinfo=NY),                  # Monday
                  wednesday_ny - timedelta(minutes=1),
                  # Already 18:00 on Wednesday in Riga, not yet in New York.
                  datetime(2030, 3, 6, 18, 30, tzinfo=RIGA)):
        assert await digest.tick(t.pool, bus, early.astimezone(timezone.utc)) == {}
    assert bus.calls == []
    assert await digest.tick(t.pool, bus, wednesday_ny.astimezone(timezone.utc)) == {t.tid: "telegram"}
    # Late is fine too (the scheduler was down): later that week, once.
    await t.pool.execute("DELETE FROM digest_log")
    assert await digest.tick(t.pool, bus, (wednesday_ny + timedelta(days=2)).astimezone(timezone.utc)) \
        == {t.tid: "telegram"}


async def test_the_digest_can_be_switched_off(t):
    await configure(t, {"digest": {"enabled": False}})
    bus = FakeBus(SENT)
    assert await digest.tick(t.pool, bus, MONDAY + timedelta(hours=10)) == {}
    assert bus.calls == [] and await log_rows(t) == []


async def test_the_previous_full_weeks_numbers(t):
    """Real rows made now, so `now` for the digest is next Monday 10:00:
    the week being reported is this one."""
    await configure(t, {"timezone": "Europe/Riga"})
    real_now = datetime.now(timezone.utc)
    this_week = stats.week_start(real_now, "Europe/Riga")
    next_week = this_week + timedelta(days=7)
    now = next_week + timedelta(hours=10)

    db = Database(t.pool, "acct")
    await db.connect()
    for chat in (1, 2):
        await db.upsert_conversation(chat, f"Customer {chat}", None, False, 1)
    await db.record_message(1, DIR_IN, STATUS_RECEIVED, "Hi")
    await db.record_message(1, DIR_OUT, STATUS_SENT, "Hello!", llm_model="deepseek-chat")
    await db.record_message(1, DIR_OUT, STATUS_SENT, "Typed by hand")
    await db.record_message(2, DIR_IN, STATUS_RECEIVED, "Price?")
    last_in = await db.record_message(2, DIR_IN, STATUS_RECEIVED, "Hello??")
    await db.close()
    await unanswered.record(t.pool, tenant_id=t.tid, session_id="acct", chat_id=2, message_id=last_in["id"],
                            reason=unanswered.AI_ERROR)

    store = booking_store.BookingStore(t.pool, t.tid, "acct")

    async def booking(start, state):
        b = await store.create(chat_id=1, customer_name="Customer 1", customer_username=None, starts_at=start,
                               ends_at=start + timedelta(hours=1), buffer_minutes=0, tz="Europe/Riga")
        await t.pool.execute("UPDATE bookings SET state = $2 WHERE id = $1", b["id"], state)

    await booking(this_week + timedelta(days=1, hours=10), "completed")
    await booking(this_week + timedelta(days=1, hours=12), "no_show")
    await booking(this_week + timedelta(days=2, hours=10), "cancelled")
    await booking(next_week + timedelta(days=1, hours=10), "confirmed")      # the coming week
    await booking(next_week + timedelta(days=9), "confirmed")                 # beyond 7 days

    bus = FakeBus(SENT)
    assert await digest.tick(t.pool, bus, now) == {t.tid: "telegram"}
    last_day = next_week - timedelta(days=1)
    assert bus.notices() == [
        f"Weekly summary for Salon Anna, {this_week:%d.%m}–{last_day:%d.%m}: 2 bookings (1 no-shows, 1 cancelled), "
        "3 messages from 2 people, the bot answered with 1 messages, 1 unanswered (open now: 1). "
        "Confirmed for the next 7 days: 1."
    ]
    assert (await log_rows(t))[0]["week_start"] == this_week.date()


async def test_by_email_too_when_smtp_is_set_up(t, monkeypatch):
    mails = []

    async def fake_send(settings, *, to, subject, body, **kw):
        mails.append((to, subject, body))

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    await configure(t, {"booking": {"owner_email": "anna@example.com"}})
    bus = FakeBus(SENT)
    assert await digest.tick(t.pool, bus, MONDAY + timedelta(hours=10)) == {t.tid: "telegram,email"}
    [(to, subject, body)] = mails
    assert to == "anna@example.com" and subject == "Weekly summary: Salon Anna, 25.02–03.03"
    assert body == bus.notices()[0]

    # digest.email wins over booking.owner_email; e-mail alone is enough.
    await configure(t, {"booking": {"owner_email": "anna@example.com"}, "digest": {"email": "books@example.com"}})
    assert await digest.tick(t.pool, FakeBus(), MONDAY + timedelta(days=7, hours=10)) == {t.tid: "email"}
    assert mails[1][0] == "books@example.com"
    assert await alerts.list_alerts(t.pool, open_only=True) == []


async def test_an_alert_when_the_owner_cannot_be_reached(t):
    await configure(t, {"booking": {"owner_email": "anna@example.com"}})      # but no SMTP
    bus = FakeBus({"owner_notice": {"sent": False, "error": "the owner could not be reached (booking.provider)"}})
    [via] = (await digest.tick(t.pool, bus, MONDAY + timedelta(hours=10))).values()
    assert via.startswith("none: telegram: the owner could not be reached") and "SMTP is not set up" in via
    assert (await log_rows(t))[0]["sent_via"] == via
    [alert] = await alerts.list_alerts(t.pool, open_only=True)
    assert (alert["kind"], alert["severity"], alert["tenant_id"]) == ("digest", alerts.WARNING, t.tid)

    # Not retried that week; the next week's failure counts on the same alert.
    assert await digest.tick(t.pool, bus, MONDAY + timedelta(hours=11)) == {}
    await t.pool.execute("UPDATE telegram_sessions SET lease_expires_at = NULL WHERE session_id = 'acct'")
    [via] = (await digest.tick(t.pool, bus, MONDAY + timedelta(days=7, hours=10))).values()
    assert "the account is not running" in via
    [alert] = await alerts.list_alerts(t.pool, open_only=True)
    assert alert["count"] == 2
    assert len(bus.calls) == 1                           # an account nobody runs is not asked

    # Reached again: the alert closes.
    await t.pool.execute(
        "UPDATE telegram_sessions SET lease_expires_at = now() + interval '1 hour' WHERE session_id = 'acct'")
    assert await digest.tick(t.pool, FakeBus(SENT), MONDAY + timedelta(days=14, hours=10)) == {t.tid: "telegram"}
    assert await alerts.list_alerts(t.pool, open_only=True) == []


async def test_a_tenant_without_an_account_gets_none(t):
    await t.pool.execute("INSERT INTO tenants (name, industry_id) VALUES ('No account', 1)")
    bus = FakeBus(SENT)
    assert await digest.tick(t.pool, bus, MONDAY + timedelta(hours=10)) == {t.tid: "telegram"}


async def test_the_scheduler_runs_the_digest(t):
    # Due from Monday 00:00, so whenever this test runs.
    await configure(t, {"digest": {"weekday": 0, "hour": 0}})
    bus = FakeBus({**SENT, "reload_controls": {"off": ""}})
    await scheduler.platform_tick(t.pool, bus)
    await scheduler.platform_tick(t.pool, bus)
    assert len(bus.notices()) == 1
    [row] = await log_rows(t)
    assert row["sent_via"] == "telegram"
