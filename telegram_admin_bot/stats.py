"""Numbers for the client dashboard and the weekly digest, per tenant, and
for the admin's view of how well the bot answers (quality: Finetune →
Quality).

Weeks run Monday 00:00 to Monday 00:00 in the tenant's timezone. Every
function takes one tenant id; callers decide which tenants a viewer may see.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg

import audit
from bookings import tzinfo_for

# Who sent an outgoing message: the model wrote it (llm_model), or the bot or
# the system sent it without the model (the address, the way in, a reminder:
# a message_sent audit row with that actor), or else a person typed it, on
# the phone or by hand in the panel. The same rule as the finetune runs from
# hand-typed chats (finetune_api._HAND_MESSAGES). $1 tenant, $2 since (a
# message's audit row is never older than the message), $3 until, $4 the
# machine actors.
_MACHINE_ACTORS = [audit.BOT, audit.SYSTEM]
_SENT = f"""
WITH machine AS (
  SELECT DISTINCT payload->>'message_id' AS id FROM audit_log
   WHERE tenant_id = $1 AND event = '{audit.MESSAGE_SENT}' AND actor = ANY($4::text[]) AND created_at >= $2
), sent AS (
  SELECT m.chat_id, (m.llm_model IS NULL AND machine.id IS NULL) AS typed
    FROM messages m LEFT JOIN machine ON machine.id = m.id::text
   WHERE m.tenant_id = $1 AND m.created_at >= $2 AND m.created_at < $3
     AND m.direction = 'out' AND m.status = 'sent'
)
SELECT count(*) FILTER (WHERE NOT typed) AS by_bot,
       count(*) FILTER (WHERE typed) AS by_hand,
       count(DISTINCT chat_id) FILTER (WHERE typed) AS chats_by_hand
  FROM sent
"""
# All time: from before the first message to after the last.
_EVER = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FOREVER = datetime(9999, 1, 1, tzinfo=timezone.utc)


async def _sent(pool: asyncpg.Pool, tenant_id: int, since: datetime, until: datetime) -> asyncpg.Record:
    return await pool.fetchrow(_SENT, tenant_id, since, until, _MACHINE_ACTORS)


def week_start(now: datetime, timezone_name: str) -> datetime:
    """Monday 00:00 of the week containing `now`, in the tenant's zone."""
    local = now.astimezone(tzinfo_for(timezone_name))
    monday = (local - timedelta(days=local.weekday())).date()
    return datetime(monday.year, monday.month, monday.day, tzinfo=tzinfo_for(timezone_name))


def day_start(now: datetime, timezone_name: str) -> datetime:
    local = now.astimezone(tzinfo_for(timezone_name))
    return datetime(local.year, local.month, local.day, tzinfo=tzinfo_for(timezone_name))


async def period(pool: asyncpg.Pool, tenant_id: int, since: datetime, until: datetime) -> dict[str, Any]:
    """What happened in [since, until): bookings by the time they were for,
    messages by when they were stored, and the unanswered queue."""
    b = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE state IN ('requested', 'pending', 'confirmed', 'completed', 'no_show')) AS booked,
               count(*) FILTER (WHERE state = 'confirmed') AS confirmed,
               count(*) FILTER (WHERE state = 'completed') AS completed,
               count(*) FILTER (WHERE state = 'no_show') AS no_show,
               count(*) FILTER (WHERE state = 'cancelled') AS cancelled
          FROM bookings WHERE tenant_id = $1 AND starts_at >= $2 AND starts_at < $3
        """,
        tenant_id, since, until,
    )
    m = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE direction = 'in') AS received,
               count(DISTINCT chat_id) FILTER (WHERE direction = 'in') AS conversations
          FROM messages WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
        """,
        tenant_id, since, until,
    )
    sent = await _sent(pool, tenant_id, since, until)
    unanswered = await pool.fetchval(
        "SELECT count(*) FROM unanswered_queue WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3",
        tenant_id, since, until,
    )
    attended = b["completed"] + b["no_show"]
    return {
        "since": since.isoformat(timespec="seconds"),
        "until": until.isoformat(timespec="seconds"),
        "bookings": {
            "booked": b["booked"], "confirmed": b["confirmed"], "completed": b["completed"],
            "no_show": b["no_show"], "cancelled": b["cancelled"],
            # Of the bookings someone marked done or missed.
            "no_show_rate": round(b["no_show"] / attended, 3) if attended else None,
        },
        "messages": {
            "received": m["received"], "sent_by_bot": sent["by_bot"], "sent_by_hand": sent["by_hand"],
            "conversations": m["conversations"], "chats_by_hand": sent["chats_by_hand"],
        },
        "unanswered": unanswered,
    }


