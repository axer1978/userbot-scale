"""Review batches (review_api.py): what goes into a batch, the context each
reply carries, decisions, the JSONL export, and that one client's batch
never holds another client's messages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import review_api
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

UTC = timezone.utc
CHAT = 100
# Asia/Tokyo is UTC+9 all year: the local day 2026-09-10 is
# [2026-09-09 15:00Z, 2026-09-10 15:00Z).
DAY = "2026-09-10"
DAY_START = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
DAY_END = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


async def add(pool, session_id, tenant_id, *, direction, status, text, at, chat=CHAT, model=None) -> int:
    return await pool.fetchval(
        "INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text, created_at, llm_model) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
        session_id, tenant_id, chat, direction, status, text, at, model,
    )


def ai(pool, session_id, tenant_id, text, at, chat=CHAT):
    return add(pool, session_id, tenant_id, direction="out", status="sent", text=text, at=at, chat=chat,
               model="deepseek-chat")


def inc(pool, session_id, tenant_id, text, at, chat=CHAT):
    return add(pool, session_id, tenant_id, direction="in", status="received", text=text, at=at, chat=chat)


@pytest_asyncio.fixture
async def two(pg_pool):
    """Tenant A (Tokyo time) and tenant B, both talking to chat 100."""
    a = await seed_session(pg_pool, "acct_a", name="Salon A")
    b = await seed_session(pg_pool, "acct_b", name="Salon B")
    await pg_pool.execute("UPDATE tenants SET config_json = '{\"timezone\": \"Asia/Tokyo\"}' WHERE id = $1", a)
    await pg_pool.execute(
        "INSERT INTO conversations (session_id, tenant_id, chat_id, display_name) VALUES ('acct_a', $1, $2, 'Anna')",
        a, CHAT,
    )
    return a, b


async def create(client, tenant_id, date_from=DAY, date_to=DAY, name="September"):
    return await client.post("/api/review/batches", json={
        "tenant_id": tenant_id, "name": name, "date_from": date_from, "date_to": date_to})


async def events(pool, event):
    rows = await pool.fetch("SELECT * FROM audit_log WHERE event = $1 ORDER BY id", event)
    return [dict(r, payload=json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"])
            for r in rows]


async def test_everything_needs_the_admin_login(panel_client, two):
    await panel_client.post("/api/logout")
    for method, path in (("GET", "/api/review/batches"), ("POST", "/api/review/batches"),
                         ("GET", "/api/review/batches/1"), ("POST", "/api/review/items/1"),
                         ("GET", "/api/review/batches/1/export.jsonl"), ("DELETE", "/api/review/batches/1"),
                         ("GET", "/api/onboarding"), ("GET", "/api/onboarding/1")):
        assert (await panel_client.request(method, path, json={})).status_code == 401


async def test_a_batch_takes_only_ai_replies_sent_in_the_clients_own_day(panel_client, pg_pool, two):
    a, b = two
    before = await ai(pg_pool, "acct_a", a, "local 23:59 the day before", DAY_START - timedelta(minutes=1))
    first = await ai(pg_pool, "acct_a", a, "local midnight", DAY_START)
    last = await ai(pg_pool, "acct_a", a, "local 23:59", DAY_END - timedelta(minutes=1))
    await ai(pg_pool, "acct_a", a, "next local day", DAY_END)
    at = DAY_START + timedelta(hours=3)
    # Not AI-written, not sent, or not outbound: never a review item.
    await add(pg_pool, "acct_a", a, direction="out", status="sent", text="typed by hand", at=at)
    await add(pg_pool, "acct_a", a, direction="out", status="pending_approval", text="draft", at=at,
              model="deepseek-chat")
    await add(pg_pool, "acct_a", a, direction="out", status="rejected", text="rejected", at=at, model="deepseek-chat")
    await add(pg_pool, "acct_a", a, direction="system", status="note", text="note", at=at)
    await inc(pg_pool, "acct_a", a, "customer", at)
    # Another client's AI reply in the same chat id, same time.
    await ai(pg_pool, "acct_b", b, "tenant B reply", at)

    r = await create(panel_client, a)
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch["counts"] == {"total": 2, "approved": 0, "rejected": 0, "edited": 0, "undecided": 2}
    assert batch["tenant_id"] == a and batch["status"] == "open"

    full = (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()
    assert [i["message_id"] for i in full["items"]] == [first, last]
    assert [i["reply"] for i in full["items"]] == ["local midnight", "local 23:59"]
    assert full["items"][0]["chat_name"] == "Anna"
    assert before not in [i["message_id"] for i in full["items"]]
    rows = await pg_pool.fetch("SELECT tenant_id FROM review_items WHERE batch_id = $1", batch["id"])
    assert {r["tenant_id"] for r in rows} == {a}

    # A wider range in B's (default) timezone takes only B's reply.
    rb = await create(panel_client, b, "2026-09-01", "2026-09-30")
    assert rb.status_code == 200
    items_b = (await panel_client.get(f"/api/review/batches/{rb.json()['id']}")).json()["items"]
    assert [i["reply"] for i in items_b] == ["tenant B reply"]


async def test_the_context_is_what_crossed_the_wire_in_that_chat_only(panel_client, pg_pool, two):
    a, b = two
    t = DAY_START + timedelta(hours=1)
    step = timedelta(seconds=10)
    await inc(pg_pool, "acct_a", a, "hi, are you open?", t)
    await add(pg_pool, "acct_a", a, direction="system", status="note", text="Chat paused", at=t + step)
    await add(pg_pool, "acct_a", a, direction="out", status="pending_approval", text="unapproved draft",
              at=t + 2 * step, model="deepseek-chat")
    await add(pg_pool, "acct_a", a, direction="out", status="error", text="failed send", at=t + 3 * step)
    await add(pg_pool, "acct_a", a, direction="out", status="rejected", text="rejected draft", at=t + 4 * step)
    await inc(pg_pool, "acct_a", a, "   ", t + 5 * step)
    await add(pg_pool, "acct_a", a, direction="out", status="sent", text="typed by the owner", at=t + 6 * step)
    await inc(pg_pool, "acct_a", a, "other chat", t + 6 * step, chat=CHAT + 1)
    await inc(pg_pool, "acct_b", b, "tenant B, same chat id", t + 6 * step)
    await inc(pg_pool, "acct_a", a, "and tomorrow?", t + 7 * step)
    await ai(pg_pool, "acct_a", a, "Yes, 9 to 18.", t + 8 * step)
    await ai(pg_pool, "acct_a", a, "Want a time?", t + 9 * step)

    batch = (await create(panel_client, a)).json()
    items = (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()["items"]
    assert items[0]["context"] == [
        {"role": "user", "content": "hi, are you open?"},
        {"role": "assistant", "content": "typed by the owner"},
        {"role": "user", "content": "and tomorrow?"},
    ]
    # The second part of a burst sees the first as the assistant's.
    assert items[1]["context"][-1] == {"role": "assistant", "content": "Yes, 9 to 18."}


async def test_the_context_keeps_the_last_twenty_messages(panel_client, pg_pool, two):
    a, _ = two
    t = DAY_START + timedelta(hours=1)
    for n in range(25):
        await inc(pg_pool, "acct_a", a, f"m{n}", t + timedelta(seconds=n))
    await ai(pg_pool, "acct_a", a, "reply", t + timedelta(minutes=5))
    batch = (await create(panel_client, a)).json()
    [item] = (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()["items"]
    assert [m["content"] for m in item["context"]] == [f"m{n}" for n in range(5, 25)]


async def test_bad_requests_are_refused(panel_client, pg_pool, two):
    a, _ = two
    assert (await create(panel_client, a)).status_code == 400                     # nothing in range
    assert (await create(panel_client, 9999)).status_code == 404
    assert (await create(panel_client, a, "2026-09-10", "2026-09-01")).status_code == 400
    assert (await create(panel_client, a, name="  ")).status_code == 400
    assert (await panel_client.get("/api/review/batches/9999")).status_code == 404
    assert await pg_pool.fetchval("SELECT count(*) FROM review_batches") == 0


async def test_a_range_with_too_many_replies_is_refused(panel_client, pg_pool, two):
    a, _ = two
    at = DAY_START + timedelta(hours=2)
    await pg_pool.executemany(
        "INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text, created_at, llm_model) "
        "VALUES ('acct_a', $1, $2, 'out', 'sent', 'r', $3, 'deepseek-chat')",
        [(a, CHAT + n % 7, at) for n in range(review_api.MAX_ITEMS + 1)],
    )
    r = await create(panel_client, a)
    assert r.status_code == 400 and "shorter" in r.json()["detail"]
    assert await pg_pool.fetchval("SELECT count(*) FROM review_batches") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM review_items") == 0


async def test_decisions(panel_client, pg_pool, two):
    a, _ = two
    for n in range(3):
        await ai(pg_pool, "acct_a", a, f"reply {n}", DAY_START + timedelta(hours=n + 1))
    batch = (await create(panel_client, a)).json()
    items = (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()["items"]
    ids = [i["id"] for i in items]

    assert (await panel_client.post(f"/api/review/items/{ids[0]}", json={"decision": "maybe"})).status_code == 400
    assert (await panel_client.post(f"/api/review/items/{ids[0]}", json={"decision": "edit"})).status_code == 400
    assert (await panel_client.post(f"/api/review/items/{ids[0]}",
                                    json={"decision": "edit", "edited_text": "   "})).status_code == 400
    assert (await panel_client.post("/api/review/items/99999", json={"decision": "approve"})).status_code == 404

    r = await panel_client.post(f"/api/review/items/{ids[0]}", json={"decision": "edit", "edited_text": " Better. "})
    assert r.status_code == 200
    assert r.json()["decision"] == "edit" and r.json()["edited_text"] == "Better."
    assert r.json()["decided_by"] == "admin" and r.json()["decided_at"]

    # Changing an edit to approve drops the edited text.
    r = await panel_client.post(f"/api/review/items/{ids[1]}", json={"decision": "edit", "edited_text": "x"})
    r = await panel_client.post(f"/api/review/items/{ids[1]}", json={"decision": "approve", "edited_text": "x"})
    assert r.json()["decision"] == "approve" and r.json()["edited_text"] is None

    full = (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()
    assert full["counts"] == {"total": 3, "approved": 1, "rejected": 0, "edited": 1, "undecided": 1}
    assert full["first_undecided"] == 2

    await panel_client.post(f"/api/review/items/{ids[2]}", json={"decision": "reject"})
    full = (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()
    assert full["first_undecided"] is None and full["counts"]["rejected"] == 1

    page = (await panel_client.get(f"/api/review/batches/{batch['id']}?offset=1&limit=1")).json()
    assert [i["id"] for i in page["items"]] == [ids[1]]


async def test_export_holds_approved_and_edited_items_only(panel_client, pg_pool, two):
    a, _ = two
    t = DAY_START + timedelta(hours=1)
    await inc(pg_pool, "acct_a", a, "Привет, есть время завтра?", t)
    for n in range(4):
        await ai(pg_pool, "acct_a", a, f"reply {n}", t + timedelta(minutes=n + 1))
    batch = (await create(panel_client, a, name="Sept / week 1")).json()
    ids = [i["id"] for i in (await panel_client.get(f"/api/review/batches/{batch['id']}")).json()["items"]]
    await panel_client.post(f"/api/review/items/{ids[0]}", json={"decision": "approve"})
    await panel_client.post(f"/api/review/items/{ids[1]}", json={"decision": "reject"})
    await panel_client.post(f"/api/review/items/{ids[2]}", json={"decision": "edit", "edited_text": "Да, в 10:00."})
    # ids[3] stays undecided.

    r = await panel_client.get(f"/api/review/batches/{batch['id']}/export.jsonl")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and ".jsonl" in disposition and "/" not in disposition
    lines = r.content.decode("utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [rec["meta"] for rec in records] == [
        {"tenant_id": a, "batch_id": batch["id"], "item_id": ids[0], "decision": "approve"},
        {"tenant_id": a, "batch_id": batch["id"], "item_id": ids[2], "decision": "edit"},
    ]
    assert records[0]["messages"] == [
        {"role": "user", "content": "Привет, есть время завтра?"},
        {"role": "assistant", "content": "reply 0"},
    ]
    assert records[1]["messages"][-1] == {"role": "assistant", "content": "Да, в 10:00."}
    assert all(m["role"] != "system" for rec in records for m in rec["messages"])

    [exported] = await events(pg_pool, review_api.REVIEW_EXPORTED)
    assert exported["tenant_id"] == a and exported["payload"] == {"batch_id": batch["id"], "lines": 2}


async def test_batches_are_listed_per_client_and_audited(panel_client, pg_pool, two):
    a, b = two
    await ai(pg_pool, "acct_a", a, "a reply", DAY_START + timedelta(hours=1))
    await ai(pg_pool, "acct_b", b, "b reply", DAY_START + timedelta(hours=12))
    batch_a = (await create(panel_client, a)).json()
    batch_b = (await create(panel_client, b)).json()

    only_a = (await panel_client.get(f"/api/review/batches?tenant_id={a}")).json()
    assert [x["id"] for x in only_a] == [batch_a["id"]]
    assert only_a[0]["counts"]["total"] == 1
    assert {x["id"] for x in (await panel_client.get("/api/review/batches")).json()} == {batch_a["id"], batch_b["id"]}

    [created, _] = await events(pg_pool, review_api.REVIEW_BATCH_CREATED)
    assert created["tenant_id"] == a and created["actor"] == "admin"
    assert created["payload"]["items"] == 1 and created["payload"]["timezone"] == "Asia/Tokyo"

    done = await panel_client.post(f"/api/review/batches/{batch_a['id']}/done")
    assert done.json()["status"] == "done"
    [marked] = await events(pg_pool, review_api.REVIEW_BATCH_DONE)
    assert marked["tenant_id"] == a

    assert (await panel_client.delete(f"/api/review/batches/{batch_a['id']}")).status_code == 200
    assert (await panel_client.get(f"/api/review/batches/{batch_a['id']}")).status_code == 404
    assert await pg_pool.fetchval("SELECT count(*) FROM review_items WHERE batch_id = $1", batch_a["id"]) == 0
    [deleted] = await events(pg_pool, review_api.REVIEW_BATCH_DELETED)
    assert deleted["tenant_id"] == a and deleted["payload"]["batch_id"] == batch_a["id"]
    # B's batch is untouched.
    assert (await panel_client.get(f"/api/review/batches/{batch_b['id']}")).json()["counts"]["total"] == 1


async def test_export_lines_without_a_database():
    items = [
        {"id": 1, "context": '[{"role": "user", "content": "q"}]', "reply": "a", "decision": "approve",
         "edited_text": None},
        {"id": 2, "context": [], "reply": "a", "decision": "reject", "edited_text": None},
        {"id": 3, "context": [], "reply": "a", "decision": None, "edited_text": None},
        {"id": 4, "context": [], "reply": "old", "decision": "edit", "edited_text": "new"},
    ]
    lines = [json.loads(x) for x in review_api.export_lines(items, tenant_id=7, batch_id=3)]
    assert [x["meta"]["item_id"] for x in lines] == [1, 4]
    assert lines[0]["messages"] == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert lines[1]["messages"] == [{"role": "assistant", "content": "new"}]
