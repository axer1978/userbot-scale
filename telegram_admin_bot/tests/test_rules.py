"""Operator corrections as rules (rules.py, rules_api.py): the store, its
scopes and cap, the rules in every reply's prompt, suggestions from edited
drafts and flagged-chat corrections (with a fake DeepSeek), the fold into
the prompt as a finetune run and what applying it does, and the weekly
round. Every chat here is made up."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

import audit
import finetune_api
import platform_api
import prompt_layers
import rules
import tenants
from conftest import seed_session

STANDARD = {"sections": {
    "about": "Independent companions.",
    "booking": "Ask his time in one short line. Only the APPOINTMENTS notes confirm a time.",
    "tone": "Short, warm messages.",
    "boundaries": "Adults only.\nNever type an address or a door code: the system sends [address].",
}}
CLIENT = {"overrides": {"services": {"mode": "override", "text": "[block]\n30 min 100\n1 h 150\n[/block]"}},
          "addendum": ""}


async def make_rule(pool, tenant_id=None, *, industry_id=1, text="Ask for his time before sending the rates.",
                    section="booking", status=rules.ACTIVE, source=rules.MANUAL, ref=None):
    return await rules.create(pool, industry_id=industry_id, tenant_id=tenant_id, section=section, text=text,
                              source=source, source_ref=ref or {}, status=status, created_by="admin",
                              actor=audit.ADMIN)


@pytest_asyncio.fixture
async def world(pg_pool):
    """Industry 1 with a standard (Pam, Leaui) and a second industry with
    one account of its own (Salon); Pam has a business layer."""
    store = tenants.TenantStore(pg_pool)
    await store.save_industry_template(1, STANDARD, actor="admin", note="standard")
    hair = (await store.create_industry("Hair", actor="admin"))["id"]
    pam = await seed_session(pg_pool, "pam", name="Pam")
    leaui = await seed_session(pg_pool, "leaui", name="Leaui")
    salon = await seed_session(pg_pool, "salon", name="Salon", industry_id=hair)
    await store.save_client_prompt(pam, CLIENT, actor="admin", note="rates")
    return {"pool": pg_pool, "store": store, "pam": pam, "leaui": leaui, "salon": salon, "hair": hair}


@pytest.fixture
def reloads(monkeypatch):
    """The accounts told to reload, in calls."""
    calls: list[list[str]] = []

    async def fake_reload(session_ids, bundle=None):
        calls.append(sorted(s for s in session_ids if s))

    monkeypatch.setattr(platform_api, "reload_accounts", fake_reload)
    return calls


@pytest.fixture
def deepseek(monkeypatch):
    """rules.complete, answering with seen["answer"] (a string, or a
    function of the prompt) and keeping every prompt it was sent."""
    seen: dict = {"prompts": [], "answer": '{"rules": []}'}

    async def fake_complete(prompt, **kwargs):
        seen["prompts"].append(prompt)
        seen["kwargs"] = kwargs
        answer = seen["answer"]
        return answer(prompt) if callable(answer) else answer

    monkeypatch.setattr(rules, "complete", fake_complete)
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    return seen


# --------------------------------------------------------------- the store


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_scopes_an_industry_rule_reaches_every_account_of_it(world):
    pool, pam, leaui, salon = world["pool"], world["pam"], world["leaui"], world["salon"]
    own = await make_rule(pool, pam, text="Pam answers in English only.", section="tone")
    shared = await make_rule(pool, text="Ask for his time before sending the rates.")
    await make_rule(pool, text="Never send the rates twice in one chat.", status=rules.PROPOSED)
    other = await make_rule(pool, salon, industry_id=world["hair"], text="Mention parking in the yard.")

    assert (own["scope"], shared["scope"]) == ("account", "industry")
    # The industry's first, then the account's, whatever order they came in.
    assert await rules.active_for(pool, pam, 1) == [shared["text"], own["text"]]
    assert await rules.active_for(pool, leaui, 1) == [shared["text"]]
    assert await rules.active_for(pool, salon, world["hair"]) == [other["text"]]
    assert [r["id"] for r in await rules.list_for(pool, leaui, 1)] == [shared["id"], shared["id"] + 1]
    assert await rules.counts(pool, pam, 1) == {"industry": {"active": 1, "proposed": 1},
                                                 "account": {"active": 1, "proposed": 0}}


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_at_most_25_active_rules_per_scope(world):
    pool, pam = world["pool"], world["pam"]
    made = [await make_rule(pool, text=f"Industry rule number {chr(65 + i)} here.") for i in range(rules.CAP)]
    with pytest.raises(rules.CapReached, match="Fold them into the prompt first"):
        await make_rule(pool, text="One rule too many for the industry.")
    waiting = await make_rule(pool, text="One rule too many for the industry.", status=rules.PROPOSED)
    with pytest.raises(rules.CapReached):
        await rules.update(pool, waiting["id"], actor="admin", status=rules.ACTIVE)
    # The account's own rules are a scope of their own.
    own = await make_rule(pool, pam, text="Pam's own rule, active.")
    with pytest.raises(rules.CapReached, match="industry"):
        await rules.update(pool, own["id"], actor="admin", scope=rules.INDUSTRY)
    # Retire one, and there is room again.
    await rules.update(pool, made[0]["id"], actor="admin", status=rules.RETIRED)
    _, accepted = await rules.update(pool, waiting["id"], actor="admin", status=rules.ACTIVE)
    assert accepted["status"] == rules.ACTIVE
    assert len(await rules.active_for(pool, pam, 1)) == rules.CAP + 1


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_changing_a_rule(world):
    pool, pam, salon = world["pool"], world["pam"], world["salon"]
    rule = await make_rule(pool, text="Ask for his time first.", ref={"tenant_id": pam})
    with pytest.raises(rules.RuleError, match="5 to 400"):
        await rules.update(pool, rule["id"], actor="admin", text="ok")
    with pytest.raises(rules.RuleError, match="Unknown section"):
        await rules.update(pool, rule["id"], actor="admin", section="platform_rules")
    with pytest.raises(rules.RuleError, match="merged"):
        await rules.update(pool, rule["id"], actor="admin", status=rules.MERGED)
    with pytest.raises(rules.RuleError, match="not in this rule's industry"):
        await rules.update(pool, rule["id"], actor="admin", scope=rules.ACCOUNT, tenant_id=salon)
    before, after = await rules.update(pool, rule["id"], actor="admin", scope=rules.ACCOUNT,
                                       text="  Ask for  his time first, kindly. ")
    assert (after["tenant_id"], after["text"]) == (pam, "Ask for his time first, kindly.")  # the account it came from
    assert rules.changes_prompt(before, after)
    _, same = await rules.update(pool, rule["id"], actor="admin", section="faq")
    assert not rules.changes_prompt(after, same)  # the section is for the fold only
    await pool.execute("UPDATE prompt_rules SET status = 'merged', merged_into = 'b1/i1v3/c2' WHERE id = $1",
                       rule["id"])
    with pytest.raises(rules.RuleConflict, match="b1/i1v3/c2"):
        await rules.update(pool, rule["id"], actor="admin", status=rules.RETIRED)
    events = [e["event"] for e in await audit.list_events(pool)]
    assert events.count(rules.RULE_CHANGED) == 2 and rules.RULE_CREATED in events


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_bundle_renders_the_active_rules(world):
    pool, store, pam, leaui = world["pool"], world["store"], world["pam"], world["leaui"]
    plain = await store.bundle(pam)
    assert plain.layers["rules"] == [] and prompt_layers.RULES_HEADER not in plain.prompt.text
    shared = await make_rule(pool, text="Ask for his time before sending the rates.")
    own = await make_rule(pool, pam, text="Pam answers in English only.", section="tone")
    await make_rule(pool, text="A proposed rule is not in any prompt yet.", status=rules.PROPOSED)

    bundle = await store.bundle(pam)
    assert bundle.layers["rules"] == [shared["text"], own["text"]]
    assert f"{prompt_layers.RULES_HEADER}\n- {shared['text']}\n- {own['text']}" in bundle.prompt.text
    assert bundle.prompt.version_tag.startswith(plain.prompt.version_tag + "/r2-")
    assert (await store.bundle(leaui)).layers["rules"] == [shared["text"]]
    await rules.update(pool, own["id"], actor="admin", status=rules.RETIRED)
    assert (await store.bundle(pam)).layers["rules"] == [shared["text"]]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_replay_tries_the_live_rules_or_the_candidates(world, tmp_path):
    import replay

    pool, pam = world["pool"], world["pam"]
    shared = await make_rule(pool, text="Ask for his time before sending the photos.")
    bundle, live = await replay.render(pool, pam, None)
    assert live == bundle.prompt and f"- {shared['text']}" in live.text

    candidate = tmp_path / "candidate.json"
    tried = {}
    for name, content in (("folded", {"rules": []}), ("other", {"rules": ["Try this rule instead."]}),
                          ("client only", {"client": CLIENT})):
        candidate.write_text(json.dumps(content), encoding="utf-8")
        tried[name] = (await replay.render(pool, pam, candidate))[1].text
    assert prompt_layers.RULES_HEADER not in tried["folded"]
    assert "- Try this rule instead." in tried["other"] and shared["text"] not in tried["other"]
    assert f"- {shared['text']}" in tried["client only"]  # no "rules" in it: the live ones


# ------------------------------------------------------------- suggestions


async def seed_conversation(pool, session, chat_id, name="Toms Berzins", username="toms_lv"):
    await pool.execute("INSERT INTO conversations (session_id, chat_id, display_name, username) "
                       "VALUES ($1, $2, $3, $4)", session, chat_id, name, username)


async def seed_message(pool, session, chat_id, direction, status, text, *, model=None, draft_text=None):
    return await pool.fetchval(
        "INSERT INTO messages (session_id, chat_id, direction, status, text, llm_model, draft_text) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        session, chat_id, direction, status, text, model, draft_text)


async def seed_corrections(world):
    """Pam's chat with Toms: an edited draft (sent as two messages), and a
    flagged chat with a correction on one of the bot's replies; the
    business's address and door code in her config."""
    pool, store, pam = world["pool"], world["store"], world["pam"]
    booking = {"address": "Ganu iela 4, Riga", "arrival_instructions": "door code 1234"}
    await store.save_config(pam, {"booking": booking}, actor="admin")
    await seed_conversation(pool, "pam", 501)
    await seed_message(pool, "pam", 501, "in", "received", "Hi, Toms here, free tonight? +37126942162")
    edited = await seed_message(pool, "pam", 501, "out", "sent", "hey love 😘 what time you come?",
                                model="deepseek-chat",
                                draft_text="Hello Toms! Yes I am free, come to Ganu iela 4, Riga, door code 1234.")
    await seed_message(pool, "pam", 501, "out", "sent", "text me 15 min before", model="deepseek-chat")
    await seed_message(pool, "pam", 501, "in", "received", "send more photos @toms_lv")
    corrected = await seed_message(pool, "pam", 501, "out", "sent", "okay Toms, here", model="deepseek-chat")
    example = await pool.fetchval(
        "INSERT INTO finetune_examples (tenant_id, chat_id, upto_message_id, verdict, corrections, created_by) "
        "VALUES ($1, 501, $2, 'bad', $3::jsonb, 'admin') RETURNING id",
        pam, corrected, json.dumps({str(corrected): "Toms collects photos: ask for his time, call +37126942162"}))
    return {"edited": edited, "corrected": corrected, "example": example}


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_suggestions_from_edits_and_corrections_see_no_personal_data(world, deepseek):
    pool, pam = world["pool"], world["pam"]
    ids = await seed_corrections(world)
    assert await rules.waiting(pool, pam) == {"draft_edits": 1, "corrections": 1}
    deepseek["answer"] = json.dumps({"rules": [
        {"item": 1, "rule": "When he keeps asking for photos without a time, ask for his time first.",
         "section": "booking", "scope": "industry"},
        {"item": 2, "rule": "Never write the address yourself: the system sends [address].",
         "section": "boundaries", "scope": "account"},
    ]})

    result = await rules.suggest(pool, pam, api_key="pk", actor=audit.ADMIN)

    [prompt] = deepseek["prompts"]
    for secret in ("Toms", "toms_lv", "Berzins", "2694", "Ganu", "1234"):
        assert secret not in prompt, secret
    assert "[client]" in prompt and "[number]" in prompt and "[address]" in prompt and "[way in]" in prompt
    # The correction comes first, then the edit, each with the chat before it.
    assert prompt.index("### Item 1\n(a correction") < prompt.index("### Item 2\n(a draft the operator edited)")
    assert "The business sent: okay [client], here\nThe operator's correction: [client] collects photos" in prompt
    assert "The assistant wrote: Hello [client]! Yes I am free, come to [address], [way in]." in prompt
    assert "The operator sent instead: hey love 😘 what time you come? text me 15 min before" in prompt
    assert "Client: Hi, [client] here, free tonight? [number]" in prompt
    assert "never with names, numbers, prices, times, addresses or door codes" in prompt
    assert deepseek["kwargs"]["model"] == "deepseek-chat" and deepseek["kwargs"]["api_key"] == "pk"

    first, second = result["rules"]
    assert result["read"] == 2
    assert (first["status"], first["created_by"], first["source"], first["scope"]) == (
        "proposed", "suggested", "correction", "industry")
    assert first["source_ref"] == {"tenant_id": pam, "message_id": ids["corrected"], "example_id": ids["example"]}
    assert (second["source"], second["scope"], second["tenant_id"], second["section"]) == (
        "draft_edit", "account", pam, "boundaries")
    assert second["source_ref"] == {"tenant_id": pam, "message_id": ids["edited"]}
    # Proposed rules are not in any prompt until accepted.
    assert await rules.active_for(pool, pam, 1) == []
    assert await rules.waiting(pool, pam) == {"draft_edits": 0, "corrections": 0}

    # Nothing new: no model call at all.
    again = await rules.suggest(pool, pam, api_key="pk", actor=audit.ADMIN)
    assert again == {"rules": [], "read": 0} and len(deepseek["prompts"]) == 1


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_rejected_and_ruleless_items_never_come_back(world, deepseek):
    pool, pam = world["pool"], world["pam"]
    ids = await seed_corrections(world)
    rule_text = "When he keeps asking for photos without a time, ask for his time first."
    # A rule for the correction; none for the edit (a typo fix, say).
    deepseek["answer"] = json.dumps({"rules": [{"item": 1, "rule": rule_text, "section": "booking"},
                                               {"item": 2, "rule": None}]})
    [proposed] = (await rules.suggest(pool, pam, api_key="pk", actor=audit.ADMIN))["rules"]
    await rules.update(pool, proposed["id"], actor=audit.ADMIN, status=rules.REJECTED)

    # A new correction on the same flagged chat: only it is read next time,
    # and the model proposing the rejected rule again is not stored.
    later = await seed_message(pool, "pam", 501, "out", "sent", "here love", model="deepseek-chat")
    await pool.execute("UPDATE finetune_examples SET corrections = corrections || $2::jsonb WHERE id = $1",
                       ids["example"], json.dumps({str(later): "he asked twice: ask his time"}))
    deepseek["answer"] = json.dumps({"rules": [{"item": 1, "rule": rule_text.upper(), "section": "booking"}]})
    result = await rules.suggest(pool, pam, api_key="pk", actor=audit.ADMIN)

    prompt = deepseek["prompts"][-1]
    assert result == {"rules": [], "read": 1}
    assert "### Item 2" not in prompt and "The business sent: here love" in prompt
    assert "okay [client], here" in prompt  # as context only: the chat line before it
    assert f"- {rule_text}" not in prompt  # a rejected rule is not shown as one in place
    statuses = [r["status"] for r in await rules.list_for(pool, pam, 1)]
    assert statuses == ["rejected"]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_suggestion_with_personal_details_or_a_bad_answer_is_not_stored(world, deepseek):
    pool, pam = world["pool"], world["pam"]
    await seed_corrections(world)
    deepseek["answer"] = "I think the bot should be nicer."
    with pytest.raises(rules.RuleError, match="JSON"):
        await rules.suggest(pool, pam, api_key="pk", actor=audit.ADMIN)
    assert await rules.waiting(pool, pam) == {"draft_edits": 1, "corrections": 1}  # nothing marked read

    deepseek["answer"] = json.dumps({"rules": [
        {"item": 1, "rule": "Ask [client] for his time before photos.", "section": "booking"},
        {"item": 2, "rule": "Tell him to call +37126942162 for the way in.", "section": "nowhere", "scope": "x"},
        {"item": 3, "rule": "An item that was never sent.", "section": "faq"},
    ]})
    result = await rules.suggest(pool, pam, api_key="pk", actor=audit.ADMIN)
    assert result == {"rules": [], "read": 2}
    assert rules.parse_suggestions('{"rules": [{"item": 1, "rule": "Ask for his time first.", '
                                   '"section": "nowhere", "scope": "x"}]}', 1) == {
        1: {"text": "Ask for his time first.", "section": rules.DEFAULT_SECTION, "scope": "industry"}}


