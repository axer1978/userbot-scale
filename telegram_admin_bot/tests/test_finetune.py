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


def test_the_escort_business_layer_holds_only_services_and_costs():
    # Every escort business shares the voice and pipeline (the standard); the
    # business layer the template asks for is services, hours_location and
    # appended boundaries only, with no addendum.
    lines = finetune.default_template("Escort").splitlines()
    example = json.loads(lines[lines.index("=== BUSINESS LAYER ===") + 1])
    layer = finetune.check_business_layer(example)
    assert set(layer["overrides"]) == {"services", "hours_location", "boundaries"}
    assert layer["overrides"]["boundaries"]["mode"] == "append" and layer["addendum"] == ""


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


# ----------------------------------------------------------- flagged chats


def msg(id, direction, status, text, at="2026-10-12T19:40:00+00:00", attachments=()):
    return {"id": id, "direction": direction, "status": status, "text": text, "created_at": at,
            "attachments": list(attachments)}


def test_a_flagged_chat_is_read_as_what_crossed_the_wire():
    lines = finetune.chat_lines([
        msg(1, "in", "received", "Hi, can I come over?"),
        msg(2, "out", "sent", "Hi my love ❤️ around what hour you come? 😘"),
        msg(3, "out", "rejected", "a draft the admin threw away"),
        msg(4, "out", "pending_approval", "a draft still waiting"),
        msg(5, "system", "note", "No reply: bare ok."),
        msg(6, "system", "note", "📅 Booking #3 requested for today 23:00, waiting for the owner."),
        msg(7, "out", "sent", "", attachments=[3]),
        msg(8, "out", "error", "FloodWait"),
        msg(9, "in", "received", "  "),
    ])
    assert [(line["id"], line["who"]) for line in lines] == [(1, "client"), (2, "business"), (6, "event"),
                                                               (7, "business")]
    assert lines[3]["text"] == "[sent a file]"


def test_personal_data_and_the_way_in_are_masked():
    masks = finetune.masks_for(display_name="Toms Berzins", username="toms_lv", address="ganu street 4",
                               way_in="door code 1234\nfloor 4 apart 42")
    text = finetune.mask(
        "Hi Toms! I'm at Ganu Street 4, door code 1234, call +371 2694 2162 or 26942162, toms@mail.lv, @toms_lv "
        "@someone_else", masks)
    assert text == ("Hi [client]! I'm at [address], [way in], call [number] or [number], [email], [username] "
                    "[username]")
    # Prices, times and dates are not phone numbers.
    assert finetune.mask("30 min 100, 1h 150 200 300, at 23:00 on 12.10.2026, +50 extra", masks) == \
        "30 min 100, 1h 150 200 300, at 23:00 on 12.10.2026, +50 extra"
    # A contact called "Love" does not eat "my love".
    assert finetune.mask("Hi my love, Love", finetune.masks_for(display_name="Love")) == "Hi my love, [client]"


def test_a_whatsapp_chat_labelled_with_the_number_still_hides_his_name():
    masks = finetune.masks_for(display_name="+37126123456", person_name="Toms Berzins")
    assert finetune.mask("Hi Toms, this is +37126123456? Toms Berzins here", masks) == \
        "Hi [client], this is [number]? [client] here"
    assert ("+37126123456", "[client]") not in masks


