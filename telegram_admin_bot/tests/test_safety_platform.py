"""The platform side of safety and control, without a running account:
holds and the global stop (controls.py), alerts and their delivery
(alerts.py), billing grace and suspension (billing.py), the health watchdog
(health.py), the send-volume maths (anomaly.py), hard-off, and a
deactivated account losing its lease.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import alerts
import anomaly
import audit
import billing
import commands
import controls
import health
import leasing
import mailer
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def two(pg_pool):
    a = await seed_session(pg_pool, "acct-a", name="Salon A")
    b = await seed_session(pg_pool, "acct-b", name="Salon B")
    yield SimpleNamespace(pool=pg_pool, a=a, b=b)
    await alerts.drain()


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
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def publish_event(self, session_id, payload):
        pass


# -------------------------------------------------------------------- holds


async def test_holds_are_per_cause_audited_and_isolated_per_tenant(two):
    pool = two.pool
    assert await controls.off_reason(pool, two.a) == ""
    assert await controls.add_hold(pool, two.a, controls.MANUAL, "holiday", actor=audit.ADMIN)
    assert not await controls.add_hold(pool, two.a, controls.MANUAL, "again", actor=audit.ADMIN)  # already on
    assert await controls.add_hold(pool, two.a, controls.BILLING, "unpaid", actor=audit.SYSTEM)
    assert await controls.off_reason(pool, two.a) == "paused: holiday; suspended (billing): unpaid"
    assert await controls.off_reason(pool, two.b) == ""               # the other tenant is untouched

    # Lifting one cause leaves the other.
    assert await controls.remove_hold(pool, two.a, controls.MANUAL, actor=audit.ADMIN)
    assert not await controls.remove_hold(pool, two.a, controls.MANUAL, actor=audit.ADMIN)
    assert await controls.off_reason(pool, two.a) == "suspended (billing): unpaid"

    events = [(e["event"], e["payload"]["kind"]) for e in reversed(await audit.list_events(pool, tenant_id=two.a))]
    assert events == [(audit.TENANT_SOFT_OFF, "manual"), (audit.TENANT_SOFT_OFF, "billing"),
                      (audit.TENANT_RESUMED, "manual")]
    with pytest.raises(ValueError):
        await controls.add_hold(pool, two.a, "made_up", "x", actor=audit.ADMIN)


async def test_the_global_stop_covers_every_tenant(two):
    await controls.set_global_stop(two.pool, True, reason="DeepSeek outage", actor=audit.ADMIN)
    assert await controls.off_reason(two.pool, two.a) == "global stop: DeepSeek outage"
    assert await controls.off_reason(two.pool, two.b) == "global stop: DeepSeek outage"
    state = await controls.global_stop(two.pool)
    assert state["on"] and state["by"] == audit.ADMIN and state["at"]
    await controls.set_global_stop(two.pool, False, reason="", actor=audit.ADMIN)
    assert await controls.off_reason(two.pool, two.a) == ""
    kinds = [e["event"] for e in await audit.list_events(two.pool)]
    assert audit.GLOBAL_STOP in kinds and audit.GLOBAL_RESUMED in kinds


async def test_the_upgrade_turns_a_paused_account_into_a_manual_hold(pg_pool, tmp_path):
    """Migration 0004 on an account that was paused with the old switch."""
    import shutil

    import pg as pg_module

    async with pg_pool.acquire() as con:
        schema = await con.fetchval("SELECT current_schema()")
        await con.execute(f'DROP SCHEMA "{schema}" CASCADE; CREATE SCHEMA "{schema}"')
    first = tmp_path / "m"
    first.mkdir()
    for name in ("0001_init.sql", "0002_tenants.sql", "0003_bookings.sql"):
        shutil.copy(pg_module.MIGRATIONS_DIR / name, first)
    assert await pg_module.apply_migrations(pg_pool, first) == [1, 2, 3]
    await pg_pool.execute("INSERT INTO telegram_sessions (session_id, state, state_reason) VALUES "
                          "('p1', 'running', ''), ('p2', 'halted', 'PeerFloodError'), ('p3', 'running', '')")
    for sid, paused in (("p1", True), ("p2", True), ("p3", False)):
        tid = await pg_pool.fetchval("INSERT INTO tenants (name, industry_id, session_id) VALUES ($1, 1, $1) "
                                     "RETURNING id", sid)
        await pg_pool.execute("INSERT INTO session_config (session_id, tenant_id, config) VALUES ($1, $2, $3::jsonb)",
                              sid, tid, json.dumps({"behavior": {"global_pause": paused, "auto_send": True}}))
    assert await pg_module.apply_migrations(pg_pool) == [4, 5, 6]
    rows = await pg_pool.fetch("SELECT t.session_id, h.kind, h.reason FROM tenant_holds h "
                               "JOIN tenants t ON t.id = h.tenant_id ORDER BY t.session_id")
    assert [tuple(r) for r in rows] == [("p1", "manual", "Paused before the upgrade"),
                                        ("p2", "telegram", "PeerFloodError")]
    left = await pg_pool.fetchval("SELECT count(*) FROM session_config "
                                  "WHERE (config->'behavior'->>'global_pause')::boolean")
    assert left == 0


# -------------------------------------------------------------------- alerts


async def test_one_open_alert_per_cause_counts_repeats_and_reopens_after_ack(two, monkeypatch):
    delivered = []

    async def fake_deliver(pool, alert):
        delivered.append(alert["message"])

    monkeypatch.setattr(alerts, "_deliver", fake_deliver)
    first = await alerts.raise_alert(two.pool, tenant_id=two.a, kind="send_cap", message="held back")
    again = await alerts.raise_alert(two.pool, tenant_id=two.a, kind="send_cap", message="held back")
    other = await alerts.raise_alert(two.pool, tenant_id=two.b, kind="send_cap", message="held back too")
    platform = await alerts.raise_alert(two.pool, tenant_id=None, kind="scheduler", message="p")
    await alerts.drain()
    assert first["new"] and not again["new"] and again["id"] == first["id"] and again["count"] == 2
    assert other["new"] and platform["new"]
    assert delivered == ["held back", "held back too", "p"]      # a repeat is not delivered again
    assert (await alerts.open_count(two.pool))["total"] == 3

    await alerts.acknowledge(two.pool, first["id"], by=audit.ADMIN)
    reopened = await alerts.raise_alert(two.pool, tenant_id=two.a, kind="send_cap", message="held back")
    assert reopened["new"] and reopened["id"] != first["id"]
    assert await alerts.resolve(two.pool, tenant_id=two.a, kind="send_cap", by="system") is not None
    assert await alerts.acknowledge_all(two.pool, by=audit.ADMIN) == 2
    assert (await alerts.open_count(two.pool))["total"] == 0


async def test_alerts_go_out_by_email_and_webhook_when_configured(two, monkeypatch):
    mails, posts = [], []

    async def fake_send(settings, *, to, subject, body, timeout=20.0, **_):
        mails.append((to, subject, body))

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(alerts.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    await alerts.raise_alert(two.pool, tenant_id=two.a, kind="telegram", severity=alerts.CRITICAL, message="Stopped")
    await alerts.drain()
    [(to, subject, body)] = mails
    assert to == "ops@example.com" and "Salon A" in subject and "Stopped" in body
    [post] = posts
    assert post["text"] == post["content"] == "[CRITICAL] Salon A: Stopped"
    assert post["tenant_id"] == two.a and post["kind"] == "telegram"


async def test_alert_delivery_failures_never_reach_the_caller(two, monkeypatch):
    async def broken(*a, **k):
        raise mailer.MailError("smtp down")

    monkeypatch.setattr(mailer, "send", broken)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://127.0.0.1:9/nothing-listens")
    await alerts.raise_alert(two.pool, tenant_id=two.a, kind="x", message="y")
    await alerts.drain()


# -------------------------------------------------------------------- hard-off


async def set_key(pool, session_id):
    await pool.execute("UPDATE telegram_sessions SET auth_key_enc = '\\x00'::bytea, dc_id = 2 WHERE session_id = $1",
                       session_id)


async def test_hard_off_through_the_running_account(two):
    await set_key(two.pool, "acct-a")
    bus = FakeBus({"hard_off": {"logged_out": True}})
    result = await controls.hard_off(two.pool, bus, two.a, reason="hijack suspected", actor=audit.ADMIN)
    assert result["logged_out"] and result["how"] == "by the running account"
    row = await two.pool.fetchrow("SELECT is_active, auth_key_enc, state, state_reason FROM telegram_sessions "
                                  "WHERE session_id = 'acct-a'")
    assert (row["is_active"], row["auth_key_enc"], row["state"], row["state_reason"]) == \
        (False, None, "revoked", "hijack suspected")
    [event] = [e for e in await audit.list_events(two.pool, tenant_id=two.a) if e["event"] == audit.HARD_OFF]
    assert event["payload"]["logged_out"] is True
    [alert] = await alerts.list_alerts(two.pool, open_only=True)
    assert alert["kind"] == "hard_off" and "Telegram logged it out" in alert["message"]
    # The other tenant's account is untouched.
    assert await two.pool.fetchval("SELECT is_active FROM telegram_sessions WHERE session_id = 'acct-b'")


async def test_hard_off_when_nothing_runs_the_account_logs_out_directly(two, monkeypatch):
    import session_runtime

    await set_key(two.pool, "acct-a")
    calls = []

    async def fake_log_out(pool, session_id):
        calls.append(session_id)
        return False                       # Telegram could not be reached

    monkeypatch.setattr(session_runtime, "log_out_session", fake_log_out)
    result = await controls.hard_off(two.pool, FakeBus(), two.a, reason="leaked key", actor=audit.ADMIN)
    assert calls == ["acct-a"] and result == {**result, "logged_out": False, "how": "directly"}
    # The key is deleted all the same, and the operator is told what to do.
    assert await two.pool.fetchval("SELECT auth_key_enc FROM telegram_sessions WHERE session_id = 'acct-a'") is None
    [alert] = await alerts.list_alerts(two.pool, open_only=True)
    assert "Settings → Devices" in alert["message"]


async def test_hard_off_needs_a_reason(two):
    with pytest.raises(ValueError):
        await controls.hard_off(two.pool, FakeBus(), two.a, reason="  ", actor=audit.ADMIN)


async def test_a_deactivated_account_loses_its_lease_at_the_next_renewal(two):
    lease = await leasing.acquire(two.pool, "acct-a", "w1")
    assert lease is not None
    assert await leasing.renew(two.pool, "acct-a", "w1") is not None
    await two.pool.execute("UPDATE telegram_sessions SET is_active = false WHERE session_id = 'acct-a'")
    assert await leasing.renew(two.pool, "acct-a", "w1") is None


# -------------------------------------------------------------------- billing


async def test_billing_moves_active_to_grace_then_suspended(two):
    pool = two.pool
    await billing.set_due(pool, two.a, date(2030, 3, 4), actor=audit.ADMIN)
    bus = FakeBus({"owner_notice": {"sent": True}, "reload_controls": {"off": ""}})

    # Still the due day in Riga: nothing happens.
    assert await billing.tick(pool, bus, datetime(2030, 3, 4, 20, 0, tzinfo=timezone.utc)) == []
    # 00:30 in Riga on the 5th (22:30 UTC on the 4th): grace, and the owner is told.
    now = datetime(2030, 3, 4, 22, 30, tzinfo=timezone.utc)
    assert await billing.tick(pool, bus, now) == [f"{two.a}:grace", f"{two.a}:notified"]
    tenant = await pool.fetchrow("SELECT status, grace_until, billing_notice_sent_at FROM tenants WHERE id = $1",
                                 two.a)
    assert tenant["status"] == "grace" and tenant["grace_until"] == now + timedelta(hours=48)
    assert tenant["billing_notice_sent_at"] is not None
    [(sid, action, args)] = [c for c in bus.calls if c[1] == "owner_notice"]
    assert sid == "acct-a" and "Salon A" in args["text"] and "04.03.2030" in args["text"]
    assert "07.03.2030 00:30" in args["text"]                     # the grace end in Riga time
    assert await controls.off_reason(pool, two.a) == ""           # grace still sends
    assert await billing.tick(pool, bus, now + timedelta(minutes=1)) == []   # idempotent

    # 48 hours later: suspended, soft-off with a billing hold.
    later = now + timedelta(hours=48, minutes=1)
    assert await billing.tick(pool, bus, later) == [f"{two.a}:suspended"]
    assert await pool.fetchval("SELECT status FROM tenants WHERE id = $1", two.a) == "suspended"
    assert await controls.off_reason(pool, two.a) == "suspended (billing): grace period ended without a payment"
    kinds = {a["kind"] for a in await alerts.list_alerts(pool, open_only=True)}
    assert kinds == {"billing", "billing_suspended"}

    # A payment lifts it and sets the next due date; data was never touched.
    await billing.mark_paid(pool, bus, two.a, next_due=date(2030, 4, 4), actor=audit.ADMIN)
    tenant = await pool.fetchrow("SELECT status, billing_next_due, grace_until FROM tenants WHERE id = $1", two.a)
    assert (tenant["status"], tenant["billing_next_due"], tenant["grace_until"]) == ("active", date(2030, 4, 4), None)
    assert await controls.off_reason(pool, two.a) == ""
    changes = [e for e in await audit.list_events(pool, tenant_id=two.a) if e["event"] == audit.BILLING_CHANGED]
    assert len(changes) == 4             # due set, grace, suspended, paid
    # Tenant B has no due date and was never touched.
    assert await pool.fetchval("SELECT status FROM tenants WHERE id = $1", two.b) == "active"


async def test_an_owner_who_cannot_be_told_is_retried_and_the_operator_alerted(two):
    pool = two.pool
    await billing.set_due(pool, two.a, date(2030, 3, 1), actor=audit.ADMIN)
    now = datetime(2030, 3, 4, 12, 0, tzinfo=timezone.utc)
    silent = FakeBus()                      # the account is not running
    assert await billing.tick(pool, silent, now) == [f"{two.a}:grace"]
    [alert] = [a for a in await alerts.list_alerts(pool, open_only=True) if a["kind"] == "billing_notice"]
    assert "did not answer" in alert["message"]
    ok = FakeBus({"owner_notice": {"sent": True}})
    assert await billing.tick(pool, ok, now + timedelta(minutes=1)) == [f"{two.a}:notified"]
    assert not [a for a in await alerts.list_alerts(pool, open_only=True) if a["kind"] == "billing_notice"]


async def test_the_status_can_always_be_set_by_hand(two):
    pool, bus = two.pool, FakeBus({"reload_controls": {}})
    with pytest.raises(ValueError):
        await billing.set_status(pool, bus, two.a, "suspended", actor=audit.ADMIN, reason="")
    with pytest.raises(ValueError):
        await billing.set_status(pool, bus, two.a, "gone", actor=audit.ADMIN, reason="x")
    await billing.set_status(pool, bus, two.a, "suspended", actor=audit.ADMIN, reason="chargeback")
    assert "billing" in await controls.off_reason(pool, two.a)
    await billing.set_status(pool, bus, two.a, "grace", actor=audit.ADMIN, reason="gave them two more days")
    assert await controls.off_reason(pool, two.a) == ""
    assert await pool.fetchval("SELECT grace_until FROM tenants WHERE id = $1", two.a) is not None
    await billing.set_status(pool, bus, two.a, "active", actor=audit.ADMIN, reason="paid in cash")
    assert await pool.fetchval("SELECT grace_until FROM tenants WHERE id = $1", two.a) is None


async def test_billing_settings_are_validated(two):
    with pytest.raises(ValueError):
        await billing.save_settings(two.pool, grace_hours=0, notice="x", actor=audit.ADMIN)
    with pytest.raises(ValueError):
        await billing.save_settings(two.pool, grace_hours=24, notice="Pay {amount}", actor=audit.ADMIN)
    saved = await billing.save_settings(two.pool, grace_hours=24, notice="Maksājums {business} līdz {until}",
                                        actor=audit.ADMIN)
    assert saved == await billing.settings(two.pool)


# -------------------------------------------------------------------- health


NOW = datetime(2030, 3, 4, 12, 0, tzinfo=timezone.utc)


def srow(**over):
    base = {"state": "running", "state_reason": "", "is_active": True, "lease_expires_at": NOW + timedelta(seconds=20),
            "lease_seen_at": NOW - timedelta(seconds=5), "activated_at": NOW - timedelta(days=3),
            "last_seen_at": NOW - timedelta(seconds=30), "rate_limited_until": None}
    return {**base, **over}


@pytest.mark.parametrize("over, expected", [
    ({}, health.OK),
    ({"state": "revoked"}, health.REVOKED),
    ({"is_active": False}, health.STOPPED),
    ({"state": "needs_login"}, health.LOGGED_OUT),
    ({"lease_expires_at": NOW - timedelta(minutes=5), "lease_seen_at": NOW - timedelta(minutes=5)}, health.NOT_RUNNING),
    ({"lease_expires_at": None, "lease_seen_at": None, "activated_at": NOW - timedelta(minutes=10)}, health.NOT_RUNNING),
    ({"lease_expires_at": NOW - timedelta(seconds=10), "lease_seen_at": NOW - timedelta(seconds=40)}, health.UNKNOWN),
    ({"last_seen_at": NOW - timedelta(minutes=4)}, health.DISCONNECTED),
    ({"last_seen_at": None, "lease_seen_at": NOW - timedelta(seconds=20), "state": "ready"}, health.UNKNOWN),
    ({"rate_limited_until": NOW + timedelta(minutes=10)}, health.RATE_LIMITED),
])
async def test_health_status(over, expected):
    assert health.status_of(srow(**over), NOW)[0] == expected


async def test_the_watchdog_alerts_on_a_change_and_closes_it_on_recovery(two, monkeypatch):
    notices = []

    async def fake_notify(pool, *, tenant_id, text, **_):
        notices.append((tenant_id, text))

    monkeypatch.setattr(alerts, "notify", fake_notify)
    pool = two.pool
    await set_key(pool, "acct-a")
    await pool.execute("UPDATE telegram_sessions SET state = 'running', lease_worker_id = 'w', "
                       "lease_expires_at = now() + interval '30 seconds', last_seen_at = now() "
                       "WHERE session_id = 'acct-a'")
    await health.seen(pool, two.a, "acct-a")
    assert (await health.check_all(pool))[two.a] == health.OK
    assert await alerts.list_alerts(pool) == []

    # The worker died four minutes ago.
    await pool.execute("UPDATE telegram_sessions SET lease_expires_at = now() - interval '4 minutes', "
                       "last_seen_at = now() - interval '4 minutes' WHERE session_id = 'acct-a'")
    assert (await health.check_all(pool))[two.a] == health.NOT_RUNNING
    [alert] = await alerts.list_alerts(pool, open_only=True)
    assert alert["kind"] == "health:not_running" and alert["severity"] == alerts.CRITICAL
    await health.check_all(pool)                                   # no change, no new alert
    assert len(await alerts.list_alerts(pool)) == 1

    # Back up.
    await pool.execute("UPDATE telegram_sessions SET lease_expires_at = now() + interval '30 seconds', "
                       "last_seen_at = now() WHERE session_id = 'acct-a'")
    await health.seen(pool, two.a, "acct-a")
    assert (await health.check_all(pool))[two.a] == health.OK
    assert await alerts.list_alerts(pool, open_only=True) == []
    assert notices == [(two.a, "Back to normal (was not running).")]
    # Accounts that were never signed in are not watched.
    assert two.b not in await health.check_all(pool)


# -------------------------------------------------------------------- anomaly


async def test_new_logins_ignore_the_first_check_and_the_accounts_own_session():
    now = [{"hash": 0, "current": True}, {"hash": 5, "current": False}]
    assert anomaly.new_logins(None, now) == []
    assert anomaly.new_logins([{"hash": 5}], now) == []
    assert anomaly.new_logins([], now) == [{"hash": 5, "current": False}]


async def test_a_login_record_keeps_no_ip_address():
    auth = SimpleNamespace(hash=7, current=False, device_model="Pixel", platform="Android", app_name="Telegram",
                           app_version="10.1", country="Latvia", ip="203.0.113.9", region="Riga",
                           date_created=datetime(2030, 1, 1, tzinfo=timezone.utc))
    record = anomaly.login_record(auth)
    assert "203.0.113.9" not in json.dumps(record) and record["device"] == "Pixel"
    assert anomaly.describe_login(record) == "Pixel, Android, Telegram 10.1, Latvia"


async def test_volume_spike_is_off_at_multiplier_zero(two):
    assert await anomaly.volume_spike(two.pool, two.a, {"volume_multiplier": 0, "volume_min_messages": 1,
                                                        "volume_baseline_days": 7}) == ""
