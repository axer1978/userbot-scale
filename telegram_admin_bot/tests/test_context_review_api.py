"""Context review, the API (context_review_api.py, migration 0024): how a
reply is built for a real account in the database, the chats a review would
read, and a review start to end with the model faked: done with its numbers
and the checked report, failed when the answer is not JSON or the panel
restarted, one at a time per account, removed; audit rows with ids and
counts only, the spend metered as the platform's; and admin only.

All data is made up: the names, numbers and addresses below are fakes. No
real model is called."""

from __future__ import annotations

import asyncio
import json
from functools import partial

import httpx
import pytest
import pytest_asyncio

import audit
import context_review
import context_review_api as api
import finetune_api
from conftest import seed_session
from test_staff import fresh_state, staff_member  # noqa: F401  (fresh_state is an autouse fixture)

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

SECTIONS = {"about": "Independent companion.", "services": "1 hour 150.", "hours_location": "Centre.",
            "booking": "Ask for his time first.", "faq": "Parking in the yard.", "tone": "Warm.",
            "boundaries": "Adults only.", "sign_off": "kisses", "writing_samples": "hi love 😘"}
ADDRESS = "Fake Street 12"
# What must never leave the chats: the client's name (in any case) and number, the address, the way in.
SECRETS = ("Janis", "JANIS", "janis", "Ozols", "37120001122", "Fake Street", "door code", "4321")
ROUTES = (("GET", "/api/finetune/context?tenant_id=1"), ("GET", "/api/finetune/review-chats?tenant_id=1"),
          ("GET", "/api/finetune/reviews"), ("POST", "/api/finetune/reviews"), ("GET", "/api/finetune/reviews/1"),
          ("DELETE", "/api/finetune/reviews/1"))


@pytest_asyncio.fixture
async def accounts(panel_client, pg_pool):
    """pam (bookings on, her own booking box over the industry's) and leaui, in the one industry."""
    pam = await seed_session(pg_pool, "acct", name="pam")
    leaui = await seed_session(pg_pool, "other", name="leaui")
    r = await panel_client.put("/api/industries/1/template", json={"sections": SECTIONS, "note": "test"})
    assert r.status_code == 200, r.text
    r = await panel_client.put(f"/api/tenants/{pam}/prompt", json={
        "overrides": {"booking": {"mode": "override", "text": "Her own old booking steps."}}, "addendum": ""})
    assert r.status_code == 200, r.text
    # Straight into the row: saving it through the API would ask a running account to resend bookings.
    await pg_pool.execute("UPDATE tenants SET config_json = $2::jsonb WHERE id = $1", pam, json.dumps({"booking": {
        "enabled": True, "provider": "@her_booker", "address": ADDRESS, "arrival_instructions": "door code 4321"}}))
    return pam, leaui


async def chat(pool, session, chat_id, name, *, username=None):
    await pool.execute("INSERT INTO conversations (session_id, chat_id, display_name, username) "
                       "VALUES ($1, $2, $3, $4)", session, chat_id, name, username)


async def say(pool, session, chat_id, direction, status, text, *, ago, model=None, draft=None, actor=None,
              reason=""):
    """A message `ago` before now; `actor`: who sent it through the runtime (its message_sent audit row,
    with `reason`)."""
    message_id = await pool.fetchval(
        "INSERT INTO messages (session_id, chat_id, direction, status, text, llm_model, draft_text, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, now() - $8::text::interval) RETURNING id",
        session, chat_id, direction, status, text, model, draft, ago)
    if actor:
        tenant = await pool.fetchval("SELECT tenant_id FROM messages WHERE id = $1", message_id)
        await audit.record(pool, tenant_id=tenant, actor=actor, event=audit.MESSAGE_SENT, reason=reason,
                           payload={"message_id": message_id, "kind": "text"})
    return message_id


async def booking(pool, tenant, session, chat_id, state):
    await pool.execute(
        "INSERT INTO bookings (tenant_id, session_id, number, chat_id, customer_ref, starts_at, ends_at, "
        "blocked_until, tz, state) VALUES ($1, $2, (SELECT coalesce(max(number), 0) + 1 FROM bookings "
        "WHERE tenant_id = $1), $3, 'ref', now() + interval '1 day', now() + interval '25 hours', "
        "now() + interval '25 hours', 'Europe/Riga', $4)",
        tenant, session, chat_id, state)