def test_a_flagged_chat_carries_the_operators_judgement_and_corrections():
    from zoneinfo import ZoneInfo

    example = {"verdict": "bad", "client_kind": "time_waster", "outcome": "never_booked",
               "note": "Toms asked for photos 3 times, never named a time.\nShould have asked for his time.",
               "corrections": {"2": "should have been: send me a voice message love and tell me your time 😘"}}
    lines = finetune.chat_lines([
        msg(1, "in", "received", "I want to see your energy first", at="2026-10-12T20:59:00+00:00"),
        msg(2, "out", "sent", "okay love", at="2026-10-12T21:01:00+00:00"),
        msg(3, "system", "note", "📅 Booking #3 declined.", at="2026-10-12T21:30:00+00:00"),
    ])
    text = finetune.render_example(example, lines, masks=finetune.masks_for(display_name="Toms"),
                                   tz=ZoneInfo("Europe/Riga"))
    assert text == (
        "OPERATOR'S JUDGEMENT (not part of the chat):\n"
        f"- Handled: {finetune.VERDICTS['bad']}\n"
        "- Client: TIME-WASTER\n"
        "- Ended: he never booked\n"
        "- Why: [client] asked for photos 3 times, never named a time.\n  Should have asked for his time.\n\n"
        "— Mon 12.10.2026 —\n"
        "[23:59] Client: I want to see your energy first\n"
        "— Tue 13.10.2026 —\n"
        "[00:01] Business: okay love\n"
        "ANNOTATION FOR MODEL: should have been: send me a voice message love and tell me your time 😘\n"
        "— booking: 📅 Booking #3 declined. —"
    )


def test_a_flag_is_checked():
    ok = finetune.check_example(verdict="good", client_kind="", outcome="came", note=" fine ",
                                corrections={"5": " x ", "6": "  "}, message_ids={5, 6})
    assert ok == {"verdict": "good", "client_kind": "", "outcome": "came", "note": "fine", "corrections": {"5": "x"}}
    base = {"verdict": "good", "client_kind": "", "outcome": "", "note": "", "corrections": {}, "message_ids": {5}}
    for bad in ({"verdict": "great"}, {"client_kind": "rich"}, {"outcome": "married"},
                {"corrections": {"9": "not in this chat"}}, {"corrections": {"x": "not a message"}},
                {"note": "n" * (finetune.MAX_NOTE_CHARS + 1)}):
        with pytest.raises(finetune.ExampleError):
            finetune.check_example(**{**base, **bad})
    assert [finetune.example_name(7, v) for v in ("good", "bad", "plain")] == ["07-good", "07-bad", "07"]


def test_flagged_chats_come_with_a_note_on_the_judgement():
    [message] = finetune.build_example_messages("TEMPLATE", [("07-good", "OPERATOR'S JUDGEMENT ...")])
    assert message["content"].startswith(f"TEMPLATE\n\n{finetune.TRANSCRIPT_NOTE}\n\n{finetune.EXAMPLES_NOTE}\n\n")
    assert message["content"].endswith("### 07-good\nOPERATOR'S JUDGEMENT ...")
    assert "ANNOTATION FOR MODEL" in finetune.EXAMPLES_NOTE


