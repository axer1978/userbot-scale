"""Admin API for safety and control: kill switches, billing, alerts,
health. Mounted by panel.py behind the admin login, so all of it (the global
stop included) is reachable only by the platform admin.

Every change goes through controls.py / billing.py (an audit row each) and
then tells the running account to re-read its switches; it also rechecks
on every send and every scheduler tick.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import alerts
import audit
import billing
import commands
import controls
import health
import proxies
from database import SessionRegistry

log = logging.getLogger("safety_api")

router = APIRouter()
ACTOR = audit.ADMIN
SCHEDULER_STALE_SECONDS = 180

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


async def _tenant(tenant_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow(
        "SELECT t.id, t.name, t.session_id, s.label, s.state, s.is_active FROM tenants t "
        "LEFT JOIN telegram_sessions s ON s.session_id = t.session_id WHERE t.id = $1", tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown client")
    return dict(row)


async def _changed(session_ids: Optional[list[Optional[str]]]) -> None:
    """Tell the running accounts, and any open panel tab, right away."""
    pool, bus = _get_pool(), _get_bus()
    await controls.reload_controls(pool, bus, None if session_ids is None else [s for s in session_ids if s])
    if bus is None:
        return
    if session_ids is None:
        rows = await pool.fetch("SELECT id, session_id FROM tenants WHERE session_id IS NOT NULL")
    else:
        rows = await pool.fetch("SELECT id, session_id FROM tenants WHERE session_id = ANY($1)",
                                [s for s in session_ids if s])
    for row in rows:
        await bus.publish_event(row["session_id"], {
            "type": "controls", "off_reason": await controls.off_reason(pool, row["id"]),
            "holds": await controls.holds(pool, row["id"]),
        })


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


async def _heartbeat() -> dict[str, Any]:
    value = await _get_pool().fetchval("SELECT value FROM platform_settings WHERE key = 'scheduler_heartbeat'")
    value = json.loads(value) if isinstance(value, str) else value
    if not value:
        return {"at": None, "stale": True}
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return {"at": _iso(at), "stale": (datetime.now(timezone.utc) - at).total_seconds() > SCHEDULER_STALE_SECONDS}


# ------------------------------------------------------------------ overview


@router.get("/api/safety")
async def api_overview() -> dict[str, Any]:
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name, t.session_id, t.status, t.billing_next_due, t.grace_until,
               s.label, s.state, s.is_active,
               h.status AS health, h.status_reason AS health_reason, h.last_seen_at, h.last_error,
               (SELECT json_agg(json_build_object('kind', kind, 'reason', reason, 'created_at', created_at)
                                ORDER BY created_at, kind)
                  FROM tenant_holds WHERE tenant_id = t.id) AS holds,
               (SELECT count(*) FROM alerts a WHERE a.tenant_id = t.id AND a.acknowledged_at IS NULL) AS alerts
          FROM tenants t
          LEFT JOIN telegram_sessions s ON s.session_id = t.session_id
          LEFT JOIN sessions_health h ON h.tenant_id = t.id
         ORDER BY lower(t.name), t.id
        """
    )
    tenants = []
    for row in rows:
        holds = row["holds"]
        holds = json.loads(holds) if isinstance(holds, str) else (holds or [])
        tenants.append({
            "id": row["id"], "name": row["name"], "session_id": row["session_id"], "label": row["label"],
            "session_state": row["state"], "session_active": row["is_active"],
            "billing": {"status": row["status"], "next_due": row["billing_next_due"].isoformat()
                        if row["billing_next_due"] else None, "grace_until": _iso(row["grace_until"])},
            "holds": [{**h, "label": controls.LABELS.get(h["kind"], h["kind"])} for h in holds],
            "health": {"status": row["health"] or health.UNKNOWN, "reason": row["health_reason"] or "",
                       "last_seen_at": _iso(row["last_seen_at"]), "last_error": row["last_error"] or ""},
            "open_alerts": row["alerts"],
        })
    return {
        "global_stop": await controls.global_stop(pool),
        "scheduler": await _heartbeat(),
        "alerts": await alerts.open_count(pool),
        "tenants": tenants,
    }


@router.get("/api/safety/summary")
async def api_summary() -> dict[str, Any]:
    """Polled by the top bar: open alerts, the global stop, the scheduler."""
    pool = _get_pool()
    return {"alerts": await alerts.open_count(pool), "global_stop": await controls.global_stop(pool),
            "scheduler": await _heartbeat()}