async def weeks(pool: asyncpg.Pool, tenant_id: int, timezone_name: str, now: datetime,
                count: int = 4, of: Callable[..., Awaitable[dict[str, Any]]] = period) -> list[dict[str, Any]]:
    """The last `count` weeks, oldest first; the last one is this week so far.
    `of` is what is counted for each: period, or quality for the admin."""
    this_week = week_start(now, timezone_name)
    out = []
    for back in range(count - 1, -1, -1):
        start = this_week - timedelta(weeks=back)
        out.append(await of(pool, tenant_id, start, start + timedelta(weeks=1)))
    return out


async def summary(pool: asyncpg.Pool, tenant_id: int, timezone_name: str, now: datetime,
                  last_week: Optional[bool] = False) -> dict[str, Any]:
    """This week (or the last full week), plus today and the open queue."""
    start = week_start(now, timezone_name)
    if last_week:
        start -= timedelta(weeks=1)
    today = day_start(now, timezone_name)
    return {
        "week": await period(pool, tenant_id, start, start + timedelta(weeks=1)),
        "today": await period(pool, tenant_id, today, today + timedelta(days=1)),
        "unanswered_open": await pool.fetchval(
            "SELECT count(*) FROM unanswered_queue WHERE tenant_id = $1 AND status = 'open'", tenant_id),
    }


# ------------------------------------------------------------------ quality


async def quality(pool: asyncpg.Pool, tenant_id: int, since: datetime, until: datetime) -> dict[str, Any]:
    """How the bot did in [since, until), for the admin (Finetune → Quality):
    period's numbers, plus the replies the model wrote, what happened to the
    drafts that waited for approval, the chats a person took over and why
    messages went unanswered. The baseline that shows whether a prompt
    change helps."""
    counts = await pool.fetchrow(
        """
        SELECT (SELECT count(*) FROM llm_usage
                 WHERE tenant_id = $1 AND purpose = 'reply' AND created_at >= $2 AND created_at < $3) AS written,
               count(*) FILTER (WHERE event = $4) AS approved,
               count(*) FILTER (WHERE event = $4 AND payload->>'edited' = 'true') AS edited,
               count(*) FILTER (WHERE event = $5) AS rejected,
               count(DISTINCT payload->>'chat_id') FILTER (WHERE event = $6) AS takeovers
          FROM audit_log
         WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3 AND event IN ($4, $5, $6)
        """,
        tenant_id, since, until, audit.DRAFT_APPROVED, audit.DRAFT_REJECTED, audit.HUMAN_TAKEOVER,
    )
    reasons = await pool.fetch(
        "SELECT reason, count(*) AS n FROM unanswered_queue WHERE tenant_id = $1 "
        "AND created_at >= $2 AND created_at < $3 GROUP BY reason ORDER BY reason",
        tenant_id, since, until,
    )
    return {
        **await period(pool, tenant_id, since, until),
        # Model calls for a reply: one per reply, however many messages it became.
        "replies_written": counts["written"],
        # Approved includes edited: approved as written = approved - edited.
        "drafts": {"approved": counts["approved"], "edited": counts["edited"], "rejected": counts["rejected"]},
        "takeovers": counts["takeovers"],
        "unanswered_by_reason": {r["reason"]: r["n"] for r in reasons},
    }


async def quality_totals(pool: asyncpg.Pool, tenant_id: int) -> dict[str, Any]:
    """All time: what there is to learn from. The replies typed by hand and
    the chats they are in (what the hand-typed finetune runs read), the
    drafts corrected before approval (kept since migration 0016) and the
    chats flagged for finetuning."""
    sent = await _sent(pool, tenant_id, _EVER, _FOREVER)
    row = await pool.fetchrow(
        "SELECT (SELECT count(*) FROM messages WHERE tenant_id = $1 AND draft_text IS NOT NULL) AS edited, "
        "(SELECT count(*) FROM finetune_examples WHERE tenant_id = $1) AS flagged",
        tenant_id,
    )
    return {"typed_by_hand": sent["by_hand"], "chats_by_hand": sent["chats_by_hand"],
            "drafts_edited": row["edited"], "chats_flagged": row["flagged"]}
