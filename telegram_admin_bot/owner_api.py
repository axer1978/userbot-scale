"""Client-facing API: the owner's login and dashboard, under /api/owner/*.

Mounted by panel.py WITHOUT the admin login: these routes have their own
(owner_auth.py). Every query is scoped to the tenants linked to the
logged-in owner (owner_tenants): a tenant id or queue item that isn't one
of theirs is answered with 404, the same as one that doesn't exist, so an
owner can't even learn which ids belong to someone else.

What an owner can do is deliberately small: read their numbers, bookings
and unanswered messages, mark an unanswered message reviewed, and manage
their own password and authenticator. Nothing here changes a bot's config,
pauses it, or reaches the kill switches; those stay behind the admin login.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import audit
import booking_states as bs
import booking_store
import controls
import health
import owner_auth
import stats
import tenant_config
import totp
import unanswered

log = logging.getLogger("owner_api")

router = APIRouter()

# Audit events written by the owner's own actions.
EVENT_LOGIN = "owner_login"
EVENT_PASSWORD = "owner_password_changed"
EVENT_TOTP = "owner_totp_changed"
EVENT_REVIEWED = "unanswered_reviewed"

# Booking columns an owner never needs: the customer's secret page token,
# the pseudonymous ref, and plumbing between this system and Telegram/Google.
HIDDEN_BOOKING_FIELDS = ("customer_token", "customer_ref", "provider_chat_id", "provider_message_id",
                         "calendar_event_id", "session_id", "legacy")
UPCOMING_DAYS = 7
CHART_WEEKS = 8
# How long a freshly issued authenticator secret waits for its first code.
TOTP_SETUP_SECONDS = 10 * 60
TOTP_ISSUER = "Receptionist dashboard"

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731
# owner id -> (secret, issued at on time.monotonic()), until confirmed with a code.
_pending_totp: dict[int, tuple[str, float]] = {}


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus
    owner_auth.bind(get_pool=get_pool)


async def _audit(owner: dict[str, Any], event: str, *, reason: str = "", payload: Optional[dict] = None,
                 tenant_ids: Optional[list[int]] = None) -> None:
    """One row per tenant it concerns (so each business's trail shows it),
    or one platform row when the owner has none linked."""
    pool = _get_pool()
    ids = owner["tenant_ids"] if tenant_ids is None else tenant_ids
    for tenant_id in ids or [None]:
        await audit.record(pool, tenant_id=tenant_id, actor=owner_auth.actor(owner), event=event,
                           reason=reason, payload={"owner_id": owner["id"], **(payload or {})})


# ---------------------------------------------------------------- the page


@router.get("/owner", include_in_schema=False)
async def owner_page() -> RedirectResponse:
    """The dashboard is static/owner/index.html, served by the static mount
    at /owner/; this makes the address without the slash work too."""
    return RedirectResponse("/owner/", status_code=307)


# ------------------------------------------------------------------- login


class LoginBody(BaseModel):
    username: str = Field("", max_length=200)
    password: str = Field("", max_length=1000)
    code: str = Field("", max_length=20)


@router.post("/api/owner/login")
async def api_login(body: LoginBody, request: Request) -> JSONResponse:
    pool = _get_pool()
    ip = owner_auth.client_ip(request)
    owner_auth.check_rate_limit(ip, body.username)
    try:
        row = await owner_auth.authenticate(pool, body.username, body.password, body.code)
    except owner_auth.LoginFailed:
        owner_auth.note_failure(ip, body.username)
        log.warning("Failed owner login from %s.", ip)
        raise HTTPException(status_code=401, detail=owner_auth.WRONG_CREDENTIALS) from None
    except owner_auth.CodeRequired as exc:
        if exc.wrong:
            owner_auth.note_failure(ip, body.username)
            log.warning("Owner login from %s: wrong or reused authenticator code.", ip)
            raise HTTPException(status_code=401, detail="Wrong or already used code") from None
        # The password was right: ask for the code (not counted as a failure).
        raise HTTPException(status_code=401, detail=owner_auth.CODE_REQUIRED) from None
    owner_auth.clear_failures(ip, body.username)
    # A session already in this browser is ended, not left alive beside the new one.
    previous = owner_auth.token_from(request)
    if previous:
        await owner_auth.delete_session(pool, previous)
    token = await owner_auth.create_session(pool, row["id"], ip)
    await pool.execute("UPDATE owners SET last_login_at = now() WHERE id = $1", row["id"])
    owner = await owner_auth.session_owner(pool, token)
    await _audit(owner, EVENT_LOGIN, reason="dashboard login", payload={"ip": ip})
    response = JSONResponse({"ok": True, "must_change_password": owner["must_change_password"]})
    owner_auth.set_cookie(response, token)
    return response


@router.post("/api/owner/logout")
async def api_logout(request: Request) -> JSONResponse:
    """Works with or without a valid session: it always clears the cookie."""
    token = owner_auth.token_from(request)
    if token:
        await owner_auth.delete_session(_get_pool(), token)
    response = JSONResponse({"ok": True})
    owner_auth.clear_cookie(response)
    return response


class PasswordBody(BaseModel):
    current: str = Field("", max_length=1000)
    new: str = Field("", max_length=1000)


@router.post("/api/owner/password")
async def api_password(body: PasswordBody, request: Request,
                       owner: dict = Depends(owner_auth.any_owner)) -> dict[str, Any]:
    """Also the way out of must_change_password. Ends the owner's other
    sessions; this one stays logged in."""
    pool = _get_pool()
    ip = owner_auth.client_ip(request)
    owner_auth.check_rate_limit(ip, owner["username"])
    stored = await pool.fetchval("SELECT password_hash FROM owners WHERE id = $1", owner["id"])
    if not owner_auth.verify_password(body.current, stored):
        owner_auth.note_failure(ip, owner["username"])
        raise HTTPException(status_code=400, detail="The current password is wrong.")
    try:
        owner_auth.check_new_password(body.new)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if body.new == body.current:
        raise HTTPException(status_code=400, detail="Choose a password different from the current one.")
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            "UPDATE owners SET password_hash = $2, must_change_password = false, updated_at = now() WHERE id = $1",
            owner["id"], owner_auth.hash_password(body.new),
        )
        await owner_auth.kill_sessions(con, owner["id"], keep_token=owner_auth.token_from(request))
    await _audit(owner, EVENT_PASSWORD, reason="changed by the owner")
    return {"ok": True}


# -------------------------------------------------------------------- TOTP


class CodeBody(BaseModel):
    code: str = Field("", max_length=20)


@router.post("/api/owner/totp")
async def api_totp_setup(body: CodeBody, request: Request,
                         owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    """Two steps. Without a code: a new secret and its otpauth:// link, the
    only time the secret is ever shown. With a code from the app: that
    secret is checked and switched on."""
    pool = _get_pool()
    if owner["totp"]:
        raise HTTPException(status_code=409, detail="Two-step login is already on. Turn it off first.")
    if not body.code.strip():
        secret = totp.new_secret()
        _pending_totp[owner["id"]] = (secret, time.monotonic())
        return {"secret": secret,
                "uri": totp.provisioning_uri(secret, account=owner["username"], issuer=TOTP_ISSUER)}
    ip = owner_auth.client_ip(request)
    owner_auth.check_rate_limit(ip, owner["username"])
    pending = _pending_totp.get(owner["id"])
    if pending is None or time.monotonic() - pending[1] > TOTP_SETUP_SECONDS:
        _pending_totp.pop(owner["id"], None)
        raise HTTPException(status_code=400, detail="Start the setup again: the secret expired.")
    secret = pending[0]
    if not owner_auth.use_code(owner["id"], secret, body.code):
        owner_auth.note_failure(ip, owner["username"])
        raise HTTPException(status_code=400, detail="That code doesn't match. Check the app's clock and try again.")
    await pool.execute("UPDATE owners SET totp_secret_enc = $2, updated_at = now() WHERE id = $1",
                       owner["id"], owner_auth.encrypt_totp(owner["id"], secret))
    _pending_totp.pop(owner["id"], None)
    await _audit(owner, EVENT_TOTP, reason="two-step login turned on by the owner", payload={"on": True})
    return {"ok": True, "totp": True}


@router.delete("/api/owner/totp")
async def api_totp_disable(body: CodeBody, request: Request,
                           owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    """Turning it off takes a current code, so a borrowed session alone
    can't remove it. Lost the phone? The admin can remove it."""
    pool = _get_pool()
    blob = await pool.fetchval("SELECT totp_secret_enc FROM owners WHERE id = $1", owner["id"])
    if blob is None:
        raise HTTPException(status_code=409, detail="Two-step login is not on.")
    ip = owner_auth.client_ip(request)
    owner_auth.check_rate_limit(ip, owner["username"])
    if not owner_auth.use_code(owner["id"], owner_auth.decrypt_totp(owner["id"], blob), body.code):
        owner_auth.note_failure(ip, owner["username"])
        raise HTTPException(status_code=400, detail="Wrong or already used code.")
    await pool.execute("UPDATE owners SET totp_secret_enc = NULL, updated_at = now() WHERE id = $1", owner["id"])
    await _audit(owner, EVENT_TOTP, reason="two-step login turned off by the owner", payload={"on": False})
    return {"ok": True, "totp": False}


# ------------------------------------------------------------ their tenants


async def _tenants(owner: dict[str, Any]) -> list[dict[str, Any]]:
    """The owner's businesses with what the dashboard shows about each: its
    timezone (from the effective config), whether the bot may send on its
    own (and why not), and the account's health."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name, t.session_id, t.config_json, i.default_config,
               h.status AS health, h.last_seen_at
          FROM tenants t
          JOIN industries i ON i.id = t.industry_id
          LEFT JOIN sessions_health h ON h.tenant_id = t.id
         WHERE t.id = ANY($1)
         ORDER BY lower(t.name), t.id
        """,
        owner["tenant_ids"],
    )

    def parsed(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else (value or {})

    out = []
    for row in rows:
        try:
            zone = tenant_config.resolve(parsed(row["default_config"]), parsed(row["config_json"])).config.timezone
        except tenant_config.ConfigError:
            zone = "UTC"
        off = await controls.off_reason(pool, row["id"])
        out.append({
            "id": row["id"], "name": row["name"], "session_id": row["session_id"], "timezone": zone,
            "bot": {"sending": not off, "off_reason": off},
            "health": {"status": row["health"] or health.UNKNOWN,
                       "last_seen_at": row["last_seen_at"].isoformat(timespec="seconds")
                       if row["last_seen_at"] else None},
        })
    return out


async def _tenant(owner: dict[str, Any], tenant_id: int) -> dict[str, Any]:
    """One of the owner's businesses; anything else is 404, never 403."""
    if tenant_id not in owner["tenant_ids"]:
        raise HTTPException(status_code=404, detail="Unknown business")
    for tenant in await _tenants(owner):
        if tenant["id"] == tenant_id:
            return tenant
    raise HTTPException(status_code=404, detail="Unknown business")


def _public_tenant(tenant: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in tenant.items() if key != "session_id"}


def _booking(row: dict[str, Any]) -> dict[str, Any]:
    out = booking_store.public(row)
    for key in HIDDEN_BOOKING_FIELDS:
        out.pop(key, None)
    return out


@router.get("/api/owner/me")
async def api_me(owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    return {
        "id": owner["id"], "username": owner["username"], "display_name": owner["display_name"],
        "totp": owner["totp"],
        "tenants": [_public_tenant(t) for t in await _tenants(owner)],
    }


@router.get("/api/owner/dashboard")
async def api_dashboard(tenant_id: int, owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    pool = _get_pool()
    tenant = await _tenant(owner, tenant_id)
    now = datetime.now(timezone.utc)
    today = stats.day_start(now, tenant["timezone"])
    tomorrow = today + timedelta(days=1)
    store = booking_store.BookingStore(pool, tenant_id, tenant["session_id"] or "")
    upcoming_states = list(bs.LIVE) + [bs.COMPLETED, bs.NO_SHOW]
    return {
        "tenant": _public_tenant(tenant),
        "today": today.date().isoformat(),
        "bookings_today": [_booking(b) for b in await store.between(today, tomorrow)],
        "bookings_upcoming": [_booking(b) for b in await store.between(
            tomorrow, tomorrow + timedelta(days=UPCOMING_DAYS), upcoming_states)],
        "summary": await stats.summary(pool, tenant_id, tenant["timezone"], now),
        "weeks": await stats.weeks(pool, tenant_id, tenant["timezone"], now, count=CHART_WEEKS),
        "unanswered_open": await unanswered.open_count(pool, tenant_id),
        # Customers the owner flagged (no-shows, trouble) arrive in phase 5.
        "flagged_customers": [],
    }


@router.get("/api/owner/overview")
async def api_overview(owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    """The headline numbers of every business of theirs, for "All my businesses"."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    out = []
    for tenant in await _tenants(owner):
        summary = await stats.summary(pool, tenant["id"], tenant["timezone"], now)
        out.append({**_public_tenant(tenant), "week": summary["week"], "today": summary["today"],
                    "unanswered_open": summary["unanswered_open"]})
    return {"tenants": out}


# --------------------------------------------------------- unanswered queue


_STATUS_FILTER = {"open": unanswered.OPEN, "reviewed": unanswered.REVIEWED, "all": None}


@router.get("/api/owner/unanswered")
async def api_unanswered(tenant_id: Optional[int] = None, status: str = "open",
                         owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    """Without tenant_id: every business of theirs at once."""
    if status not in _STATUS_FILTER:
        raise HTTPException(status_code=400, detail="status must be open, reviewed or all")
    if tenant_id is None:
        ids = owner["tenant_ids"]
    elif tenant_id in owner["tenant_ids"]:
        ids = [tenant_id]
    else:
        raise HTTPException(status_code=404, detail="Unknown business")
    items = await unanswered.list_items(_get_pool(), ids, status=_STATUS_FILTER[status])
    return {"items": items}


# Postgres ids are int4: a bigger number is refused here (422) instead of
# reaching the query and failing there.
MAX_ID = 2 ** 31 - 1


@router.post("/api/owner/unanswered/{item_id}/reviewed")
async def api_unanswered_reviewed(item_id: int = Path(ge=1, le=MAX_ID),
                                  owner: dict = Depends(owner_auth.current_owner)) -> dict[str, Any]:
    item = await unanswered.set_status(_get_pool(), owner["tenant_ids"], item_id, unanswered.REVIEWED,
                                       by=owner_auth.actor(owner))
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown item")
    await _audit(owner, EVENT_REVIEWED, reason="marked reviewed on the dashboard",
                 payload={"item": item_id}, tenant_ids=[item["tenant_id"]])
    return item
