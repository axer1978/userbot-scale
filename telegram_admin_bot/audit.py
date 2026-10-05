"""The audit log: who did what to which tenant, when, and why.

Every outbound bot message and every tenant state change writes one row.
The table is append-only in Postgres itself (see migration 0002), so a row
cannot be edited or removed after the fact, by this code or anyone else.

`record()` takes a pool or a connection: pass the connection when the change
being audited happens in a transaction, so the audit row commits or rolls
back with it.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

import asyncpg

# Actors
ADMIN = "admin"
BOT = "bot"
SYSTEM = "system"
# The person using the account's own Telegram (the business owner or staff).
OWNER = "owner"
MIGRATION = "migration"

# Events
MESSAGE_SENT = "message_sent"
MESSAGES_DELETED = "messages_deleted"
POLICY_HOLD = "policy_hold"
TENANT_CREATED = "tenant_created"
TENANT_UPDATED = "tenant_updated"
CONFIG_CHANGED = "config_changed"
CONFIG_PROPOSED = "config_proposed"
INDUSTRY_CREATED = "industry_created"
INDUSTRY_CONFIG_CHANGED = "industry_config_changed"
PROMPT_VERSION_CREATED = "prompt_version_created"
PROMPT_ROLLBACK = "prompt_rollback"
PROMPT_PINNED = "prompt_pinned"
LEGACY_IMPORTED = "legacy_imported"
ACCOUNT_PAUSED = "account_paused"
ACCOUNT_RESUMED = "account_resumed"
ACCOUNT_HALTED = "account_halted"
BOOKING_CREATED = "booking_created"
# payload.action says which transition (booking_states.py).
BOOKING_CHANGED = "booking_changed"
BOOKINGS_IMPORTED = "bookings_imported"
AVAILABILITY_CHANGED = "availability_changed"
WAITLIST_CHANGED = "waitlist_changed"
ARRIVAL_PHOTO_CHECKED = "arrival_photo_checked"
REMINDER_SENT = "reminder_sent"
# The bot did not answer a message: a reply limit, an acknowledgement, or
# the tenant's own no-reply instruction.
REPLY_SKIPPED = "reply_skipped"
AI_LIMIT_REACHED = "ai_limit_reached"
# Soft-off (controls.py): payload.kind says which hold was added or lifted.
TENANT_SOFT_OFF = "tenant_soft_off"
TENANT_RESUMED = "tenant_resumed"
GLOBAL_STOP = "global_stop"
GLOBAL_RESUMED = "global_resumed"
# The account's Telegram session was logged out and its key deleted.
HARD_OFF = "hard_off"
BILLING_CHANGED = "billing_changed"
# payload.trigger: new_login, volume or tripwire (anomaly.py).
ANOMALY_DETECTED = "anomaly_detected"
ESCALATED = "escalated"
HUMAN_TAKEOVER = "human_takeover"
TAKEOVER_ENDED = "takeover_ended"
# payload.proxy: type, host, port, user; never the password.
PROXY_CHANGED = "proxy_changed"

Executor = Union[asyncpg.Pool, asyncpg.Connection]


async def record(
    executor: Executor,
    *,
    tenant_id: Optional[int],
    actor: str,
    event: str,
    reason: str = "",
    payload: Optional[dict[str, Any]] = None,
) -> None:
    await executor.execute(
        "INSERT INTO audit_log (tenant_id, actor, event, reason, payload) VALUES ($1, $2, $3, $4, $5::jsonb)",
        tenant_id,
        actor,
        event,
        reason or "",
        json.dumps(payload or {}, default=str),
    )


async def list_events(
    pool: asyncpg.Pool, *, tenant_id: Optional[int] = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Newest first. With tenant_id, only that tenant's rows; without it,
    everything (platform admin view)."""
    async with pool.acquire() as con:
        if tenant_id is None:
            rows = await con.fetch("SELECT * FROM audit_log ORDER BY id DESC LIMIT $1", limit)
        else:
            rows = await con.fetch(
                "SELECT * FROM audit_log WHERE tenant_id = $1 ORDER BY id DESC LIMIT $2", tenant_id, limit
            )
    return [
        {
            "id": r["id"],
            "tenant_id": r["tenant_id"],
            "actor": r["actor"],
            "event": r["event"],
            "reason": r["reason"],
            "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
            "created_at": r["created_at"].isoformat(timespec="seconds"),
        }
        for r in rows
    ]
