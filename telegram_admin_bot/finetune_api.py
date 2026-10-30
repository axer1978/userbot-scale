"""Admin API for finetuning from chats: screenshots or transcripts (finetune.py).

Mounted by panel.py behind the admin login.

- One finetune template per industry, in platform_settings under
  'finetune_template:<industry id>'. A sub-folder without one of its own
  uses the nearest folder's above it; a run's industry standard is the
  client's own folder's (tenants.save_folder_standard).
- A run is started with the screenshots (read by the vision model) or with
  chats the admin transcribed (read by DeepSeek on the platform key), and
  works in the background; the page polls it. The chats stay in memory for
  the run only and are never written anywhere.
- Flagged chats: the admin marks real chats from the conversations view
  (good / bad, kind of client, how it ended, why, corrections on single
  messages). Only that judgement is stored, in finetune_examples; a run
  from them reads the chats from messages, masked, when it starts.
- Hand-typed chats: the run picks the chats where a person typed the
  business's replies, from this account and any others the admin adds, and
  reads them from messages, masked, when it starts. Nothing is stored.
- Applying a run saves ordinary prompt versions through tenants.py, the
  industry standard first, then the business layer, and reloads the
  accounts affected, as platform_api does. Either part can be left out,
  and both can be edited before applying.
- Operator rules (rules.py) are folded into the prompt by a run of their
  own (source 'rules'), reviewed and applied here like any other; applying
  it marks the rules it folded in as merged.
- Quality: how the bot did, week by week (stats.quality), as a baseline
  for seeing whether an applied run made its replies better.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import finetune
import llm_usage
import platform_api
import prompt_layers
import stats
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
# A run that folds the operator's rules into the prompt (rules.py).
RULES_SOURCE = "rules"
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


async def _own_template(industry_id: int) -> str:
    value = await _get_pool().fetchval("SELECT value FROM platform_settings WHERE key = $1",
                                       _template_key(industry_id))
    value = tenants._json(value) if value is not None else None
    return value.get("template", "") if isinstance(value, dict) else ""


async def _template_and_source(industry_id: int) -> tuple[str, Optional[dict[str, Any]]]:
    """The template a run in this folder uses: its own, else the nearest
    folder's above it; with the folder it comes from (None: none at all)."""
    for folder in reversed(await platform_api.store().chain(industry_id)):
        template = await _own_template(folder["id"])
        if template:
            return template, folder
    return "", None


async def load_template(industry_id: int) -> str:
    return (await _template_and_source(industry_id))[0]


async def businesses_so_far(industry_id: int) -> int:
    """Businesses whose run updated this industry's standard, in this folder
    or one above it (a sub-folder starts from its parent's). A fold of the
    operator's rules learned nothing from a business's chats: not counted."""
    folders = [f["id"] for f in await platform_api.store().chain(industry_id)]
    return await _get_pool().fetchval(
        "SELECT count(DISTINCT tenant_id) FROM finetune_runs "
        "WHERE industry_id = ANY($1::int[]) AND status = 'applied' AND applied ? 'industry_version' AND source <> $2",
        folders, RULES_SOURCE,
    )


async def _live_sections(industry: dict[str, Any]) -> dict[str, str]:
    """The standard this folder hands its clients now, with what the
    folders above it hand down."""
    return await platform_api.store().folder_sections(industry["id"])


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


