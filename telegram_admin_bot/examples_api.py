"""Admin API for the reply examples (examples.py): how many there are,
rebuilding them, wording them, trying a message to see what it would find,
and switching a bad one off. Mounted by panel.py behind the admin login;
every route is under /api/finetune/ (admin only, never a manager's, see
staff.ADMIN_ONLY_PREFIXES). Not /api/finetune/examples: those are the
flagged chats (finetune_api.py).

The hourly round only works on industries where some account has
retrieval on; these routes work on any account, so the admin can build its
examples and judge what they find before switching it on.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import embeddings
import examples
import platform_api
import tenants

router = APIRouter()
ACTOR = audit.ADMIN
PATH = "/api/finetune/reply-examples"

# Audit events of this module (audit.py lists the older ones).
REPLY_EXAMPLES_REBUILT = "reply_examples_rebuilt"
REPLY_EXAMPLES_WORDED = "reply_examples_worded"
REPLY_EXAMPLE_CHANGED = "reply_example_changed"

# The admin waits for these: smaller than a scheduler round.
REBUILD_EMBED_BUDGET = 128
MAX_WORDINGS = examples.WORDINGS_PER_CALL * examples.WORDING_CALLS_PER_RUN
MAX_ITEMS = 200

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


async def _bundle(tenant_id: int) -> tenants.Bundle:
    return await platform_api.guarded(platform_api.store().bundle(tenant_id))


async def _status(bundle: tenants.Bundle, *, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    pool = _get_pool()
    tenant_id, industry_id = bundle.tenant["id"], bundle.tenant["industry_id"]
    model = await embeddings.health(client=client)
    return {
        "tenant_id": tenant_id,
        "industry_id": industry_id,
        "retrieval": bundle.config["retrieval"],
        # The hourly round works on this account only when some account of
        # its industry has retrieval on.
        "in_hourly_round": tenant_id in await examples.accounts(pool),
        "counts": await examples.counts(pool, tenant_id, industry_id),
        "embedder": {"up": model is not None, "model": model},
        "wordings_ready": bool(platform_api.platform_key()),
        "last_ingest_at": await examples.last_ingest(pool, tenant_id),
    }


@router.get(PATH)
async def api_status(tenant_id: int) -> dict[str, Any]:
    """What the account has: counts by source and phase, embedded or not,
    wordings, whether the embedder answers (and its model), the account's
    retrieval config, and when its chats were last read."""
    return await _status(await _bundle(tenant_id))


def _item(row: Any, wordings: list[str]) -> dict[str, Any]:
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "chat_id": row["chat_id"],
        "source": row["source"], "verdict": row["verdict"], "phase": row["phase"],
        "outcome": row["outcome"], "client_kind": row["client_kind"], "has_figures": row["has_figures"],
        "active": row["active"], "context_text": row["context_text"], "client_text": row["client_text"],
        "reply_text": row["reply_text"], "embedded": bool(row["embedded"]), "wordings": wordings,
        "created_at": row["created_at"],
    }


async def _items(where: str, *args: Any, limit: int = MAX_ITEMS, offset: int = 0) -> list[dict[str, Any]]:
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT e.*, EXISTS (SELECT 1 FROM example_queries q WHERE q.example_id = e.id AND NOT q.generated) "
        f"AS embedded FROM reply_examples e WHERE {where} ORDER BY e.id DESC LIMIT {int(limit)} OFFSET {int(offset)}",
        *args)
    wordings: dict[int, list[str]] = {}
    for q in await pool.fetch("SELECT example_id, text FROM example_queries WHERE generated "
                              "AND example_id = ANY($1::bigint[]) ORDER BY id", [r["id"] for r in rows]):
        wordings.setdefault(q["example_id"], []).append(q["text"])
    return [_item(r, wordings.get(r["id"], [])) for r in rows]


@router.get(PATH + "/items")
async def api_items(tenant_id: int, source: Optional[str] = None, phase: Optional[str] = None,
                    active: Optional[bool] = None, limit: int = Query(default=50, ge=1, le=MAX_ITEMS),
                    offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """The account's examples, newest first, masked as stored, with their
    wordings: to read them and switch a bad one off."""
    await _bundle(tenant_id)
    where, args = ["e.tenant_id = $1"], [tenant_id]
    for column, value in (("source", source), ("phase", phase), ("active", active)):
        if value is not None:
            args.append(value)
            where.append(f"e.{column} = ${len(args)}")
    total = await _get_pool().fetchval(
        f"SELECT count(*) FROM reply_examples e WHERE {' AND '.join(where)}", *args)
    return {"total": total, "items": await _items(" AND ".join(where), *args, limit=limit, offset=offset)}


class TenantBody(BaseModel):
    tenant_id: int


class RebuildBody(TenantBody):
    # Read every chat again instead of going on from the last run.
    from_start: bool = False


@router.post(PATH + "/rebuild")
async def api_rebuild(body: RebuildBody) -> dict[str, Any]:
    """Read the account's chats now (at most examples.CHATS_PER_RUN, from
    where the last run stopped unless `from_start`) and embed what is new
    (at most REBUILD_EMBED_BUDGET). `ingest.more`: call again to go on."""
    bundle = await _bundle(body.tenant_id)
    pool = _get_pool()
    ingested = await examples.ingest_account(pool, body.tenant_id, from_start=body.from_start)
    async with httpx.AsyncClient(timeout=examples.WORDINGS_TIMEOUT) as client:
        embedded = await examples.embed_pending(pool, [body.tenant_id], client=client, budget=REBUILD_EMBED_BUDGET)
        status = await _status(bundle, client=client)
    await audit.record(pool, tenant_id=body.tenant_id, actor=ACTOR, event=REPLY_EXAMPLES_REBUILT, payload={
        "from_start": body.from_start, "chats": ingested["chats"], "added": ingested["added"],
        "removed": ingested["removed"], "embedded": embedded["embedded"]})
    return {"ingest": ingested, "embedded": embedded["embedded"], **status}


class WordingsBody(TenantBody):
    limit: int = Field(examples.WORDINGS_PER_CALL, ge=1, le=MAX_WORDINGS)


@router.post(PATH + "/wordings")
async def api_wordings(body: WordingsBody) -> dict[str, Any]:
    """Other wordings of the client side for up to `limit` examples without
    any yet: one DeepSeek call per examples.WORDINGS_PER_CALL, on the
    platform key, metered as platform spend."""
    bundle = await _bundle(body.tenant_id)
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to word examples.")
    pool = _get_pool()
    async with httpx.AsyncClient(timeout=examples.WORDINGS_TIMEOUT) as client:
        if await embeddings.health(client=client) is None:
            raise HTTPException(status_code=503, detail="The embedder is not running: start it, then try again.")
        result = await examples.make_wordings(pool, [body.tenant_id], api_key=api_key, client=client,
                                              limit=body.limit)
        status = await _status(bundle, client=client)
    await audit.record(pool, tenant_id=body.tenant_id, actor=ACTOR, event=REPLY_EXAMPLES_WORDED,
                       payload={k: result[k] for k in ("examples", "wordings", "calls")})
    return {"result": result, **status}


class TryBody(TenantBody):
    text: str = Field(..., min_length=1, max_length=2000)
    # The chat's phase to match; null: any.
    phase: Optional[str] = examples.BEFORE_TIME


@router.post(PATH + "/try")
async def api_try(body: TryBody) -> dict[str, Any]:
    """What the bot would find for a client message, switched on or not: the
    best matches with their similarity, which of them the note would show
    (k, min_similarity, max_chars), and the note itself."""
    if body.phase is not None and body.phase not in examples.PHASES:
        raise HTTPException(status_code=400, detail=f"Unknown phase: {body.phase}.")
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Type a client message to try.")
    bundle = await _bundle(body.tenant_id)
    settings = bundle.config["retrieval"]
    async with httpx.AsyncClient(timeout=examples.EMBED_TIMEOUT) as client:
        found = await examples.try_message(_get_pool(), tenant_id=body.tenant_id,
                                           industry_id=bundle.tenant["industry_id"], settings=settings,
                                           text=body.text, phase=body.phase, client=client)
    if found is None:
        raise HTTPException(status_code=503, detail="The embedder is not running: start it, then try again.")
    return {"tenant_id": body.tenant_id, "phase": body.phase, "retrieval": settings, **found}


class ChangeBody(BaseModel):
    active: bool


@router.patch(PATH + "/{example_id}")
async def api_change(example_id: int, body: ChangeBody) -> dict[str, Any]:
    """Switch one example off (never shown again) or back on."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow("UPDATE reply_examples SET active = $2 WHERE id = $1 RETURNING tenant_id",
                                 example_id, body.active)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown reply example")
        await audit.record(con, tenant_id=row["tenant_id"], actor=ACTOR, event=REPLY_EXAMPLE_CHANGED,
                           payload={"example_id": example_id, "active": body.active})
    [item] = await _items("e.id = $1", example_id, limit=1)
    return item
