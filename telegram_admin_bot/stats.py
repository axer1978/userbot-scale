"""Numbers for the client dashboard and the weekly digest, per tenant.

Weeks run Monday 00:00 to Monday 00:00 in the tenant's timezone. Every
function takes one tenant id; callers decide which tenants a viewer may see.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import asyncpg

from bookings import tzinfo_for


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
               count(*) FILTER (WHERE direction = 'out' AND status = 'sent' AND llm_model IS NOT NULL) AS sent_by_bot,
               count(*) FILTER (WHERE direction = 'out' AND status = 'sent' AND llm_model IS NULL) AS sent_by_hand,
               count(DISTINCT chat_id) FILTER (WHERE direction = 'in') AS conversations
          FROM messages WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
        """,
        tenant_id, since, until,
    )
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
            "received": m["received"], "sent_by_bot": m["sent_by_bot"], "sent_by_hand": m["sent_by_hand"],
            "conversations": m["conversations"],
        },
        "unanswered": unanswered,
    }


async def weeks(pool: asyncpg.Pool, tenant_id: int, timezone_name: str, now: datetime,
                count: int = 4) -> list[dict[str, Any]]:
    """The last `count` weeks, oldest first; the last one is this week so far."""
    this_week = week_start(now, timezone_name)
    out = []
    for back in range(count - 1, -1, -1):
        start = this_week - timedelta(weeks=back)
        out.append(await period(pool, tenant_id, start, start + timedelta(weeks=1)))
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
