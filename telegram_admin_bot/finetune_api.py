"""Admin API for finetuning from chats: screenshots or transcripts (finetune.py).

Mounted by panel.py behind the admin login.

- One finetune template per industry, in platform_settings under
  'finetune_template:<industry id>'.
- A run is started with the screenshots (read by the vision model) or with
  chats the admin transcribed (read by DeepSeek on the platform key), and
  works in the background; the page polls it. The chats stay in memory for
  the run only and are never written anywhere.
- Applying a run saves ordinary prompt versions through tenants.py, the
  industry standard first, then the business layer, and reloads the
  accounts affected, as platform_api does. Either part can be left out,
  and both can be edited before applying.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import finetune
import llm_usage
import platform_api
import prompt_layers
import tenants
import vision

log = logging.getLogger("finetune_api")

router = APIRouter()
ACTOR = audit.ADMIN

# Audit events of this module (audit.py lists the older ones).
FINETUNE_TEMPLATE_CHANGED = "finetune_template_changed"
FINETUNE_STARTED = "finetune_started"
FINETUNE_APPLIED = "finetune_applied"
FINETUNE_DISCARDED = "finetune_discarded"

# A run still "running" after this was cut off by a restart: its task is gone.
STALE_MINUTES = 20
RUN_PATH = "/api/finetune/runs"
TEXT_RUN_PATH = "/api/finetune/text-runs"

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731
# Keeps background runs referenced until they finish.
_tasks: set[asyncio.Task] = set()


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


def _template_key(industry_id: int) -> str:
    return f"finetune_template:{industry_id}"


async def load_template(industry_id: int) -> str:
    value = await _get_pool().fetchval("SELECT value FROM platform_settings WHERE key = $1",
                                       _template_key(industry_id))
    value = tenants._json(value) if value is not None else None
    return value.get("template", "") if isinstance(value, dict) else ""


async def businesses_so_far(industry_id: int) -> int:
    """Businesses whose run updated this industry's standard."""
    return await _get_pool().fetchval(
        "SELECT count(DISTINCT tenant_id) FROM finetune_runs "
        "WHERE industry_id = $1 AND status = 'applied' AND applied ? 'industry_version'",
        industry_id,
    )


async def _live_sections(industry: dict[str, Any]) -> dict[str, str]:
    live = await platform_api.store().version(tenants.INDUSTRY, industry["id"], industry["template_version"])
    return live["content"].get("sections") or {}


def _run(row: Any, *, full: bool = True) -> dict[str, Any]:
    run = {
        "id": row["id"], "tenant_id": row["tenant_id"], "industry_id": row["industry_id"],
        "status": row["status"], "model": row["model"], "source": row["source"],
        "files": tenants._json(row["files"]),
        "industry_version": row["industry_version"], "error": row["error"],
        "applied": tenants._json(row["applied"]), "created_by": row["created_by"],
        "created_at": row["created_at"], "finished_at": row["finished_at"], "applied_at": row["applied_at"],
        "interrupted": bool(row["interrupted"]),
    }
    if run["interrupted"]:
        run["status"] = "failed"
        run["error"] = "The run was cut off (the panel restarted). Start it again."
    if full:
        run["result"] = tenants._json(row["result"])
        run["raw_output"] = row["raw_output"]
    return run


_SELECT = (f"SELECT *, (status = 'running' AND created_at < now() - interval '{STALE_MINUTES} minutes') "
           "AS interrupted FROM finetune_runs")


