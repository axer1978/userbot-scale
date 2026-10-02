"""Manager (moderator) API, under /api/manager/*, and its page at /manager/.

Mounted by panel.py WITHOUT the admin login: these routes have their own
(manager_auth.py), and every one but login/logout checks it.

A manager moderates while the admin is away. What a manager may do is
fixed here, never stored, so there is no setting that widens it:

  may                                          may not (admin only)
  ---------------------------------------      ------------------------------------------
  see every client: health, holds, alerts      change any config, prompt or template
  pause a client's bot (a manual hold)         lift a billing, spend-cap, Telegram or
  lift a manual or anomaly hold                  WhatsApp hold; record payments
  read conversations (read-only)               send messages or approve drafts
  pause / unpause one conversation             global stop, hard-off, proxies, outreach
  acknowledge alerts                           link a business to a client login
  approve or reject client sign-ups            reset passwords or authenticators
  disable / enable a client login              create managers, edit the terms

Every action requires a reason and writes an audit row with the actor
"manager:<username>", so the admin can see exactly who did what.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import alerts
import audit
import commands
import controls
import health
import manager_auth
import owner_admin_api
import owner_auth
import totp
from database import Database

log = logging.getLogger("manager_api")

router = APIRouter()

EVENT_LOGIN = "manager_login"
EVENT_PASSWORD = "manager_password_changed"
EVENT_TOTP = "manager_totp_changed"
EVENT_CHAT_PAUSED = "chat_paused"
EVENT_CHAT_RESUMED = "chat_resumed"
EVENT_ALERT_ACK = "alert_acknowledged"

# The holds a manager may lift. A billing hold is lifted by a payment, a
# spend cap by its limit, a Telegram/WhatsApp hold by logging in again:
# all of those stay with the admin.
RESUMABLE = (controls.MANUAL, controls.ANOMALY)
TOTP_SETUP_SECONDS = 10 * 60
TOTP_ISSUER = "Moderator panel"
BEST_EFFORT_TIMEOUT = 5.0
MAX_ID = 2 ** 31 - 1

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731
# manager id -> (secret, issued at on time.monotonic()), until confirmed with a code.
_pending_totp: dict[int, tuple[str, float]] = {}


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus
    manager_auth.bind(get_pool=get_pool)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _reason(text: str) -> str:
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Give a reason: the admin sees it in the audit log.")
    return text


# ---------------------------------------------------------------- the page


@router.get("/manager", include_in_schema=False)
async def manager_page() -> RedirectResponse:
    return RedirectResponse("/manager/", status_code=307)


# ------------------------------------------------------------------- login


class LoginBody(BaseModel):
    username: str = Field("", max_length=200)
    password: str = Field("", max_length=1000)
    code: str = Field("", max_length=20)


@router.post("/api/manager/login")
async def api_login(body: LoginBody, request: Request) -> JSONResponse:
    pool = _get_pool()
    ip = owner_auth.client_ip(request)
    key = manager_auth.limit_key(body.username)
    owner_auth.check_rate_limit(ip, key)
    try:
        row = await manager_auth.authenticate(pool, body.username, body.password, body.code)
    except owner_auth.LoginFailed:
        owner_auth.note_failure(ip, key)
        log.warning("Failed manager login from %s.", ip)
        raise HTTPException(status_code=401, detail=manager_auth.WRONG_CREDENTIALS) from None
    except owner_auth.CodeRequired as exc:
        if exc.wrong:
            owner_auth.note_failure(ip, key)
            log.warning("Manager login from %s: wrong or reused authenticator code.", ip)
            raise HTTPException(status_code=401, detail="Wrong or already used code") from None
        raise HTTPException(status_code=401, detail=manager_auth.CODE_REQUIRED) from None
    owner_auth.clear_failures(ip, key)
    previous = manager_auth.token_from(request)
    if previous:
        await manager_auth.delete_session(pool, previous)
    token = await manager_auth.create_session(pool, row["id"], ip)
    await pool.execute("UPDATE managers SET last_login_at = now() WHERE id = $1", row["id"])
    manager = await manager_auth.session_manager(pool, token)
    await audit.record(pool, tenant_id=None, actor=manager_auth.actor(manager), event=EVENT_LOGIN,
                       reason="moderator panel login", payload={"manager_id": manager["id"], "ip": ip})
    response = JSONResponse({"ok": True, "gate": manager_auth.gate(manager)})
    manager_auth.set_cookie(response, token)
    return response


@router.post("/api/manager/logout")
async def api_logout(request: Request) -> JSONResponse:
    token = manager_auth.token_from(request)
    if token:
        await manager_auth.delete_session(_get_pool(), token)
    response = JSONResponse({"ok": True})
    manager_auth.clear_cookie(response)
    return response


@router.get("/api/manager/account")
async def api_account(manager: dict = Depends(manager_auth.any_manager)) -> dict[str, Any]:
    return {"username": manager["username"], "display_name": manager["display_name"],
            "totp": manager["totp"], "gate": manager_auth.gate(manager)}


class PasswordBody(BaseModel):
    current: str = Field("", max_length=1000)
    new: str = Field("", max_length=1000)


@router.post("/api/manager/password")
async def api_password(body: PasswordBody, request: Request,
                       manager: dict = Depends(manager_auth.any_manager)) -> dict[str, Any]:
    pool = _get_pool()
    ip = owner_auth.client_ip(request)
    key = manager_auth.limit_key(manager["username"])
    owner_auth.check_rate_limit(ip, key)
    stored = await pool.fetchval("SELECT password_hash FROM managers WHERE id = $1", manager["id"])
    if not owner_auth.verify_password(body.current, stored):
        owner_auth.note_failure(ip, key)
        raise HTTPException(status_code=400, detail="The current password is wrong.")
    try:
        owner_auth.check_new_password(body.new)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if body.new == body.current:
        raise HTTPException(status_code=400, detail="Choose a password different from the current one.")
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            "UPDATE managers SET password_hash = $2, must_change_password = false, updated_at = now() WHERE id = $1",
            manager["id"], owner_auth.hash_password(body.new),
        )
        await manager_auth.kill_sessions(con, manager["id"], keep_token=manager_auth.token_from(request))
        await audit.record(con, tenant_id=None, actor=manager_auth.actor(manager), event=EVENT_PASSWORD,
                           reason="changed by the manager", payload={"manager_id": manager["id"]})
    return {"ok": True}


class CodeBody(BaseModel):
    code: str = Field("", max_length=20)


@router.post("/api/manager/totp")
async def api_totp_setup(body: CodeBody, request: Request,
                         manager: dict = Depends(manager_auth.any_manager)) -> dict[str, Any]:
    """Two steps, as on the client dashboard: without a code a new secret,
    with a code from the app that secret switched on. There is no way to
    turn it off here; the admin removes it for a lost phone."""
    pool = _get_pool()
    if manager["must_change_password"]:
        raise HTTPException(status_code=403, detail=manager_auth.CHANGE_PASSWORD)
    if manager["totp"]:
        raise HTTPException(status_code=409, detail="The authenticator is already set up.")
    if not body.code.strip():
        secret = totp.new_secret()
        _pending_totp[manager["id"]] = (secret, time.monotonic())
        return {"secret": secret,
                "uri": totp.provisioning_uri(secret, account=manager["username"], issuer=TOTP_ISSUER)}
    ip = owner_auth.client_ip(request)
    key = manager_auth.limit_key(manager["username"])
    owner_auth.check_rate_limit(ip, key)
    pending = _pending_totp.get(manager["id"])
    if pending is None or time.monotonic() - pending[1] > TOTP_SETUP_SECONDS:
        _pending_totp.pop(manager["id"], None)
        raise HTTPException(status_code=400, detail="Start the setup again: the secret expired.")
    if not manager_auth.use_code(manager["id"], pending[0], body.code):
        owner_auth.note_failure(ip, key)
        raise HTTPException(status_code=400, detail="That code doesn't match. Check the app's clock and try again.")
    await pool.execute("UPDATE managers SET totp_secret_enc = $2, updated_at = now() WHERE id = $1",
                       manager["id"], manager_auth.encrypt_totp(manager["id"], pending[0]))
    _pending_totp.pop(manager["id"], None)
    await audit.record(pool, tenant_id=None, actor=manager_auth.actor(manager), event=EVENT_TOTP,
                       reason="authenticator set up by the manager", payload={"manager_id": manager["id"]})
    return {"ok": True, "totp": True}


# -------------------------------------------------------------- overview


@router.get("/api/manager/overview")
async def api_overview(manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name, t.channel, t.status AS billing,
               s.state AS session_state, s.is_active,
               h.status AS health, h.status_reason AS health_reason, h.last_seen_at,
               (SELECT json_agg(json_build_object('kind', kind, 'reason', reason, 'created_at', created_at)
                                ORDER BY created_at, kind)
                  FROM tenant_holds WHERE tenant_id = t.id) AS holds,
               (SELECT count(*) FROM alerts a WHERE a.tenant_id = t.id AND a.acknowledged_at IS NULL) AS alerts,
               (SELECT count(*) FROM unanswered_queue u WHERE u.tenant_id = t.id AND u.status = 'open')
                 AS unanswered
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
            "id": row["id"], "name": row["name"], "channel": row["channel"], "billing": row["billing"],
            "session_state": row["session_state"], "session_active": row["is_active"],
            "holds": [{**h, "label": controls.LABELS.get(h["kind"], h["kind"]),
                       "resumable": h["kind"] in RESUMABLE} for h in holds],
            "health": {"status": row["health"] or health.UNKNOWN, "reason": row["health_reason"] or "",
                       "last_seen_at": _iso(row["last_seen_at"])},
            "open_alerts": row["alerts"], "unanswered_open": row["unanswered"],
        })
    return {
        "me": {"username": manager["username"], "display_name": manager["display_name"]},
        "global_stop": await controls.global_stop(pool),
        "alerts": await alerts.open_count(pool),
        "pending_signups": await pool.fetchval("SELECT count(*) FROM owners WHERE status = 'pending'"),
        "tenants": tenants,
    }


# ---------------------------------------------------------- one client


async def _tenant(tenant_id: int) -> dict[str, Any]:
    row = await _get_pool().fetchrow("SELECT id, name, session_id FROM tenants WHERE id = $1", tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown client")
    return dict(row)


async def _controls_changed(tenant: dict[str, Any]) -> None:
    """Tell the running account, and any open admin tab, right away. It
    also rechecks on every send and every scheduler tick."""
    pool, bus = _get_pool(), _get_bus()
    if not tenant["session_id"]:
        return
    await controls.reload_controls(pool, bus, [tenant["session_id"]])
    if bus is not None:
        await bus.publish_event(tenant["session_id"], {
            "type": "controls", "off_reason": await controls.off_reason(pool, tenant["id"]),
            "holds": await controls.holds(pool, tenant["id"]),
        })


async def _controls(tenant_id: int) -> dict[str, Any]:
    pool = _get_pool()
    overview = await controls.overview(pool, tenant_id)
    return {"holds": [{**h, "resumable": h["kind"] in RESUMABLE} for h in overview["holds"]],
            "off_reason": overview["off_reason"], "global_stop": overview["global_stop"]}


class ReasonBody(BaseModel):
    reason: str = Field("", max_length=500)


@router.post("/api/manager/tenants/{tenant_id}/pause")
async def api_pause(reason_body: ReasonBody, tenant_id: int = Path(ge=1, le=MAX_ID),
                    manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    """Soft-off: messages are still received and stored, nothing is sent
    on its own until the hold is lifted."""
    reason = _reason(reason_body.reason)
    tenant = await _tenant(tenant_id)
    added = await controls.add_hold(_get_pool(), tenant_id, controls.MANUAL,
                                    f"{reason} (by {manager_auth.actor(manager)})", actor=manager_auth.actor(manager))
    if not added:
        raise HTTPException(status_code=409, detail="This client is already paused by hand.")
    await _controls_changed(tenant)
    return await _controls(tenant_id)


class ResumeBody(BaseModel):
    kind: str
    reason: str = Field("", max_length=500)


@router.post("/api/manager/tenants/{tenant_id}/resume")
async def api_resume(body: ResumeBody, tenant_id: int = Path(ge=1, le=MAX_ID),
                     manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    reason = _reason(body.reason)
    if body.kind not in RESUMABLE:
        raise HTTPException(status_code=403, detail="Only the admin can lift this kind of hold.")
    tenant = await _tenant(tenant_id)
    pool, who = _get_pool(), manager_auth.actor(manager)
    if not await controls.remove_hold(pool, tenant_id, body.kind, actor=who, reason=reason):
        raise HTTPException(status_code=404, detail="That hold is not on")
    if body.kind == controls.ANOMALY:
        for row in await pool.fetch(
            "SELECT id FROM alerts WHERE tenant_id = $1 AND acknowledged_at IS NULL AND (kind = $2 OR kind LIKE $3)",
            tenant_id, body.kind, body.kind + ":%",
        ):
            await alerts.acknowledge(pool, row["id"], by=who)
    await _controls_changed(tenant)
    return await _controls(tenant_id)


def _db(tenant: dict[str, Any]) -> Database:
    if not tenant["session_id"]:
        raise HTTPException(status_code=404, detail="This client has no messaging account.")
    return Database(_get_pool(), tenant["session_id"])


@router.get("/api/manager/tenants/{tenant_id}/conversations")
async def api_conversations(tenant_id: int = Path(ge=1, le=MAX_ID),
                            manager: dict = Depends(manager_auth.current_manager)) -> list[dict[str, Any]]:
    return await _db(await _tenant(tenant_id)).list_conversations()


@router.get("/api/manager/tenants/{tenant_id}/conversations/{chat_id}/messages")
async def api_messages(tenant_id: int = Path(ge=1, le=MAX_ID), chat_id: int = Path(),
                       manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    db = _db(await _tenant(tenant_id))
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    return {"conversation": conversation, "messages": await db.get_messages(chat_id)}


class ChatPauseBody(BaseModel):
    paused: bool
    reason: str = Field("", max_length=500)


@router.post("/api/manager/tenants/{tenant_id}/conversations/{chat_id}/pause")
async def api_chat_pause(body: ChatPauseBody, tenant_id: int = Path(ge=1, le=MAX_ID), chat_id: int = Path(),
                         manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    reason = _reason(body.reason)
    tenant = await _tenant(tenant_id)
    db = _db(tenant)
    conversation = await db.set_paused(chat_id, body.paused)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    await audit.record(_get_pool(), tenant_id=tenant_id, actor=manager_auth.actor(manager),
                       event=EVENT_CHAT_PAUSED if body.paused else EVENT_CHAT_RESUMED, reason=reason,
                       payload={"chat_id": chat_id})
    bus = _get_bus()
    if bus is not None:
        if body.paused:
            try:
                await bus.dispatch(tenant["session_id"], "cancel_draft", {"chat_id": chat_id},
                                   timeout=BEST_EFFORT_TIMEOUT)
            except commands.CommandTimeout:
                pass  # not running: nothing is being drafted
            except Exception:
                log.debug("[%s] cancel_draft after a manager pause failed", tenant["session_id"], exc_info=True)
        await bus.publish_event(tenant["session_id"], {"type": "conversation", "conversation": conversation})
    return conversation


# ---------------------------------------------------------------- alerts


@router.get("/api/manager/alerts")
async def api_alerts(open: bool = True, tenant_id: Optional[int] = None,
                     manager: dict = Depends(manager_auth.current_manager)) -> list[dict[str, Any]]:
    return await alerts.list_alerts(_get_pool(), open_only=open, tenant_id=tenant_id, limit=200)


@router.post("/api/manager/alerts/{alert_id}/ack")
async def api_ack(alert_id: int = Path(ge=1, le=MAX_ID),
                  manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    pool = _get_pool()
    alert = await alerts.acknowledge(pool, alert_id, by=manager_auth.actor(manager))
    if alert is None:
        raise HTTPException(status_code=404, detail="Unknown alert")
    await audit.record(pool, tenant_id=alert["tenant_id"], actor=manager_auth.actor(manager),
                       event=EVENT_ALERT_ACK, reason=alert["kind"], payload={"alert_id": alert_id})
    return alert


# ------------------------------------------------------------ client logins


@router.get("/api/manager/clients")
async def api_clients(manager: dict = Depends(manager_auth.current_manager)) -> list[dict[str, Any]]:
    """Every client login, waiting sign-ups first."""
    rows = await _get_pool().fetch(owner_admin_api._SELECT +
                                   " ORDER BY o.status <> 'pending', lower(o.username)")
    return [owner_admin_api._owner(r) for r in rows]


@router.post("/api/manager/clients/{owner_id}/approve")
async def api_approve(body: ReasonBody, owner_id: int = Path(ge=1, le=MAX_ID),
                      manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    """Activates the login; linking it to a business stays with the admin."""
    return await owner_admin_api.approve(_get_pool(), owner_id, actor=manager_auth.actor(manager),
                                         reason=_reason(body.reason))


@router.post("/api/manager/clients/{owner_id}/reject")
async def api_reject(body: ReasonBody, owner_id: int = Path(ge=1, le=MAX_ID),
                     manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    return await owner_admin_api.reject(_get_pool(), owner_id, actor=manager_auth.actor(manager),
                                        reason=body.reason)


class DisableBody(BaseModel):
    disabled: bool
    reason: str = Field("", max_length=500)


@router.post("/api/manager/clients/{owner_id}/disabled")
async def api_disable(body: DisableBody, owner_id: int = Path(ge=1, le=MAX_ID),
                      manager: dict = Depends(manager_auth.current_manager)) -> dict[str, Any]:
    return await owner_admin_api.set_disabled(_get_pool(), owner_id, body.disabled,
                                              actor=manager_auth.actor(manager), reason=_reason(body.reason))