class GlobalStopBody(BaseModel):
    on: bool
    reason: str = Field("", max_length=500)


@router.post("/api/safety/global-stop")
async def api_global_stop(body: GlobalStopBody) -> dict[str, Any]:
    try:
        state = await controls.set_global_stop(_get_pool(), body.on, reason=body.reason, actor=ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    log.warning("GLOBAL STOP %s from the panel: %s", "ON" if body.on else "OFF", body.reason)
    await _changed(None)
    return state


# -------------------------------------------------------------------- alerts


@router.get("/api/alerts")
async def api_alerts(open: bool = False, tenant_id: Optional[int] = None, limit: int = 200) -> list[dict[str, Any]]:
    return await alerts.list_alerts(_get_pool(), open_only=open, tenant_id=tenant_id,
                                    limit=max(1, min(limit, 1000)))


@router.post("/api/alerts/{alert_id}/ack")
async def api_ack(alert_id: int) -> dict[str, Any]:
    alert = await alerts.acknowledge(_get_pool(), alert_id, by=ACTOR)
    if alert is None:
        raise HTTPException(status_code=404, detail="Unknown alert")
    return alert


class AckAllBody(BaseModel):
    tenant_id: Optional[int] = None


@router.post("/api/alerts/ack-all")
async def api_ack_all(body: AckAllBody) -> dict[str, Any]:
    return {"acknowledged": await alerts.acknowledge_all(_get_pool(), by=ACTOR, tenant_id=body.tenant_id)}


# ------------------------------------------------------------ one tenant


@router.get("/api/tenants/{tenant_id}/controls")
async def api_controls(tenant_id: int) -> dict[str, Any]:
    tenant = await _tenant(tenant_id)
    pool = _get_pool()
    return {
        "tenant": tenant,
        **await controls.overview(pool, tenant_id),
        "health": await health.overview(pool, tenant_id),
        "alerts": await alerts.list_alerts(pool, tenant_id=tenant_id, limit=50),
        # Host, port and user only: the password never leaves the server.
        "proxy": proxies.describe(await SessionRegistry(pool).load_proxy(tenant["session_id"]))
        if tenant["session_id"] else None,
    }


class ProxyBody(BaseModel):
    # socks5://user:pass@host:port, or "" for a direct connection.
    proxy_url: str = Field("", max_length=500)


RECONNECT_TIMEOUT = 30.0


@router.put("/api/tenants/{tenant_id}/proxy")
async def api_set_proxy(tenant_id: int, body: ProxyBody) -> dict[str, Any]:
    """Set, change or clear the account's Telegram proxy. The server first
    checks it can reach the proxy at all; then the running account
    reconnects through it."""
    tenant = await _tenant(tenant_id)
    if not tenant["session_id"]:
        raise HTTPException(status_code=400, detail="This client has no Telegram account.")
    url = body.proxy_url.strip()
    pool = _get_pool()
    if url:
        try:
            ok, why = await proxies.reachable(url)
        except proxies.ProxyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not ok:
            raise HTTPException(status_code=400, detail=f"The proxy is not reachable from the server: {why}.")
    await SessionRegistry(pool).set_proxy(tenant["session_id"], url or None)
    described = proxies.describe(url or None)
    await audit.record(pool, tenant_id=tenant_id, actor=ACTOR, event=audit.PROXY_CHANGED,
                       reason="proxy set" if url else "proxy removed",
                       payload={"proxy": described})
    reconnected = False
    bus = _get_bus()
    if bus is not None:
        try:
            await bus.dispatch(tenant["session_id"], "reconnect", {}, timeout=RECONNECT_TIMEOUT)
            reconnected = True
        except commands.CommandTimeout:
            pass  # not running: it uses the proxy when it next starts
        except commands.CommandError as exc:
            log.warning("[%s] Reconnect after a proxy change failed: %s", tenant["session_id"], exc)
    return {"proxy": described, "reconnected": reconnected}


class ReasonBody(BaseModel):
    reason: str = Field("", max_length=500)


class ResumeBody(BaseModel):
    kind: str
    reason: str = Field("", max_length=500)


@router.post("/api/tenants/{tenant_id}/soft-off")
async def api_soft_off(tenant_id: int, body: ReasonBody) -> dict[str, Any]:
    tenant = await _tenant(tenant_id)
    await controls.add_hold(_get_pool(), tenant_id, controls.MANUAL, body.reason.strip() or "paused from the panel",
                            actor=ACTOR)
    await _changed([tenant["session_id"]])
    return await api_controls(tenant_id)


@router.post("/api/tenants/{tenant_id}/resume")
async def api_resume(tenant_id: int, body: ResumeBody) -> dict[str, Any]:
    tenant = await _tenant(tenant_id)
    if body.kind not in controls.KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(controls.KINDS)}")
    if body.kind == controls.BILLING:
        raise HTTPException(status_code=400,
                            detail="A billing suspension is lifted by recording a payment or setting the status.")
    if body.kind == controls.VERIFICATION:
        raise HTTPException(status_code=400,
                            detail="This lifts by itself when you approve the client's verification video (Review).")
    pool = _get_pool()
    removed = await controls.remove_hold(pool, tenant_id, body.kind, actor=ACTOR,
                                         reason=body.reason.strip() or "resumed from the panel")
    if not removed:
        raise HTTPException(status_code=404, detail="That hold is not on")
    if body.kind in (controls.ANOMALY, controls.TELEGRAM, controls.WHATSAPP, controls.SPEND_CAP):
        # The operator has looked; the alerts that led here are done.
        for row in await pool.fetch(
            "SELECT id FROM alerts WHERE tenant_id = $1 AND acknowledged_at IS NULL AND (kind = $2 OR kind LIKE $3)",
            tenant_id, body.kind, body.kind + ":%",
        ):
            await alerts.acknowledge(pool, row["id"], by=ACTOR)
    await _changed([tenant["session_id"]])
    return await api_controls(tenant_id)