async def _get_run(run_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow(_SELECT + " WHERE id = $1", run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown finetune run")
    return _run(row)


# ------------------------------------------------------------------ templates


class TemplateBody(BaseModel):
    template: str = Field(..., max_length=finetune.MAX_TEMPLATE_CHARS)
    reason: str = ""


@router.get("/api/finetune/industries/{industry_id}")
async def api_industry(industry_id: int) -> dict[str, Any]:
    industry = await platform_api.guarded(platform_api.store().get_industry(industry_id))
    return {
        "industry": {"id": industry["id"], "name": industry["name"],
                     "template_version": industry["template_version"]},
        "template": await load_template(industry_id),
        # Loaded into the editor on request; nothing runs on it until saved.
        "default_template": finetune.default_template(industry["name"]),
        "placeholders": list(finetune.PLACEHOLDERS),
        "businesses_so_far": await businesses_so_far(industry_id),
        "has_standard": bool(await _live_sections(industry)),
        # Which kinds of run the server can do now: the page offers those.
        "screenshots_ready": all(vision.endpoint_from_env()),
        "transcripts_ready": bool(platform_api.platform_key()),
    }


@router.put("/api/finetune/industries/{industry_id}/template")
async def api_save_template(industry_id: int, body: TemplateBody) -> dict[str, Any]:
    await platform_api.guarded(platform_api.store().get_industry(industry_id))
    try:
        template = finetune.check_template(body.template)
    except finetune.FinetuneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            "INSERT INTO platform_settings (key, value, updated_by, updated_at) VALUES ($1, $2::jsonb, $3, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()",
            _template_key(industry_id), json.dumps({"template": template}), ACTOR,
        )
        await audit.record(con, tenant_id=None, actor=ACTOR, event=FINETUNE_TEMPLATE_CHANGED,
                           reason=body.reason, payload={"industry_id": industry_id, "chars": len(template)})
    return await api_industry(industry_id)


# ----------------------------------------------------------------------- runs


class Screenshot(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # base64, without the data: prefix
    data: str


class RunBody(BaseModel):
    tenant_id: int
    images: list[Screenshot] = Field(..., min_length=1, max_length=finetune.MAX_IMAGES)


def _model(bundle: tenants.Bundle) -> str:
    return (os.getenv("FINETUNE_MODEL") or "").strip() or bundle.config["vision"]["model"]


@router.get("/api/finetune/runs")
async def api_runs(tenant_id: Optional[int] = None, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    if tenant_id is None:
        rows = await _get_pool().fetch(_SELECT + " ORDER BY id DESC LIMIT $1", limit)
    else:
        rows = await _get_pool().fetch(_SELECT + " WHERE tenant_id = $1 ORDER BY id DESC LIMIT $2", tenant_id, limit)
    return [_run(r, full=False) for r in rows]


@router.get("/api/finetune/runs/{run_id}")
async def api_run(run_id: int) -> dict[str, Any]:
    """The run, with what is live now to compare it against."""
    run = await _get_run(run_id)
    s = platform_api.store()
    bundle = await platform_api.guarded(s.bundle(run["tenant_id"]))
    industry = await s.get_industry(run["industry_id"])
    run["current"] = {
        "industry_version": industry["template_version"],
        "industry_sections": await _live_sections(industry),
        "business_layer": bundle.layers["client"],
        "tenant_industry_id": bundle.tenant["industry_id"],
    }
    run["stale"] = _stale_reason(run, industry, bundle.tenant)
    run["sections"] = [{"key": key, "heading": heading, "append_only": key in prompt_layers.APPEND_ONLY_SECTIONS}
                       for key, heading in prompt_layers.SECTIONS]
    run["addendum_limit"] = prompt_layers.MAX_ADDENDUM_CHARS
    return run


def _stale_reason(run: dict[str, Any], industry: dict[str, Any], tenant: dict[str, Any]) -> str:
    if tenant["industry_id"] != run["industry_id"]:
        return "This client has moved to another industry since the run."
    if industry["template_version"] != run["industry_version"]:
        return (f"The industry template changed since the run (v{run['industry_version']} → "
                f"v{industry['template_version']}). Start a new run so nothing saved since is lost.")
    return ""


@router.post(RUN_PATH)
async def api_start(body: RunBody) -> dict[str, Any]:
    s = platform_api.store()
    bundle = await platform_api.guarded(s.bundle(body.tenant_id))
    industry = bundle.industry
    template = await load_template(industry["id"])
    if not template:
        raise HTTPException(status_code=400, detail=f"Write the finetune template for {industry['name']} first.")
    api_url, api_key = vision.endpoint_from_env()
    model = _model(bundle)
    if not (api_url and api_key):
        raise HTTPException(status_code=400, detail="VISION_API_URL and VISION_API_KEY must be set in .env.")
    if not model:
        raise HTTPException(status_code=400, detail="Set FINETUNE_MODEL in .env, or a vision model in this "
                                                    "client's config.")

    images: list[tuple[str, bytes]] = []
    for shot in body.images:
        try:
            images.append((shot.name, base64.b64decode(shot.data, validate=True)))
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail=f"{shot.name} did not arrive intact.") from None
    try:
        images = finetune.check_images(images)
        prompt = finetune.fill_template(
            template, business_name=bundle.tenant["name"],
            businesses_so_far=await businesses_so_far(industry["id"]),
            industry_sections=await _live_sections(industry),
        )
    except finetune.FinetuneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await _launch(
        body.tenant_id, industry, model=model, source="screenshots", files=[name for name, _ in images],
        call=lambda meter: finetune.complete(prompt, images, api_url=api_url, api_key=api_key, model=model,
                                             usage_sink=meter),
    )


class Chat(BaseModel):
    # e.g. "03" or "03-good"
    name: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., max_length=finetune.MAX_TRANSCRIPT_CHARS)


class TextRunBody(BaseModel):
    tenant_id: int
    chats: list[Chat] = Field(..., min_length=1, max_length=finetune.MAX_CHATS)


@router.post(TEXT_RUN_PATH)
async def api_start_text(body: TextRunBody) -> dict[str, Any]:
    """A run from chats the admin transcribed, read by the text model
    (DeepSeek, on the platform key) instead of the vision model."""
    s = platform_api.store()
    bundle = await platform_api.guarded(s.bundle(body.tenant_id))
    industry = bundle.industry
    template = await load_template(industry["id"])
    if not template:
        raise HTTPException(status_code=400, detail=f"Write the finetune template for {industry['name']} first.")
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to read transcripts.")
    try:
        chats = finetune.check_chats([(c.name, c.text) for c in body.chats])
        prompt = finetune.fill_template(
            template, business_name=bundle.tenant["name"],
            businesses_so_far=await businesses_so_far(industry["id"]),
            industry_sections=await _live_sections(industry),
        )
    except finetune.FinetuneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    model = finetune.text_model()
    return await _launch(
        body.tenant_id, industry, model=model, source="text", files=[name for name, _ in chats],
        call=lambda meter: finetune.complete_text(prompt, chats, api_key=api_key, model=model, usage_sink=meter),
    )


async def _launch(tenant_id: int, industry: dict[str, Any], *, model: str, source: str, files: list[str],
                  call: Callable[[Any], Awaitable[str]]) -> dict[str, Any]:
    """Record the run and start it in the background. `call(meter)` is the
    model call; only names are recorded, never the chats themselves."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        run_id = await con.fetchval(
            "INSERT INTO finetune_runs (tenant_id, industry_id, model, source, files, industry_version, created_by) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7) RETURNING id",
            tenant_id, industry["id"], model, source, json.dumps(files), industry["template_version"], ACTOR,
        )
        await audit.record(con, tenant_id=tenant_id, actor=ACTOR, event=FINETUNE_STARTED,
                           payload={"run_id": run_id, "model": model, "source": source, "files": files})
    task = asyncio.create_task(_execute(run_id, call))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return await _get_run(run_id)


async def _execute(run_id: int, call: Callable[[Any], Awaitable[str]]) -> None:
    pool = _get_pool()

    async def meter(used_model: str, usage: dict[str, int]) -> None:
        # Platform spend, like the config helper: the admin started it.
        await llm_usage.record(pool, tenant_id=None, purpose="finetune", model=used_model, usage=usage)

    try:
        raw = await call(meter)
    except vision.VisionError as exc:
        await _finish(run_id, status="failed", error=str(exc))
        return
    except Exception:
        log.exception("Finetune run %s failed", run_id)
        await _finish(run_id, status="failed", error="Unexpected error; see the panel log.")
        return
    await _finish(run_id, status="done", raw=raw, result=finetune.parse_output(raw))


async def _finish(run_id: int, *, status: str, raw: str = "", result: Optional[dict[str, Any]] = None,
                  error: str = "") -> None:
    await _get_pool().execute(
        "UPDATE finetune_runs SET status = $2, raw_output = $3, result = $4::jsonb, error = $5, "
        "finished_at = now() WHERE id = $1 AND status = 'running'",
        run_id, status, raw, json.dumps(result) if result is not None else None, error,
    )


class ApplyBody(BaseModel):
    # Leave one out to apply only the other. Both may be edited first.
    business_layer: Optional[dict[str, Any]] = None
    industry_sections: Optional[dict[str, Any]] = None
    note: str = ""


@router.post("/api/finetune/runs/{run_id}/apply")
async def api_apply(run_id: int, body: ApplyBody) -> dict[str, Any]:
    run = await _get_run(run_id)
    if run["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Only a finished run can be applied; this one is {run['status']}.")
    if body.business_layer is None and body.industry_sections is None:
        raise HTTPException(status_code=400, detail="Choose what to apply.")
    s = platform_api.store()
    tenant = await platform_api.guarded(s.get(run["tenant_id"]))
    industry = await s.get_industry(run["industry_id"])
    stale = _stale_reason(run, industry, tenant)
    if stale:
        raise HTTPException(status_code=409, detail=stale)

    # Both are checked before either is saved, so a bad business layer
    # cannot leave a half-applied run behind.
    try:
        sections = (finetune.check_industry_sections(body.industry_sections)
                    if body.industry_sections is not None else None)
        layer = (finetune.check_business_layer(body.business_layer)
                 if body.business_layer is not None else None)
    except finetune.FinetuneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Claimed first, so a double click cannot save the versions twice.
    pool = _get_pool()
    claimed = await pool.fetchval(
        "UPDATE finetune_runs SET status = 'applied', applied_at = now() WHERE id = $1 AND status = 'done' "
        "RETURNING id", run_id)
    if claimed is None:
        raise HTTPException(status_code=409, detail="This run is already being applied.")

    note = body.note.strip() or f"Finetune run {run_id}"
    applied: dict[str, Any] = {}
    try:
        if sections is not None:
            saved = await platform_api.guarded(s.save_industry_template(
                industry["id"], {"sections": sections}, actor=ACTOR, note=note))
            applied["industry_version"] = saved["template_version"]
        if layer is not None:
            bundle = await platform_api.guarded(s.save_client_prompt(tenant["id"], layer, actor=ACTOR, note=note))
            applied["client_version"] = bundle.tenant["prompt_version"]
    finally:
        if not applied:
            await pool.execute("UPDATE finetune_runs SET status = 'done', applied_at = NULL WHERE id = $1", run_id)

    async with pool.acquire() as con, con.transaction():
        await con.execute("UPDATE finetune_runs SET applied = $2::jsonb WHERE id = $1", run_id, json.dumps(applied))
        await audit.record(con, tenant_id=tenant["id"], actor=ACTOR, event=FINETUNE_APPLIED, reason=note,
                           payload={"run_id": run_id, **applied})
    if sections is not None:
        await platform_api._reload_industry(industry["id"])
    else:
        await platform_api.reload_accounts([tenant["session_id"]])
    return await api_run(run_id)


@router.post("/api/finetune/runs/{run_id}/discard")
async def api_discard(run_id: int) -> dict[str, Any]:
    run = await _get_run(run_id)
    if run["status"] not in ("done", "failed"):
        raise HTTPException(status_code=409, detail=f"A run that is {run['status']} cannot be discarded.")
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        await con.execute("UPDATE finetune_runs SET status = 'discarded' WHERE id = $1", run_id)
        await audit.record(con, tenant_id=run["tenant_id"], actor=ACTOR, event=FINETUNE_DISCARDED,
                           payload={"run_id": run_id})
    return await _get_run(run_id)
