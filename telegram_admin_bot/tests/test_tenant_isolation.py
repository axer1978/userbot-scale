"""Tenant isolation: no code path reads or changes another tenant's rows.

Both tenants below talk to the same Telegram user (chat_id 42): the case
where a missing tenant filter would silently mix their data.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from conftest import seed_session
from database import DIR_IN, DIR_OUT, STATUS_PENDING, STATUS_RECEIVED, STATUS_REJECTED, STATUS_SENT, Database

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

CHAT = 42


@pytest_asyncio.fixture
async def two_tenants(pg_pool):
    tid_a = await seed_session(pg_pool, "tenant-a", industry_id=1)
    tid_b = await seed_session(pg_pool, "tenant-b", industry_id=1)   # same industry on purpose
    a, b = Database(pg_pool, "tenant-a"), Database(pg_pool, "tenant-b")
    for db, who in ((a, "A"), (b, "B")):
        await db.connect()
        await db.upsert_conversation(CHAT, f"Customer of {who}", None, False)
        await db.record_message(CHAT, DIR_IN, STATUS_RECEIVED, f"hello {who}", telegram_id=1)
        await db.record_message(CHAT, DIR_OUT, STATUS_SENT, f"reply {who}", telegram_id=2)
        await db.record_message(CHAT, DIR_OUT, STATUS_PENDING, f"draft {who}")
        await db.save_summary(CHAT, f"summary {who}", 1)
        await db.queue_outreach([(CHAT, "x")], f"goal {who}")
    await a.link_chats(CHAT, 43)
    return {"a": a, "b": b, "tid_a": tid_a, "tid_b": tid_b}


async def test_reads_only_see_the_own_tenant(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    assert [c["display_name"] for c in await a.list_conversations()] == ["Customer of A"]
    assert (await b.get_conversation(CHAT))["display_name"] == "Customer of B"
    assert [m["text"] for m in await a.get_messages(CHAT)] == ["hello A", "reply A", "draft A"]
    assert [m["content"] for m in await b.get_history_for_ai(CHAT)] == ["hello B", "reply B"]
    assert [d["text"] for d in await a.pending_drafts()] == ["draft A"]
    assert (await b.get_summary(CHAT))["summary"] == "summary B"
    assert [o["goal"] for o in await b.list_outreach()] == ["goal B"]
    assert await b.get_links(CHAT) == [] and len(await a.get_links(CHAT)) == 1
    assert await a.sent_since("2000-01-01T00:00:00+00:00") == 1


async def test_another_tenants_row_ids_resolve_to_nothing(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    b_draft = (await b.pending_drafts())[0]
    b_outreach = (await b.list_outreach())[0]

    assert await a.get_message(b_draft["id"]) is None
    assert await a.get_outreach(b_outreach["id"]) is None
    assert await a.outreach_for_draft(b_draft["id"]) is None
    assert await a.find_by_telegram_id(CHAT, 1) is not None       # A's own, same numbers
    assert (await a.find_by_telegram_id(CHAT, 1))["text"] == "hello A"


async def test_writes_cannot_reach_another_tenant(two_tenants):
    a, b = two_tenants["a"], two_tenants["b"]
    b_draft = (await b.pending_drafts())[0]

    assert await a.update_message(b_draft["id"], status=STATUS_REJECTED, text="hijacked") is None
    await a.set_paused(CHAT, True)
    await a.reject_pending(CHAT)
    await a.cancel_queued_outreach()
    await a.clear_summary(CHAT)

    after = await b.get_message(b_draft["id"])
    assert (after["status"], after["text"]) == (STATUS_PENDING, "draft B")
    assert (await b.get_conversation(CHAT))["automation_paused"] is False
    assert (await b.list_outreach())[0]["status"] == "queued"
    assert await b.get_summary(CHAT) is not None


async def test_same_person_gets_a_different_customer_ref_per_tenant(two_tenants):
    ref_a = (await two_tenants["a"].get_conversation(CHAT))
    ref_b = (await two_tenants["b"].get_conversation(CHAT))
    async with two_tenants["a"]._pool.acquire() as con:
        refs = await con.fetch("SELECT tenant_id, customer_ref FROM conversations WHERE chat_id = $1", CHAT)
    values = {r["tenant_id"]: r["customer_ref"] for r in refs}
    assert len(values) == 2 and None not in values.values()
    assert values[two_tenants["tid_a"]] != values[two_tenants["tid_b"]]
    assert ref_a and ref_b


async def test_database_refuses_a_row_tagged_with_the_wrong_tenant(two_tenants, pg_pool):
    async with pg_pool.acquire() as con:
        with pytest.raises(asyncpg.RaiseError, match="does not own session"):
            await con.execute(
                "INSERT INTO messages (session_id, tenant_id, chat_id, direction, status, text) "
                "VALUES ('tenant-a', $1, 1, 'in', 'received', 'x')",
                two_tenants["tid_b"],
            )
        with pytest.raises(asyncpg.RaiseError, match="does not own session"):
            await con.execute(
                "UPDATE conversations SET tenant_id = $1 WHERE session_id = 'tenant-a'", two_tenants["tid_b"]
            )


async def test_an_account_with_no_tenant_cannot_store_anything(pg_pool):
    async with pg_pool.acquire() as con:
        await con.execute("INSERT INTO telegram_sessions (session_id) VALUES ('orphan')")
        with pytest.raises(asyncpg.RaiseError, match="no tenant owns session"):
            await con.execute(
                "INSERT INTO conversations (session_id, chat_id) VALUES ('orphan', 1)"
            )
    with pytest.raises(LookupError):
        await Database(pg_pool, "orphan").list_conversations()


async def test_every_table_with_account_data_carries_a_required_tenant_id(pg_pool):
    """Guards future migrations: a new table keyed by session_id without a
    tenant_id would fail here."""
    async with pg_pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT c.table_name,
                   bool_or(c.column_name = 'tenant_id' AND c.is_nullable = 'NO') AS has_tenant
              FROM information_schema.columns c
             WHERE c.table_schema = current_schema()
             GROUP BY c.table_name
            HAVING bool_or(c.column_name = 'session_id')
            """
        )
    missing = sorted(r["table_name"] for r in rows if not r["has_tenant"] and r["table_name"] != "telegram_sessions")
    # tenants itself links to its account through session_id and IS the tenant.
    assert missing == ["tenants"]


async def test_audit_log_is_append_only(pg_pool):
    import audit

    await audit.record(pg_pool, tenant_id=None, actor="admin", event="test")
    async with pg_pool.acquire() as con:
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await con.execute("UPDATE audit_log SET actor = 'someone else'")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await con.execute("DELETE FROM audit_log")
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await con.execute("TRUNCATE audit_log")
        assert await con.fetchval("SELECT count(*) FROM audit_log") == 1


async def test_audit_listing_for_a_tenant_leaves_out_other_tenants(two_tenants, pg_pool):
    import audit

    await audit.record(pg_pool, tenant_id=two_tenants["tid_a"], actor="admin", event="a-thing")
    await audit.record(pg_pool, tenant_id=two_tenants["tid_b"], actor="admin", event="b-thing")
    events = await audit.list_events(pg_pool, tenant_id=two_tenants["tid_a"])
    assert {e["event"] for e in events} == {"a-thing"}
