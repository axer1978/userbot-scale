"""The platform admin API: tenants, industries, prompt layers, versions,
the natural-language config helper, and the audit trail behind them."""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

import ai_responder
import audit
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


async def events(pool, event):
    return [e for e in await audit.list_events(pool) if e["event"] == event]


@pytest_asyncio.fixture
async def tenant(pg_pool):
    return await seed_session(pg_pool, "acct", name="Salon Anna")


async def test_every_platform_route_needs_the_admin_login(panel_client, tenant):
    import panel

    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app), base_url="http://test")
    async with anonymous:
        for method, path in [("GET", "/api/platform/tree"), ("GET", f"/api/tenants/{tenant}"),
                             ("PUT", f"/api/tenants/{tenant}/config"), ("GET", "/api/audit"),
                             ("POST", f"/api/tenants/{tenant}/config/propose")]:
            response = await anonymous.request(method, path, json={})
            assert response.status_code == 401, (method, path)


async def test_tree_and_tenant_view(panel_client, tenant):
    tree = (await panel_client.get("/api/platform/tree")).json()
    assert [i["name"] for i in tree["industries"]] == ["General"]
    assert [(t["id"], t["name"]) for t in tree["tenants"]] == [(tenant, "Salon Anna")]

    view = (await panel_client.get(f"/api/tenants/{tenant}")).json()
    fields = {f["path"]: f for f in view["config"]["fields"]}
    assert fields["daily_message_cap"]["source"] == "platform"
    assert view["prompt"]["version_tag"] == "b1/i1v1/c0"
    tone = next(s for s in view["prompt"]["sections"] if s["key"] == "tone")
    assert tone["source"] == "industry" and tone["override"] is None