# -------------------------------------------------------------------- fold


def fold_answer(business=None, industry=None, notes=""):
    return (f"=== BUSINESS LAYER ===\n{json.dumps(business) if business is not None else '{}'}\n\n"
            f"=== INDUSTRY STANDARD (General) ===\n{json.dumps(industry) if industry is not None else '{}'}\n\n"
            f"=== NOTES FOR OPERATOR ===\n{notes}\n")


async def fold_and_wait(client, tenant_id):
    response = await client.post("/api/finetune/rules/fold", json={"tenant_id": tenant_id})
    await asyncio.gather(*finetune_api._tasks)
    return response


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_fold_run_folds_both_scopes_and_applying_merges_them(panel_client, world, deepseek, reloads):
    pool, store, pam = world["pool"], world["store"], world["pam"]
    shared = await make_rule(pool, text="Ask for his time before sending the photos.")
    own = await make_rule(pool, pam, text="Say the 30 min rate is for incall only.", section="services")
    folded_standard = {"sections": {**STANDARD["sections"],
                                    "booking": STANDARD["sections"]["booking"] + " Ask his time before photos."}}
    services = CLIENT["overrides"]["services"]["text"] + "\n30 min: incall only."
    folded_layer = {"overrides": {"services": {"mode": "override", "text": services}}, "addendum": ""}
    deepseek["answer"] = fold_answer(folded_layer, folded_standard,
                                     f"FOLDED: rule {shared['id']} -> booking\nFOLDED: rule {own['id']} -> services")

    started = await fold_and_wait(panel_client, pam)

    assert started.status_code == 200, started.text
    [prompt] = deepseek["prompts"]
    assert f"- rule {shared['id']} (booking): {shared['text']}" in prompt
    assert f"- rule {own['id']} (services): {own['text']}" in prompt
    assert prompt.index("INDUSTRY-WIDE RULES") < prompt.index(shared["text"]) < prompt.index("RULES FOR Pam ONLY")
    assert STANDARD["sections"]["booking"] in prompt and "30 min 100" in prompt  # the live layers, as JSON
    assert "Keep everything a rule does not touch word for word" in prompt
    assert "the newer rule (later in its list) wins" in prompt
    assert 'FOLDED: rule <id> -> <section>' in prompt
    assert "Never weaken the boundaries" in prompt and '"boundaries" can only be appended to' in prompt
    assert "current length plus 1,500 characters, and under 20,000 characters" in prompt
    assert "Never claim or imply that you are a human" in prompt  # the platform rules, for reference
    assert deepseek["kwargs"]["max_tokens"] >= 1000

    run = (await panel_client.get(f"/api/finetune/runs/{started.json()['id']}")).json()
    assert (run["source"], run["status"], run["created_by"]) == ("rules", "done", "admin")
    assert run["files"] == [f"rule-{shared['id']}", f"rule-{own['id']}"]
    assert run["result"]["errors"] == []
    assert run["result"]["rules"] == {"industry": [shared["id"]], "account": [own["id"]],
                                      "folded": [shared["id"], own["id"]], "skipped": []}
    assert run["result"]["industry_sections"] == folded_standard["sections"]
    assert run["result"]["business_layer"] == folded_layer
    assert (await panel_client.get(f"/api/finetune/runs?tenant_id={pam}")).json()[0]["source"] == "rules"

    applied = await panel_client.post(f"/api/finetune/runs/{run['id']}/apply", json={
        "business_layer": run["result"]["business_layer"], "industry_sections": run["result"]["industry_sections"]})
    assert applied.status_code == 200, applied.text
    tag = f"b1/i1v{applied.json()['applied']['industry_version']}/c{applied.json()['applied']['client_version']}"
    after = {r["id"]: r for r in await rules.list_for(pool, pam, 1)}
    assert [(after[i]["status"], after[i]["merged_into"]) for i in (shared["id"], own["id"])] == [("merged", tag)] * 2
    bundle = await store.bundle(pam)
    assert bundle.layers["rules"] == [] and bundle.prompt.version_tag == tag
    assert "Ask his time before photos." in bundle.prompt.text and "30 min: incall only." in bundle.prompt.text
    [merged] = [e for e in await audit.list_events(pool) if e["event"] == rules.RULES_MERGED]
    assert merged["payload"] == {"run_id": run["id"], "rule_ids": [shared["id"], own["id"]], "merged_into": tag}
    assert reloads == [["leaui", "pam"]]  # the industry's accounts: the standard changed
    # A fold learned from no business's chats: the finetune template's count stays.
    assert (await panel_client.get("/api/finetune/industries/1")).json()["businesses_so_far"] == 0


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_fold_of_one_accounts_rules_leaves_the_standard_alone(panel_client, world, deepseek, reloads):
    pool, store, leaui = world["pool"], world["store"], world["leaui"]
    own = await make_rule(pool, leaui, text="Leaui does outcall only: say so when asked.", section="hours_location")
    late = await make_rule(pool, leaui, text="Leaui works from noon: say so when asked.", section="hours_location")
    layer = {"overrides": {"hours_location": {"mode": "append", "text": "Outcall only."}}, "addendum": ""}
    # The model writes {} for the standard, as told; and forgets one rule in its notes.
    deepseek["answer"] = fold_answer(layer, None, f"FOLDED: rule {own['id']} -> hours_location")

    run = (await fold_and_wait(panel_client, leaui)).json()
    assert "INDUSTRY-WIDE RULES, to fold into the INDUSTRY STANDARD (oldest first):\n(none)" in deepseek["prompts"][0]
    assert "there are no rules for this part: write exactly {}" in deepseek["prompts"][0]
    run = (await panel_client.get(f"/api/finetune/runs/{run['id']}")).json()
    assert run["result"]["industry_sections"] is None and run["result"]["business_layer"] == layer
    assert run["result"]["errors"] == [f"Rule {late['id']} is not in the notes as FOLDED: it stays active after "
                                       "applying. Check whether it went in."]

    applied = await panel_client.post(f"/api/finetune/runs/{run['id']}/apply",
                                      json={"business_layer": run["result"]["business_layer"]})
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == {"client_version": 1}
    statuses = {r["id"]: r["status"] for r in await rules.list_for(pool, leaui, 1)}
    assert statuses == {own["id"]: "merged", late["id"]: "active"}
    assert (await store.get_industry(1))["template_version"] == 2  # untouched
    assert reloads == [["leaui"]]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_rule_changed_while_its_fold_ran_stays_active(panel_client, world, deepseek, reloads):
    pool, pam = world["pool"], world["pam"]
    own = await make_rule(pool, pam, text="Say the 30 min rate is for incall only.", section="services")
    deepseek["answer"] = fold_answer(CLIENT, None, f"FOLDED: rule {own['id']} -> services")
    run = (await fold_and_wait(panel_client, pam)).json()
    await rules.update(pool, own["id"], actor="admin", text="Say the 30 min rate is for incall and outcall.")
    run = (await panel_client.get(f"/api/finetune/runs/{run['id']}")).json()
    assert (await panel_client.post(f"/api/finetune/runs/{run['id']}/apply",
                                    json={"business_layer": run["result"]["business_layer"]})).status_code == 200
    assert (await rules.get(pool, own["id"]))["status"] == "active"


