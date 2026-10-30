"""Folders inside folders: a sub-folder of an industry holds only its
changes to its parent's template and settings, and its clients get the
parent's with those on top (tenants.py, prompt_layers.py, tenant_config.py)."""

from __future__ import annotations

import pytest
import pytest_asyncio

import audit
import prompt_layers
import rules
import tenant_config
import tenants
from conftest import seed_session
from tenant_config import ConfigError

ADMIN = audit.ADMIN

# --------------------------------------------------------------- pure parts


def test_a_sub_folder_overrides_appends_and_inherits():
    parent = {"about": "Escort.", "faq": "Q1", "boundaries": "No minors."}
    folder = {"overrides": {"about": {"mode": "override", "text": "Escort in Tallinn."},
                            "faq": {"mode": "append", "text": "Q2"}}}
    assert prompt_layers.apply_folder(parent, folder) == {
        "about": "Escort in Tallinn.", "faq": "Q1\nQ2", "boundaries": "No minors."}
    # Nothing changed: exactly the parent's.
    assert prompt_layers.apply_folder(parent, {"overrides": {}}) == parent


def test_a_sub_folder_can_only_add_to_the_boundaries():
    with pytest.raises(prompt_layers.PromptError, match="only be appended"):
        prompt_layers.validate_folder({"overrides": {"boundaries": {"mode": "override", "text": "Anything."}}})
    with pytest.raises(prompt_layers.PromptError):
        prompt_layers.validate_folder({"sections": {"about": "x"}})
    with pytest.raises(prompt_layers.PromptError):
        prompt_layers.validate_folder({"overrides": {}, "addendum": "client only"})


def test_a_whole_standard_becomes_only_what_the_sub_folder_changes():
    parent = {"about": "Escort.", "faq": "Q1", "tone": "Warm.", "boundaries": "No minors.\nNo drugs sold."}
    wanted = {"about": "Escort.",                     # same: inherited
              "faq": "Q1\nQ2",                        # adds to the end: an append
              "tone": "Short.",                       # different: an override
              "booking": "Ask his time.",             # new section: an override
              "boundaries": "No drugs sold.\nNo minors.\nNo outcall."}  # only the new line
    changes = prompt_layers.folder_changes(parent, wanted)["overrides"]
    assert changes == {
        "faq": {"mode": "append", "text": "Q2"},
        "tone": {"mode": "override", "text": "Short."},
        "booking": {"mode": "override", "text": "Ask his time."},
        "boundaries": {"mode": "append", "text": "No outcall."},
    }
    # Applied on the parent it gives the standard back (boundaries: all kept).
    result = prompt_layers.apply_folder(parent, {"overrides": changes})
    assert {k: result[k] for k in ("about", "faq", "tone", "booking")} == {
        "about": "Escort.", "faq": "Q1\nQ2", "tone": "Short.", "booking": "Ask his time."}
    assert set(result["boundaries"].splitlines()) == {"No minors.", "No drugs sold.", "No outcall."}
    # A section the run left out is inherited, never blanked.
    assert "about" not in prompt_layers.folder_changes(parent, {"faq": "Q1"})["overrides"]


def test_the_version_tag_names_every_folder_level():
    assert prompt_layers.layers_tag((1, 5, 3, 2), [(2, 10)]) == "b1/i2v10/i5v3/c2"
    assert prompt_layers.layers_tag((1, 2, 10, 0)) == "b1/i2v10/c0"