@pytest_asyncio.fixture
async def chats(pg_pool, accounts):
    """pam: one chat that booked (bot, edited draft, typed by hand, the system's address; taken over), one the
    bot left unanswered, one too old, and the booker's own chat; leaui: one chat."""
    pam, leaui = accounts
    await chat(pg_pool, "acct", 501, "Janis Ozols", username="janis_lv")
    await say(pg_pool, "acct", 501, "in", "received", "Hi, Janis here, free tonight? +37120001122", ago="2 days")
    await say(pg_pool, "acct", 501, "out", "sent", "Hi love what time do you want to come?", ago="47 hours",
              model="deepseek-chat", actor=audit.BOT)
    await say(pg_pool, "acct", 501, "out", "rejected", "a draft thrown away", ago="47 hours", model="deepseek-chat")
    await say(pg_pool, "acct", 501, "in", "received", "22:00 please, JANIS", ago="46 hours")
    await say(pg_pool, "acct", 501, "out", "sent", "22 is perfect love", ago="46 hours", model="deepseek-chat",
              draft="22:00 ok Janis, come to Fake Street 12", actor=audit.ADMIN)
    await say(pg_pool, "acct", 501, "out", "sent", ADDRESS, ago="46 hours", actor=audit.SYSTEM)
    await say(pg_pool, "acct", 501, "out", "sent", "see you soon 😘 door code 4321", ago="45 hours")  # on the phone
    await booking(pg_pool, pam, "acct", 501, "confirmed")
    await audit.record(pg_pool, tenant_id=pam, actor=audit.ADMIN, event=audit.HUMAN_TAKEOVER,
                       payload={"chat_id": 501, "until": "later"})

    await chat(pg_pool, "acct", 502, "+37120001122")
    await say(pg_pool, "acct", 502, "out", "sent", "Hi love what time do you want to come?", ago="5 hours",
              model="deepseek-chat", actor=audit.BOT)
    await say(pg_pool, "acct", 502, "in", "received", "how much for 1 hour?", ago="3 hours")
    await chat(pg_pool, "acct", 503, "Old Client")
    await say(pg_pool, "acct", 503, "in", "received", "an old question", ago="40 days")
    await chat(pg_pool, "acct", 504, "Her Booker", username="her_booker")
    await say(pg_pool, "acct", 504, "in", "received", "YES 1", ago="1 day")

    await chat(pg_pool, "other", 601, "Anna")
    await say(pg_pool, "other", 601, "in", "received", "hello, are you free?", ago="26 hours")
    await say(pg_pool, "other", 601, "out", "sent", "Hi love what time do you want to come?", ago="25 hours",
              model="deepseek-chat", actor=audit.BOT)
    return pam, leaui


# The model writes a name, a number, the address and the way in anyway: none of it may be stored.
ANSWER = {
    "summary": "Clients ask the price first; answer it in one line.",
    "questions": [{"question": "How much?", "chats": ["C2", "C9"],
                   "wordings": ["how much for 1 hour?", "Janis here, free tonight?"],
                   "her_answer": "", "bot_answer": "", "covered_by": "services", "problem": "+37120001122 asked twice",
                   "suggested": {"box": "services", "line": "Never send Fake Street 12 before the time."}}],
    "booking": {"works": [{"point": "Asked for the time.", "chats": ["C1"]}], "loses": [], "speed": "Fast."},
    "wrong": [], "phrases": [{"text": "door code 4321", "note": "Never type it.", "chats": ["C1"]}],
    "boxes": [{"boxes": ["booking"], "problem": "Old flow.", "fix": "Use the industry's."}],
}


@pytest.fixture
def model(monkeypatch):
    """The model call, faked: records what it was given, meters a call, and answers `reply`."""
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    seen = {"reply": json.dumps(ANSWER), "gate": None}

    async def fake_complete(messages, *, api_key, model, usage_sink=None, client=None):
        seen.update(messages=messages, api_key=api_key, model=model)
        if seen["gate"] is not None:
            await seen["gate"].wait()
        await usage_sink(model, {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 900,
                                 "completion_tokens": 300})
        return seen["reply"]

    monkeypatch.setattr(api, "complete", fake_complete)
    return seen


