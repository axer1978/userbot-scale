"""The admin's reply examples (examples_api.py, Finetune → Reply examples):
what an account has, reading its chats now, wording the client side,
trying a message to see what it would find before switching retrieval on,
and switching a bad example off. Made-up chats; the embedder and DeepSeek
are stand-ins (test_examples.py)."""

from __future__ import annotations

import pytest
import pytest_asyncio

import audit
import examples
import examples_api
import tenants
from conftest import seed_session
from test_examples import ADDRESS, BEFORE_TIME, BOOKED, MODEL, NOTE_HEADER, WAY_IN, Chat, deepseek, embedder, example  # noqa: F401

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

PATH = "/api/finetune/reply-examples"


@pytest_asyncio.fixture
async def mia(pg_pool):
    tenant = await seed_session(pg_pool, "acct", name="Mia")
    await tenants.TenantStore(pg_pool).save_config(
        tenant, {"booking": {"address": ADDRESS, "arrival_instructions": WAY_IN}}, actor="admin")
    return tenant


async def chats(pool, tenant):
    toms = await Chat(pool, tenant, 501).open()
    await toms.say("client", "Hi, Toms here, how much for an hour?")
    await toms.say("hand", "hey Toms 😘 150 for an hour")
    await toms.say("client", "free now?")
    await toms.say("bot", "yes love, come")
    ann = await Chat(pool, tenant, 502, name="Ann").open()
    await ann.say("client", "where are you? send a photo")
    await ann.say("approved", "centre love, here 😘", draft="I am in the centre")


async def events(pool, name):
    return [e for e in await audit.list_events(pool) if e["event"] == name]


async def test_an_account_with_nothing_yet(panel_client, mia, embedder):
    response = await panel_client.get(f"{PATH}?tenant_id={mia}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {
        "total": 0, "active": 0, "with_figures": 0, "embedded": 0, "not_embedded": 0, "worded": 0, "wordings": 0,
        "by_source": {"hand": 0, "edited": 0, "flagged": 0},
        "by_phase": {"before_time": 0, "booked": 0, "after": 0}, "shared_in_industry": 0,
    }
    assert body["embedder"] == {"up": True, "model": MODEL}
    assert body["retrieval"] == {"enabled": False, "k": 3, "min_similarity": 0.55, "share_in_industry": True,
                                 "max_chars": 1200, "copy_guard": True}
    assert (body["in_hourly_round"], body["wordings_ready"], body["last_ingest_at"]) == (False, False, None)
    assert (await panel_client.get(f"{PATH}?tenant_id=999")).status_code == 404


async def test_rebuild_reads_the_chats_and_embeds_them(panel_client, pg_pool, mia, embedder):
    await chats(pg_pool, mia)

    response = await panel_client.post(f"{PATH}/rebuild", json={"tenant_id": mia})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ingest"]["added"] == 2 and body["ingest"]["skipped"] == {"bot": 1}
    assert body["ingest"]["more"] is False and body["embedded"] == 2
    assert body["counts"]["by_source"] == {"hand": 1, "edited": 1, "flagged": 0}
    assert (body["counts"]["embedded"], body["counts"]["with_figures"]) == (2, 1)
    assert body["last_ingest_at"] is not None
    [event] = await events(pg_pool, examples_api.REPLY_EXAMPLES_REBUILT)
    assert event["tenant_id"] == mia and event["payload"]["added"] == 2
    assert "Toms" not in str(event)  # counts only

    again = (await panel_client.post(f"{PATH}/rebuild", json={"tenant_id": mia, "from_start": True})).json()
    assert again["ingest"]["added"] == 0 and again["counts"]["total"] == 2


async def test_examples_are_listed_and_one_can_be_switched_off(panel_client, pg_pool, mia, embedder):
    await chats(pg_pool, mia)
    await panel_client.post(f"{PATH}/rebuild", json={"tenant_id": mia})

    listed = (await panel_client.get(f"{PATH}/items?tenant_id={mia}")).json()

    assert listed["total"] == 2
    assert [item["id"] for item in listed["items"]] == sorted((item["id"] for item in listed["items"]), reverse=True)
    hand = next(item for item in listed["items"] if item["source"] == "hand")
    edited = next(item for item in listed["items"] if item["source"] == "edited")
    assert (hand["chat_id"], hand["client_text"], hand["reply_text"], hand["has_figures"]) == (
        501, "Hi, [client] here, how much for an hour?", "hey [client] 😘 150 for an hour", True)
    assert (edited["reply_text"], edited["embedded"], edited["active"], edited["wordings"]) == (
        "centre love, here 😘", True, True, [])
    assert (await panel_client.get(f"{PATH}/items?tenant_id={mia}&source=edited")).json()["total"] == 1

    off = await panel_client.patch(f"{PATH}/{hand['id']}", json={"active": False})
    assert off.status_code == 200 and off.json()["active"] is False
    assert (await panel_client.get(f"{PATH}/items?tenant_id={mia}&active=true")).json()["total"] == 1
    tried = (await panel_client.post(f"{PATH}/try", json={"tenant_id": mia, "text": "how much?"})).json()
    assert hand["id"] not in [r["id"] for r in tried["results"]]
    [event] = await events(pg_pool, examples_api.REPLY_EXAMPLE_CHANGED)
    assert event["payload"] == {"example_id": hand["id"], "active": False}
    assert (await panel_client.patch(f"{PATH}/999999", json={"active": False})).status_code == 404