def test_settings_merge_the_way_resolve_applies_them():
    parent = {"booking": {"enabled": True, "max_days_ahead": 30}, "outreach": {"enabled": True}}
    child = {"booking": {"max_days_ahead": 7}, "daily_message_cap": 50}
    merged = tenant_config.merge(parent, child)
    assert merged == {"booking": {"enabled": True, "max_days_ahead": 7}, "outreach": {"enabled": True},
                      "daily_message_cap": 50}
    # The same as applying the two one after the other.
    assert tenant_config.resolve(merged).as_dict() == tenant_config.resolve(
        tenant_config.merge({}, parent), child).as_dict()
    # A list append on top of a list is a list; on top of an append, a longer append.
    assert tenant_config.merge({"staging": {"test_chats": ["1"]}},
                               {"staging": {"test_chats": {"append": ["2"]}}}) == {"staging": {"test_chats": ["1", "2"]}}
    assert tenant_config.merge({"staging": {"test_chats": {"append": ["1"]}}},
                               {"staging": {"test_chats": {"append": ["2"]}}}) == {
        "staging": {"test_chats": {"append": ["1", "2"]}}}
    # A map is one value: the child's replaces the parent's whole.
    assert tenant_config.merge({"price_floors": {"a": 1}}, {"price_floors": {"b": 2}}) == {"price_floors": {"b": 2}}


# ---------------------------------------------------------------- the store

