"""Admin API for review batches (trainer export) and the onboarding wizard's
server side. Mounted by panel.py behind the admin login.

Review batches
--------------
A trainer goes through what the bot actually sent to a client's customers
and marks each reply approve / reject / edit. Approved and edited replies,
each with the conversation that led to it, are exported as JSONL for
fine-tuning. A batch is a snapshot taken when it is created: the context and
the reply are copied into `review_items`, so later edits to the chat (or the
message being deleted) do not change what was reviewed.

What goes into a batch: every message of that tenant with
`direction='out' AND llm_model IS NOT NULL AND status='sent'` whose
`created_at` falls in [date_from 00:00, date_to + 1 day 00:00) in the
tenant's own timezone. `llm_model` is only set on replies the model wrote,
so hand-typed messages and panel sends never become training targets
(they do appear in the context, as the assistant, because the customer saw
them). The context is built the way `Database.get_history_for_ai` builds
the model's history: only messages that crossed the wire (received / sent),
no notes, errors, drafts or rejections, and no empty texts.

Every query is scoped by tenant (on creation) or by batch (afterwards),
and item rows carry the batch's tenant, so one client's batch can never
show another client's messages. Creation, marking done, deleting and each
export write an audit row; individual decisions do not (a batch can hold
2000 of them), they carry `decided_by` / `decided_at` instead.

Onboarding
----------
The "New client" wizard runs on the existing routes (tenant PATCH, config
PUT, the account sign-in). The two GET routes here only summarise, per
tenant, which of its steps are already done, so the wizard can be resumed.
They write nothing.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import audit
import billing
import tenant_config
import tenants

router = APIRouter()

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


ACTOR = audit.ADMIN

# Audit events of this module (audit.py lists the older ones).
REVIEW_BATCH_CREATED = "review_batch_created"
REVIEW_BATCH_DONE = "review_batch_done"
REVIEW_BATCH_DELETED = "review_batch_deleted"
REVIEW_EXPORTED = "review_exported"

# A batch bigger than this is refused rather than truncated: a silently cut
# batch would leave the trainer thinking they had seen the whole range.
MAX_ITEMS = 2000
# Messages of the chat before the reply that go into its context.
CONTEXT_MESSAGES = 20
PAGE_DEFAULT = 50
PAGE_MAX = 200

DECISIONS = ("approve", "reject", "edit")

# The candidate replies of one tenant in one local-date range. $1 tenant,
# $2 date_from, $3 date_to, $4 timezone name. `timestamp AT TIME ZONE zone`
# reads a wall-clock time as local to that zone, so the range follows the
# tenant's midnight (and its DST changes), not the server's.
_CANDIDATES = """
    FROM messages m
   WHERE m.tenant_id = $1
     AND m.direction = 'out'
     AND m.status = 'sent'
     AND m.llm_model IS NOT NULL
     AND btrim(m.text) <> ''
     AND m.created_at >= ($2::date)::timestamp AT TIME ZONE $4
     AND m.created_at <  ($3::date + 1)::timestamp AT TIME ZONE $4
"""

# The context of each candidate: the previous messages of the same chat and
# tenant that actually crossed the wire, oldest first.
_CONTEXT = f"""
    COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                   'role', CASE WHEN p.direction = 'in' THEN 'user' ELSE 'assistant' END,
                   'content', p.text) ORDER BY p.id)
          FROM (SELECT id, direction, text FROM messages
                 WHERE tenant_id = m.tenant_id
                   AND chat_id = m.chat_id
                   AND id < m.id
                   AND status IN ('received', 'sent')
                   AND direction IN ('in', 'out')
                   AND btrim(text) <> ''
                 ORDER BY id DESC LIMIT {CONTEXT_MESSAGES}) p
    ), '[]'::jsonb)