async def _get_run(run_id: int, pool: Any = None) -> dict[str, Any]:
    row = await (pool or _get_pool()).fetchrow(_SELECT + " WHERE id = $1", run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown finetune run")
    return _run(row)


# ------------------------------------------------------------------ templates


class TemplateBody(BaseModel):
    template: str = Field(..., max_length=finetune.MAX_TEMPLATE_CHARS)
    reason: str = ""


@router.get("/api/finetune/industries/{industry_id}")
async def api_industry(industry_id: int) -> dict[str, Any]:
    chain = await platform_api.guarded(platform_api.store().chain(industry_id))
    industry = chain[-1]
    template = await _own_template(industry_id)
    used, source = (template, industry) if template else await _template_and_source(industry_id)
    return {
        "industry": {"id": industry["id"], "name": industry["name"],
                     "path": tenants.PATH_SEPARATOR.join(f["name"] for f in chain),
                     "parent_id": industry["parent_id"],
                     "template_version": industry["template_version"]},
        "template": template,
        # A sub-folder without a template of its own runs on this one, from
        # the nearest folder above it that has one.
        "inherited_template": used if source is not None and source["id"] != industry_id else "",
        "template_from": source["name"] if source is not None and source["id"] != industry_id else "",
        # Loaded into the editor on request; nothing runs on it until saved.
        # The top-level industry's name picks it: a sub-folder is the same trade.
        "default_template": finetune.default_template(chain[0]["name"]),
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
                  call: Callable[[Any], Awaitable[str]],
                  parse: Callable[[str], dict[str, Any]] = finetune.parse_output,
                  pool: Any = None, actor: str = ACTOR) -> dict[str, Any]:
    """Record the run and start it in the background. `call(meter)` is the
    model call; only names are recorded, never the chats themselves.
    `parse` reads its answer into the run's result (rules.py folds use their
    own). `pool` and `actor` are for a run the scheduler starts (rules.py's
    weekly fold): it runs outside the panel, where this module is bound."""
    pool = pool or _get_pool()
    async with pool.acquire() as con, con.transaction():
        run_id = await con.fetchval(
            "INSERT INTO finetune_runs (tenant_id, industry_id, model, source, files, industry_version, created_by) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7) RETURNING id",
            tenant_id, industry["id"], model, source, json.dumps(files), industry["template_version"], actor,
        )
        await audit.record(con, tenant_id=tenant_id, actor=actor, event=FINETUNE_STARTED,
                           payload={"run_id": run_id, "model": model, "source": source, "files": files})
    task = asyncio.create_task(_execute(run_id, call, parse, pool))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return await _get_run(run_id, pool)


async def _execute(run_id: int, call: Callable[[Any], Awaitable[str]],
                   parse: Callable[[str], dict[str, Any]] = finetune.parse_output, pool: Any = None) -> None:
    pool = pool or _get_pool()

    async def meter(used_model: str, usage: dict[str, int]) -> None:
        # Platform spend, like the config helper: the admin started it.
        await llm_usage.record(pool, tenant_id=None, purpose="finetune", model=used_model, usage=usage)

    try:
        raw = await call(meter)
    except vision.VisionError as exc:
        await _finish(run_id, status="failed", error=str(exc), pool=pool)
        return
    except Exception:
        log.exception("Finetune run %s failed", run_id)
        await _finish(run_id, status="failed", error="Unexpected error; see the panel log.", pool=pool)
        return
    await _finish(run_id, status="done", raw=raw, result=parse(raw), pool=pool)


async def _finish(run_id: int, *, status: str, raw: str = "", result: Optional[dict[str, Any]] = None,
                  error: str = "", pool: Any = None) -> None:
    await (pool or _get_pool()).execute(
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
            saved = await platform_api.guarded(s.save_folder_standard(
                industry["id"], sections, actor=ACTOR, note=note))
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
    if run["source"] == RULES_SOURCE:
        # The rules this run folded in are in the prompt now: they stop being
        # added separately (before the reload below, which drops them).
        # rules.py builds on this module, so it is imported only here.
        import rules

        await rules.on_applied(pool, run, applied, actor=ACTOR)
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


# -------------------------------------------------------------- flagged chats
# The admin flags a real chat from the conversations view: good or bad, what
# kind of client, how it ended, why, and corrections on single messages.
# Only that judgement is stored (finetune_examples); the chat is read from
# messages when a run starts, up to where it was when flagged.

FINETUNE_EXAMPLE_SAVED = "finetune_example_saved"
FINETUNE_EXAMPLE_REMOVED = "finetune_example_removed"
EXAMPLE_RUN_PATH = "/api/finetune/example-runs"
# Rows read per chat before chat_lines() keeps what crossed the wire.
_RAW_ROWS = finetune.MAX_EXAMPLE_MESSAGES * 3


def _example(row: Any, conversation: Optional[Any] = None) -> dict[str, Any]:
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "chat_id": row["chat_id"],
        "name": finetune.example_name(row["id"], row["verdict"]),
        "upto_message_id": row["upto_message_id"],
        "verdict": row["verdict"], "client_kind": row["client_kind"], "outcome": row["outcome"],
        "note": row["note"], "corrections": tenants._json(row["corrections"]) or {},
        "last_run_id": row["last_run_id"], "created_by": row["created_by"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "display_name": (conversation["display_name"] or "") if conversation else "",
    }


async def _chat(con: Any, tenant_id: int, chat_id: int,
                upto: Optional[int] = None) -> tuple[Any, list[dict[str, Any]]]:
    """The conversation row and the chat's lines (finetune.chat_lines), up to
    message `upto` when given."""
    conversation = await con.fetchrow(
        "SELECT display_name, username FROM conversations WHERE tenant_id = $1 AND chat_id = $2", tenant_id, chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    rows = await con.fetch(
        "SELECT id, direction, status, text, attachments, created_at FROM messages "
        "WHERE tenant_id = $1 AND chat_id = $2 AND ($3::bigint IS NULL OR id <= $3) ORDER BY id LIMIT $4",
        tenant_id, chat_id, upto, _RAW_ROWS)
    return conversation, finetune.chat_lines([dict(r) for r in rows])


def _masks(bundle: tenants.Bundle, conversation: Any) -> list[tuple[str, str]]:
    booking = bundle.config.get("booking") or {}
    return finetune.masks_for(display_name=conversation["display_name"] or "",
                              username=conversation["username"] or "",
                              address=booking.get("address", ""), way_in=booking.get("arrival_instructions", ""))


def _tz(bundle: tenants.Bundle) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(bundle.config.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return None


async def _render(con: Any, bundle: tenants.Bundle, example: dict[str, Any]) -> str:
    """What the model reads for this flagged chat."""
    conversation, lines = await _chat(con, example["tenant_id"], example["chat_id"], example["upto_message_id"])
    return finetune.render_example(example, lines, masks=_masks(bundle, conversation), tz=_tz(bundle))


async def _get_example(example_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow(
        "SELECT e.*, c.display_name FROM finetune_examples e LEFT JOIN conversations c "
        "ON c.tenant_id = e.tenant_id AND c.chat_id = e.chat_id WHERE e.id = $1", example_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown flagged chat")
    return _example(row, row)


class ExampleBody(BaseModel):
    verdict: str
    client_kind: str = ""
    outcome: str = ""
    note: str = Field("", max_length=finetune.MAX_NOTE_CHARS)
    # {"<message id>": "what was wrong / what it should have said"}
    corrections: dict[str, str] = Field(default_factory=dict)


@router.get("/api/finetune/sessions/{session_id}/flags")
async def api_session_flags(session_id: str) -> dict[str, str]:
    """{chat id: verdict} of the account's flagged chats, for the chat list."""
    tenant = await platform_api.store().by_session(session_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown account")
    rows = await _get_pool().fetch("SELECT chat_id, verdict FROM finetune_examples WHERE tenant_id = $1", tenant["id"])
    return {str(r["chat_id"]): r["verdict"] for r in rows}


@router.get("/api/finetune/sessions/{session_id}/chats/{chat_id}/example")
async def api_chat_example(session_id: str, chat_id: int) -> dict[str, Any]:
    """The chat's flag (or null), the chat as it is now to mark corrections
    on, and what the model would read for the flag as saved."""
    bundle = await platform_api.guarded(platform_api.store().bundle_for_session(session_id))
    tenant_id = bundle.tenant["id"]
    async with _get_pool().acquire() as con:
        _, lines = await _chat(con, tenant_id, chat_id)
        row = await con.fetchrow("SELECT * FROM finetune_examples WHERE tenant_id = $1 AND chat_id = $2",
                                 tenant_id, chat_id)
        example = _example(row) if row else None
        preview = await _render(con, bundle, example) if example else ""
    return {
        "tenant_id": tenant_id, "example": example, "lines": lines, "preview": preview,
        # Written since it was flagged: saving again takes them in.
        "newer": sum(1 for line in lines if example and line["id"] > example["upto_message_id"]),
    }


@router.put("/api/finetune/sessions/{session_id}/chats/{chat_id}/example")
async def api_save_chat_example(session_id: str, chat_id: int, body: ExampleBody) -> dict[str, Any]:
    """Flag the chat, or change its flag. It covers the chat as it is now."""
    bundle = await platform_api.guarded(platform_api.store().bundle_for_session(session_id))
    tenant_id = bundle.tenant["id"]
    async with _get_pool().acquire() as con, con.transaction():
        _, lines = await _chat(con, tenant_id, chat_id)
        if not lines:
            raise HTTPException(status_code=400, detail="This chat has no messages to learn from yet.")
        try:
            clean = finetune.check_example(
                verdict=body.verdict, client_kind=body.client_kind, outcome=body.outcome, note=body.note,
                corrections=body.corrections, message_ids={line["id"] for line in lines})
        except finetune.FinetuneError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        example_id = await con.fetchval(
            "INSERT INTO finetune_examples (tenant_id, chat_id, upto_message_id, verdict, client_kind, outcome, "
            "note, corrections, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9) "
            "ON CONFLICT (tenant_id, chat_id) DO UPDATE SET upto_message_id = EXCLUDED.upto_message_id, "
            "verdict = EXCLUDED.verdict, client_kind = EXCLUDED.client_kind, outcome = EXCLUDED.outcome, "
            # A changed judgement is news: the next run picks it again by default.
            "note = EXCLUDED.note, corrections = EXCLUDED.corrections, last_run_id = NULL, updated_at = now() "
            "RETURNING id",
            tenant_id, chat_id, lines[-1]["id"], clean["verdict"], clean["client_kind"], clean["outcome"],
            clean["note"], json.dumps(clean["corrections"]), ACTOR,
        )
        # The judgement only, not the note: it can quote the client.
        await audit.record(con, tenant_id=tenant_id, actor=ACTOR, event=FINETUNE_EXAMPLE_SAVED, payload={
            "example_id": example_id, "chat_id": chat_id, "verdict": clean["verdict"],
            "client_kind": clean["client_kind"], "outcome": clean["outcome"],
            "corrections": len(clean["corrections"])})
    return await api_chat_example(session_id, chat_id)


@router.get("/api/finetune/examples")
async def api_examples(tenant_id: int) -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(
        "SELECT e.*, c.display_name FROM finetune_examples e LEFT JOIN conversations c "
        "ON c.tenant_id = e.tenant_id AND c.chat_id = e.chat_id WHERE e.tenant_id = $1 ORDER BY e.id", tenant_id)
    return [_example(r, r) for r in rows]


@router.get("/api/finetune/examples/{example_id}")
async def api_example(example_id: int) -> dict[str, Any]:
    """The flag, with what the model reads for it."""
    example = await _get_example(example_id)
    bundle = await platform_api.guarded(platform_api.store().bundle(example["tenant_id"]))
    async with _get_pool().acquire() as con:
        example["preview"] = await _render(con, bundle, example)
    return example


@router.delete("/api/finetune/examples/{example_id}")
async def api_remove_example(example_id: int) -> dict[str, Any]:
    """Unflag. The chat itself is not touched."""
    example = await _get_example(example_id)
    async with _get_pool().acquire() as con, con.transaction():
        await con.execute("DELETE FROM finetune_examples WHERE id = $1", example_id)
        await audit.record(con, tenant_id=example["tenant_id"], actor=ACTOR, event=FINETUNE_EXAMPLE_REMOVED,
                           payload={"example_id": example_id, "chat_id": example["chat_id"]})
    return {"removed": example_id}


class ExampleRunBody(BaseModel):
    tenant_id: int
    example_ids: list[int] = Field(..., min_length=1, max_length=finetune.MAX_CHATS)


@router.post(EXAMPLE_RUN_PATH)
async def api_start_examples(body: ExampleRunBody) -> dict[str, Any]:
    """A run from flagged chats, read by the text model (DeepSeek, on the
    platform key) like typed chats, with the admin's judgement on each."""
    s = platform_api.store()
    bundle = await platform_api.guarded(s.bundle(body.tenant_id))
    industry = bundle.industry
    template = await load_template(industry["id"])
    if not template:
        raise HTTPException(status_code=400, detail=f"Write the finetune template for {industry['name']} first.")
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to read flagged chats.")
    pool = _get_pool()
    wanted = sorted(set(body.example_ids))
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT * FROM finetune_examples WHERE tenant_id = $1 AND id = ANY($2::bigint[]) "
                               "ORDER BY id", body.tenant_id, wanted)
        if len(rows) != len(wanted):
            raise HTTPException(status_code=400, detail="Some of these flagged chats are gone or belong to another "
                                                        "client. Reload the page.")
        chats = []
        for r in rows:
            name = finetune.example_name(r["id"], r["verdict"])
            try:
                chats.append((name, await _render(con, bundle, _example(r))))
            except HTTPException:
                raise HTTPException(status_code=400, detail=f"The chat flagged as {name} is gone (its account was "
                                                            "removed). Remove the flag and start again.") from None
    try:
        chats = finetune.check_chats(chats)
        prompt = finetune.fill_template(
            template, business_name=bundle.tenant["name"],
            businesses_so_far=await businesses_so_far(industry["id"]),
            industry_sections=await _live_sections(industry),
        )
    except finetune.FinetuneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    model = finetune.text_model()
    run = await _launch(
        body.tenant_id, industry, model=model, source="examples", files=[name for name, _ in chats],
        call=lambda meter: finetune.complete_text(prompt, chats, api_key=api_key, model=model, flagged=True,
                                                  usage_sink=meter),
    )
    await pool.execute("UPDATE finetune_examples SET last_run_id = $1 WHERE id = ANY($2::bigint[])",
                       run["id"], wanted)
    return run


# ----------------------------------------------------------- hand-typed chats
# The run picks, by itself, the chats where a person typed the business's
# replies (on the phone, or by hand in the panel): from this account, and
# from any other accounts the admin adds to learn the shared voice from.
# Nothing is stored; the chats are read from messages, masked, each time.
#
# A reply was typed by hand when the model did not write it (no llm_model)
# and neither the bot nor the system sent it: every send through the
# runtime writes a message_sent audit row naming who caused it (the bot for
# replies, booking notices, the address, the way in and reminders; the admin
# or the owner for a panel send), and a reply typed on the phone has none.
# (Phone replies are also marked sent_elsewhere since migration 0013, but
# older ones are not, so the audit rows decide.)

HAND_RUN_PATH = "/api/finetune/hand-runs"
MAX_HAND_ACCOUNTS = 10
_MACHINE_ACTORS = [audit.BOT, audit.SYSTEM]

_HAND_MESSAGES = """
WITH machine AS (
  SELECT DISTINCT payload->>'message_id' AS id FROM audit_log
  WHERE tenant_id = ANY($1::int[]) AND event = $2 AND actor = ANY($3::text[])
), marked AS (
  SELECT m.tenant_id, m.chat_id, m.id, m.direction, m.status, m.text, m.attachments, m.created_at,
         (m.direction = 'out' AND m.status = 'sent' AND m.llm_model IS NULL
          AND NOT EXISTS (SELECT 1 FROM machine WHERE machine.id = m.id::text)) AS typed
  FROM messages m WHERE m.tenant_id = ANY($1::int[])
), numbered AS (
  SELECT marked.*, row_number() OVER (PARTITION BY tenant_id, chat_id ORDER BY id) AS n
  FROM marked
  WHERE (tenant_id, chat_id) IN (SELECT tenant_id, chat_id FROM marked WHERE typed)
)
SELECT tenant_id, chat_id, id, direction, status, text, attachments, created_at, typed
FROM numbered WHERE n <= $4 ORDER BY tenant_id, chat_id, id
"""


async def _hand_chats(tenant_id: int, also: list[int]) -> tuple[tenants.Bundle, list[dict[str, Any]]]:
    """The account's bundle, and every chat of it and of `also` with a reply
    typed by hand, rendered as the model reads it and planned
    (finetune.plan_hand_run): named, and picked to fit one run."""
    s = platform_api.store()
    bundle = await platform_api.guarded(s.bundle(tenant_id))
    others = [t for t in dict.fromkeys(also) if t != tenant_id]
    if len(others) > MAX_HAND_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_HAND_ACCOUNTS} other accounts per run.")
    bundles = {tenant_id: bundle}
    for other in others:
        bundles[other] = await platform_api.guarded(s.bundle(other))

    async with _get_pool().acquire() as con:
        rows = await con.fetch(_HAND_MESSAGES, list(bundles), audit.MESSAGE_SENT, _MACHINE_ACTORS, _RAW_ROWS)
        keys = sorted({(r["tenant_id"], r["chat_id"]) for r in rows})
        people = await con.fetch(
            "SELECT tenant_id, chat_id, display_name, username FROM conversations "
            "WHERE (tenant_id, chat_id) IN (SELECT * FROM unnest($1::int[], $2::bigint[]))",
            [k[0] for k in keys], [k[1] for k in keys])
    conversations = {(p["tenant_id"], p["chat_id"]): p for p in people}
    messages: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for r in rows:
        messages.setdefault((r["tenant_id"], r["chat_id"]), []).append(dict(r))

    chats = []
    for (owner, chat_id), raw in messages.items():
        conversation = conversations.get((owner, chat_id))
        if conversation is None:
            continue
        lines = finetune.chat_lines(raw)
        typed = sum(1 for line in lines if line.get("typed"))
        if not typed:
            continue  # its typed replies lie beyond what a run reads
        own = bundles[owner]
        text = finetune.render_hand_chat(
            lines, origin=finetune.hand_origin(own.tenant["name"], own=owner == tenant_id),
            masks=_masks(own, conversation), tz=_tz(own))
        chats.append({
            "key": f"{owner}:{chat_id}", "tenant_id": owner, "account": own.tenant["name"], "chat_id": chat_id,
            "display_name": conversation["display_name"] or "", "typed": typed,
            "assistant": sum(1 for line in lines if line["who"] == "business" and not line.get("typed")),
            "client": sum(1 for line in lines if line["who"] == "client"),
            "last_at": lines[-1]["at"], "chars": len(text), "text": text,
        })
    accounts = [(t, b.tenant["name"]) for t, b in bundles.items()]
    return bundle, finetune.plan_hand_run(chats, accounts)


@router.get("/api/finetune/hand-chats")
async def api_hand_chats(tenant_id: int, also: list[int] = Query(default=[])) -> dict[str, Any]:
    """The chats with replies typed by hand that a run for this account
    would read (picked) or could read, with what the model reads for each,
    and a count per account."""
    _, chats = await _hand_chats(tenant_id, also)
    accounts: dict[int, dict[str, Any]] = {}
    for c in chats:
        account = accounts.setdefault(c["tenant_id"], {"tenant_id": c["tenant_id"], "name": c["account"],
                                                       "chats": 0, "typed": 0, "picked": 0})
        account["chats"] += 1
        account["typed"] += c["typed"]
        account["picked"] += int(c["picked"])
    return {"accounts": list(accounts.values()), "chats": chats,
            "limits": {"chats": finetune.MAX_CHATS, "chars": finetune.MAX_TRANSCRIPT_CHARS}}


class HandRunBody(BaseModel):
    tenant_id: int
    also_tenant_ids: list[int] = Field(default_factory=list, max_length=MAX_HAND_ACCOUNTS)
    # "<tenant id>:<chat id>" as the list names them; empty = the default pick.
    chats: list[str] = Field(default_factory=list, max_length=finetune.MAX_CHATS)


@router.post(HAND_RUN_PATH)
async def api_start_hand(body: HandRunBody) -> dict[str, Any]:
    """A run from the chats where a person typed the business's replies,
    read by the text model (DeepSeek, on the platform key)."""
    bundle, planned = await _hand_chats(body.tenant_id, body.also_tenant_ids)
    industry = bundle.industry
    template = await load_template(industry["id"])
    if not template:
        raise HTTPException(status_code=400, detail=f"Write the finetune template for {industry['name']} first.")
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to read chats.")
    if body.chats:
        by_key = {c["key"]: c for c in planned}
        if any(key not in by_key for key in body.chats):
            raise HTTPException(status_code=400, detail="Some of these chats no longer have replies typed by hand, "
                                                        "or belong to an account not in this run. Reload the page.")
        chosen = [by_key[key] for key in dict.fromkeys(body.chats)]
    else:
        chosen = [c for c in planned if c["picked"]]
    if not chosen:
        raise HTTPException(status_code=400, detail="No chats with replies typed by hand yet: answer some clients "
                                                    "yourself on the phone or from the panel first.")
    try:
        chats = finetune.check_chats([(c["name"], c["text"]) for c in chosen])
        prompt = finetune.fill_template(
            template, business_name=bundle.tenant["name"],
            businesses_so_far=await businesses_so_far(industry["id"]),
            industry_sections=await _live_sections(industry),
        )
    except finetune.FinetuneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    model = finetune.text_model()
    return await _launch(
        body.tenant_id, industry, model=model, source="hand", files=[name for name, _ in chats],
        call=lambda meter: finetune.complete_text(prompt, chats, api_key=api_key, model=model, hand=True,
                                                  usage_sink=meter),
    )


# ---------------------------------------------------------------- quality

MAX_QUALITY_WEEKS = 12


@router.get("/api/finetune/quality")
async def api_quality(tenant_id: int, weeks: int = Query(default=4, ge=1, le=MAX_QUALITY_WEEKS)) -> dict[str, Any]:
    """How the bot did for this account, week by week in its own timezone
    (oldest first, the last one this week so far), and what there is to
    learn from in all. auto_send says whether drafts can be edited or
    rejected at all: with it on, replies go out without waiting."""
    bundle = await platform_api.guarded(platform_api.store().bundle(tenant_id))
    config = bundle.resolved.config
    pool = _get_pool()
    return {
        "tenant_id": tenant_id,
        "timezone": config.timezone,
        "auto_send": config.auto_send,
        "takeover_hours": config.takeover_hours,
        "weeks": await stats.weeks(pool, tenant_id, config.timezone, datetime.now(timezone.utc), count=weeks,
                                   of=stats.quality),
        "totals": await stats.quality_totals(pool, tenant_id),
    }
