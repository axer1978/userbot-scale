"""Admin API for operator rules (rules.py): list, propose, accept, edit,
reject, retire, and fold them into the prompt; and the style card
(stylecard.py). Mounted by panel.py behind the admin login; every route is
under /api/finetune/ (admin only, never a manager's, see
staff.ADMIN_ONLY_PREFIXES).

- A rule the admin types is created active: writing it is the decision, so
  it is not proposed back to its own author. Past the cap it is refused,
  like any activation, with the advice to fold first.
- Suggest sends the account's next edits and corrections to DeepSeek (on
  the platform key) and stores what it finds as proposed rules.
- Every change to the set of active rules reloads the accounts it reaches,
  as platform_api does for a prompt change: the industry's accounts for an
  industry-wide rule, the one account for an account's own.
- Fold starts a finetune run (source 'rules') at once instead of waiting for
  the weekly round; it is reviewed and applied in Finetune → Runs.

Audit events are rules.py's (it writes them, in the same transaction as the
change): prompt_rule_created, prompt_rule_changed, prompt_rules_suggested,
prompt_rules_merged; a fold run is finetune_started like any run.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import finetune_api
import platform_api
import prompt_layers
import rules
import stylecard
import vision

router = APIRouter()
ACTOR = audit.ADMIN

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


async def _guarded(call):
    """Run a rules call, turning its errors into HTTP answers."""
    try:
        return await call
    except rules.CapReached as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rules.RuleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rules.RuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Unknown rule") from exc


async def _tenant(tenant_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    s = platform_api.store()
    tenant = await platform_api.guarded(s.get(tenant_id))
    return tenant, await s.get_industry(tenant["industry_id"])


async def _reload(*changed: dict[str, Any]) -> None:
    """Reload, at once, the accounts these rules (before and after a
    change) reach: every account of the industry for an industry-wide
    rule, the one account for an account's own."""
    s = platform_api.store()
    sessions: set[Optional[str]] = set()
    for industry_id in {r["industry_id"] for r in changed if r["tenant_id"] is None}:
        sessions |= {t["session_id"] for t in await s.tenants_in_subtree(industry_id)}
    for tenant_id in {r["tenant_id"] for r in changed if r["tenant_id"] is not None}:
        sessions.add((await s.get(tenant_id))["session_id"])
    await platform_api.reload_accounts(sorted(session for session in sessions if session))


async def _last_fold(tenant_id: int) -> Optional[dict[str, Any]]:
    row = await _get_pool().fetchrow(finetune_api._SELECT + " WHERE tenant_id = $1 AND source = $2 ORDER BY id DESC "
                                     "LIMIT 1", tenant_id, finetune_api.RULES_SOURCE)
    return finetune_api._run(row, full=False) if row else None


# --------------------------------------------------------------------- rules


@router.get("/api/finetune/rules")
async def api_rules(tenant_id: int) -> dict[str, Any]:
    """The rules that reach this account (its industry's and its own, in
    every status), the cap and the counts per scope, what waits for a
    suggestion, whether the platform key is set, and the last fold run."""
    tenant, industry = await _tenant(tenant_id)
    pool = _get_pool()
    return {
        "tenant_id": tenant_id,
        "industry": {"id": industry["id"], "name": industry["name"]},
        "rules": await rules.list_for(pool, tenant_id, industry["id"]),
        "cap": rules.CAP,
        "counts": await rules.counts(pool, tenant_id, industry["id"]),
        "waiting": await rules.waiting(pool, tenant_id),
        "platform_key": bool(platform_api.platform_key()),
        "sections": [{"key": key, "heading": heading} for key, heading in prompt_layers.SECTIONS],
        "last_fold": await _last_fold(tenant_id),
    }


class RuleBody(BaseModel):
    tenant_id: int
    text: str = Field(..., max_length=rules.MAX_CHARS * 2)
    section: str
    scope: Literal["industry", "account"] = "industry"


@router.post("/api/finetune/rules")
async def api_create(body: RuleBody) -> dict[str, Any]:
    """A rule typed by the admin: created active (see the module docstring)."""
    tenant, industry = await _tenant(body.tenant_id)
    rule = await _guarded(rules.create(
        _get_pool(), industry_id=industry["id"], tenant_id=tenant["id"] if body.scope == rules.ACCOUNT else None,
        section=body.section, text=body.text, source=rules.MANUAL, source_ref={"tenant_id": tenant["id"]},
        status=rules.ACTIVE, created_by=ACTOR, actor=ACTOR))
    await _reload(rule)
    return rule


class TenantBody(BaseModel):
    tenant_id: int


@router.post("/api/finetune/rules/suggest")
async def api_suggest(body: TenantBody) -> dict[str, Any]:
    """The next batch of this account's edits and corrections, read by
    DeepSeek on the platform key: the rules it proposes, how many it read,
    and what still waits."""
    await _tenant(body.tenant_id)
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to suggest rules.")
    pool = _get_pool()
    try:
        result = await rules.suggest(pool, body.tenant_id, api_key=api_key, actor=ACTOR)
    except rules.RuleError as exc:
        raise HTTPException(status_code=502, detail=f"{exc} Nothing was saved; try again.") from exc
    except vision.VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result["waiting"] = await rules.waiting(pool, body.tenant_id)
    return result


class RulePatch(BaseModel):
    text: Optional[str] = Field(None, max_length=rules.MAX_CHARS * 2)
    section: Optional[str] = None
    scope: Optional[Literal["industry", "account"]] = None
    status: Optional[Literal["proposed", "active", "rejected", "retired"]] = None
    # The account a rule moves to with scope "account"; else the one it came from.
    tenant_id: Optional[int] = None


@router.patch("/api/finetune/rules/{rule_id}")
async def api_patch(rule_id: int, body: RulePatch) -> dict[str, Any]:
    """Accept (status active), reject, retire, or edit a rule."""
    before, after = await _guarded(rules.update(
        _get_pool(), rule_id, actor=ACTOR, text=body.text, section=body.section, scope=body.scope,
        status=body.status, tenant_id=body.tenant_id))
    if rules.changes_prompt(before, after):
        await _reload(before, after)
    return after


@router.post("/api/finetune/rules/fold")
async def api_fold(body: TenantBody) -> dict[str, Any]:
    """Fold now: this account's active rules into its business layer and its
    industry's into the standard, as a finetune run (returned like
    /api/finetune/runs returns one)."""
    await _tenant(body.tenant_id)
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to fold rules.")
    pool = _get_pool()
    running = await rules.running_fold(pool, body.tenant_id)
    if running is not None:
        raise HTTPException(status_code=409, detail=f"A fold of this account is already running (run {running}).")
    plan = await rules.plan_fold(pool, body.tenant_id)
    return await _guarded(rules.start_fold(pool, plan, api_key=api_key, actor=ACTOR))


# ---------------------------------------------------------------- style card


@router.get("/api/finetune/style-card")
async def api_style_card(tenant_id: int) -> dict[str, Any]:
    """How the business types (stylecard.py), measured with this account's
    style_card settings even while the card is off, so it can be previewed."""
    bundle = await platform_api.guarded(platform_api.store().bundle(tenant_id))
    return {"tenant_id": tenant_id, **await stylecard.card(_get_pool(), tenant_id, bundle.config)}
