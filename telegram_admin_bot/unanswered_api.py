"""Admin API for the unanswered queue: list, mark reviewed, promote an
answer into the industry template's FAQ. Mounted by panel.py behind the
admin login, so the admin sees every client's queue.

The queue itself is filled by the running accounts (session_runtime.py,
reasons in unanswered.py). Promoting is the one write that reaches the
bot's behaviour: the admin types the answer (nothing is generated), it is
appended to the `faq` section of the client's *industry* template as a new
template version (tenants.TenantStore.save_industry_template, audited and
versioned like any template save, so it can be rolled back), and every
account in that industry is told to reload, as platform_api does.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import platform_api
import prompt_layers
import tenants
import unanswered

router = APIRouter()
ACTOR = audit.ADMIN

# Audit events of this module (audit.py lists the older ones).
UNANSWERED_REVIEWED = "unanswered_reviewed"
UNANSWERED_REOPENED = "unanswered_reopened"
UNANSWERED_PROMOTED = "unanswered_promoted"

STATUS_FILTERS = (*unanswered.STATUSES, "all")
MAX_LIMIT = 500

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


async def _tenants() -> dict[int, dict[str, Any]]:
    rows = await _get_pool().fetch("SELECT id, name, industry_id FROM tenants ORDER BY lower(name), id")
    return {r["id"]: dict(r) for r in rows}


async def _item(item_id: int, known: dict[int, dict[str, Any]]) -> dict[str, Any]:
    item = await unanswered.get(_get_pool(), known.keys(), item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown item")
    return item


def _named(item: dict[str, Any], known: dict[int, dict[str, Any]]) -> dict[str, Any]:
    tenant = known.get(item["tenant_id"]) or {}
    return {**item, "tenant_name": tenant.get("name") or f"Client {item['tenant_id']}"}


# ------------------------------------------------------------------ reads


@router.get("/api/unanswered")
async def api_list(tenant_id: Optional[int] = None, status: str = unanswered.OPEN,
                   limit: int = 200) -> dict[str, Any]:
    if status not in STATUS_FILTERS:
        raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(STATUS_FILTERS)}")
    known = await _tenants()
    ids = [tenant_id] if tenant_id is not None else list(known)
    if tenant_id is not None and tenant_id not in known:
        raise HTTPException(status_code=404, detail="Unknown client")
    items = await unanswered.list_items(_get_pool(), ids, status=None if status == "all" else status,
                                        limit=max(1, min(int(limit), MAX_LIMIT)))
    return {
        "items": [_named(i, known) for i in items],
        "open": await _open_total(),
        "tenants": [{"id": t["id"], "name": t["name"]} for t in known.values()],
    }


async def _open_total() -> int:
    return await _get_pool().fetchval("SELECT count(*) FROM unanswered_queue WHERE status = 'open'")


@router.get("/api/unanswered/count")
async def api_count() -> dict[str, int]:
    """For the top bar's badge."""
    return {"open": await _open_total()}


# ------------------------------------------------------------------ writes


async def _set(item_id: int, status: str, event: str, reason: str) -> dict[str, Any]:
    pool = _get_pool()
    known = await _tenants()
    await _item(item_id, known)
    item = await unanswered.set_status(pool, known.keys(), item_id, status, by=ACTOR)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown item")
    await audit.record(pool, tenant_id=item["tenant_id"], actor=ACTOR, event=event, reason=reason,
                       payload={"item_id": item_id, "chat_id": item["chat_id"], "reason": item["reason"]})
    return _named(item, known)


@router.post("/api/unanswered/{item_id}/reviewed")
async def api_reviewed(item_id: int) -> dict[str, Any]:
    return await _set(item_id, unanswered.REVIEWED, UNANSWERED_REVIEWED, "marked reviewed")


@router.post("/api/unanswered/{item_id}/reopen")
async def api_reopen(item_id: int) -> dict[str, Any]:
    return await _set(item_id, unanswered.OPEN, UNANSWERED_REOPENED, "reopened")


class PromoteBody(BaseModel):
    # Empty = the customer's message, as the question.
    question: str = Field("", max_length=2000)
    answer: str = Field(..., max_length=prompt_layers.MAX_SECTION_CHARS)


def faq_block(question: str, answer: str) -> str:
    """One Q&A entry for the FAQ section. The question is kept on one line
    so every entry reads "Q: …" then "A: …"."""
    return f"Q: {' '.join(question.split())}\nA: {answer.strip()}"


@router.post("/api/unanswered/{item_id}/promote")
async def api_promote(item_id: int, body: PromoteBody) -> dict[str, Any]:
    pool = _get_pool()
    known = await _tenants()
    item = await _item(item_id, known)
    question = body.question.strip() or (item["text"] or "").strip()
    answer = body.answer.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Write the question (the customer's message is gone).")
    if not answer:
        raise HTTPException(status_code=400, detail="Write the answer to add.")

    store = tenants.TenantStore(pool)
    industry_id = known[item["tenant_id"]]["industry_id"]
    industry = await store.get_industry(industry_id)
    current = (await store.version(tenants.INDUSTRY, industry_id, industry["template_version"]))["content"]
    sections = dict(current.get("sections") or {})
    faq = (sections.get("faq") or "").strip()
    block = faq_block(question, answer)
    new_faq = f"{faq}\n\n{block}" if faq else block
    if len(new_faq) > prompt_layers.MAX_SECTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"The FAQ of {industry['name']} would be {len(new_faq)} characters; the limit is "
                   f"{prompt_layers.MAX_SECTION_CHARS}. Shorten the answer or tidy the FAQ first.",
        )
    try:
        saved = await store.save_industry_template(
            industry_id, {"sections": {**sections, "faq": new_faq}}, actor=ACTOR,
            note=f"From the unanswered queue #{item_id}",
        )
    except prompt_layers.PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = await unanswered.set_status(pool, known.keys(), item_id, unanswered.ADDED, by=ACTOR)
    await audit.record(
        pool, tenant_id=item["tenant_id"], actor=ACTOR, event=UNANSWERED_PROMOTED,
        reason=f"added to the FAQ of {industry['name']}",
        payload={"item_id": item_id, "industry_id": industry_id, "version": saved["template_version"],
                 "question": question, "answer": answer},
    )
    # Every account in the industry picks up the new template.
    await platform_api._reload_industry(industry_id)
    pinned = await pool.fetchval(
        "SELECT count(*) FROM tenants WHERE industry_id = $1 AND prompt_pin_version IS NOT NULL", industry_id,
    )
    return {
        "item": _named(updated or item, known),
        "industry": {"id": industry_id, "name": industry["name"], "template_version": saved["template_version"]},
        # Clients pinned to an older template version don't see it until unpinned.
        "pinned_clients": pinned,
    }