async def start(client, tenant, **body):
    response = await client.post("/api/finetune/reviews", json={"tenant_id": tenant, **body})
    await asyncio.gather(*api._tasks)
    return response


# ------------------------------------------------------- how a reply is built


async def test_the_context_of_an_account_with_its_own_text_over_the_industrys(panel_client, accounts):
    pam, _ = accounts
    r = await panel_client.get(f"/api/finetune/context?tenant_id={pam}")
    assert r.status_code == 200, r.text
    view = r.json()
    pieces = {p["key"]: p for p in view["pieces"]}
    assert view["account"] == "pam" and view["industry"]["template_version"] == 2 and view["pinned"] is None
    assert pieces["section:booking"]["source"] == "client"
    assert pieces["section:booking"]["inherited"] == SECTIONS["booking"]
    assert pieces["section:about"]["source_label"] == "Industry General v2"
    assert pieces["appointments"]["enabled"] and not pieces["media"]["enabled"]  # no files in the library
    assert any(w["level"] == "warn" and "replaces the industry's HOW BOOKING WORKS" in w["text"]
               for w in view["warnings"])
    assert view["system_text"].startswith("PLATFORM RULES") and ADDRESS not in json.dumps(view)
    assert (await panel_client.get("/api/finetune/context?tenant_id=999")).status_code == 404


# ------------------------------------------------------------------ the chats


async def test_the_chats_a_review_would_read(panel_client, chats):
    pam, leaui = chats
    r = await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}&also={leaui}&days=30")
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["days"] == 30 and preview["limits"] == {"chats": 100, "chars": 100_000}
    assert preview["accounts"] == [{"tenant_id": pam, "name": "pam", "chats": 2, "picked": 2},
                                   {"tenant_id": leaui, "name": "leaui", "chats": 1, "picked": 1}]
    by_chat = {c["chat_id"]: c for c in preview["chats"]}
    assert set(by_chat) == {501, 502, 601}  # not the old one, not the booker's chat
    booked, unanswered, other = by_chat[501], by_chat[502], by_chat[601]
    assert [c["ref"] for c in preview["chats"]] == ["C1", "C2", "C3"]
    assert (booked["ref"], booked["outcome"], booked["messages"]) == ("C1", "booked", 6)
    assert (unanswered["outcome"], unanswered["outcome_label"]) == ("unanswered", "his last message got no answer")
    assert other["ref"] == "C2" and other["account"] == "leaui" and unanswered["ref"] == "C3"
    assert set(booked) == {"key", "ref", "tenant_id", "session_id", "chat_id", "account", "display_name", "outcome",
                           "outcome_label", "messages", "chars", "picked", "text"}
    text = booked["text"]
    assert booked["chars"] == len(text) and booked["session_id"] == "acct" and booked["key"] == f"{pam}:501"
    assert text.startswith("### C1 — pam — 6 messages — outcome: booked — first answer after ")
    assert text.splitlines()[0].endswith(" — taken over by a person")
    assert "Bot: Hi love what time do you want to come?" in text
    assert ("Bot (edited by her before sending; the bot had written: «22:00 ok [client], come to [address]»): "
            "22 is perfect love") in text
    assert "System: [address]" in text and "Her (typed by hand): see you soon 😘 [way in]" in text
    assert "Client: 22:00 please, [client]" in text
    assert "Client: Hi, [client] here, free tonight? [number]" in text and "thrown away" not in text
    for secret in SECRETS:
        assert all(secret not in c["text"] for c in preview["chats"]), secret

    only = (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}")).json()
    assert [c["chat_id"] for c in only["chats"]] == [501, 502]
    assert (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}&also=999")).status_code == 400
    assert (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}&days=5")).status_code == 422


# -------------------------------------------------------------------- reviews