def plan(**over):
    base = {"tenant": {"id": 1, "name": "Pam"}, "industry": {"id": 1, "name": "General", "template_version": 2},
            "base_rules": "Never claim to be a human.", "sections": dict(STANDARD["sections"]),
            "client": json.loads(json.dumps(CLIENT)),
            "industry_rules": [{"id": 7, "section": "booking", "text": "Ask his time first.", "scope": "industry"}],
            "account_rules": [{"id": 8, "section": "services", "text": "Incall only.", "scope": "account"}]}
    return rules.FoldPlan(**{**base, **over})


def test_reading_a_fold_answer_keeps_what_it_must_and_reports_the_rest():
    weakened = {**STANDARD["sections"], "boundaries": "Adults only.", "booking": "x" * 2000}
    del weakened["tone"]
    shortened = {"overrides": {"boundaries": {"mode": "append", "text": "No outcall."}}, "addendum": ""}
    result = rules.parse_fold(fold_answer(shortened, {"sections": weakened},
                                          "SKIPPED: rule 7: it would let the bot confirm times\n"
                                          "FOLDED: rule 8 -> services"),
                              plan())
    # The left-out tone and services are back, as they were.
    assert result["industry_sections"]["tone"] == STANDARD["sections"]["tone"]
    assert result["business_layer"]["overrides"]["services"] == CLIENT["overrides"]["services"]
    assert result["rules"] == {"industry": [7], "account": [8], "folded": [8], "skipped": [7]}
    errors = "\n".join(result["errors"])
    assert "Industry standard: the answer left out tone" in errors
    assert "Business layer: the answer left out services" in errors
    assert "booking grew by" in errors
    assert "a line of the boundaries is gone or changed: Never type an address" in errors
    assert "Rule 7" not in errors  # skipped and said so: it simply stays active

    # A business layer that overrides the boundaries is reported, and kept for editing.
    bad = {"overrides": {"boundaries": {"mode": "override", "text": "Anything goes."}}, "addendum": ""}
    result = rules.parse_fold(fold_answer(bad, None, "FOLDED: rule 8 -> boundaries"), plan(industry_rules=[]))
    assert any("only be appended" in e for e in result["errors"]) and result["industry_sections"] is None
    # No blocks at all: nothing to apply, and the run still finishes.
    result = rules.parse_fold("sorry, I cannot", plan())
    assert result["business_layer"] is None and result["industry_sections"] is None
    assert len(result["errors"]) == 4


