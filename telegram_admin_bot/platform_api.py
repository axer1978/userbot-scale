"""Admin API for the platform layer: industries, tenants, the three prompt
layers, versions, the natural-language config helper and the audit log.

Mounted by panel.py behind the admin login. Kept out of panel.py, which is
about running accounts; this is about how tenants are configured. Every
write goes through tenants.TenantStore (validation + an audit row), then
tells the affected accounts' workers to reload, best effort: a worker that
misses it picks the change up within SessionRuntime.REBIND_SECONDS anyway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import commands
import config_assist
import llm_usage
import prompt_layers
import tenant_config
import tenants
from tenant_config import ConfigError

log = logging.getLogger("platform_api")

router = APIRouter()
ACTOR = audit.ADMIN
RELOAD_TIMEOUT = 5.0

# Wired by panel.py, read at call time so tests can swap panel's globals.
_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


def store() -> tenants.TenantStore:
    return tenants.TenantStore(_get_pool())


def platform_key() -> str:
    return (os.getenv("DEEPSEEK_PLATFORM_KEY") or "").strip()


async def reload_accounts(session_ids: list[Optional[str]], bundle: Optional[tenants.Bundle] = None) -> None:
    """Tell the workers running these accounts to reload, all at once. Only
    accounts with a live lease are asked: one nobody runs would just time
    out, and it loads the new config when it starts anyway."""
    bus = _get_bus()
    wanted = [s for s in session_ids if s]
    if bus is None or not wanted:
        return
    running = [r["session_id"] for r in await _get_pool().fetch(
        "SELECT session_id FROM telegram_sessions WHERE session_id = ANY($1) AND lease_expires_at > now()",
        wanted,
    )]

    async def one(session_id: str) -> None:
        try:
            await bus.dispatch(session_id, "reload_config", {}, timeout=RELOAD_TIMEOUT)
        except commands.CommandError:
            log.warning("[%s] Did not confirm the config reload; it rebinds on its own within minutes.", session_id)

    await asyncio.gather(*(one(s) for s in running))
    if bundle is not None:
        for session_id in wanted:
            await bus.publish_event(session_id, {"type": "tenant_config", "config": bundle.config})


async def guarded(call):
    """Run a store call, turning its errors into HTTP answers."""
    try:
        return await call
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "errors": exc.errors}) from exc
    except tenants.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except tenants.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (prompt_layers.PromptError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------ reads


def _tenant_view(bundle: tenants.Bundle) -> dict[str, Any]:
    client = bundle.layers["client"]
    return {
        "tenant": bundle.tenant,
        "industry": {"id": bundle.industry["id"], "name": bundle.industry["name"],
                     "template_version": bundle.industry["template_version"]},
        "config": {
            "effective": bundle.config,
            "overrides": bundle.tenant["config_json"],
            "fields": tenant_config.field_catalog(bundle.resolved, bundle.inherited),
            "revision": bundle.tenant["config_revision"],
        },
        "prompt": {
            "sections": [
                {"key": key, "heading": heading, **section,
                 "override": (client.get("overrides") or {}).get(key)}
                for (key, heading), section in zip(
                    prompt_layers.SECTIONS,
                    prompt_layers.effective_sections(bundle.layers["industry"], client).values(),
                )
            ],
            "addendum": client.get("addendum", ""),
            "addendum_limit": prompt_layers.MAX_ADDENDUM_CHARS,
            "client_version": bundle.tenant["prompt_version"],
            "industry_version": bundle.layers["industry_version"],
            "pinned": bundle.tenant["prompt_pin_version"],
            "rendered": bundle.prompt.text,
            "version_tag": bundle.prompt.version_tag,
        },
    }


@router.get("/api/platform/tree")
async def api_tree() -> dict[str, Any]:
    s = store()
    return {"industries": await s.list_industries(), "tenants": await s.list(),
            "base_version": await s.base_version()}


@router.get("/api/platform/base")
async def api_base() -> dict[str, Any]:
    s = store()
    current = await s.base()
    return {"current": current, "versions": await s.versions(tenants.BASE, 0)}


@router.get("/api/industries/{industry_id}")
async def api_industry(industry_id: int) -> dict[str, Any]:
    s = store()
    industry = await guarded(s.get_industry(industry_id))
    live = await s.version(tenants.INDUSTRY, industry_id, industry["template_version"])
    resolved = tenant_config.resolve(industry["default_config"], None)
    defaults = tenant_config.resolve(None, None).as_dict()
    return {
        "industry": industry,
        "sections": [
            {"key": key, "heading": heading, "text": live["content"]["sections"].get(key, "")}
            for key, heading in prompt_layers.SECTIONS
        ],
        "versions": await s.versions(tenants.INDUSTRY, industry_id),
        "config": {"overrides": industry["default_config"], "revision": industry["config_revision"],
                   "fields": tenant_config.field_catalog(resolved, defaults)},
        "tenants": await s.tenants_in_industry(industry_id),
    }


@router.get("/api/tenants/{tenant_id}")
async def api_tenant(tenant_id: int) -> dict[str, Any]:
    s = store()
    bundle = await guarded(s.bundle(tenant_id))
    view = _tenant_view(bundle)
    view["prompt"]["client_versions"] = await s.versions(tenants.CLIENT, tenant_id)
    view["prompt"]["industry_versions"] = [
        {k: v[k] for k in ("version", "note", "created_by", "created_at")}
        for v in await s.versions(tenants.INDUSTRY, bundle.industry["id"])
    ]
    return view


@router.get("/api/tenants/by-session/{session_id}")
async def api_tenant_for_session(session_id: str) -> dict[str, Any]:
    tenant = await store().by_session(session_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="No tenant owns this account")
    return tenant


@router.get("/api/audit")
async def api_audit(tenant_id: Optional[int] = None, limit: int = 200) -> list[dict[str, Any]]:
    return await audit.list_events(_get_pool(), tenant_id=tenant_id, limit=max(1, min(limit, 1000)))


# ----------------------------------------------------------------- writes


class BaseBody(BaseModel):
    rules: str
    note: str = ""


class VersionBody(BaseModel):
    version: int
    reason: str = ""


class PinBody(BaseModel):
    version: Optional[int] = None
    reason: str = ""


class NameBody(BaseModel):
    name: str
    reason: str = ""


class TemplateBody(BaseModel):
    sections: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class ConfigBody(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    expected_revision: Optional[int] = None


class PromptBody(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)
    addendum: str = ""
    note: str = ""


class TenantPatch(BaseModel):
    name: Optional[str] = None
    industry_id: Optional[int] = None
    reason: str = ""


class ProposeBody(BaseModel):
    intent: str


async def _reload_all() -> None:
    await reload_accounts([t["session_id"] for t in await store().list()])


async def _reload_industry(industry_id: int) -> None:
    await reload_accounts([t["session_id"] for t in await store().tenants_in_industry(industry_id)])


@router.put("/api/platform/base")
async def api_save_base(body: BaseBody) -> dict[str, Any]:
    saved = await guarded(store().save_base(body.rules, actor=ACTOR, note=body.note))
    await _reload_all()
    return saved


@router.post("/api/platform/base/rollback")
async def api_rollback_base(body: VersionBody) -> dict[str, Any]:
    saved = await guarded(store().rollback_base(body.version, actor=ACTOR, reason=body.reason))
    await _reload_all()
    return saved


class ModelPrice(BaseModel):
    model_config = {"extra": "forbid"}
    input_cache_hit: float = Field(ge=0, le=1000)
    input_cache_miss: float = Field(ge=0, le=1000)
    output: float = Field(ge=0, le=1000)


class PricesBody(BaseModel):
    """llm_prices: per 1M tokens, in `currency`. The vision model (a
    different provider) needs its own row, or it is costed at the highest
    listed rate."""
    model_config = {"extra": "forbid"}
    currency: str = Field("USD", pattern="^(USD|EUR)$")
    usd_to_eur: float = Field(gt=0, le=10)
    models: dict[str, ModelPrice] = Field(min_length=1, max_length=50)


@router.get("/api/platform/prices")
async def get_prices() -> dict[str, Any]:
    llm_usage.reset_cache()
    return await llm_usage.load_prices(_get_pool())


@router.put("/api/platform/prices")
async def put_prices(body: PricesBody) -> dict[str, Any]:
    pool = _get_pool()
    before = await llm_usage.load_prices(pool)
    value = body.model_dump()
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            "INSERT INTO platform_settings (key, value) VALUES ('llm_prices', $1::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps(value),
        )
        await audit.record(con, tenant_id=None, actor=ACTOR, event=audit.CONFIG_CHANGED,
                           reason="LLM prices changed", payload={"before": before, "after": value})
    llm_usage.reset_cache()
    return value


@router.post("/api/industries")
async def api_create_industry(body: NameBody) -> dict[str, Any]:
    return await guarded(store().create_industry(body.name, actor=ACTOR, reason=body.reason))


@router.put("/api/industries/{industry_id}/template")
async def api_save_template(industry_id: int, body: TemplateBody) -> dict[str, Any]:
    saved = await guarded(store().save_industry_template(
        industry_id, {"sections": body.sections}, actor=ACTOR, note=body.note))
    await _reload_industry(industry_id)
    return saved


@router.post("/api/industries/{industry_id}/template/rollback")
async def api_rollback_template(industry_id: int, body: VersionBody) -> dict[str, Any]:
    saved = await guarded(store().rollback_industry_template(
        industry_id, body.version, actor=ACTOR, reason=body.reason))
    await _reload_industry(industry_id)
    return saved


@router.put("/api/industries/{industry_id}/config")
async def api_save_industry_config(industry_id: int, body: ConfigBody) -> dict[str, Any]:
    saved = await guarded(store().save_industry_config(
        industry_id, body.overrides, actor=ACTOR, reason=body.reason, expected_revision=body.expected_revision))
    await _reload_industry(industry_id)
    return saved


@router.patch("/api/tenants/{tenant_id}")
async def api_patch_tenant(tenant_id: int, body: TenantPatch) -> dict[str, Any]:
    tenant = await guarded(store().update(
        tenant_id, actor=ACTOR, reason=body.reason, name=body.name, industry_id=body.industry_id))
    await reload_accounts([tenant["session_id"]])
    return tenant


@router.put("/api/tenants/{tenant_id}/config")
async def api_save_tenant_config(tenant_id: int, body: ConfigBody) -> dict[str, Any]:
    before = (await guarded(store().bundle(tenant_id))).config["booking"]
    bundle = await guarded(store().save_config(
        tenant_id, body.overrides, actor=ACTOR, reason=body.reason, expected_revision=body.expected_revision))
    await reload_accounts([bundle.tenant["session_id"]], bundle)
    # Booking requests that waited for a provider go out once there is one.
    after = bundle.config["booking"]
    if bundle.tenant["session_id"] and after["enabled"] and (
        after["provider"] != before["provider"] or not before["enabled"]
    ):
        try:
            await _get_bus().dispatch(bundle.tenant["session_id"], "resend_unsent_bookings", {},
                                      timeout=RELOAD_TIMEOUT)
        except commands.CommandError:
            pass
    return await api_tenant(tenant_id)


@router.put("/api/tenants/{tenant_id}/prompt")
async def api_save_tenant_prompt(tenant_id: int, body: PromptBody) -> dict[str, Any]:
    bundle = await guarded(store().save_client_prompt(
        tenant_id, {"overrides": body.overrides, "addendum": body.addendum}, actor=ACTOR, note=body.note))
    await reload_accounts([bundle.tenant["session_id"]], bundle)
    return await api_tenant(tenant_id)


@router.post("/api/tenants/{tenant_id}/prompt/rollback")
async def api_rollback_tenant_prompt(tenant_id: int, body: VersionBody) -> dict[str, Any]:
    bundle = await guarded(store().rollback_client_prompt(tenant_id, body.version, actor=ACTOR, reason=body.reason))
    await reload_accounts([bundle.tenant["session_id"]], bundle)
    return await api_tenant(tenant_id)


@router.post("/api/tenants/{tenant_id}/pin")
async def api_pin(tenant_id: int, body: PinBody) -> dict[str, Any]:
    bundle = await guarded(store().pin(tenant_id, body.version, actor=ACTOR, reason=body.reason))
    await reload_accounts([bundle.tenant["session_id"]], bundle)
    return await api_tenant(tenant_id)


@router.post("/api/tenants/{tenant_id}/config/propose")
async def api_propose(tenant_id: int, body: ProposeBody) -> dict[str, Any]:
    """Asks the LLM for a config change. Writes nothing but an audit row
    recording that a proposal was made; applying it is a separate PUT."""
    key = platform_key()
    if not key:
        raise HTTPException(
            status_code=400,
            detail="DEEPSEEK_PLATFORM_KEY is not set in .env, so the config helper has no API key to use.",
        )
    s = store()
    tenant = await guarded(s.get(tenant_id))
    industry = await s.get_industry(tenant["industry_id"])
    pool = _get_pool()

    async def meter(model: str, usage: dict[str, int]) -> None:
        # The platform's own key: counted as platform spend, not the tenant's.
        await llm_usage.record(pool, tenant_id=None, purpose="config_assist", model=model, usage=usage)

    try:
        result = await config_assist.propose(
            api_key=key, intent=body.intent, industry_config=industry["default_config"],
            client_overrides=tenant["config_json"], usage_sink=meter,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except config_assist.ai_responder.AIResponderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await audit.record(
        pool, tenant_id=tenant_id, actor=ACTOR, event=audit.CONFIG_PROPOSED, reason=result["intent"],
        payload={"proposal": result["proposal"], "valid": result["valid"], "changes": result["changes"]},
    )
    result["revision"] = tenant["config_revision"]
    return result