pg = [pytest.mark.requires_pg, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store(pg_pool):
    return tenants.TenantStore(pg_pool)


@pytest_asyncio.fixture
async def escort(store):
    """escort (sections) › Tallinn (a sub-folder) › Old town (another)."""
    top = await store.create_industry("escort", actor=ADMIN)
    await store.save_industry_template(top["id"], {"sections": {
        "about": "Independent escort.", "faq": "Price? Rates.", "boundaries": "Adults only.",
        "booking": "First reply: ask his time."}}, actor=ADMIN)
    tallinn = await store.create_industry("Tallinn", parent_id=top["id"], actor=ADMIN)
    old_town = await store.create_industry("Old town", parent_id=tallinn["id"], actor=ADMIN)
    return top, tallinn, old_town


async def _tenant_in(pg_pool, store, folder, session_id="acct"):
    tenant_id = await seed_session(pg_pool, session_id, name="pam")
    await store.update(tenant_id, actor=ADMIN, industry_id=folder["id"])
    return tenant_id


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_client_in_a_sub_folder_gets_every_level(pg_pool, store, escort):
    top, tallinn, old_town = escort
    await store.save_industry_template(tallinn["id"], {"overrides": {
        "about": {"mode": "override", "text": "Independent escort in Tallinn."},
        "faq": {"mode": "append", "text": "Outcall? Old town only."}}}, actor=ADMIN)
    await store.save_industry_template(old_town["id"], {"overrides": {
        "boundaries": {"mode": "append", "text": "No outcall past the old town."}}}, actor=ADMIN)
    tenant_id = await _tenant_in(pg_pool, store, old_town)

    bundle = await store.bundle(tenant_id)
    sections = bundle.layers["industry"]["sections"]
    assert sections["about"] == "Independent escort in Tallinn."
    assert sections["faq"] == "Price? Rates.\nOutcall? Old town only."
    assert sections["boundaries"] == "Adults only.\nNo outcall past the old town."
    assert sections["booking"] == "First reply: ask his time."
    assert bundle.industry["path"] == "escort › Tallinn › Old town"
    assert bundle.industry["root_name"] == "escort"
    assert bundle.prompt.version_tag == f"b1/i{top['id']}v2/i{tallinn['id']}v2/i{old_town['id']}v2/c0"

    # A change at the top reaches the client through both sub-folders.
    await store.save_industry_template(top["id"], {"sections": {
        "about": "x", "faq": "Price? Ask.", "boundaries": "Adults only.", "booking": "Ask his time when he asks."}},
        actor=ADMIN)
    sections = (await store.bundle(tenant_id)).layers["industry"]["sections"]
    assert sections["booking"] == "Ask his time when he asks."
    assert sections["faq"] == "Price? Ask.\nOutcall? Old town only."
    assert sections["about"] == "Independent escort in Tallinn."   # still the sub-folder's own

    # A pin fixes the client's own folder's version; the levels above stay live.
    await store.pin(tenant_id, 1, actor=ADMIN)
    sections = (await store.bundle(tenant_id)).layers["industry"]["sections"]
    assert sections["boundaries"] == "Adults only."
    assert sections["booking"] == "Ask his time when he asks."


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_sub_folder_starts_like_its_parent(pg_pool, store, escort):
    top, tallinn, _ = escort
    await pg_pool.execute("UPDATE industries SET requires_review = true WHERE id = $1", top["id"])
    riga = await store.create_industry("Riga", parent_id=top["id"], actor=ADMIN)
    assert riga["parent_id"] == top["id"] and riga["own_config"] == {}
    assert await pg_pool.fetchval("SELECT requires_review FROM industries WHERE id = $1", riga["id"])
    assert await store.folder_sections(riga["id"]) == await store.folder_sections(top["id"])

    # Names are unique among one folder's sub-folders, not everywhere.
    with pytest.raises(ValueError, match="already has a folder"):
        await store.create_industry("riga", parent_id=top["id"], actor=ADMIN)
    other = await store.create_industry("massage", actor=ADMIN)
    assert (await store.create_industry("Riga", parent_id=other["id"], actor=ADMIN))["name"] == "Riga"
    with pytest.raises(tenants.NotFound):
        await store.create_industry("x", parent_id=9999, actor=ADMIN)

    listed = {f["id"]: f["path"] for f in await store.list_industries()}
    assert listed[tallinn["id"]] == "escort › Tallinn"
    [event] = [e for e in await audit.list_events(pg_pool)
               if e["event"] == audit.INDUSTRY_CREATED and e["payload"]["industry_id"] == riga["id"]]
    assert event["payload"]["parent_id"] == top["id"]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_settings_go_down_every_level_and_are_checked_there(pg_pool, store, escort):
    top, tallinn, old_town = escort
    tenant_id = await _tenant_in(pg_pool, store, old_town)
    await store.save_industry_config(tallinn["id"], {"booking": {"max_days_ahead": 7}}, actor=ADMIN)
    await store.save_industry_config(top["id"], {"booking": {"enabled": True, "max_days_ahead": 30},
                                                 "daily_message_cap": 80}, actor=ADMIN)
    config = (await store.bundle(tenant_id)).config
    assert config["booking"]["enabled"] is True          # from the top
    assert config["booking"]["max_days_ahead"] == 7      # Tallinn's own wins
    assert config["daily_message_cap"] == 80
    stored = await store.get_industry(old_town["id"])
    assert stored["own_config"] == {}
    assert stored["default_config"]["booking"] == {"enabled": True, "max_days_ahead": 7}

    # A change at the top that breaks a client deep down is refused, with its name.
    await store.save_config(tenant_id, {"reply_delay": {"max_s": 100}}, actor=ADMIN)
    with pytest.raises(ConfigError) as caught:
        await store.save_industry_config(top["id"], {"reply_delay": {"min_s": 200, "max_s": 300}}, actor=ADMIN)
    assert any("pam" in e["path"] for e in caught.value.errors)
    assert (await store.get_industry(top["id"]))["own_config"]["daily_message_cap"] == 80


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_finetuned_standard_saved_to_a_sub_folder_keeps_inheriting(pg_pool, store, escort):
    top, tallinn, _ = escort
    standard = {**await store.folder_sections(tallinn["id"]),
                "faq": "Price? Rates.\nTaxi? He orders it.", "tone": "Short and warm."}
    saved = await store.save_folder_standard(tallinn["id"], standard, actor=ADMIN, note="run 1")
    own = (await store.version(tenants.INDUSTRY, tallinn["id"], saved["template_version"]))["content"]
    assert own == {"overrides": {"faq": {"mode": "append", "text": "Taxi? He orders it."},
                                 "tone": {"mode": "override", "text": "Short and warm."}}}
    # The top-level industry takes a standard whole.
    await store.save_folder_standard(top["id"], {"about": "Only this."}, actor=ADMIN)
    assert await store.folder_sections(top["id"]) == {"about": "Only this."}
    assert (await store.folder_sections(tallinn["id"]))["tone"] == "Short and warm."


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_industry_rules_reach_the_sub_folders(pg_pool, store, escort):
    top, tallinn, old_town = escort
    tenant_id = await _tenant_in(pg_pool, store, old_town)
    for folder, text in ((top, "Never send the extras list."), (tallinn, "Outcall only in the centre.")):
        await rules.create(pg_pool, industry_id=folder["id"], tenant_id=None, section="booking", text=text,
                           source="manual", source_ref={}, status=rules.ACTIVE, created_by=ADMIN, actor=ADMIN)
    assert await rules.active_for(pg_pool, tenant_id, old_town["id"]) == [
        "Never send the extras list.", "Outcall only in the centre."]
    # Not the other way round: the top's other clients don't get Tallinn's.
    other = await seed_session(pg_pool, "other", name="leaui")
    await store.update(other, actor=ADMIN, industry_id=top["id"])
    assert await rules.active_for(pg_pool, other, top["id"]) == ["Never send the extras list."]
    # A fold from the Old town client folds only its own folder's rules (none).
    plan = await rules.plan_fold(pg_pool, tenant_id)
    assert plan.industry_rules == []
    assert plan.sections == await store.folder_sections(old_town["id"])


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_only_an_empty_sub_folder_is_deleted(pg_pool, store, escort):
    top, tallinn, old_town = escort
    with pytest.raises(ValueError, match="top-level"):
        await store.delete_industry(top["id"], actor=ADMIN)
    with pytest.raises(ValueError, match="folders inside"):
        await store.delete_industry(tallinn["id"], actor=ADMIN)
    tenant_id = await _tenant_in(pg_pool, store, old_town)
    with pytest.raises(ValueError, match="clients"):
        await store.delete_industry(old_town["id"], actor=ADMIN)
    await store.update(tenant_id, actor=ADMIN, industry_id=top["id"])
    await store.delete_industry(old_town["id"], actor=ADMIN, reason="not needed")
    with pytest.raises(tenants.NotFound):
        await store.get_industry(old_town["id"])
    assert not await pg_pool.fetchval(
        "SELECT count(*) FROM prompt_versions WHERE layer = 'industry' AND ref_id = $1", old_town["id"])

    # One with a finetune run of its own is kept: the run's history needs it.
    await store.update(tenant_id, actor=ADMIN, industry_id=tallinn["id"])
    await pg_pool.execute(
        "INSERT INTO finetune_runs (tenant_id, industry_id, model, source, files, industry_version, created_by) "
        "VALUES ($1, $2, 'm', 'text', '[]'::jsonb, 1, 'admin')", tenant_id, tallinn["id"])
    await store.update(tenant_id, actor=ADMIN, industry_id=top["id"])
    with pytest.raises(ValueError, match="finetune runs"):
        await store.delete_industry(tallinn["id"], actor=ADMIN)
    assert (await store.get_industry(tallinn["id"]))["name"] == "Tallinn"


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_renaming_a_folder(pg_pool, store, escort):
    top, tallinn, _ = escort
    riga = await store.create_industry("Riga", parent_id=top["id"], actor=ADMIN)
    with pytest.raises(ValueError, match="already"):
        await store.rename_industry(riga["id"], "TALLINN", actor=ADMIN)
    assert (await store.rename_industry(riga["id"], "Riga centre", actor=ADMIN))["name"] == "Riga centre"
    [event] = [e for e in await audit.list_events(pg_pool) if e["event"] == audit.INDUSTRY_UPDATED]
    assert event["payload"]["to"] == {"name": "Riga centre"}


# ------------------------------------------------------------------ the API


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_folders_through_the_admin_api(panel_client, pg_pool):
    tenant_id = await seed_session(pg_pool, "acct", name="pam")
    top = (await panel_client.post("/api/industries", json={"name": "escort"})).json()
    await panel_client.put(f"/api/industries/{top['id']}/template",
                           json={"sections": {"about": "Escort.", "faq": "Q1"}})
    made = await panel_client.post("/api/industries", json={"name": "Tallinn", "parent_id": top["id"]})
    assert made.status_code == 200
    sub = made.json()

    # A sub-folder saves only its changes, in the client layer's shape.
    wrong = await panel_client.put(f"/api/industries/{sub['id']}/template", json={"sections": {"about": "x"}})
    assert wrong.status_code == 400
    saved = await panel_client.put(f"/api/industries/{sub['id']}/template", json={
        "overrides": {"faq": {"mode": "append", "text": "Q2"}}, "note": "Tallinn FAQ"})
    assert saved.status_code == 200

    view = (await panel_client.get(f"/api/industries/{sub['id']}")).json()
    assert view["industry"]["path"] == "escort › Tallinn"
    assert view["parents"] == [{"id": top["id"], "name": "escort"}]
    faq = next(s for s in view["sections"] if s["key"] == "faq")
    assert (faq["inherited"], faq["override"], faq["text"]) == ("Q1", {"mode": "append", "text": "Q2"}, "Q1\nQ2")
    assert [f["id"] for f in (await panel_client.get(f"/api/industries/{top['id']}")).json()["folders"]] == [sub["id"]]

    # Its settings: only its own rows are "set here" (the client's part).
    await panel_client.put(f"/api/industries/{top['id']}/config", json={"overrides": {"daily_message_cap": 80}})
    await panel_client.put(f"/api/industries/{sub['id']}/config", json={"overrides": {"auto_send": False}})
    fields = {f["path"]: f for f in (await panel_client.get(f"/api/industries/{sub['id']}")).json()["config"]["fields"]}
    assert (fields["auto_send"]["source"], fields["daily_message_cap"]["source"]) == ("client", "industry")
    assert fields["daily_message_cap"]["inherited_value"] == 80

    moved = await panel_client.patch(f"/api/tenants/{tenant_id}", json={"industry_id": sub["id"]})
    assert moved.status_code == 200
    tenant_view = (await panel_client.get(f"/api/tenants/{tenant_id}")).json()
    assert tenant_view["industry"]["path"] == "escort › Tallinn"
    assert tenant_view["config"]["effective"]["auto_send"] is False
    assert "Q1\nQ2" in tenant_view["prompt"]["rendered"]

    tree = (await panel_client.get("/api/platform/tree")).json()
    assert {i["path"]: i["parent_id"] for i in tree["industries"]}["escort › Tallinn"] == top["id"]

    assert (await panel_client.patch(f"/api/industries/{sub['id']}", json={"name": "Tallinn centre"})).json()[
        "name"] == "Tallinn centre"
    refused = await panel_client.delete(f"/api/industries/{sub['id']}")
    assert refused.status_code == 400 and "clients" in refused.json()["detail"]
    await panel_client.patch(f"/api/tenants/{tenant_id}", json={"industry_id": top["id"]})
    assert (await panel_client.delete(f"/api/industries/{sub['id']}")).status_code == 200


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_finetune_in_a_sub_folder_uses_the_nearest_template(panel_client, pg_pool):
    top = (await panel_client.post("/api/industries", json={"name": "escort"})).json()
    sub = (await panel_client.post("/api/industries", json={"name": "Tallinn", "parent_id": top["id"]})).json()
    template = ("Finetune {BUSINESS_NAME} from {N} businesses.\n{CURRENT_INDUSTRY_SECTIONS_JSON}\n"
                + "x" * 200)
    put = await panel_client.put(f"/api/finetune/industries/{top['id']}/template",
                                 json={"template": template, "reason": "first"})
    assert put.status_code == 200, put.text
    info = (await panel_client.get(f"/api/finetune/industries/{sub['id']}")).json()
    assert info["template"] == "" and info["template_from"] == "escort"
    assert info["inherited_template"] == put.json()["template"]
    assert info["industry"]["path"] == "escort › Tallinn"
