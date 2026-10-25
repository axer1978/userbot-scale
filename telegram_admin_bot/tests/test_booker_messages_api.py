"""The page where an account sets how the bot's messages to the booker look
(booker_settings), from the admin panel and from the owner's own pages: what
it shows, the preview, the save, and who may use it."""

from __future__ import annotations

import pytest
import pytest_asyncio

import audit
from conftest import seed_session
from test_owner_api import fresh_owner_state, owner_client, ready  # noqa: F401  (fixture)

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def two(pg_pool):
    return {"a": await seed_session(pg_pool, "acct_a", name="Salon Anna"),
            "b": await seed_session(pg_pool, "acct_b", name="Barber Bob")}


async def config_events(pool):
    return [e for e in await audit.list_events(pool) if e["event"] == audit.CONFIG_CHANGED]


async def test_the_page_shows_every_message_with_its_blanks_defaults_and_preview(panel_client, two):
    view = (await panel_client.get(f"/api/tenants/{two['a']}/booker-messages")).json()
    assert view["language"] == "en" and view["messages"] == {}
    assert [lang["key"] for lang in view["languages"]] == ["en", "ru", "lv", "es"]
    keys = [t["key"] for t in view["templates"]]
    assert keys[0] == "request" and len(keys) == 10
    request = view["templates"][0]
    assert request["kept"] == ["number", "commands"]
    assert {"number", "customer", "when", "commands"} <= {b["name"] for b in request["blanks"]}
    assert set(request["defaults"]) == {"en", "ru", "lv", "es"}
    assert set(view["preview"]) == set(keys) and view["problems"] == {}
    assert view["preview"]["request"].startswith("📅 Booking request #3")
    assert "Salon Anna" in view["preview"]["request"]  # her name: the account's, none set


async def test_the_preview_saves_nothing_and_points_at_bad_text(panel_client, two):
    r = await panel_client.post(f"/api/tenants/{two['a']}/booker-messages/preview", json={
        "language": "ru", "messages": {"confirmed": "Готово: #{number} на {when}", "declined": "{nope}"}})
    assert r.status_code == 200
    body = r.json()
    assert body["preview"]["confirmed"].startswith("Готово: #3 на ")
    assert "{nope}" in body["problems"]["declined"][0]
    assert body["preview"]["declined"].startswith("#3 отклонена")  # shown as it would go: the default
    view = (await panel_client.get(f"/api/tenants/{two['a']}/booker-messages")).json()
    assert view["language"] == "en" and view["messages"] == {}


async def test_saving_changes_only_these_settings_validates_and_audits(panel_client, pg_pool, two):
    a = two["a"]
    await panel_client.put(f"/api/tenants/{a}/config", json={"overrides": {"quiet_hours": {"enabled": True}}})
    revision = (await panel_client.get(f"/api/tenants/{a}/booker-messages")).json()["revision"]

    bad = await panel_client.put(f"/api/tenants/{a}/booker-messages", json={
        "language": "lv", "messages": {"confirmed": "{when:>9}"}, "expected_revision": revision})
    assert bad.status_code == 422
    assert "confirmed" in bad.json()["detail"]["errors"][0]["message"]
    bad = await panel_client.put(f"/api/tenants/{a}/booker-messages", json={"language": "de"})
    assert bad.status_code == 422

    ok = await panel_client.put(f"/api/tenants/{a}/booker-messages", json={
        "language": "lv", "messages": {"confirmed": "Apstiprināts #{number}: {when}", "declined": "  "},
        "expected_revision": revision, "reason": "her own words"})
    assert ok.status_code == 200, ok.text
    view = ok.json()
    assert view["language"] == "lv" and view["messages"] == {"confirmed": "Apstiprināts #{number}: {when}"}
    assert view["preview"]["confirmed"].startswith("Apstiprināts #3: ")

    tenant = (await panel_client.get(f"/api/tenants/{a}")).json()
    overrides = tenant["config"]["overrides"]
    assert overrides["quiet_hours"] == {"enabled": True}  # the rest of its own settings stay
    assert overrides["booking"]["booker_language"] == "lv"
    event = (await config_events(pg_pool))[0]  # newest first
    assert (event["actor"], event["reason"]) == ("admin", "her own words")

    stale = await panel_client.put(f"/api/tenants/{a}/booker-messages",
                                   json={"language": "en", "expected_revision": revision})
    assert stale.status_code == 409

    # Back to the defaults: the texts go from its settings.
    cleared = await panel_client.put(f"/api/tenants/{a}/booker-messages", json={"language": "en", "messages": {}})
    assert cleared.json()["messages"] == {}
    tenant = (await panel_client.get(f"/api/tenants/{a}")).json()
    assert "booker_messages" not in tenant["config"]["overrides"]["booking"]


async def test_an_owner_edits_only_their_own_business(panel_client, pg_pool, two):
    a, b = two["a"], two["b"]
    async with owner_client() as c:
        await ready(panel_client, c, "anna", [a])
        view = await c.get(f"/api/owner/booker-messages?tenant_id={a}")
        assert view.status_code == 200 and view.json()["language"] == "en"
        assert (await c.get(f"/api/owner/booker-messages?tenant_id={b}")).status_code == 404
        assert (await c.post(f"/api/owner/booker-messages/preview?tenant_id={b}", json={})).status_code == 404
        r = await c.put(f"/api/owner/booker-messages?tenant_id={b}", json={"language": "ru"})
        assert r.status_code == 404
        assert (await panel_client.get(f"/api/tenants/{b}/booker-messages")).json()["language"] == "en"

        preview = await c.post(f"/api/owner/booker-messages/preview?tenant_id={a}",
                               json={"language": "es", "messages": {}})
        assert preview.json()["preview"]["confirmed"].startswith("#3 confirmada")

        bad = await c.put(f"/api/owner/booker-messages?tenant_id={a}",
                          json={"language": "es", "messages": {"request": "{"}})
        assert bad.status_code == 422
        ok = await c.put(f"/api/owner/booker-messages?tenant_id={a}",
                         json={"language": "ru", "messages": {"cancelled": "#{number} отменено, {customer}"}})
        assert ok.status_code == 200, ok.text
        assert ok.json()["language"] == "ru"
    event = (await config_events(pg_pool))[0]  # newest first
    assert event["actor"].startswith("owner") and event["tenant_id"] == a
    assert (await panel_client.get(f"/api/tenants/{a}/booker-messages")).json()["messages"] == {
        "cancelled": "#{number} отменено, {customer}"}


async def test_the_owner_routes_need_an_owner_login(two):
    async with owner_client() as anonymous:
        assert (await anonymous.get(f"/api/owner/booker-messages?tenant_id={two['a']}")).status_code == 401
        r = await anonymous.put(f"/api/owner/booker-messages?tenant_id={two['a']}", json={"language": "ru"})
        assert r.status_code == 401