"""

_COUNTS = """
    count(i.id)                                   AS total,
    count(i.id) FILTER (WHERE i.decision = 'approve') AS approved,
    count(i.id) FILTER (WHERE i.decision = 'reject')  AS rejected,
    count(i.id) FILTER (WHERE i.decision = 'edit')    AS edited,
    count(i.id) FILTER (WHERE i.decision IS NULL)     AS undecided
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _batch(row: Any) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "date_from": row["date_from"].isoformat(),
        "date_to": row["date_to"].isoformat(),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
    }
    if "total" in row.keys():
        out["counts"] = {k: row[k] for k in ("total", "approved", "rejected", "edited", "undecided")}
    return out


def _item(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "chat_id": row["chat_id"],
        "chat_name": row["chat_name"] or "",
        "message_id": row["message_id"],
        "sent_at": row["sent_at"].isoformat(timespec="seconds") if row["sent_at"] else None,
        "context": _json(row["context"]),
        "reply": row["reply"],
        "decision": row["decision"],
        "edited_text": row["edited_text"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"].isoformat(timespec="seconds") if row["decided_at"] else None,
    }


async def _get_batch(batch_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow(
        f"""
        SELECT b.*, {_COUNTS}
          FROM review_batches b LEFT JOIN review_items i ON i.batch_id = b.id
         WHERE b.id = $1
         GROUP BY b.id
        """,
        batch_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No review batch {batch_id}")
    return _batch(row)


# ------------------------------------------------------------ review: API


class BatchBody(BaseModel):
    tenant_id: int
    name: str = Field(min_length=1, max_length=200)
    date_from: date
    date_to: date


class DecisionBody(BaseModel):
    decision: str
    edited_text: Optional[str] = None


@router.post("/api/review/batches")
async def api_create_batch(body: BatchBody) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the batch a name.")
    if body.date_to < body.date_from:
        raise HTTPException(status_code=400, detail="The end date is before the start date.")
    pool = _get_pool()
    if await pool.fetchval("SELECT 1 FROM tenants WHERE id = $1", body.tenant_id) is None:
        raise HTTPException(status_code=404, detail=f"No client {body.tenant_id}")
    tz = await billing._timezone(pool, body.tenant_id)
    args = (body.tenant_id, body.date_from, body.date_to, tz)

    async with pool.acquire() as con, con.transaction():
        count = await con.fetchval(f"SELECT count(*) {_CANDIDATES}", *args)
        if count == 0:
            raise HTTPException(status_code=400, detail="The bot sent no replies to this client's customers "
                                                        "in that date range.")
        if count > MAX_ITEMS:
            raise HTTPException(status_code=400, detail=f"That range holds {count} replies; a batch takes at "
                                                        f"most {MAX_ITEMS}. Pick a shorter date range.")
        batch_id = await con.fetchval(
            "INSERT INTO review_batches (tenant_id, name, date_from, date_to, created_by) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            body.tenant_id, name, body.date_from, body.date_to, ACTOR,
        )
        await con.execute(
            f"""
            INSERT INTO review_items (batch_id, tenant_id, chat_id, message_id, context, reply)
            SELECT $5::int, m.tenant_id, m.chat_id, m.id, {_CONTEXT}, m.text
            {_CANDIDATES}
             ORDER BY m.id
            """,
            *args, batch_id,
        )
        await audit.record(
            con, tenant_id=body.tenant_id, actor=ACTOR, event=REVIEW_BATCH_CREATED, reason=name,
            payload={"batch_id": batch_id, "date_from": body.date_from.isoformat(),
                     "date_to": body.date_to.isoformat(), "timezone": tz, "items": count},
        )
    return await _get_batch(batch_id)


@router.get("/api/review/batches")
async def api_list_batches(tenant_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Newest first; with tenant_id, only that client's batches."""
    rows = await _get_pool().fetch(
        f"""
        SELECT b.*, {_COUNTS}
          FROM review_batches b LEFT JOIN review_items i ON i.batch_id = b.id
         WHERE $1::int IS NULL OR b.tenant_id = $1
         GROUP BY b.id
         ORDER BY b.id DESC
        """,
        tenant_id,
    )
    return [_batch(r) for r in rows]


@router.get("/api/review/batches/{batch_id}")
async def api_batch(batch_id: int, offset: int = 0, limit: int = PAGE_DEFAULT) -> dict[str, Any]:
    """The batch, its counts, and one page of items in the order they were
    sent. `first_undecided` is the offset of the first item without a
    decision (None when all are decided), so the UI can resume there."""
    batch = await _get_batch(batch_id)
    offset = max(0, offset)
    limit = max(1, min(limit, PAGE_MAX))
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT i.*, m.created_at AS sent_at,
               (SELECT c.display_name FROM conversations c
                 WHERE c.tenant_id = i.tenant_id AND c.chat_id = i.chat_id) AS chat_name
          FROM review_items i
          LEFT JOIN messages m ON m.id = i.message_id AND m.tenant_id = i.tenant_id
         WHERE i.batch_id = $1
         ORDER BY i.id
         OFFSET $2 LIMIT $3
        """,
        batch_id, offset, limit,
    )
    first_undecided = await pool.fetchval(
        """
        SELECT count(*) FROM review_items
         WHERE batch_id = $1
           AND id < (SELECT min(id) FROM review_items WHERE batch_id = $1 AND decision IS NULL)
        """,
        batch_id,
    )
    batch["first_undecided"] = first_undecided if batch["counts"]["undecided"] else None
    batch["offset"] = offset
    batch["limit"] = limit
    batch["items"] = [_item(r) for r in rows]
    return batch


@router.post("/api/review/items/{item_id}")
async def api_decide(item_id: int, body: DecisionBody) -> dict[str, Any]:
    if body.decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="decision must be approve, reject or edit")
    edited = (body.edited_text or "").strip()
    if body.decision == "edit" and not edited:
        raise HTTPException(status_code=400, detail="An edit needs the corrected reply text.")
    row = await _get_pool().fetchrow(
        """
        WITH updated AS (
            UPDATE review_items SET decision = $2, edited_text = $3, decided_by = $4, decided_at = now()
             WHERE id = $1
            RETURNING *
        )
        SELECT u.*, m.created_at AS sent_at,
               (SELECT c.display_name FROM conversations c
                 WHERE c.tenant_id = u.tenant_id AND c.chat_id = u.chat_id) AS chat_name
          FROM updated u LEFT JOIN messages m ON m.id = u.message_id AND m.tenant_id = u.tenant_id
        """,
        item_id, body.decision, edited if body.decision == "edit" else None, ACTOR,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No review item {item_id}")
    return _item(row)


@router.post("/api/review/batches/{batch_id}/done")
async def api_batch_done(batch_id: int) -> dict[str, Any]:
    batch = await _get_batch(batch_id)
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        await con.execute("UPDATE review_batches SET status = 'done' WHERE id = $1", batch_id)
        await audit.record(con, tenant_id=batch["tenant_id"], actor=ACTOR, event=REVIEW_BATCH_DONE,
                           reason=batch["name"], payload={"batch_id": batch_id, "counts": batch["counts"]})
    return await _get_batch(batch_id)


@router.delete("/api/review/batches/{batch_id}")
async def api_delete_batch(batch_id: int) -> dict[str, Any]:
    batch = await _get_batch(batch_id)
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        await con.execute("DELETE FROM review_batches WHERE id = $1", batch_id)  # items cascade
        await audit.record(con, tenant_id=batch["tenant_id"], actor=ACTOR, event=REVIEW_BATCH_DELETED,
                           reason=batch["name"], payload={"batch_id": batch_id, "counts": batch["counts"]})
    return {"deleted": batch_id}


def export_lines(items: list[dict[str, Any]], *, tenant_id: int, batch_id: int) -> list[str]:
    """One JSON object per approved or edited item: the context, then the
    final assistant turn (the edited text when there is one). No system
    prompt: the prompt is the platform owner's and changes over time; the
    trainer adds whatever system turn the training run needs."""
    lines = []
    for item in items:
        if item["decision"] not in ("approve", "edit"):
            continue
        final = item["edited_text"] if item["decision"] == "edit" and item["edited_text"] else item["reply"]
        record = {
            "messages": [*_json(item["context"]), {"role": "assistant", "content": final}],
            "meta": {"tenant_id": tenant_id, "batch_id": batch_id, "item_id": item["id"],
                     "decision": item["decision"]},
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    return lines


@router.get("/api/review/batches/{batch_id}/export.jsonl")
async def api_export(batch_id: int) -> Response:
    batch = await _get_batch(batch_id)
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, context, reply, decision, edited_text FROM review_items "
        "WHERE batch_id = $1 AND tenant_id = $2 AND decision IN ('approve', 'edit') ORDER BY id",
        batch_id, batch["tenant_id"],
    )
    lines = export_lines([dict(r) for r in rows], tenant_id=batch["tenant_id"], batch_id=batch_id)
    await audit.record(pool, tenant_id=batch["tenant_id"], actor=ACTOR, event=REVIEW_EXPORTED,
                       reason=batch["name"], payload={"batch_id": batch_id, "lines": len(lines)})
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", batch["name"]).strip("-")[:60] or "batch"
    filename = f"review-{batch['tenant_id']}-{batch_id}-{slug}.jsonl"
    return Response(
        content="".join(line + "\n" for line in lines).encode("utf-8"),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------- onboarding


def _status(row: Any) -> dict[str, Any]:
    """Which wizard steps a tenant has behind it. Derived from what is
    stored, never from a separate progress flag, so a change made outside
    the wizard (Clients → Config) counts too.

    - business: the tenant has a name of its own, i.e. not the account's id
      or label it was created with (SessionRegistry.create names it so).
    - settings: the owner's Telegram (booking.provider) is set; without it
      nothing can reach the owner.
    - staging: turned on at some point (it is on, or test chats are kept).
    - live: named, owner set, and staging off, i.e. it answers everyone.
      Skipping staging is allowed; the wizard only recommends it.
    """
    tenant = tenants._tenant(row)
    try:
        config = tenant_config.resolve(_json(row["default_config"]), tenant["config_json"]).config
        valid = True
    except tenant_config.ConfigError:
        config, valid = tenant_config.resolve(None, None).config, False
    session_names = {n for n in (row["session_id"], row["session_label"]) if n}
    named = bool(tenant["name"].strip()) and tenant["name"] not in session_names
    staging = config.staging
    steps = {
        "account": bool(row["session_id"]) and bool(row["session_active"]),
        "business": named,
        "settings": bool(config.booking.provider.strip()),
        "staging": staging.enabled or bool(staging.test_chats),
    }
    steps["live"] = steps["business"] and steps["settings"] and not staging.enabled
    order = ("account", "business", "settings", "staging", "live")
    return {
        "tenant_id": tenant["id"],
        "name": tenant["name"],
        "industry_id": tenant["industry_id"],
        "industry": row["industry_name"],
        "session_id": row["session_id"],
        "session_label": row["session_label"] or "",
        "session_state": row["session_state"],
        "config_valid": valid,
        "config_revision": tenant["config_revision"],
        "staging": {"enabled": staging.enabled, "test_chats": list(staging.test_chats)},
        "provider": config.booking.provider,
        "steps": steps,
        "next_step": next((s for s in order if not steps[s]), None),
        "configured": steps["business"] and steps["settings"],
    }


_ONBOARDING_SQL = """
    SELECT t.*, i.default_config, i.name AS industry_name,
           s.label AS session_label, s.state AS session_state, s.is_active AS session_active
      FROM tenants t
      JOIN industries i ON i.id = t.industry_id
      LEFT JOIN telegram_sessions s ON s.session_id = t.session_id
"""


@router.get("/api/onboarding")
async def api_onboarding_list() -> list[dict[str, Any]]:
    """Every tenant with its wizard status; the wizard's first step offers
    the ones that are not configured yet."""
    rows = await _get_pool().fetch(_ONBOARDING_SQL + " ORDER BY t.id DESC")
    return [_status(r) for r in rows]


@router.get("/api/onboarding/{tenant_id}")
async def api_onboarding(tenant_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow(_ONBOARDING_SQL + " WHERE t.id = $1", tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No client {tenant_id}")
    return _status(row)