async def test_trying_a_message_shows_what_the_bot_would_find(panel_client, pg_pool, mia, embedder):
    price = await example(pg_pool, mia, "how much for an hour?", "150 for an hour love", chat_id=501)
    both = await example(pg_pool, mia, "price now?", "for now 100, come", chat_id=502)
    await example(pg_pool, mia, "where are you?", "centre love", chat_id=503)
    await example(pg_pool, mia, "how much again?", "as agreed love", chat_id=504, phase=BOOKED)
    other = await seed_session(pg_pool, "other", name="Leaui")
    shared = await example(pg_pool, other, "how much love?", "depends on time 😘", chat_id=601)
    elsewhere = await tenants.TenantStore(pg_pool).create_industry("Salons", actor="admin")
    far = await seed_session(pg_pool, "far", name="Salon", industry_id=elsewhere["id"])
    await example(pg_pool, far, "what's the price?", "a haircut is nice", industry=elsewhere["id"])

    response = await panel_client.post(f"{PATH}/try", json={"tenant_id": mia, "text": "what's the price?"})

    assert response.status_code == 200, response.text
    body = response.json()
    results = body["results"]
    assert [r["id"] for r in results][:3] == [shared, price, both]
    assert [r["similarity"] for r in results] == sorted((r["similarity"] for r in results), reverse=True)
    first = next(r for r in results if r["id"] == price)
    assert (first["client_text"], first["reply_text"], first["source"], first["phase"], first["own"],
            first["chat"], first["matched"], first["generated"]) == (
        "how much for an hour?", "150 for an hour love", "hand", BEFORE_TIME, True, "Mia, chat 501",
        "how much for an hour?", False)
    assert next(r for r in results if r["id"] == shared)["own"] is False
    # The note takes the best three above the threshold; another industry's never.
    assert sum(r["shown"] for r in results) == 3 and all(r["account"] != "Salon" for r in results)
    assert body["note"].startswith(NOTE_HEADER) and body["model"] == MODEL
    assert body["retrieval"]["enabled"] is False  # switched off, and still tried

    booked = (await panel_client.post(f"{PATH}/try", json={"tenant_id": mia, "text": "price?", "phase": "booked"}))
    assert [r["phase"] for r in booked.json()["results"]] == [BOOKED]
    anyphase = (await panel_client.post(f"{PATH}/try", json={"tenant_id": mia, "text": "price?", "phase": None}))
    assert BOOKED in {r["phase"] for r in anyphase.json()["results"]}
    assert (await panel_client.post(f"{PATH}/try", json={"tenant_id": mia, "text": "x", "phase": "nope"})
            ).status_code == 400

    embedder["down"] = True
    down = await panel_client.post(f"{PATH}/try", json={"tenant_id": mia, "text": "what's the price?"})
    assert down.status_code == 503


async def test_wordings_need_the_platform_key_and_the_embedder(panel_client, pg_pool, mia, embedder, deepseek,
                                                               monkeypatch):
    await chats(pg_pool, mia)
    await panel_client.post(f"{PATH}/rebuild", json={"tenant_id": mia})
    monkeypatch.delenv("DEEPSEEK_PLATFORM_KEY", raising=False)

    refused = await panel_client.post(f"{PATH}/wordings", json={"tenant_id": mia})
    assert refused.status_code == 400 and "DEEPSEEK_PLATFORM_KEY" in refused.json()["detail"]

    monkeypatch.setenv("DEEPSEEK_PLATFORM_KEY", "pk")
    embedder["down"] = True
    assert (await panel_client.post(f"{PATH}/wordings", json={"tenant_id": mia})).status_code == 503
    embedder["down"] = False

    response = await panel_client.post(f"{PATH}/wordings", json={"tenant_id": mia, "limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == {"examples": 1, "wordings": 2, "calls": 1, "error": ""}
    assert (body["counts"]["worded"], body["counts"]["wordings"], body["wordings_ready"]) == (1, 2, True)
    [event] = await events(pg_pool, examples_api.REPLY_EXAMPLES_WORDED)
    assert event["payload"] == {"examples": 1, "wordings": 2, "calls": 1}
    listed = (await panel_client.get(f"{PATH}/items?tenant_id={mia}")).json()["items"]
    assert sorted(len(item["wordings"]) for item in listed) == [0, 2]


async def test_the_hourly_round_is_shown(panel_client, pg_pool, mia, embedder):
    store = tenants.TenantStore(pg_pool)
    overrides = (await store.get(mia))["config_json"]
    other = await seed_session(pg_pool, "other", name="Leaui")
    await store.save_config(other, {"retrieval": {"enabled": True}}, actor="admin")

    body = (await panel_client.get(f"{PATH}?tenant_id={mia}")).json()

    # Mia's own retrieval is off, but her industry uses it: her chats are read too.
    assert body["retrieval"]["enabled"] is False and body["in_hourly_round"] is True
    assert await examples.accounts(pg_pool) == [mia, other]
    assert overrides["booking"]["address"] == ADDRESS