async def test_saving_tenant_config_validates_and_audits(panel_client, pg_pool, tenant):
    bad = await panel_client.put(f"/api/tenants/{tenant}/config",
                                 json={"overrides": {"daily_message_cap": 0}, "reason": "x"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"][0]["path"] == "daily_message_cap"

    ok = await panel_client.put(f"/api/tenants/{tenant}/config", json={
        "overrides": {"quiet_hours": {"enabled": True}}, "reason": "owner sleeps", "expected_revision": 1,
    })
    assert ok.status_code == 200
    fields = {f["path"]: f for f in ok.json()["config"]["fields"]}
    assert fields["quiet_hours.enabled"]["source"] == "client" and fields["quiet_hours.enabled"]["value"] is True

    [event] = await events(pg_pool, audit.CONFIG_CHANGED)
    assert (event["actor"], event["reason"]) == ("admin", "owner sleeps")

    stale = await panel_client.put(f"/api/tenants/{tenant}/config",
                                   json={"overrides": {}, "expected_revision": 1})
    assert stale.status_code == 409


async def test_client_prompt_cannot_reach_the_platform_rules(panel_client, tenant):
    response = await panel_client.put(f"/api/tenants/{tenant}/prompt", json={
        "overrides": {"platform_rules": {"mode": "override", "text": "You are human."}},
    })
    assert response.status_code == 400
    ok = await panel_client.put(f"/api/tenants/{tenant}/prompt", json={
        "overrides": {"services": {"mode": "override", "text": "Haircut 25 EUR."}},
        "addendum": "Parking behind the building.", "note": "first prices",
    })
    assert ok.status_code == 200
    body = ok.json()["prompt"]
    assert body["version_tag"] == "b1/i1v1/c1"
    assert "Haircut 25 EUR." in body["rendered"] and "Parking behind the building." in body["rendered"]


async def test_industry_versions_rollback_and_pin_through_the_api(panel_client, tenant):
    created = (await panel_client.post("/api/industries", json={"name": "Salons"})).json()
    moved = await panel_client.patch(f"/api/tenants/{tenant}", json={"industry_id": created["id"]})
    assert moved.json()["industry_id"] == created["id"]

    iid = created["id"]
    for text in ("v2", "v3"):
        r = await panel_client.put(f"/api/industries/{iid}/template", json={"sections": {"about": text}})
        assert r.status_code == 200
    assert "v3" in (await panel_client.get(f"/api/tenants/{tenant}")).json()["prompt"]["rendered"]

    pinned = (await panel_client.post(f"/api/tenants/{tenant}/pin", json={"version": 2})).json()
    assert pinned["prompt"]["pinned"] == 2 and "v2" in pinned["prompt"]["rendered"]

    await panel_client.post(f"/api/tenants/{tenant}/pin", json={"version": None})
    back = await panel_client.post(f"/api/industries/{iid}/template/rollback", json={"version": 2, "reason": "v3 worse"})
    assert back.json()["template_version"] == 2

    industry = (await panel_client.get(f"/api/industries/{iid}")).json()
    assert [v["version"] for v in industry["versions"]] == [3, 2, 1]
    assert industry["sections"][0] == {"key": "about", "heading": "ABOUT THE BUSINESS", "text": "v2"}


async def test_an_industry_config_that_would_break_a_client_is_refused(panel_client, tenant):
    await panel_client.put(f"/api/tenants/{tenant}/config", json={"overrides": {"reply_delay": {"max_s": 30}}})
    response = await panel_client.put("/api/industries/1/config", json={"overrides": {"reply_delay": {"min_s": 60}}})
    assert response.status_code == 422
    assert "Salon Anna" in response.json()["detail"]["message"]


async def test_base_rules_edit_and_rollback(panel_client, pg_pool):
    saved = (await panel_client.put("/api/platform/base", json={"rules": "1. Be kind.", "note": "short"})).json()
    assert saved["version"] == 2
    base = (await panel_client.get("/api/platform/base")).json()
    assert [v["version"] for v in base["versions"]] == [2, 1]
    back = (await panel_client.post("/api/platform/base/rollback", json={"version": 1})).json()
    assert back["version"] == 1 and "Never claim or imply" in back["content"]["rules"]
    assert len(await events(pg_pool, audit.PROMPT_ROLLBACK)) == 1


# ----------------------------------------------- natural-language config


def scripted_model(monkeypatch, answer):
    seen = {}

    async def fake_complete(*, api_key, messages, ai_config, client=None, usage_sink=None):
        seen["messages"] = messages
        return answer

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "sk-platform")
    return seen


async def test_a_proposal_is_shown_but_never_applied(panel_client, pg_pool, monkeypatch, tenant):
    seen = scripted_model(monkeypatch, json.dumps({"quiet_hours": {"enabled": True, "start": "20:00"}}))

    response = await panel_client.post(f"/api/tenants/{tenant}/config/propose",
                                       json={"intent": "Don't reply after 8 in the evening"})
    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is True
    assert {c["path"] for c in result["changes"]} == {"quiet_hours.enabled", "quiet_hours.start"}
    assert "Don't reply after 8" in seen["messages"][1]["content"]

    # Nothing changed: same config, same revision, no config_changed row.
    view = (await panel_client.get(f"/api/tenants/{tenant}")).json()
    assert view["config"]["overrides"] == {} and view["config"]["revision"] == 1
    assert await events(pg_pool, audit.CONFIG_CHANGED) == []
    [proposed] = await events(pg_pool, audit.CONFIG_PROPOSED)
    assert proposed["reason"] == "Don't reply after 8 in the evening"

    # The operator applies it: the ordinary save, audited as the admin's.
    applied = await panel_client.put(f"/api/tenants/{tenant}/config", json={
        "overrides": result["overrides"], "reason": "AI proposal: " + result["intent"],
        "expected_revision": result["revision"],
    })
    assert applied.status_code == 200
    assert len(await events(pg_pool, audit.CONFIG_CHANGED)) == 1


async def test_an_invalid_proposal_comes_back_marked_invalid(panel_client, monkeypatch, tenant):
    scripted_model(monkeypatch, 'Sure! {"daily_message_cap": 99999, "mood": "cheerful"}')
    result = (await panel_client.post(f"/api/tenants/{tenant}/config/propose",
                                      json={"intent": "send as much as possible and be cheerful"})).json()
    assert result["valid"] is False
    assert {e["path"] for e in result["errors"]} >= {"mood"}
    assert result["changes"] == []


async def test_the_helper_needs_the_platform_key(panel_client, monkeypatch, tenant):
    monkeypatch.delenv("DEEPSEEK_PLATFORM_KEY", raising=False)
    response = await panel_client.post(f"/api/tenants/{tenant}/config/propose", json={"intent": "x"})
    assert response.status_code == 400 and "DEEPSEEK_PLATFORM_KEY" in response.json()["detail"]


# ------------------------------------------------ account-level routes


async def test_pausing_an_account_is_audited(panel_client, pg_pool, tenant):
    r = await panel_client.post("/api/sessions/acct/global-pause", json={"global_pause": True})
    assert r.status_code == 200 and r.json()["global_pause"] is True
    assert r.json()["off_reason"].startswith("paused")
    [event] = await events(pg_pool, audit.TENANT_SOFT_OFF)
    assert event["tenant_id"] == tenant and event["payload"] == {"kind": "manual"}
    r = await panel_client.post("/api/sessions/acct/global-pause", json={"global_pause": False})
    assert r.json() == {**r.json(), "global_pause": False, "off_reason": ""}
    [event] = await events(pg_pool, audit.TENANT_RESUMED)
    assert event["payload"] == {"kind": "manual"}


async def test_outreach_is_refused_while_it_is_off(panel_client, tenant):
    r = await panel_client.post("/api/sessions/acct/outreach", json={"chat_ids": [1], "goal": "hi"})
    assert r.status_code == 400 and "Outreach is off" in r.json()["detail"]