async def seed_chat(pool, tenant, session_id="acct", chat_id=501, name="Toms", messages=()):
    async with pool.acquire() as con:
        await con.execute("INSERT INTO conversations (session_id, tenant_id, chat_id, display_name, username) "
                          "VALUES ($1, $2, $3, $4, 'toms_lv')", session_id, tenant, chat_id, name)
        return [await con.fetchval("INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text) "
                                   "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                                   session_id, tenant, chat_id, direction, status, text)
                for direction, status, text in messages]


CHAT = (("in", "received", "Hi, Toms here, free tonight? +37126942162"),
        ("out", "sent", "Hi my love ❤️ around what hour you come? 😘"),
        ("in", "received", "send more photos"),
        ("out", "sent", "okay love"))


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_flag_a_chat_and_run_from_it(panel_client, pg_pool, tenant, monkeypatch):
    ids = await seed_chat(pg_pool, tenant, messages=CHAT)
    path = "/api/finetune/sessions/acct/chats/501/example"
    empty = (await panel_client.get(path)).json()
    assert empty["example"] is None and [line["id"] for line in empty["lines"]] == ids

    bad = await panel_client.put(path, json={"verdict": "bad", "corrections": {"999": "x"}})
    assert bad.status_code == 400
    saved = await panel_client.put(path, json={
        "verdict": "bad", "client_kind": "time_waster", "outcome": "never_booked",
        "note": "Toms only collects photos.", "corrections": {str(ids[3]): "ask for his time instead"}})
    assert saved.status_code == 200, saved.text
    flagged = saved.json()
    example = flagged["example"]
    assert example["upto_message_id"] == ids[3] and example["name"] == f"{example['id']:02d}-bad"
    assert (await panel_client.get("/api/finetune/sessions/acct/flags")).json() == {"501": "bad"}
    preview = flagged["preview"]
    assert "Client: Hi, [client] here, free tonight? [number]" in preview
    assert "- Why: [client] only collects photos." in preview
    assert "Business: okay love\nANNOTATION FOR MODEL: ask for his time instead" in preview

    # Messages after the flag are not part of it until it is saved again.
    await seed_chat_message(pg_pool, tenant, "in", "received", "ok bye")
    again = (await panel_client.get(path)).json()
    assert again["newer"] == 1 and "ok bye" not in again["preview"]

    # Another client's account does not see it.
    other = await seed_session(pg_pool, "other", name="Ann")
    await seed_chat(pg_pool, other, session_id="other", chat_id=501, messages=CHAT[:1])
    assert (await panel_client.get("/api/finetune/sessions/other/chats/501/example")).json()["example"] is None

    seen = {}

    async def fake_complete_text(prompt, chats, **kwargs):
        seen.update(chats=list(chats), flagged=kwargs.get("flagged"))
        return answer()

    monkeypatch.setattr(finetune, "complete_text", fake_complete_text)
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    await write_template(panel_client)
    refused = await panel_client.post("/api/finetune/example-runs", json={"tenant_id": other,
                                                                          "example_ids": [example["id"]]})
    assert refused.status_code == 400
    started = await panel_client.post("/api/finetune/example-runs", json={"tenant_id": tenant,
                                                                          "example_ids": [example["id"]]})
    await asyncio.gather(*finetune_api._tasks)
    assert started.status_code == 200, started.text
    assert seen["flagged"] is True and [n for n, _ in seen["chats"]] == [example["name"]]
    assert "TIME-WASTER" in seen["chats"][0][1] and "ok bye" not in seen["chats"][0][1]
    run = (await panel_client.get(f"/api/finetune/runs/{started.json()['id']}")).json()
    assert run["source"] == "examples" and run["files"] == [example["name"]] and run["status"] == "done"
    assert "okay love" not in json.dumps(run)  # the chats are not copied into the run

    listed = (await panel_client.get(f"/api/finetune/examples?tenant_id={tenant}")).json()
    assert [(e["id"], e["display_name"], e["last_run_id"]) for e in listed] == [(example["id"], "Toms", run["id"])]
    # Judged again: news for the next run, and it now covers the newer message.
    resaved = (await panel_client.put(path, json={"verdict": "good", "client_kind": "serious"})).json()
    assert resaved["example"]["last_run_id"] is None and resaved["newer"] == 0 and "ok bye" in resaved["preview"]
    assert (await panel_client.get(f"/api/finetune/examples/{example['id']}")).json()["name"].endswith("-good")
    removed = await panel_client.delete(f"/api/finetune/examples/{example['id']}")
    assert removed.status_code == 200
    assert (await panel_client.get(path)).json()["example"] is None
    events = [e["event"] for e in await audit.list_events(pg_pool)]
    assert finetune_api.FINETUNE_EXAMPLE_SAVED in events and finetune_api.FINETUNE_EXAMPLE_REMOVED in events


async def seed_chat_message(pool, tenant, direction, status, text, session_id="acct", chat_id=501):
    return await pool.fetchval("INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text) "
                               "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                               session_id, tenant, chat_id, direction, status, text)


# ---------------------------------------------------------- hand-typed chats


def test_the_escort_template_learns_from_typed_replies_and_keeps_the_fixed_rules():
    escort = finetune.default_template("Escort")
    # The flow is learned; safety and the system's details are not.
    assert "THE FIXED RULES" in escort and "HOW SHE WORKS" in escort
    assert "the typed way wins" in escort and "The fixed rules and the boundaries never change." in escort
    for fixed in ("[address] alone on a line", "PHOTOS ONLY WHEN HE ASKS", "APPOINTMENTS notes",
                  "never as an agency", "not answered at all", "Adults only.",
                  # The booking flow of 2026-10-23.
                  "PRICES ONLY WHEN HE ASKS", "THE EXTRAS MESSAGE IS NEVER SENT", "a photo that shows he is at the place"):
        assert fixed in escort
    # Sounding like a person, and what the operator asked to see.
    assert "Never two clients the same text." in escort
    for note in ("THE USUAL QUESTIONS", "WHAT GETS THE BOOKING", "CHANGED:", "PHRASE GROUPS"):
        assert note in escort
    # The template sets no reply language: the platform does.
    assert "his language" not in escort


def test_a_typed_reply_keeps_its_mark_and_others_get_none():
    lines = finetune.chat_lines([
        {**msg(1, "in", "received", "hi"), "typed": False},
        {**msg(2, "out", "sent", "hey love"), "typed": True},
        {**msg(3, "out", "sent", "Hi my love ❤️"), "typed": False},
    ])
    assert [line.get("typed") for line in lines] == [None, True, False]
    # A flagged chat's lines carry no mark at all.
    assert "typed" not in finetune.chat_lines([msg(2, "out", "sent", "hey love")])[0]


def test_a_hand_typed_chat_marks_who_wrote_each_reply():
    from zoneinfo import ZoneInfo

    lines = finetune.chat_lines([
        {**msg(1, "in", "received", "Hi, Toms here, free now? +37126942162", at="2026-10-19T20:59:00+00:00"),
         "typed": False},
        {**msg(2, "out", "sent", "Hi my love ❤️ around what hour you come? 😘", at="2026-10-19T21:00:00+00:00"),
         "typed": False},
        {**msg(3, "out", "sent", "hey Toms 😘 yes free, 30 min 100", at="2026-10-19T21:01:00+00:00"),
         "typed": True},
        {**msg(4, "system", "note", "📅 Booking #3 requested.", at="2026-10-19T21:02:00+00:00"), "typed": False},
    ])
    text = finetune.render_hand_chat(lines, origin=finetune.hand_origin("pam", own=True),
                                     masks=finetune.masks_for(display_name="Toms"), tz=ZoneInfo("Europe/Riga"))
    assert text == (
        "FROM: pam (THIS business: its prices, services, hours and area count)\n\n"
        "— Mon 19.10.2026 —\n"
        "[23:59] Client: Hi, [client] here, free now? [number]\n"
        "— Tue 20.10.2026 —\n"
        "[00:00] Assistant: Hi my love ❤️ around what hour you come? 😘\n"
        f"[00:01] {finetune.TYPED}: hey [client] 😘 yes free, 30 min 100\n"
        "— booking: 📅 Booking #3 requested. —"
    )
    assert "never its prices" in finetune.hand_origin("leaui", own=False)


def test_a_long_hand_typed_chat_keeps_its_start():
    lines = finetune.chat_lines([{**msg(i, "in", "received", f"message {i} " + "x" * 50), "typed": False}
                                 for i in range(1, 200)])
    text = finetune.render_hand_chat(lines, origin="pam", max_chars=1_000)
    assert len(text) <= 1_000 and text.endswith(finetune.CUT)
    assert "message 1 " in text and "message 199" not in text
    assert not finetune.render_hand_chat(lines[:2], origin="pam").endswith(finetune.CUT)


def test_hand_typed_chats_are_ranked_named_and_picked_in_turns():
    def chat(tenant, typed, last, chars=100):
        return {"tenant_id": tenant, "typed": typed, "last_at": last, "chars": chars, "key": f"{tenant}:{typed}{last}"}

    chats = [chat(6, 2, "2026-10-19"), chat(6, 9, "2026-10-18"), chat(6, 2, "2026-10-20"), chat(6, 1, "2026-10-20"),
             chat(5, 4, "2026-10-19"), chat(8, 1, "2026-10-19")]
    planned = finetune.plan_hand_run(chats, [(6, "Pam"), (5, "leaui"), (8, "Pam")], max_chats=4)
    # Most typed replies first, then the newest; turns between accounts, the run's own first.
    assert [(c["name"], c["typed"], c["last_at"]) for c in planned] == [
        ("pam-01", 9, "2026-10-18"), ("leaui-01", 4, "2026-10-19"), ("pam8-01", 1, "2026-10-19"),
        ("pam-02", 2, "2026-10-20"), ("pam-03", 2, "2026-10-19"), ("pam-04", 1, "2026-10-20"),
    ]
    assert [c["picked"] for c in planned] == [True, True, True, True, False, False]
    # The character budget counts too: a chat that does not fit is skipped, a smaller one after it can be.
    sized = [chat(6, 3, "a", chars=60), chat(6, 2, "b", chars=50), chat(6, 1, "c", chars=30)]
    assert [c["picked"] for c in finetune.plan_hand_run(sized, [(6, "pam")], max_chars=100)] == [True, False, True]
    assert all("name" not in c for c in sized)  # the input is not changed


def test_hand_typed_chats_come_with_their_note():
    [message] = finetune.build_hand_messages("TEMPLATE", [("pam-01", "FROM: pam ...")])
    assert message["content"].startswith(f"TEMPLATE\n\n{finetune.TRANSCRIPT_NOTE}\n\n{finetune.HAND_NOTE}\n\n")
    assert message["content"].endswith("### pam-01\nFROM: pam ...")
    for rule in (finetune.TYPED, finetune.ASSISTANT, "the typed way wins", "PHRASE GROUPS", "is a mistake, not a pattern"):
        assert rule in finetune.HAND_NOTE


@pytest.mark.asyncio
async def test_hand_typed_chats_go_to_deepseek_with_their_note(monkeypatch):
    seen = {}

    async def fake_complete(**kwargs):
        seen.update(kwargs)
        return answer()

    monkeypatch.setattr(finetune.vision, "_complete", fake_complete)
    await finetune.complete_text("T", [("pam-01", "FROM: pam")], api_key="pk", model="deepseek-chat", hand=True)
    assert finetune.HAND_NOTE in seen["payload"]["messages"][0]["content"]
    assert finetune.EXAMPLES_NOTE not in seen["payload"]["messages"][0]["content"]


async def seed_sent(pool, tenant, session_id, chat_id, text, *, model=None, actor=None):
    """An outgoing message as the runtime stores it: AI-written ones carry
    the model; sends through the runtime leave an audit row naming who
    caused them; a reply typed on the phone leaves none."""
    async with pool.acquire() as con:
        message_id = await con.fetchval(
            "INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text, llm_model) "
            "VALUES ($1, $2, $3, 'out', 'sent', $4, $5) RETURNING id", session_id, tenant, chat_id, text, model)
        if actor:
            await audit.record(con, tenant_id=tenant, actor=actor, event=audit.MESSAGE_SENT,
                               payload={"message_id": message_id, "kind": "text"})
    return message_id


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_run_from_hand_typed_chats(panel_client, pg_pool, tenant, monkeypatch):
    await seed_chat(pg_pool, tenant, messages=(("in", "received", "Hi, Toms here, free now? +37126942162"),))
    await seed_sent(pg_pool, tenant, "acct", 501, "Hi my love ❤️ around what hour you come? 😘",
                    model="deepseek-chat", actor=audit.BOT)
    await seed_sent(pg_pool, tenant, "acct", 501, "hey Toms 😘 yes free, 30 min 100")  # on the phone
    await seed_sent(pg_pool, tenant, "acct", 501, "yes love, it's booked for 23:00", actor=audit.BOT)  # booking news
    await seed_sent(pg_pool, tenant, "acct", 501, "see you soon", actor=audit.ADMIN)  # by hand from the panel
    # A chat the bot answered alone is not a hand-typed chat.
    await seed_chat(pg_pool, tenant, chat_id=502, name="Bot only", messages=(("in", "received", "price?"),))
    await seed_sent(pg_pool, tenant, "acct", 502, "1h 200", model="deepseek-chat", actor=audit.BOT)
    other = await seed_session(pg_pool, "other", name="leaui")
    await seed_chat(pg_pool, other, session_id="other", chat_id=601, name="Ann",
                    messages=(("in", "received", "hello"),))
    await seed_sent(pg_pool, other, "other", 601, "hii babe")

    listed = (await panel_client.get(f"/api/finetune/hand-chats?tenant_id={tenant}")).json()
    [chat] = listed["chats"]
    assert (chat["key"], chat["name"], chat["picked"]) == (f"{tenant}:501", "mia-01", True)
    assert (chat["typed"], chat["assistant"], chat["client"]) == (2, 2, 1)
    assert chat["text"].splitlines()[0] == "FROM: " + finetune.hand_origin("Mia", own=True)
    assert f"{finetune.TYPED}: hey [client] 😘 yes free, 30 min 100" in chat["text"]
    assert f"{finetune.TYPED}: see you soon" in chat["text"]
    assert f"{finetune.ASSISTANT}: yes love, it's booked for 23:00" in chat["text"]
    assert "[number]" in chat["text"] and "Toms" not in chat["text"]
    assert listed["accounts"] == [{"tenant_id": tenant, "name": "Mia", "chats": 1, "typed": 2, "picked": 1}]

    both = (await panel_client.get(f"/api/finetune/hand-chats?tenant_id={tenant}&also={other}")).json()
    assert [(c["name"], c["account"]) for c in both["chats"]] == [("mia-01", "Mia"), ("leaui-01", "leaui")]
    assert "never its prices" in both["chats"][1]["text"].splitlines()[0]

    seen = {}

    async def fake_complete_text(prompt, chats, **kwargs):
        seen.update(chats=list(chats), hand=kwargs.get("hand"))
        return answer()

    monkeypatch.setattr(finetune, "complete_text", fake_complete_text)
    monkeypatch.delenv("DEEPSEEK_PLATFORM_KEY", raising=False)
    body = {"tenant_id": tenant, "also_tenant_ids": [other]}
    assert (await panel_client.post("/api/finetune/hand-runs", json=body)).status_code == 400  # no template
    await write_template(panel_client)
    refused = await panel_client.post("/api/finetune/hand-runs", json=body)
    assert refused.status_code == 400 and "DEEPSEEK_PLATFORM_KEY" in refused.json()["detail"]
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    unknown = await panel_client.post("/api/finetune/hand-runs", json={**body, "chats": [f"{tenant}:502"]})
    assert unknown.status_code == 400 and "Reload the page" in unknown.json()["detail"]

    started = await panel_client.post("/api/finetune/hand-runs", json=body)
    await asyncio.gather(*finetune_api._tasks)
    assert started.status_code == 200, started.text
    assert seen["hand"] is True and [n for n, _ in seen["chats"]] == ["leaui-01", "mia-01"]
    run = (await panel_client.get(f"/api/finetune/runs/{started.json()['id']}")).json()
    assert run["source"] == "hand" and run["files"] == ["leaui-01", "mia-01"] and run["status"] == "done"
    assert "hii babe" not in json.dumps(run)  # the chats are not copied into the run

    # Only the chats the admin kept.
    only = await panel_client.post("/api/finetune/hand-runs", json={**body, "chats": [f"{other}:601"]})
    await asyncio.gather(*finetune_api._tasks)
    assert only.status_code == 200 and [n for n, _ in seen["chats"]] == ["leaui-01"]

    # An account with no hand-typed chats has nothing to run on.
    empty = await seed_session(pg_pool, "empty", name="Eva")
    nothing = await panel_client.post("/api/finetune/hand-runs", json={"tenant_id": empty})
    assert nothing.status_code == 400 and "typed by hand" in nothing.json()["detail"]
