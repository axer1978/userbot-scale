"""The unanswered queue: customer messages that got no reply sent or
drafted, or whose reply was a fallback. Decided in code, never by the model.

The owner (client dashboard) and the admin mark items reviewed; the admin
can also promote one into the industry template's FAQ (unanswered_api.py).
Every read and write takes the tenant ids the caller may see, so an owner
linked to two businesses sees exactly those two.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import asyncpg

SKIPPED = "skipped"          # a reply limit, an acknowledgement, the no-reply instruction
AI_ERROR = "ai_error"        # the model call failed or came back empty
SOFT_OFF = "soft_off"        # the client was switched off (controls.py)
PAUSED = "paused"            # the chat was paused, or a person had taken it over
ESCALATED = "escalated"      # an escalation keyword
POLICY_HOLD = "policy_hold"  # the reply was held for approval by policy.py
FALLBACK = "fallback"        # the reply contained one of unanswered.fallback_phrases
STAGING = "staging"          # staging mode: not a test chat
REASONS = (SKIPPED, AI_ERROR, SOFT_OFF, PAUSED, ESCALATED, POLICY_HOLD, FALLBACK, STAGING)

OPEN = "open"
REVIEWED = "reviewed"
ADDED = "added_to_template"
STATUSES = (OPEN, REVIEWED, ADDED)


def _row(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "chat_id": row["chat_id"],
        "message_id": row["message_id"],
        "reason": row["reason"],
        "detail": row["detail"],
        "status": row["status"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"].isoformat(timespec="seconds") if row["reviewed_at"] else None,
        "created_at": row["created_at"].isoformat(timespec="seconds"),
        "customer": row["display_name"],
        "text": row["text"],
    }


async def record(pool: asyncpg.Pool, *, tenant_id: int, session_id: str, chat_id: int,
                 message_id: Optional[int], reason: str, detail: str = "") -> Optional[int]:
    """Queue a customer message. One entry per message: a second reason for
    the same message is ignored. Returns the new id, or None."""
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    return await pool.fetchval(
        "INSERT INTO unanswered_queue (tenant_id, session_id, chat_id, message_id, reason, detail) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING RETURNING id",
        tenant_id, session_id, chat_id, message_id, reason, detail[:500],
    )


async def last_customer_message(pool: asyncpg.Pool, tenant_id: int, chat_id: int) -> Optional[int]:
    """The id of the newest message the customer sent in this chat."""
    return await pool.fetchval(
        "SELECT max(id) FROM messages WHERE tenant_id = $1 AND chat_id = $2 AND direction = 'in'",
        tenant_id, chat_id,
    )


_SELECT = """
SELECT q.*, c.display_name, m.text
  FROM unanswered_queue q
  LEFT JOIN conversations c ON c.tenant_id = q.tenant_id AND c.chat_id = q.chat_id
  LEFT JOIN messages m ON m.id = q.message_id AND m.tenant_id = q.tenant_id
"""


async def list_items(pool: asyncpg.Pool, tenant_ids: Iterable[int], *, status: Optional[str] = OPEN,
                     limit: int = 200) -> list[dict[str, Any]]:
    ids = list(tenant_ids)
    if not ids:
        return []
    if status is None:
        rows = await pool.fetch(_SELECT + " WHERE q.tenant_id = ANY($1) ORDER BY q.id DESC LIMIT $2", ids, limit)
    else:
        rows = await pool.fetch(_SELECT + " WHERE q.tenant_id = ANY($1) AND q.status = $2 ORDER BY q.id DESC LIMIT $3",
                                ids, status, limit)
    return [_row(r) for r in rows]


async def get(pool: asyncpg.Pool, tenant_ids: Iterable[int], item_id: int) -> Optional[dict[str, Any]]:
    row = await pool.fetchrow(_SELECT + " WHERE q.id = $1 AND q.tenant_id = ANY($2)", item_id, list(tenant_ids))
    return _row(row) if row else None


async def set_status(pool: asyncpg.Pool, tenant_ids: Iterable[int], item_id: int, status: str, *,
                     by: str) -> Optional[dict[str, Any]]:
    """Scoped by tenant_ids: an item of another tenant is "not found"."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    ids = list(tenant_ids)
    updated = await pool.fetchval(
        "UPDATE unanswered_queue SET status = $3, reviewed_by = CASE WHEN $3 = 'open' THEN NULL ELSE $4 END, "
        "reviewed_at = CASE WHEN $3 = 'open' THEN NULL ELSE now() END "
        "WHERE id = $1 AND tenant_id = ANY($2) RETURNING id",
        item_id, ids, status, by,
    )
    return await get(pool, ids, item_id) if updated else None


async def open_count(pool: asyncpg.Pool, tenant_id: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM unanswered_queue WHERE tenant_id = $1 AND status = 'open'", tenant_id,
    )
