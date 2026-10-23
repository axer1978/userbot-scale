"""How the bot did (stats.quality, Finetune → Quality): who sent each
message, the replies the model wrote, the drafts approved, edited and
rejected, takeovers and unanswered messages, week by week in the account's
timezone; and the audit row a rejected draft now leaves."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import audit
import stats
from conftest import seed_session

pytestmark = [pytest.mark.requires_pg, pytest.mark.asyncio]

TZ = "Europe/Riga"
WEEK = stats.week_start(datetime(2026, 10, 14, 12, tzinfo=timezone.utc), TZ)  # Monday 12.10, 00:00 Riga
AT = WEEK + timedelta(days=2, hours=10)
BEFORE = WEEK - timedelta(days=3)  # the week before
END = WEEK + timedelta(weeks=1)


async def message(pool, session, chat, direction="out", status="sent", *, model=None, at=AT, draft_text=None) -> int:
    return await pool.fetchval(
        "INSERT INTO messages (session_id, chat_id, direction, status, text, llm_model, draft_text, created_at) "
        "VALUES ($1, $2, $3, $4, 'x', $5, $6, $7) RETURNING id",
        session, chat, direction, status, model, draft_text, at,
    )


async def event(pool, tenant, name, *, actor=audit.ADMIN, at=AT, **payload) -> None:
    await pool.execute(
        "INSERT INTO audit_log (tenant_id, actor, event, payload, created_at) VALUES ($1, $2, $3, $4::jsonb, $5)",
        tenant, actor, name, json.dumps(payload), at,
    )


async def usage(pool, tenant, purpose="reply", at=AT) -> None:
    await pool.execute("INSERT INTO llm_usage (tenant_id, purpose, model, created_at) VALUES ($1, $2, 'm', $3)",
                       tenant, purpose, at)


async def unanswered(pool, tenant, session, reason, at=AT) -> None:
    await pool.execute("INSERT INTO unanswered_queue (tenant_id, session_id, chat_id, reason, created_at) "
                       "VALUES ($1, $2, 1, $3, $4)", tenant, session, reason, at)


async def test_who_sent_each_message(pg_pool):
    """The model's replies and what the bot sends without it (the way in,
    a reminder) are the bot's; the rest was typed by a person."""
    tenant = await seed_session(pg_pool, "acct")
    await message(pg_pool, "acct", 1, "in", "received")
    await message(pg_pool, "acct", 1, "in", "received")
    await message(pg_pool, "acct", 2, "in", "received")
    await message(pg_pool, "acct", 1, model="deepseek-chat")
    way_in = await message(pg_pool, "acct", 1)
    await event(pg_pool, tenant, audit.MESSAGE_SENT, actor=audit.BOT, message_id=way_in)
    from_panel = await message(pg_pool, "acct", 2)
    await event(pg_pool, tenant, audit.MESSAGE_SENT, actor=audit.ADMIN, message_id=from_panel)
    await message(pg_pool, "acct", 3)  # typed on the phone: no audit row
    await message(pg_pool, "acct", 1, status="pending_approval", model="deepseek-chat")  # not sent

    got = await stats.period(pg_pool, tenant, WEEK, END)

    assert got["messages"] == {"received": 3, "conversations": 2, "sent_by_bot": 2, "sent_by_hand": 2,
                               "chats_by_hand": 2}


