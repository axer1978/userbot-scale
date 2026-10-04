"""The panel routes for safety and control (safety_api.py and the
account-level ones in panel.py): all behind the admin login."""

from __future__ import annotations

import pytest
import pytest_asyncio

import alerts
import audit
import controls
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def tenant(pg_pool):
    tid = await seed_session(pg_pool, "acct", name="Salon Anna")
    yield tid
    await alerts.drain()


async def test_everything_needs_the_admin_login(panel_client, tenant):
    await panel_client.post("/api/logout")
    for method, path in (("GET", "/api/safety"), ("POST", "/api/safety/global-stop"), ("GET", "/api/alerts"),
                         ("POST", f"/api/tenants/{tenant}/hard-off"), ("POST", f"/api/tenants/{tenant}/soft-off")):
        assert (await panel_client.request(method, path, json={})).status_code == 401


async def test_soft_off_and_resume_from_the_panel(panel_client, pg_pool, tenant):
    r = await panel_client.post(f"/api/tenants/{tenant}/soft-off", json={"reason": "owner asked"})
    assert r.status_code == 200 and r.json()["off_reason"] == "paused: owner asked"
    status = (await panel_client.get("/api/sessions/acct/status")).json()
    assert status["global_pause"] is True and status["off_reason"] == "paused: owner asked"

    assert (await panel_client.post(f"/api/tenants/{tenant}/resume", json={"kind": "anomaly"})).status_code == 404
    r = await panel_client.post(f"/api/tenants/{tenant}/resume", json={"kind": "manual", "reason": "back"})
    assert r.json()["off_reason"] == "" and r.json()["holds"] == []


async def test_a_billing_suspension_is_not_lifted_with_resume(panel_client, pg_pool, tenant):
    await controls.add_hold(pg_pool, tenant, controls.BILLING, "unpaid", actor=audit.SYSTEM)
    r = await panel_client.post(f"/api/tenants/{tenant}/resume", json={"kind": "billing"})
    assert r.status_code == 400 and "payment" in r.json()["detail"]


async def test_resuming_an_anomaly_closes_its_alerts(panel_client, pg_pool, tenant):
    await controls.add_hold(pg_pool, tenant, controls.ANOMALY, "new login", actor=audit.SYSTEM)
    await alerts.raise_alert(pg_pool, tenant_id=tenant, kind="anomaly:new_login", message="new login",
                             deliver=False)
    await panel_client.post(f"/api/tenants/{tenant}/resume", json={"kind": "anomaly", "reason": "it was the owner"})
    assert await alerts.list_alerts(pg_pool, open_only=True) == []


async def test_resuming_a_whatsapp_halt_closes_its_alerts(panel_client, pg_pool, tenant):
    """The WhatsApp hold is lifted from Safety like the Telegram one, and the
    alerts that came with the halt (whatsapp and whatsapp:*) go with it."""
    await controls.add_hold(pg_pool, tenant, controls.WHATSAPP, "session lost", actor=audit.SYSTEM)
    await alerts.raise_alert(pg_pool, tenant_id=tenant, kind="whatsapp", message="halted", severity=alerts.CRITICAL,
                             deliver=False)
    await alerts.raise_alert(pg_pool, tenant_id=tenant, kind="whatsapp:peer_flood", message="463", deliver=False)
    await alerts.raise_alert(pg_pool, tenant_id=tenant, kind="send_cap", message="unrelated", deliver=False)
    r = await panel_client.post(f"/api/tenants/{tenant}/resume", json={"kind": "whatsapp", "reason": "paired again"})
    assert r.status_code == 200 and r.json()["holds"] == []
    assert [a["kind"] for a in await alerts.list_alerts(pg_pool, open_only=True)] == ["send_cap"]


async def test_the_global_stop_needs_a_reason_and_shows_everywhere(panel_client, pg_pool, tenant):
    r = await panel_client.post("/api/safety/global-stop", json={"on": True, "reason": ""})
    assert r.status_code == 400
    r = await panel_client.post("/api/safety/global-stop", json={"on": True, "reason": "incident"})
    assert r.json()["on"] is True
    overview = (await panel_client.get("/api/safety")).json()
    assert overview["global_stop"]["reason"] == "incident"
    [row] = overview["tenants"]
    assert row["name"] == "Salon Anna" and row["billing"]["status"] == "active"
    status = (await panel_client.get("/api/sessions/acct/status")).json()
    assert status["off_reason"] == "global stop: incident" and status["global_pause"] is False
    summary = (await panel_client.get("/api/safety/summary")).json()
    assert summary["global_stop"]["on"] and summary["scheduler"]["stale"] is True   # no scheduler here
    await panel_client.post("/api/safety/global-stop", json={"on": False, "reason": ""})
    assert (await panel_client.get("/api/sessions/acct/status")).json()["off_reason"] == ""