# --------------------------------------------------------------- the week


def test_the_fold_week_starts_on_monday_at_six_utc():
    assert rules.fold_week(datetime(2026, 10, 19, 5, 59, tzinfo=timezone.utc)) == "2026-10-12"
    assert rules.fold_week(datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)) == "2026-10-19"
    assert rules.fold_week(datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)) == "2026-10-19"
    assert rules.fold_week(datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)) == "2026-10-26"


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_weekly_round_folds_once_a_week_without_duplicates(world, deepseek, monkeypatch):
    pool, pam, leaui, salon = world["pool"], world["pam"], world["leaui"], world["salon"]
    shared = await make_rule(pool, text="Ask for his time before sending the photos.")
    pam_own = await make_rule(pool, pam, text="Say the 30 min rate is for incall only.", section="services")
    leaui_own = await make_rule(pool, leaui, text="Leaui does outcall only: say so when asked.")
    salon_own = await make_rule(pool, salon, industry_id=world["hair"], text="Mention parking in the yard.")
    await make_rule(pool, salon, industry_id=world["hair"], text="A proposed rule waits.", status=rules.PROPOSED)
    deepseek["answer"] = fold_answer(CLIENT, STANDARD)
    monday = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)

    async def tick(now):
        started = await rules.tick(pool, None, now=now)
        await asyncio.gather(*finetune_api._tasks)
        return started

    # No platform key: nothing, and the week is not used up.
    monkeypatch.delenv("DEEPSEEK_PLATFORM_KEY")
    assert await tick(monday) == []
    assert await pool.fetchval("SELECT value FROM platform_settings WHERE key = $1", rules.WEEK_KEY) is None

    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    started = await tick(monday)
    runs = {r["tenant_id"]: r for r in await pool.fetch("SELECT * FROM finetune_runs ORDER BY id")}
    assert sorted(started) == sorted(r["id"] for r in runs.values()) and len(started) == 3
    # The industry's rules once, on its lowest account (with that one's own); every other account alone.
    assert json.loads(runs[pam]["files"]) == [f"rule-{shared['id']}", f"rule-{pam_own['id']}"]
    assert json.loads(runs[leaui]["files"]) == [f"rule-{leaui_own['id']}"]
    assert json.loads(runs[salon]["files"]) == [f"rule-{salon_own['id']}"]
    assert {r["created_by"] for r in runs.values()} == {audit.SYSTEM}
    assert {r["status"] for r in runs.values()} == {"done"}
    assert json.loads(await pool.fetchval("SELECT value FROM platform_settings WHERE key = $1", rules.WEEK_KEY)) == {
        "week": "2026-10-19", "runs": started}

    # Once a week: later the same week, and early next Monday, nothing.
    assert await tick(datetime(2026, 10, 21, 12, tzinfo=timezone.utc)) == []
    assert await tick(datetime(2026, 10, 26, 5, 59, tzinfo=timezone.utc)) == []
    # Next week: the runs from last week still wait for the admin and hold
    # the same rules, so only the account whose rule changed is folded again.
    await rules.update(pool, leaui_own["id"], actor="admin", text="Leaui does outcall only: say so when he asks.")
    [again] = await tick(datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc))
    assert await pool.fetchval("SELECT tenant_id FROM finetune_runs WHERE id = $1", again) == leaui
    assert len(deepseek["prompts"]) == 4