async def test_quality_counts_one_week_of_one_account(pg_pool):
    tenant = await seed_session(pg_pool, "acct")
    other = await seed_session(pg_pool, "other")
    for t in (tenant, other):
        for _ in range(3):
            await usage(pg_pool, t)
    await usage(pg_pool, tenant, at=BEFORE)
    await usage(pg_pool, tenant, purpose="booking_extract")  # not a reply
    await event(pg_pool, tenant, audit.DRAFT_APPROVED, chat_id=1, message_id=10, edited=True)
    await event(pg_pool, tenant, audit.DRAFT_APPROVED, chat_id=2, message_id=11, edited=False)
    await event(pg_pool, tenant, audit.DRAFT_APPROVED, at=BEFORE, chat_id=2, message_id=9, edited=True)
    await event(pg_pool, tenant, audit.DRAFT_REJECTED, chat_id=3, message_id=12)
    await event(pg_pool, other, audit.DRAFT_REJECTED, chat_id=3, message_id=13)
    await event(pg_pool, tenant, audit.HUMAN_TAKEOVER, actor=audit.OWNER, chat_id=1)
    await event(pg_pool, tenant, audit.HUMAN_TAKEOVER, actor=audit.OWNER, chat_id=1)  # the same chat again
    await event(pg_pool, tenant, audit.HUMAN_TAKEOVER, actor=audit.ADMIN, chat_id=2)
    await unanswered(pg_pool, tenant, "acct", "policy_hold")
    await unanswered(pg_pool, tenant, "acct", "policy_hold")
    await unanswered(pg_pool, tenant, "acct", "soft_off")
    await unanswered(pg_pool, tenant, "acct", "paused", at=BEFORE)
    await unanswered(pg_pool, other, "other", "ai_error")

    got = await stats.quality(pg_pool, tenant, WEEK, END)

    assert got["replies_written"] == 3
    assert got["drafts"] == {"approved": 2, "edited": 1, "rejected": 1}
    assert got["takeovers"] == 2
    assert got["unanswered_by_reason"] == {"policy_hold": 2, "soft_off": 1}
    assert got["unanswered"] == 3
    assert (got["since"], got["until"]) == (WEEK.isoformat(timespec="seconds"), END.isoformat(timespec="seconds"))

    before, this = await stats.weeks(pg_pool, tenant, TZ, AT, count=2, of=stats.quality)
    assert this == got
    assert before["replies_written"] == 1
    assert before["drafts"] == {"approved": 1, "edited": 1, "rejected": 0}
    assert before["unanswered_by_reason"] == {"paused": 1}


async def test_totals_are_all_time(pg_pool):
    tenant = await seed_session(pg_pool, "acct")
    await message(pg_pool, "acct", 1, at=BEFORE - timedelta(weeks=20))
    await message(pg_pool, "acct", 1)
    await message(pg_pool, "acct", 2)
    await message(pg_pool, "acct", 2, model="deepseek-chat", draft_text="what the bot wrote")
    await message(pg_pool, "acct", 3, model="deepseek-chat", draft_text="another", at=BEFORE)
    upto = await message(pg_pool, "acct", 3, "in", "received")
    await pg_pool.execute("INSERT INTO finetune_examples (tenant_id, chat_id, upto_message_id, verdict, created_by) "
                          "VALUES ($1, 3, $2, 'bad', 'admin')", tenant, upto)

    assert await stats.quality_totals(pg_pool, tenant) == {
        "typed_by_hand": 3, "chats_by_hand": 2, "drafts_edited": 2, "chats_flagged": 1,
    }


# ------------------------------------------------------------------- API


async def test_the_quality_tab_reads_the_accounts_weeks(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct")
    await pg_pool.execute("INSERT INTO llm_usage (tenant_id, purpose, model) VALUES ($1, 'reply', 'm')", tenant)

    response = await panel_client.get(f"/api/finetune/quality?tenant_id={tenant}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["tenant_id"], body["timezone"], body["auto_send"]) == (tenant, TZ, False)
    assert body["takeover_hours"] > 0
    assert len(body["weeks"]) == 4
    assert [w["replies_written"] for w in body["weeks"]] == [0, 0, 0, 1]  # oldest first, this week last
    assert set(body["weeks"][-1]) >= {"messages", "bookings", "drafts", "takeovers", "unanswered_by_reason"}
    assert body["totals"] == {"typed_by_hand": 0, "chats_by_hand": 0, "drafts_edited": 0, "chats_flagged": 0}

    assert len((await panel_client.get(f"/api/finetune/quality?tenant_id={tenant}&weeks=2")).json()["weeks"]) == 2
    assert (await panel_client.get(f"/api/finetune/quality?tenant_id={tenant}&weeks=13")).status_code == 422
    assert (await panel_client.get("/api/finetune/quality?tenant_id=999")).status_code == 404


async def test_rejecting_a_draft_is_audited_but_not_other_messages(panel_client, pg_pool):
    tenant = await seed_session(pg_pool, "acct")
    draft = await message(pg_pool, "acct", 5, status="pending_approval", model="deepseek-chat", at=datetime.now(timezone.utc))
    sent = await message(pg_pool, "acct", 5, model="deepseek-chat", at=datetime.now(timezone.utc))

    assert (await panel_client.post(f"/api/sessions/acct/drafts/{draft}/reject")).status_code == 200
    assert (await panel_client.post(f"/api/sessions/acct/drafts/{sent}/reject")).status_code == 200

    rows = [e for e in await audit.list_events(pg_pool, tenant_id=tenant) if e["event"] == audit.DRAFT_REJECTED]
    assert [(e["actor"], e["payload"]) for e in rows] == [("admin", {"chat_id": 5, "message_id": draft})]
