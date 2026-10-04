"""Finetune from chat screenshots: the template, the model's answer, and the
run -> review -> apply flow in the admin API."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import pytest_asyncio

import audit
import finetune
import finetune_api
from conftest import seed_session

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

BUSINESS = {"overrides": {
    "services": {"mode": "override", "text": "1 hour [rate]."},
    "boundaries": {"mode": "append", "text": "No bookings after midnight."},
}, "addendum": "Always confirm the day before."}
INDUSTRY = {"sections": {"about": "Independent companions.", "boundaries": "Adults only."}}


def answer(business=BUSINESS, industry=INDUSTRY, notes="- Rates missing for overnights."):
    return (f"=== BUSINESS LAYER ===\n```json\n{json.dumps(business)}\n```\n\n"
            f"=== INDUSTRY STANDARD (escort) ===\n{json.dumps(industry)}\n\n"
            f"=== NOTES FOR OPERATOR ===\n{notes}\n")


# ------------------------------------------------------------------ pure


def test_template_placeholders_are_filled_as_plain_text():
    template = 'BUSINESS: {BUSINESS_NAME}\nN={N}\nSTANDARD:\n{CURRENT_INDUSTRY_SECTIONS_JSON}\nJSON: {"a": 1}'
    first = finetune.fill_template(template, business_name="Mia", businesses_so_far=0, industry_sections={})
    assert first == 'BUSINESS: Mia\nN=0\nSTANDARD:\nnone\nJSON: {"a": 1}'
    later = finetune.fill_template(template, business_name="Mia", businesses_so_far=3,
                                   industry_sections={"tone": "Warm."})
    assert '"tone": "Warm."' in later and "N=3" in later


def test_an_industry_with_its_own_default_gets_it():
    escort = finetune.default_template(" Escort ")
    assert escort.startswith("You are fine-tuning") and "ESCORT industry" in escort
    assert all(p in escort for p in finetune.PLACEHOLDERS)
    assert "boundaries may ONLY use mode \"append\"" in escort
    assert "-good" in escort and "-bad" in escort
    assert "GREEN bubbles = the business" in escort and "BLUE bubbles = the business" in escort


def test_any_other_industry_gets_the_generic_default_with_its_name():
    hair = finetune.default_template("Hair salons")
    assert "Hair salons industry" in hair and "=== INDUSTRY STANDARD (Hair salons) ===" in hair
    assert finetune.INDUSTRY_NAME not in hair
    assert all(p in hair for p in finetune.PLACEHOLDERS)
    # The generic file is not reachable as an industry's "own" default by name.
    assert "Generic industry" in finetune.default_template("Generic")
    assert finetune.INDUSTRY_NAME not in finetune.default_template("generic")


def test_the_defaults_fill_and_their_example_answer_parses():
    for name in ("Escort", "Hair salons"):
        filled = finetune.fill_template(finetune.default_template(name), business_name="Mia",
                                        businesses_so_far=0, industry_sections={})
        assert not any(p in filled for p in finetune.PLACEHOLDERS)
        assert "BUSINESS: Mia" in filled and "(built from 0 businesses so far" in filled
        # The output headers the template asks for are the ones the parser reads.
        header = next(line for line in filled.splitlines() if line.startswith("=== INDUSTRY STANDARD"))
        result = finetune.parse_output(answer().replace("=== INDUSTRY STANDARD (escort) ===", header))
        assert result["errors"] == [] and result["industry_sections"] is not None


def test_screenshots_are_ordered_naturally_and_checked():
    shots = [("10.png", PNG), ("03b-bad.png", PNG), ("2.png", PNG), ("03a.png", PNG)]
    assert [n for n, _ in finetune.check_images(shots)] == ["2.png", "03a.png", "03b-bad.png", "10.png"]
    with pytest.raises(finetune.FinetuneError, match="notes.txt"):
        finetune.check_images([("notes.txt", b"hello")])
    with pytest.raises(finetune.FinetuneError):
        finetune.check_images([])


def test_each_screenshot_is_labelled_with_its_file_name():
    [message] = finetune.build_messages("PROMPT", [("01a-good.png", PNG)])
    parts = message["content"]
    assert parts[0] == {"type": "text", "text": "PROMPT"}
    assert parts[1]["text"] == "Screenshot 01a-good.png:"
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_good_answer_parses_into_both_layers():
    result = finetune.parse_output(answer())
    assert result["errors"] == []
    assert result["business_layer"]["overrides"]["boundaries"]["mode"] == "append"
    assert result["industry_sections"]["boundaries"] == "Adults only."
    assert result["notes"] == "- Rates missing for overnights."


def test_a_boundaries_override_is_reported_but_kept_for_editing():
    bad = {"overrides": {"boundaries": {"mode": "override", "text": "Anything goes."}}, "addendum": ""}
    result = finetune.parse_output(answer(business=bad))
    assert any("only be appended" in e for e in result["errors"])
    assert result["business_layer"] == bad


def test_an_industry_standard_without_boundaries_is_refused():
    result = finetune.parse_output(answer(industry={"sections": {"about": "x"}}))
    assert any("no boundaries" in e for e in result["errors"])
    with pytest.raises(finetune.FinetuneError, match="no boundaries"):
        finetune.check_industry_sections({"about": "x"})


def test_missing_blocks_and_broken_json_are_reported():
    result = finetune.parse_output("=== BUSINESS LAYER ===\n{not json}\n")
    assert any("not valid JSON" in e for e in result["errors"])
    assert any("INDUSTRY STANDARD" in e for e in result["errors"])
    assert result["business_layer"] is None and result["industry_sections"] is None


# ------------------------------------------------------------------- API


@pytest_asyncio.fixture
async def tenant(pg_pool):
    return await seed_session(pg_pool, "acct", name="Mia")


@pytest_asyncio.fixture
async def model(monkeypatch):
    """The vision endpoint, answering with `answer()` unless a test swaps it."""
    monkeypatch.setenv("VISION_API_URL", "http://vision.test/v1/chat/completions")
    monkeypatch.setenv("VISION_API_KEY", "k")
    monkeypatch.setenv("FINETUNE_MODEL", "vision-model")
    seen = {}

    async def fake_complete(prompt, images, **kwargs):
        seen.update(prompt=prompt, files=[n for n, _ in images], model=kwargs["model"])
        return seen.get("reply", answer())

    monkeypatch.setattr(finetune, "complete", fake_complete)
    return seen


async def start(client, tenant, names=("01.png",)):
    body = {"tenant_id": tenant, "images": [{"name": n, "data": base64.b64encode(PNG).decode()} for n in names]}
    response = await client.post("/api/finetune/runs", json=body)
    await asyncio.gather(*finetune_api._tasks)
    return response


async def write_template(client, template="Business {BUSINESS_NAME}, {N} so far, standard {CURRENT_INDUSTRY_SECTIONS_JSON}"):
    r = await client.put("/api/finetune/industries/1/template", json={"template": template, "reason": "first"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_run_needs_a_template_first(panel_client, tenant, model):
    response = await start(panel_client, tenant)
    assert response.status_code == 400 and "template" in response.json()["detail"]


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_run_review_apply(panel_client, pg_pool, tenant, model):
    before = (await panel_client.get("/api/finetune/industries/1")).json()
    assert before["template"] == "" and before["default_template"].startswith("You are fine-tuning")
    info = await write_template(panel_client)
    assert info["businesses_so_far"] == 0

    started = await start(panel_client, tenant, names=("2.png", "01.png"))
    assert started.status_code == 200, started.text
    assert model["files"] == ["01.png", "2.png"] and model["model"] == "vision-model"
    assert "Business Mia, 0 so far" in model["prompt"]

    run = (await panel_client.get(f"/api/finetune/runs/{started.json()['id']}")).json()
    assert run["status"] == "done" and run["result"]["errors"] == [] and run["stale"] == ""
    assert run["files"] == ["01.png", "2.png"]  # names only; the images are not stored
    assert "data" not in json.dumps(run["files"])

    applied = await panel_client.post(f"/api/finetune/runs/{run['id']}/apply", json={
        "business_layer": run["result"]["business_layer"],
        "industry_sections": run["result"]["industry_sections"],
    })
    assert applied.status_code == 200, applied.text
    done = applied.json()
    assert done["status"] == "applied"
    assert done["applied"] == {"industry_version": 2, "client_version": 1}
    view = (await panel_client.get(f"/api/tenants/{tenant}")).json()
    boundaries = next(s for s in view["prompt"]["sections"] if s["key"] == "boundaries")
    assert boundaries["text"] == "Adults only.\nNo bookings after midnight."
    assert [e["payload"]["run_id"] for e in await audit.list_events(pg_pool)
            if e["event"] == finetune_api.FINETUNE_APPLIED] == [run["id"]]

    # Applying twice is refused, and the next run counts this business.
    again = await panel_client.post(f"/api/finetune/runs/{run['id']}/apply",
                                    json={"business_layer": run["result"]["business_layer"]})
    assert again.status_code == 409
    assert (await panel_client.get("/api/finetune/industries/1")).json()["businesses_so_far"] == 1


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_run_is_stale_once_the_industry_moves_on(panel_client, tenant, model):
    await write_template(panel_client)
    first = (await start(panel_client, tenant)).json()
    second = (await start(panel_client, tenant)).json()
    r = await panel_client.post(f"/api/finetune/runs/{first['id']}/apply",
                                json={"industry_sections": INDUSTRY["sections"]})
    assert r.status_code == 200, r.text

    stale = (await panel_client.get(f"/api/finetune/runs/{second['id']}")).json()
    assert "changed since the run" in stale["stale"]
    refused = await panel_client.post(f"/api/finetune/runs/{second['id']}/apply",
                                      json={"business_layer": BUSINESS})
    assert refused.status_code == 409


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_edited_layers_are_checked_before_anything_is_saved(panel_client, tenant, model):
    await write_template(panel_client)
    run = (await start(panel_client, tenant)).json()
    bad_business = {"overrides": {"boundaries": {"mode": "override", "text": "x"}}, "addendum": ""}
    r = await panel_client.post(f"/api/finetune/runs/{run['id']}/apply", json={
        "industry_sections": INDUSTRY["sections"], "business_layer": bad_business})
    assert r.status_code == 400 and "only be appended" in r.json()["detail"]
    after = (await panel_client.get(f"/api/finetune/runs/{run['id']}")).json()
    assert after["status"] == "done" and after["current"]["industry_version"] == 1


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_failed_model_call_fails_the_run(panel_client, tenant, model, monkeypatch):
    import vision

    async def broken(*args, **kwargs):
        raise vision.VisionError("Vision API timed out.")

    monkeypatch.setattr(finetune, "complete", broken)
    await write_template(panel_client)
    run = (await start(panel_client, tenant)).json()
    failed = (await panel_client.get(f"/api/finetune/runs/{run['id']}")).json()
    assert failed["status"] == "failed" and failed["error"] == "Vision API timed out."
    discarded = await panel_client.post(f"/api/finetune/runs/{run['id']}/discard")
    assert discarded.json()["status"] == "discarded"


# ------------------------------------------------------------- transcripts


def test_typed_chats_are_ordered_and_checked():
    chats = finetune.check_chats([("10", "Client: a"), ("02-good", "Client: b"), ("03", "  "), ("01", "Client: c")])
    assert [n for n, _ in chats] == ["01", "02-good", "10"]  # the empty one is left out
    for bad in ([], [("01", " ")], [("01", "x"), ("01", "y")], [("", "x")],
                [("01", "x" * (finetune.MAX_TRANSCRIPT_CHARS + 1))]):
        with pytest.raises(finetune.FinetuneError):
            finetune.check_chats(bad)


def test_typed_chats_follow_the_template_with_a_note_on_how_to_read_them():
    [message] = finetune.build_text_messages("TEMPLATE", [("01-good", "Client: hi\nBusiness: hello")])
    text = message["content"]
    assert text.startswith("TEMPLATE\n\n" + finetune.TRANSCRIPT_NOTE)
    assert text.endswith("### 01-good\nClient: hi\nBusiness: hello")


@pytest.mark.asyncio
async def test_typed_chats_go_to_deepseek(monkeypatch):
    import httpx

    import ai_responder

    seen = {}

    def handler(request):
        seen.update(url=str(request.url), body=json.loads(request.content), auth=request.headers["authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": answer()}}]})

    monkeypatch.setenv("FINETUNE_TEXT_MAX_TOKENS", "7000")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        raw = await finetune.complete_text("T", [("01", "Client: hi")], api_key="pk", model="deepseek-chat",
                                           client=client)
    assert raw == answer().strip()
    assert seen["url"] == ai_responder.API_URL and seen["auth"] == "Bearer pk"
    assert seen["body"]["model"] == "deepseek-chat" and seen["body"]["max_tokens"] == 7000
    assert isinstance(seen["body"]["messages"][0]["content"], str)


@pytest.mark.asyncio
async def test_without_the_platform_key_typed_chats_cannot_run():
    import vision

    with pytest.raises(vision.VisionError, match="DEEPSEEK_PLATFORM_KEY"):
        await finetune.complete_text("T", [("01", "x")], api_key="", model="deepseek-chat")


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_run_from_typed_chats(panel_client, tenant, monkeypatch):
    monkeypatch.delenv("VISION_API_URL", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_PLATFORM_KEY", raising=False)
    seen = {}

    async def fake_complete_text(prompt, chats, **kwargs):
        seen.update(prompt=prompt, chats=list(chats), model=kwargs["model"])
        return answer()

    monkeypatch.setattr(finetune, "complete_text", fake_complete_text)
    info = await write_template(panel_client)
    assert (info["screenshots_ready"], info["transcripts_ready"]) == (False, False)

    body = {"tenant_id": tenant, "chats": [{"name": "02-bad", "text": "Client: discount?\nBusiness: ok"},
                                           {"name": "01", "text": "Client: hi"}]}
    refused = await panel_client.post("/api/finetune/text-runs", json=body)
    assert refused.status_code == 400 and "DEEPSEEK_PLATFORM_KEY" in refused.json()["detail"]

    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    assert (await panel_client.get("/api/finetune/industries/1")).json()["transcripts_ready"] is True
    started = await panel_client.post("/api/finetune/text-runs", json=body)
    await asyncio.gather(*finetune_api._tasks)
    assert started.status_code == 200, started.text
    assert [n for n, _ in seen["chats"]] == ["01", "02-bad"] and seen["model"] == "deepseek-chat"
    assert "Business Mia, 0 so far" in seen["prompt"]

    run = (await panel_client.get(f"/api/finetune/runs/{started.json()['id']}")).json()
    assert run["source"] == "text" and run["files"] == ["01", "02-bad"]
    assert run["status"] == "done" and run["result"]["errors"] == []
    assert "discount" not in json.dumps(run)  # the chats themselves are not stored