async def test_a_review_start_to_end(panel_client, pg_pool, chats, model):
    pam, leaui = chats
    started = await start(panel_client, pam, also_tenant_ids=[leaui, pam, leaui], days=30)
    assert started.status_code == 200, started.text
    review_id = started.json()["id"]
    review = (await panel_client.get(f"/api/finetune/reviews/{review_id}")).json()
    assert review["status"] == "done" and review["error"] == "" and review["raw_output"] == ""
    assert (review["tenant_id"], review["also_tenant_ids"], review["days"]) == (pam, [leaui], 30)
    assert review["chats_read"] == 3
    assert review["model"] == "deepseek-chat" and review["created_by"] == "admin" and review["finished_at"]
    assert review["chats"][0] == {"ref": "C1", "tenant_id": pam, "session_id": "acct", "chat_id": 501, "account": "pam",
                                  "outcome": "booked", "outcome_label": "booked"}
    assert [s["key"] for s in review["sections"]][-3:] == ["writing_samples", "addendum", "rules"]

    # The report, checked: the ref the review did not read is dropped; what names someone is masked.
    [question] = review["report"]["questions"]
    assert question["chats"] == ["C2"] and question["chat_count"] == 1 and question["covered_by"] == "services"
    assert question["wordings"] == ["how much for 1 hour?", "[client] here, free tonight?"]
    assert question["problem"] == "[number] asked twice"
    assert question["suggested"] == {"box": "services", "line": "Never send [address] before the time."}
    assert review["report"]["phrases"][0]["text"] == "[way in]"
    assert review["errors"] == ["questions #1: dropped chat ref C9 (not in this review)."]
    assert review["report"]["booking"]["works"][0]["chat_count"] == 1

    # The numbers, from the database.
    counts = review["counts"]
    assert counts["window"]["days"] == 30
    assert counts["chats"] == {"in_window": 3, "read": 3, "by_account": [
        {"tenant_id": pam, "name": "pam", "in_window": 2, "read": 2},
        {"tenant_id": leaui, "name": "leaui", "in_window": 1, "read": 1}]}
    assert counts["outcomes"] == {"booked": 1, "booked_no_show": 0, "not_booked": 0, "waiting": 0, "went_quiet": 1,
                                  "unanswered": 1, "open": 0}
    assert counts["lines"] == {"client": 4, "bot": 3, "bot_edited": 1, "by_hand": 1, "system": 1}
    pam_quality = counts["quality"][0]
    assert pam_quality["tenant_id"] == pam and pam_quality["takeovers"] == 1
    assert set(pam_quality) == {"tenant_id", "name", "replies_written", "drafts", "takeovers", "unanswered_by_reason"}
    assert counts["phrases"] == [{"text": "Hi love what time do you want to come?", "chats": 3, "times": 3}]

    # What the model read: the prompt, pam's boxes, the chats, masked.
    system, user = model["messages"]
    assert system["role"] == "system" and "one JSON object" in system["content"] and model["api_key"] == "pk"
    assert "## booking — HOW BOOKING WORKS (own text, replaces the industry's)" in user["content"]
    assert "### C1 — pam" in user["content"] and "### C2 — leaui" in user["content"]
    for secret in SECRETS:
        assert secret not in user["content"], secret

    # Platform spend, metered; the audit rows hold ids and counts only.
    assert [tuple(r) for r in await pg_pool.fetch("SELECT tenant_id, purpose, completion_tokens FROM llm_usage")] == [
        (None, "context_review", 300)]
    [started_event] = [e for e in await audit.list_events(pg_pool) if e["event"] == api.CONTEXT_REVIEW_STARTED]
    assert started_event["payload"] == {"review_id": review_id, "also_tenant_ids": [leaui], "days": 30, "chats": 3}

    listed = (await panel_client.get(f"/api/finetune/reviews?tenant_id={pam}")).json()
    assert [r["id"] for r in listed] == [review_id]
    assert set(listed[0]) == {"id", "tenant_id", "also_tenant_ids", "days", "status", "model", "chats_read",
                              "created_at", "finished_at", "error"}
    assert (await panel_client.get(f"/api/finetune/reviews?tenant_id={leaui}")).json() == []
    stored = json.dumps([dict(r) for r in await pg_pool.fetch("SELECT * FROM context_reviews")], default=str)
    shown = json.dumps(review) + json.dumps(listed)

    removed = await panel_client.delete(f"/api/finetune/reviews/{review_id}")
    assert removed.json() == {"removed": review_id}
    assert (await panel_client.get(f"/api/finetune/reviews/{review_id}")).status_code == 404
    events = [e for e in await audit.list_events(pg_pool) if e["event"].startswith("context_review_")]
    assert [e["payload"] for e in events if e["event"] == api.CONTEXT_REVIEW_REMOVED] == [{"review_id": review_id}]
    payloads = json.dumps([e["payload"] for e in events]) + stored + shown
    for secret in (*SECRETS, "22 is perfect", "see you soon"):
        assert secret not in payloads, secret