async def test_alerts_list_and_acknowledge(panel_client, pg_pool, tenant):
    first = await alerts.raise_alert(pg_pool, tenant_id=tenant, kind="send_cap", message="a", deliver=False)
    await alerts.raise_alert(pg_pool, tenant_id=None, kind="scheduler", message="b", deliver=False)
    assert len((await panel_client.get("/api/alerts?open=true")).json()) == 2
    assert len((await panel_client.get(f"/api/alerts?tenant_id={tenant}")).json()) == 1
    r = await panel_client.post(f"/api/alerts/{first['id']}/ack")
    assert r.json()["acknowledged_by"] == audit.ADMIN
    assert (await panel_client.post("/api/alerts/999999/ack")).status_code == 404
    assert (await panel_client.post("/api/alerts/ack-all", json={})).json() == {"acknowledged": 1}
    assert (await panel_client.get("/api/safety/summary")).json()["alerts"]["total"] == 0


async def test_hard_off_must_be_confirmed_with_the_account_id(panel_client, pg_pool, tenant, monkeypatch):
    import session_runtime

    async def fake_log_out(pool, session_id):
        return True

    monkeypatch.setattr(controls, "HARD_OFF_TIMEOUT", 0.2)
    monkeypatch.setattr(session_runtime, "log_out_session", fake_log_out)
    r = await panel_client.post(f"/api/tenants/{tenant}/hard-off", json={"reason": "hijack", "confirm": "yes"})
    assert r.status_code == 400
    assert await pg_pool.fetchval("SELECT is_active FROM telegram_sessions WHERE session_id = 'acct'")
    r = await panel_client.post(f"/api/tenants/{tenant}/hard-off", json={"reason": "hijack", "confirm": "acct"})
    assert r.status_code == 200 and r.json()["logged_out"] is True and r.json()["how"] == "directly"
    assert r.json()["controls"]["tenant"]["state"] == "revoked"


async def test_billing_from_the_panel(panel_client, pg_pool, tenant):
    r = await panel_client.put(f"/api/tenants/{tenant}/billing/due", json={"next_due": "2030-05-01"})
    assert r.json()["billing"]["next_due"] == "2030-05-01"
    r = await panel_client.post(f"/api/tenants/{tenant}/billing/status", json={"status": "suspended", "reason": ""})
    assert r.status_code == 400
    r = await panel_client.post(f"/api/tenants/{tenant}/billing/status",
                                json={"status": "suspended", "reason": "chargeback"})
    assert r.json()["billing"]["status"] == "suspended" and "billing" in r.json()["off_reason"]
    r = await panel_client.post(f"/api/tenants/{tenant}/billing/paid", json={"next_due": "2030-06-01"})
    assert r.json()["billing"] == {**r.json()["billing"], "status": "active", "next_due": "2030-06-01"}
    assert r.json()["off_reason"] == ""

    r = await panel_client.put("/api/platform/billing", json={"grace_hours": 0, "notice": "x"})
    assert r.status_code == 400
    r = await panel_client.put("/api/platform/billing", json={"grace_hours": 72, "notice": "Pay by {until}"})
    assert r.json() == {"grace_hours": 72, "notice": "Pay by {until}"}
    assert (await panel_client.get("/api/platform/billing")).json()["grace_hours"] == 72


async def test_a_chat_can_be_handed_back_to_the_bot(panel_client, pg_pool, tenant):
    from datetime import datetime, timedelta, timezone

    from database import Database

    db = Database(pg_pool, "acct")
    await db.upsert_conversation(42, "Anna", "anna", False, 1)
    await db.set_takeover(42, datetime.now(timezone.utc) + timedelta(hours=3))
    assert (await panel_client.post("/api/sessions/acct/conversations/42/takeover",
                                    json={"active": True})).status_code == 400
    r = await panel_client.post("/api/sessions/acct/conversations/42/takeover", json={"active": False})
    assert r.status_code == 200 and r.json()["human_takeover_until"] is None
    [event] = [e for e in await audit.list_events(pg_pool, tenant_id=tenant) if e["event"] == audit.TAKEOVER_ENDED]
    assert event["payload"] == {"chat_id": 42}
    assert (await panel_client.post("/api/sessions/acct/conversations/7/takeover",
                                    json={"active": False})).status_code == 404


async def test_the_controls_view_for_one_client(panel_client, pg_pool, tenant):
    r = await panel_client.get(f"/api/tenants/{tenant}/controls")
    body = r.json()
    assert body["tenant"]["name"] == "Salon Anna" and body["holds"] == [] and body["health"]["status"] == "unknown"
    assert (await panel_client.get("/api/tenants/999/controls")).status_code == 404