class HardOffBody(BaseModel):
    reason: str = Field(max_length=500)
    # The account id typed again, so this is never one click.
    confirm: str


@router.post("/api/tenants/{tenant_id}/hard-off")
async def api_hard_off(tenant_id: int, body: HardOffBody) -> dict[str, Any]:
    tenant = await _tenant(tenant_id)
    if not tenant["session_id"] or body.confirm.strip() != tenant["session_id"]:
        raise HTTPException(status_code=400, detail="Type the account id exactly to confirm.")
    try:
        result = await controls.hard_off(_get_pool(), _get_bus(), tenant_id, reason=body.reason, actor=ACTOR)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await _changed([tenant["session_id"]])
    return {**result, "controls": await api_controls(tenant_id)}


class DueBody(BaseModel):
    next_due: Optional[date] = None
    reason: str = Field("", max_length=500)


class StatusBody(BaseModel):
    status: str
    reason: str = Field(max_length=500)


@router.put("/api/tenants/{tenant_id}/billing/due")
async def api_billing_due(tenant_id: int, body: DueBody) -> dict[str, Any]:
    await _tenant(tenant_id)
    await billing.set_due(_get_pool(), tenant_id, body.next_due, actor=ACTOR, reason=body.reason)
    return await api_controls(tenant_id)


@router.post("/api/tenants/{tenant_id}/billing/paid")
async def api_billing_paid(tenant_id: int, body: DueBody) -> dict[str, Any]:
    tenant = await _tenant(tenant_id)
    await billing.mark_paid(_get_pool(), _get_bus(), tenant_id, next_due=body.next_due, actor=ACTOR,
                            reason=body.reason)
    await _changed([tenant["session_id"]])
    return await api_controls(tenant_id)


@router.post("/api/tenants/{tenant_id}/billing/status")
async def api_billing_status(tenant_id: int, body: StatusBody) -> dict[str, Any]:
    tenant = await _tenant(tenant_id)
    try:
        await billing.set_status(_get_pool(), _get_bus(), tenant_id, body.status, actor=ACTOR, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await _changed([tenant["session_id"]])
    return await api_controls(tenant_id)


class BillingSettingsBody(BaseModel):
    grace_hours: int
    notice: str


@router.get("/api/platform/billing")
async def api_billing_settings() -> dict[str, Any]:
    return await billing.settings(_get_pool())


@router.put("/api/platform/billing")
async def api_save_billing_settings(body: BillingSettingsBody) -> dict[str, Any]:
    try:
        return await billing.save_settings(_get_pool(), grace_hours=body.grace_hours, notice=body.notice, actor=ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