async def test_an_answer_that_is_not_json_fails_the_review_and_is_kept(panel_client, chats, model, monkeypatch):
    pam, _ = chats
    model["reply"] = "Sorry, I cannot help with Janis at Fake Street 12."
    review = (await panel_client.get(f"/api/finetune/reviews/{(await start(panel_client, pam)).json()['id']}")).json()
    assert review["status"] == "failed" and review["error"] == api.UNREADABLE
    # Kept for the admin, masked like everything the model wrote.
    assert review["raw_output"] == "Sorry, I cannot help with [client] at [address]." and review["report"] is None
    assert review["errors"] == ["The answer holds no JSON object."] and review["counts"]["chats"]["read"] == 2

    import vision

    async def refused(*args, **kwargs):
        raise vision.VisionError("The DeepSeek API rejected the key (HTTP 401).")

    monkeypatch.setattr(api, "complete", refused)
    review = (await panel_client.get(f"/api/finetune/reviews/{(await start(panel_client, pam)).json()['id']}")).json()
    assert (review["status"], review["error"], review["raw_output"]) == (
        "failed", "The DeepSeek API rejected the key (HTTP 401).", "")

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(api, "complete", slow)
    monkeypatch.setattr(api, "MODEL_TIMEOUT_SECONDS", 0.05)
    review = (await panel_client.get(f"/api/finetune/reviews/{(await start(panel_client, pam)).json()['id']}")).json()
    assert (review["status"], review["error"]) == ("failed", api.TIMED_OUT)


async def test_one_review_at_a_time_per_account(panel_client, chats, model):
    pam, leaui = chats
    model["gate"] = asyncio.Event()
    first = await panel_client.post("/api/finetune/reviews", json={"tenant_id": pam})
    assert first.status_code == 200 and first.json()["status"] == "running"
    again = await panel_client.post("/api/finetune/reviews", json={"tenant_id": pam})
    assert again.status_code == 409
    busy = await panel_client.delete(f"/api/finetune/reviews/{first.json()['id']}")
    assert busy.status_code == 409
    # Another account is not held up.
    other = await panel_client.post("/api/finetune/reviews", json={"tenant_id": leaui})
    assert other.status_code == 200, other.text
    model["gate"].set()
    await asyncio.gather(*api._tasks)
    assert (await panel_client.get(f"/api/finetune/reviews/{first.json()['id']}")).json()["status"] == "done"


async def test_a_review_cut_off_by_a_restart_is_failed_when_read(panel_client, pg_pool, accounts):
    pam, _ = accounts
    stale = await pg_pool.fetchval(
        "INSERT INTO context_reviews (tenant_id, days, model, created_by, created_at) "
        "VALUES ($1, 30, 'deepseek-chat', 'admin', now() - interval '20 minutes') RETURNING id", pam)
    fresh = await pg_pool.fetchval("INSERT INTO context_reviews (tenant_id, days, model, created_by) "
                                   "VALUES ($1, 30, 'deepseek-chat', 'admin') RETURNING id", pam)
    listed = {r["id"]: r for r in (await panel_client.get(f"/api/finetune/reviews?tenant_id={pam}")).json()}
    assert (listed[stale]["status"], listed[stale]["error"]) == ("failed", api.STALE_ERROR)
    assert listed[stale]["finished_at"] and listed[fresh]["status"] == "running"
    assert (await panel_client.delete(f"/api/finetune/reviews/{stale}")).status_code == 200