# --------------------------------------------------------------------- API


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_rules_routes(panel_client, world, deepseek, reloads):
    pool, pam = world["pool"], world["pam"]
    await seed_corrections(world)
    listed = (await panel_client.get(f"/api/finetune/rules?tenant_id={pam}")).json()
    assert listed["rules"] == [] and listed["cap"] == rules.CAP and listed["platform_key"] is True
    assert listed["industry"] == {"id": 1, "name": "General"} and listed["last_fold"] is None
    assert listed["waiting"] == {"draft_edits": 1, "corrections": 1}
    assert listed["counts"] == {"industry": {"active": 0, "proposed": 0}, "account": {"active": 0, "proposed": 0}}
    assert [s["key"] for s in listed["sections"]] == list(prompt_layers.SECTION_KEYS)

    # A rule typed by the admin is active at once, and the accounts reload.
    typed = await panel_client.post("/api/finetune/rules", json={
        "tenant_id": pam, "text": "Never send the rates twice in one chat.", "section": "booking", "scope": "industry"})
    assert typed.status_code == 200, typed.text
    rule = typed.json()
    assert (rule["status"], rule["source"], rule["scope"], rule["created_by"]) == (
        "active", "manual", "industry", "admin")
    assert rule["source_ref"] == {"tenant_id": pam}
    assert reloads == [["leaui", "pam"]]
    for bad in ({"text": "ok"}, {"section": "rules"}, {"scope": "everyone"}):
        body = {"tenant_id": pam, "text": "Never send the rates twice.", "section": "booking", **bad}
        assert (await panel_client.post("/api/finetune/rules", json=body)).status_code in (400, 422)
    assert (await panel_client.post("/api/finetune/rules", json={
        "tenant_id": 999, "text": "Never send the rates twice.", "section": "booking"})).status_code == 404

    # Suggest: the platform key is needed; then proposals come back.
    deepseek["answer"] = json.dumps({"rules": [{"item": 1, "rule": "Ask for his time before more photos.",
                                                "section": "booking", "scope": "account"}]})
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("DEEPSEEK_PLATFORM_KEY")
        assert (await panel_client.post("/api/finetune/rules/suggest", json={"tenant_id": pam})).status_code == 400
    suggested = (await panel_client.post("/api/finetune/rules/suggest", json={"tenant_id": pam})).json()
    [proposal] = suggested["rules"]
    assert suggested["read"] == 2 and suggested["waiting"] == {"draft_edits": 0, "corrections": 0}
    assert (proposal["status"], proposal["scope"], proposal["tenant_id"]) == ("proposed", "account", pam)
    assert len(reloads) == 1  # a proposal changes no prompt

    # Accept it, edit it, move it, reject it: each change to the active set reloads who it reaches.
    path = f"/api/finetune/rules/{proposal['id']}"
    assert (await panel_client.patch(path, json={"status": "active"})).json()["status"] == "active"
    assert reloads[-1] == ["pam"]
    moved = (await panel_client.patch(path, json={"scope": "industry",
                                                  "text": "Ask for his time before photos."})).json()
    assert (moved["scope"], moved["tenant_id"], moved["text"]) == ("industry", None, "Ask for his time before photos.")
    assert reloads[-1] == ["leaui", "pam"]  # its industry now (and pam, which it left)
    count = len(reloads)
    assert (await panel_client.patch(path, json={"section": "faq"})).status_code == 200
    assert len(reloads) == count  # the section only matters to the fold
    assert (await panel_client.patch(path, json={"status": "rejected"})).json()["status"] == "rejected"
    assert len(reloads) == count + 1
    assert (await panel_client.patch(path, json={"status": "merged"})).status_code == 422
    assert (await panel_client.patch("/api/finetune/rules/9999", json={"status": "active"})).status_code == 404
    full = [await make_rule(pool, text=f"Filler rule number {chr(65 + i)}.") for i in range(rules.CAP - 1)]
    capped = await panel_client.patch(path, json={"status": "active"})
    assert capped.status_code == 409 and "Fold them into the prompt first" in capped.json()["detail"]
    assert (await panel_client.get(f"/api/finetune/rules?tenant_id={pam}")).json()["counts"]["industry"] == {
        "active": rules.CAP, "proposed": 0}
    await pool.execute("UPDATE prompt_rules SET status = 'merged', merged_into = 'b1/i1v3/c1' WHERE id = $1",
                       full[0]["id"])
    assert (await panel_client.patch(f"/api/finetune/rules/{full[0]['id']}",
                                     json={"status": "retired"})).status_code == 409

    # Fold now: a finetune run like any other; not twice at once, not without the key.
    deepseek["answer"] = fold_answer(None, STANDARD)
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("DEEPSEEK_PLATFORM_KEY")
        assert (await panel_client.post("/api/finetune/rules/fold", json={"tenant_id": pam})).status_code == 400
    await pool.execute("INSERT INTO finetune_runs (tenant_id, industry_id, model, source, industry_version, "
                       "created_by) VALUES ($1, 1, 'deepseek-chat', 'rules', 2, 'admin')", pam)
    busy = await panel_client.post("/api/finetune/rules/fold", json={"tenant_id": pam})
    assert busy.status_code == 409 and "already running" in busy.json()["detail"]
    await pool.execute("UPDATE finetune_runs SET status = 'failed'")
    folded = await fold_and_wait(panel_client, pam)
    assert folded.status_code == 200, folded.text
    assert set(folded.json()) >= {"id", "status", "source", "files", "result", "raw_output"}
    # The industry's active rules: the admin's, and the fillers but the merged one.
    assert folded.json()["source"] == "rules" and len(folded.json()["files"]) == rules.CAP - 1
    last = (await panel_client.get(f"/api/finetune/rules?tenant_id={pam}")).json()["last_fold"]
    assert (last["id"], last["status"]) == (folded.json()["id"], "done")
    nothing = await panel_client.post("/api/finetune/rules/fold", json={"tenant_id": world["salon"]})
    assert nothing.status_code == 400 and "no active rules" in nothing.json()["detail"]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_the_rules_routes_need_the_admin_login(panel_client, world):
    import panel

    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app), base_url="http://test")
    async with anonymous:
        for method, path in [("GET", f"/api/finetune/rules?tenant_id={world['pam']}"),
                             ("POST", "/api/finetune/rules"), ("POST", "/api/finetune/rules/suggest"),
                             ("PATCH", "/api/finetune/rules/1"), ("POST", "/api/finetune/rules/fold"),
                             ("GET", f"/api/finetune/style-card?tenant_id={world['pam']}")]:
            response = await anonymous.request(method, path, json={})
            assert response.status_code == 401, (method, path)
