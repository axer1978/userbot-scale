"""The "New client" wizard (static/js/onboarding.js): its status summary
(review_api.py, /api/onboarding) and the config saves it makes through the
existing tenant routes, done the way the wizard does them: read the
overrides, merge its fields in, PUT with expected_revision."""

from __future__ import annotations

import copy
import json

import pytest
import pytest_asyncio

import audit
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def tenant(pg_pool):
    # Named after its account, the way SessionRegistry.create names a new one.
    return await seed_session(pg_pool, "acct_new")


def merged(overrides: dict, patch: dict) -> dict:
    """What onboarding.js's deepMerge does: nested dicts merge, anything
    else replaces."""
    out = copy.deepcopy(overrides)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


async def save(client, tenant_id, patch, reason="onboarding"):
    view = (await client.get(f"/api/tenants/{tenant_id}")).json()
    return await client.put(f"/api/tenants/{tenant_id}/config", json={
        "overrides": merged(view["config"]["overrides"], patch), "reason": reason,
        "expected_revision": view["config"]["revision"]})


async def config_events(pool, tenant_id):
    rows = await pool.fetch(
        "SELECT * FROM audit_log WHERE tenant_id = $1 AND event = $2 ORDER BY id", tenant_id, audit.CONFIG_CHANGED)
    return [json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in rows]


async def test_a_new_account_shows_every_step_still_to_do(panel_client, tenant):
    status = (await panel_client.get(f"/api/onboarding/{tenant}")).json()
    assert status["steps"] == {"account": True, "business": False, "settings": False,
                               "staging": False, "live": False}
    assert status["next_step"] == "business" and status["configured"] is False
    assert status["session_id"] == "acct_new" and status["staging"] == {"enabled": False, "test_chats": []}
    assert (await panel_client.get("/api/onboarding/9999")).status_code == 404
    listed = (await panel_client.get("/api/onboarding")).json()
    assert [s["tenant_id"] for s in listed] == [tenant]


async def test_the_wizard_steps_through_to_live(panel_client, pg_pool, tenant):
    # Something set earlier (Clients → Config) must survive the wizard's saves.
    r = await save(panel_client, tenant, {"daily_message_cap": 80})
    assert r.status_code == 200

    # Business.
    r = await panel_client.patch(f"/api/tenants/{tenant}", json={"name": "Salon Anna", "industry_id": 1,
                                                                 "reason": "onboarding"})
    assert r.status_code == 200
    assert (await panel_client.get(f"/api/onboarding/{tenant}")).json()["next_step"] == "settings"

    # Key settings.
    r = await save(panel_client, tenant, {
        "timezone": "Europe/Madrid", "auto_send": True,
        "booking": {"enabled": True, "provider": "@anna_owner"},
        "quiet_hours": {"enabled": True, "start": "22:00", "end": "08:00"},
    })
    assert r.status_code == 200, r.text
    effective = r.json()["config"]["effective"]
    assert effective["booking"]["provider"] == "@anna_owner" and effective["daily_message_cap"] == 80
    status = (await panel_client.get(f"/api/onboarding/{tenant}")).json()
    assert status["configured"] is True and status["next_step"] == "staging"

    # Staging: only the test chats are answered.
    r = await save(panel_client, tenant, {"staging": {"enabled": True, "test_chats": ["@Anna_Test", " 12345 "]}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["effective"]["staging"] == {"enabled": True, "test_chats": ["anna_test", "12345"]}
    status = (await panel_client.get(f"/api/onboarding/{tenant}")).json()
    assert status["steps"]["staging"] is True and status["steps"]["live"] is False
    assert status["next_step"] == "live"

    # Go live.
    r = await save(panel_client, tenant, {"staging": {"enabled": False}}, reason="onboarding: go live")
    assert r.status_code == 200
    assert r.json()["config"]["effective"]["staging"] == {"enabled": False, "test_chats": ["anna_test", "12345"]}
    status = (await panel_client.get(f"/api/onboarding/{tenant}")).json()
    assert all(status["steps"].values()) and status["next_step"] is None

    changes = [c for e in await config_events(pg_pool, tenant) for c in e["changes"]]
    staging_enabled = [c for c in changes if c["path"] == "staging.enabled"]
    assert [(c["from"], c["to"]) for c in staging_enabled] == [(False, True), (True, False)]
    rows = await pg_pool.fetch("SELECT reason, actor FROM audit_log WHERE tenant_id = $1 AND event = $2 ORDER BY id",
                               tenant, audit.CONFIG_CHANGED)
    assert rows[-1]["reason"] == "onboarding: go live" and rows[-1]["actor"] == "admin"
    assert (await pg_pool.fetchval("SELECT count(*) FROM audit_log WHERE tenant_id = $1 AND event = $2",
                                   tenant, audit.TENANT_UPDATED)) == 1


async def test_invalid_settings_come_back_per_field_and_change_nothing(panel_client, pg_pool, tenant):
    r = await save(panel_client, tenant, {"quiet_hours": {"enabled": True, "start": "25:00"},
                                          "timezone": "Mars/Olympus", "daily_message_cap": 0})
    assert r.status_code == 422
    paths = {e["path"] for e in r.json()["detail"]["errors"]}
    assert any(p.startswith("quiet_hours") for p in paths)
    assert "timezone" in paths and "daily_message_cap" in paths
    assert await config_events(pg_pool, tenant) == []

    # A save against a revision someone else already moved is refused.
    view = (await panel_client.get(f"/api/tenants/{tenant}")).json()
    assert (await save(panel_client, tenant, {"auto_send": True})).status_code == 200
    r = await panel_client.put(f"/api/tenants/{tenant}/config", json={
        "overrides": {"staging": {"enabled": True}}, "expected_revision": view["config"]["revision"]})
    assert r.status_code == 409