async def test_a_review_says_what_is_missing(panel_client, pg_pool, chats, monkeypatch):
    pam, _ = chats
    monkeypatch.delenv("DEEPSEEK_PLATFORM_KEY", raising=False)
    refused = await panel_client.post("/api/finetune/reviews", json={"tenant_id": pam})
    assert refused.status_code == 400
    assert refused.json()["detail"] == "DEEPSEEK_PLATFORM_KEY must be set in .env to read chats."
    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    unknown = await panel_client.post("/api/finetune/reviews", json={"tenant_id": pam, "also_tenant_ids": [999]})
    assert unknown.status_code == 400 and "#999 was not found" in unknown.json()["detail"]
    empty = await seed_session(pg_pool, "empty", name="eva")
    nothing = await panel_client.post("/api/finetune/reviews", json={"tenant_id": empty})
    assert nothing.status_code == 400 and "No chats with a client message in the last 30 days" in nothing.json()[
        "detail"]
    for body in ({"tenant_id": pam, "days": 3}, {"tenant_id": pam, "also_tenant_ids": list(range(11))}):
        assert (await panel_client.post("/api/finetune/reviews", json=body)).status_code == 422
    assert (await panel_client.post("/api/finetune/reviews", json={"tenant_id": 999})).status_code == 404
    assert await pg_pool.fetchval("SELECT count(*) FROM context_reviews") == 0


async def test_an_answer_of_the_wrong_shape_or_a_failing_check_never_leaves_the_review_running(
        panel_client, chats, model, monkeypatch):
    pam, _ = chats
    model["reply"] = json.dumps({"summary": "s", "questions": [{"question": "q", "chats": ["C1"], "wordings": 2}]})
    review = (await panel_client.get(f"/api/finetune/reviews/{(await start(panel_client, pam)).json()['id']}")).json()
    assert review["status"] == "done" and review["report"]["questions"][0]["wordings"] == []
    assert "questions #1: its wordings are not a list; left out." in review["errors"]

    def broken(raw, refs):
        raise RuntimeError("a bug in the check")

    working = context_review.parse_report
    monkeypatch.setattr(context_review, "parse_report", broken)
    model["reply"] = '{"summary": "Janis"}'
    review = (await panel_client.get(f"/api/finetune/reviews/{(await start(panel_client, pam)).json()['id']}")).json()
    assert (review["status"], review["error"]) == ("failed", api.UNCHECKED)
    assert review["raw_output"] == '{"summary": "[client]"}'
    # Nothing is left running: the next review starts at once.
    monkeypatch.setattr(context_review, "parse_report", working)
    assert (await start(panel_client, pam)).status_code == 200


async def test_an_answer_cut_off_by_the_token_limit_fails_with_advice(panel_client, chats, model):
    pam, _ = chats
    model["reply"] = json.dumps(ANSWER)[:300]
    review = (await panel_client.get(f"/api/finetune/reviews/{(await start(panel_client, pam)).json()['id']}")).json()
    assert (review["status"], review["error"]) == ("failed", api.CUT_OFF)
    assert review["errors"] == [context_review.CUT_OFF] and review["raw_output"].startswith('{"summary"')


async def test_two_clicks_at_once_start_one_review(panel_client, chats, model):
    pam, _ = chats
    model["gate"] = asyncio.Event()
    first, second = await asyncio.gather(panel_client.post("/api/finetune/reviews", json={"tenant_id": pam}),
                                         panel_client.post("/api/finetune/reviews", json={"tenant_id": pam}))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    model["gate"].set()
    await asyncio.gather(*api._tasks)


async def test_a_long_chat_is_read_from_its_newest_messages_to_its_end(panel_client, pg_pool, accounts,
                                                                         monkeypatch):
    pam, _ = accounts
    await chat(pg_pool, "acct", 701, "Long Talker")
    rows = [("acct", 701, "in" if i % 2 == 0 else "out", "received" if i % 2 == 0 else "sent",
             f"message {i} " + "words " * 6, None if i % 2 == 0 else "deepseek-chat", f"{130 + (330 - i) * 20} minutes")
            for i in range(330)]
    rows.append(("acct", 701, "in", "received", "the very last question", None, "2 hours"))
    await pg_pool.executemany(
        "INSERT INTO messages (session_id, chat_id, direction, status, text, llm_model, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, now() - $7::text::interval)", rows)
    long = (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}")).json()["chats"][0]
    assert (long["chat_id"], long["messages"], long["outcome"]) == (701, 331, "unanswered")
    assert "Client: the very last question" in long["text"] and "messages left out" in long["text"]
    # Only the newest rows of a chat are read: its end says how it stands.
    monkeypatch.setattr(finetune_api, "_RAW_ROWS", 100)
    newest = (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}")).json()["chats"][0]
    assert newest["messages"] == 100 and newest["outcome"] == "unanswered"
    assert "Client: the very last question" in newest["text"] and "message 100 " not in newest["text"]


async def test_each_message_of_a_reply_says_whose_it_is(panel_client, pg_pool, accounts):
    pam, _ = accounts
    approved, auto = context_review.APPROVED_DRAFT, context_review.AUTOMATIC_REPLY
    await chat(pg_pool, "acct", 801, "Someone")
    await say(pg_pool, "acct", 801, "in", "received", "price?", ago="3 hours")
    await say(pg_pool, "acct", 801, "out", "sent", "hi love", ago="179 minutes", model="deepseek-chat",
              actor=audit.BOT, reason=auto)
    await say(pg_pool, "acct", 801, "out", "sent", "[sent photo #1: beach]", ago="179 minutes", actor=audit.BOT,
              reason=auto)
    await say(pg_pool, "acct", 801, "in", "received", "and 2 hours?", ago="170 minutes")
    # She changed the draft and approved it: it went out as two messages and a photo.
    draft = await say(pg_pool, "acct", 801, "out", "sent", "2 hours 250", ago="160 minutes", model="deepseek-chat",
                      draft="2h 300", actor=audit.ADMIN, reason=approved)
    await say(pg_pool, "acct", 801, "out", "sent", "come at 22?", ago="159 minutes", model="deepseek-chat",
              actor=audit.ADMIN, reason=approved)
    await say(pg_pool, "acct", 801, "out", "sent", "[sent photo #2: dress]", ago="159 minutes", actor=audit.ADMIN,
              reason=approved)
    await audit.record(pg_pool, tenant_id=pam, actor=audit.ADMIN, event=audit.DRAFT_APPROVED, reason=approved,
                       payload={"chat_id": 801, "message_id": draft, "edited": True})
    await say(pg_pool, "acct", 801, "out", "sent", "kisses", ago="150 minutes", actor=audit.ADMIN,
              reason="sent from the panel")
    [chat_801] = (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}")).json()["chats"]
    lines = chat_801["text"].splitlines()
    assert [line.split("] ", 1)[1] for line in lines if line.startswith("[")] == [
        "Client: price?", "Bot: hi love", "Bot: [sent photo #1: beach]", "Client: and 2 hours?",
        "Bot (edited by her before sending; the bot had written: «2h 300»): 2 hours 250",
        f"{context_review.EDITED_MORE}: come at 22?", f"{context_review.EDITED_MORE}: [sent photo #2: dress]",
        "Her (typed by hand): kisses"]


async def test_only_the_picked_chats_carry_their_text(panel_client, chats, monkeypatch):
    pam, leaui = chats
    monkeypatch.setattr(api.context_review, "plan", partial(context_review.plan, max_chats=1))
    preview = (await panel_client.get(f"/api/finetune/review-chats?tenant_id={pam}&also={leaui}")).json()
    picked = [c for c in preview["chats"] if c["picked"]]
    rest = [c for c in preview["chats"] if not c["picked"]]
    assert len(picked) == 1 and picked[0]["text"].startswith("### C1") and len(rest) == 2
    assert all(c["text"] == "" and c["chars"] > 0 and c["ref"] is None for c in rest)


# ------------------------------------------------------------------ admin only


async def test_only_the_admin_reaches_the_context_review(panel_client, pg_pool, accounts):
    import panel  # it reads its settings when imported: panel_client sets them first

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel.app), base_url="http://test") as anon:
        for method, path in ROUTES:
            assert (await anon.request(method, path)).status_code == 401, (method, path)
    # A manager whose role opens the admin panel never reaches /api/finetune/.
    async with staff_member(panel_client, pg_pool, "Senior moderator", admin_panel_login=True) as manager:
        assert (await manager.get("/api/sessions")).status_code == 200  # the login is live
        for method, path in ROUTES:
            r = await manager.request(method, path, json={"tenant_id": 1} if method == "POST" else None)
            assert r.status_code == 403, (method, path, r.status_code)
