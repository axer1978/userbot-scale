"""Unified admin panel: one stateless FastAPI app in front of the whole
fleet, routed by `session_id`. Replaces the old one-panel-per-instance
model (plan items 11-13).

Architecture (this is the fix for the original version's design flaw —
see the note below): this process owns **no live `SessionRuntime`s at
all**. Only `manager.py`'s worker processes ever hold a live Telethon
client, via leasing.py's guarantee that exactly one process holds any
given session at a time. The panel is a control plane, not a second data
plane:

- Reads (conversations, messages, config, bookings, media, session list)
  go straight to Postgres (`Database`/`config_store`/`SessionRegistry`) or
  to the session's local files (`bookings.json`, the media library) —
  none of that needs a live client, so none of it goes through a worker.
- Actions that DO need the live Telegram connection (send a message,
  approve a draft, fetch contacts, scan/decide a booking) are dispatched
  as commands over Redis (`commands.py`) to whichever worker currently
  holds that session's lease. If nothing does, `dispatch()` times out and
  the route reports the session isn't running right now — that IS the
  correct behavior, not an error to route around.
- Live updates (a message arrived, a draft is pending) reach an open
  panel tab because the worker's `SessionRuntime.hub.broadcast()` publishes
  to the same Redis channel this file's websocket handler subscribes to
  for that session_id — see `commands.py`'s `Hub`/`publish_event` and this
  file's `websocket_endpoint`. For actions the panel itself performs
  directly against Postgres (pause, reject a draft, edit config, link/
  unlink chats), this file publishes the equivalent event itself, since
  there is no runtime around to do it automatically.

What broke in the version before this rewrite, for the record: this file
used to construct a `SessionRuntime` per active session at startup and
call `.start()`, as a stated stand-in for `manager.py`. Once `manager.py`
existed as its own compose service, both processes tried to lease every
session — one process wins each lease race and the other silently ends up
running nothing, so depending on start order the panel could show every
session as unreachable. Making the panel own zero runtimes removes the
conflict at the root instead of arbitrating it.

Auth (item 14): a single shared admin password (`ADMIN_PASSWORD` env
var), checked via a signed-random token in an httponly cookie.
Deliberately simple for one operator. Because the panel can now be put on
the public internet (the optional `caddy` service in docker-compose.yml),
failed logins are rate-limited per client IP and tokens expire after
`SESSION_TTL_SECONDS` — see the Auth section below.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers as StarletteHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

import audit
import booking_api
import booking_store
import commands
import config_store
import context_link
import controls
import media
import pg
import platform_api
import owner_admin_api
import owner_api
import review_api
import safety_api
import tenants
import unanswered_api
import totp
from database import (
    OUT_CANCELLED,
    OUT_SENT,
    STATUS_PENDING,
    STATUS_REJECTED,
    Database,
    SessionRegistry,
)
from login_flow import LoginError, LoginFlow

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
# Optional second factor: a base32 TOTP secret (generate with `python totp.py`
# on the server). When set, logging in takes the password AND the current
# 6-digit code from an authenticator app.
ADMIN_TOTP_SECRET = "".join((os.getenv("ADMIN_TOTP_SECRET") or "").split())
HOST = (os.getenv("ADMIN_HOST") or "127.0.0.1").strip()
# Set when the panel is on the public internet behind Caddy (the `public`
# profile). The panel then refuses to start without an authenticator code
# for the admin and a long admin password (check_public_setup).
PANEL_DOMAIN = (os.getenv("PANEL_DOMAIN") or "").strip()
PUBLIC_MIN_PASSWORD = 14
PORT = int(os.getenv("ADMIN_PORT") or 8787)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# Commands that touch the live client can legitimately take a while (a
# typing-simulation delay alone can run up to typing_max_seconds, default
# 25s, on top of the network round trip) — give those more room than the
# bus's generic default. "Best effort" actions below are dispatched with a
# short timeout and their CommandTimeout is swallowed on purpose: the panel
# already made the change that matters (a Postgres write), notifying a
# live worker about it is a nice-to-have, not something worth failing the
# whole request over.
LIVE_ACTION_TIMEOUT = 60.0
QUICK_ACTION_TIMEOUT = 20.0
BEST_EFFORT_TIMEOUT = 5.0

# Login hardening. At most LOGIN_MAX_FAILURES wrong passwords per client IP
# in any LOGIN_FAILURE_WINDOW_SECONDS (a sliding window); after that the IP
# gets a 429 — even with the right password — until its oldest failure ages
# out. A successful login clears the IP's count. A login token is good for
# SESSION_TTL_SECONDS, then the operator logs in again.
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 12 * 60 * 60
# Past this many addresses with failures on record, expired ones are swept.
MAX_TRACKED_IPS = 10_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("panel")

# No /docs, /redoc or /openapi.json: a public panel has no reason to hand
# anyone a map of its API.
app = FastAPI(title="Telegram AI Assistant — Fleet Admin", docs_url=None, redoc_url=None, openapi_url=None)

# Sent on every response, whether it comes through Caddy or an SSH tunnel.
# script-src 'self': no inline scripts anywhere in the panel or the client
# dashboard. Inline style attributes are used by the panel's markup, hence
# style-src 'unsafe-inline'. HSTS is Caddy's job (only it knows about TLS).
# connect-src is completed per request (_csp): 'self' plus ws(s):// to this
# same host only, for the live-updates socket, never to any other host.
_CSP_BASE = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'{ws}; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP_BASE.format(ws=""),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
# What a Host header may look like to be echoed into the CSP (a name or an
# address, optionally a port); anything else just gets connect-src 'self'.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")

# Request bodies. Everything the panel accepts as JSON is small; only the
# media upload streams a file. Anything bigger is refused with 413 before
# it is buffered, so an anonymous POST to /api/login can't eat the memory.
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_UPLOAD_PATH_RE = re.compile(r"^/api/sessions/[^/]+/media/upload$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The body types a page on another site can send without a CORS preflight
# (an HTML form or a "simple" fetch). No API route takes any of them.
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data", "text/plain")


def _csp(request: Request) -> str:
    host = request.headers.get("host", "")
    if not _HOST_RE.match(host):
        return SECURITY_HEADERS["Content-Security-Policy"]
    return _CSP_BASE.format(ws=f" wss://{host} ws://{host}")


def _same_origin(headers: Any) -> bool:
    """False when the browser says the request was started by another
    origin. Checked on every state-changing request and the websocket.

    SameSite=Strict cookies are not enough on their own: "site" means the
    registrable domain, and a panel on <ip>.sslip.io shares it with every
    other *.sslip.io host on the internet (sslip.io is not on the Public
    Suffix List), so a page there is "same-site" and would get the cookie
    sent along. Sec-Fetch-Site and Origin are set by the browser and can't
    be forged by a page; a request with neither (curl, tests) comes from no
    browser page at all and is left to the cookie check."""
    fetch_site = (headers.get("sec-fetch-site") or "").lower()
    if fetch_site in ("cross-site", "same-site"):
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    parsed = urlsplit(origin)
    host = (headers.get("host") or "").lower()
    return bool(parsed.scheme in ("http", "https", "ws", "wss") and parsed.netloc
                and parsed.netloc.lower() == host)


class _BodyTooLarge(StarletteHTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="The request is too large.")


class RequestGuard:
    """Pure ASGI (so it sees the websocket handshake and the raw body):
    refuses cross-origin state-changing requests and websockets, form-type
    bodies on the API, and bodies over the size limit."""

    def __init__(self, app_: Any) -> None:
        self.app = app_

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = StarletteHeaders(scope=scope)
        if scope["type"] == "websocket":
            if not _same_origin(headers):
                await send({"type": "websocket.close", "code": 4403})
                return
            await self.app(scope, receive, send)
            return

        path, method = scope["path"], scope["method"]
        if method in _UNSAFE_METHODS:
            if not _same_origin(headers):
                await self._refuse(scope, receive, send, 403, "Cross-origin request refused.")
                return
            content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
            if path.startswith("/api/") and content_type in _FORM_TYPES:
                await self._refuse(scope, receive, send, 415, "Send JSON (Content-Type: application/json).")
                return
        limit = MAX_UPLOAD_BYTES if _UPLOAD_PATH_RE.match(path) else MAX_BODY_BYTES
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            await self._refuse(scope, receive, send, 413, "The request is too large.")
            return
        received = 0

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside the route's body read, so FastAPI answers 413.
                    raise _BodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _refuse(scope: dict, receive: Any, send: Any, status: int, detail: str) -> None:
        response = JSONResponse({"detail": detail}, status_code=status,
                                headers={"Cache-Control": "no-store", **SECURITY_HEADERS})
        await response(scope, receive, send)


app.add_middleware(RequestGuard)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _csp(request))
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/api/"):
        # Nothing the API returns may be cached by a browser or a proxy.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def check_public_setup() -> list[str]:
    """What must be fixed before the panel may face the internet. Empty
    when PANEL_DOMAIN is unset (SSH-tunnel only) or everything is in place."""
    if not PANEL_DOMAIN:
        return []
    problems = []
    if not ADMIN_TOTP_SECRET:
        problems.append("ADMIN_TOTP_SECRET is not set: a public panel needs an authenticator code for the admin "
                        "(generate one with `python totp.py` on the server)")
    if len(ADMIN_PASSWORD) < PUBLIC_MIN_PASSWORD:
        problems.append(f"ADMIN_PASSWORD is shorter than {PUBLIC_MIN_PASSWORD} characters")
    return problems

pool = None  # set in startup
registry: Optional[SessionRegistry] = None
bus: Optional[commands.CommandBus] = None
# Both in memory on purpose: the panel is a single process, and a restart
# logging everyone out / forgetting failed attempts is harmless.
_valid_tokens: dict[str, float] = {}  # token -> expiry, on the _now() clock
_login_failures: dict[str, list[float]] = {}  # client IP -> times of recent failed logins
# Highest TOTP time step already used to log in: a code works once, so one
# seen over someone's shoulder (or replayed) is useless even within its 30 s.
_last_totp_step = -1


def db_for(session_id: str) -> Database:
    """A `Database` facade is just (pool, session_id) — cheap to construct
    per request, no connection of its own to hold open. Its `.connect()` is
    for seeding id counters, which this file never touches, so it's
    correctly skipped."""
    return Database(pool, session_id)


async def tenant_dir(session_id: str) -> Path:
    """The tenant's folder for this account (tenants.tenant_data_dir)."""
    tenant = await tenants.TenantStore(pool).by_session(session_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return tenants.tenant_data_dir(DATA_DIR, tenant["id"], session_id)


async def booking_store_for(session_id: str) -> booking_store.BookingStore:
    tenant = await tenants.TenantStore(pool).by_session(session_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return booking_store.BookingStore(pool, tenant["id"], session_id)


async def media_library_for(session_id: str) -> media.MediaLibrary:
    return media.MediaLibrary(await tenant_dir(session_id) / "media")


async def best_effort_dispatch(session_id: str, action: str, args: Optional[dict[str, Any]] = None) -> None:
    """Nudge a live worker if one happens to be running this session; do
    nothing (not even a log line above debug) if none is — that is the
    normal, expected state for a paused or not-yet-logged-in session."""
    try:
        await bus.dispatch(session_id, action, args or {}, timeout=BEST_EFFORT_TIMEOUT)
    except commands.CommandTimeout:
        pass
    except Exception:
        log.debug("[%s] best-effort dispatch %r failed", session_id, action, exc_info=True)


async def publish(session_id: str, payload: dict[str, Any]) -> None:
    """This file's equivalent of the old in-process `hub.broadcast()`, for
    the routes here that write straight to Postgres/files with no worker
    involved — there is no SessionRuntime around to publish this for us."""
    await bus.publish_event(session_id, payload)


# ---------------------------------------------------------------------------
# Auth (item 14) — single shared password, random bearer token in a cookie
# ---------------------------------------------------------------------------


class LoginBody(BaseModel):
    password: str = ""
    code: str = ""  # authenticator code; only checked when ADMIN_TOTP_SECRET is set


def _now() -> float:
    """The clock tokens and failed-login windows are measured on. Monotonic,
    so a wall-clock change can't extend or cut short either; a function so
    tests can move it without touching `time.monotonic` itself (which the
    event loop also uses)."""
    return time.monotonic()


def _token_is_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    expires = _valid_tokens.get(token)
    if expires is None:
        return False
    if expires <= _now():
        _valid_tokens.pop(token, None)
        return False
    return True


def _client_ip(request: Request) -> str:
    """The real client's IP. Behind Caddy that comes from X-Forwarded-For
    via uvicorn's proxy_headers (see the uvicorn.run call at the bottom);
    `request.client` would otherwise be Caddy's container address, and
    every visitor would share one rate-limit bucket."""
    return request.client.host if request.client else "unknown"


def _recent_failures(ip: str, now: float) -> list[float]:
    """This IP's failed logins still inside the window, oldest first.
    Forgets the IP entirely once none are left."""
    cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
    recent = [t for t in _login_failures.get(ip, ()) if t > cutoff]
    if recent:
        _login_failures[ip] = recent
    else:
        _login_failures.pop(ip, None)
    return recent


def _cookie_secure() -> bool:
    """Secure unless the panel listens on loopback only (plain http through
    an SSH tunnel). In Docker ADMIN_HOST is 0.0.0.0, so always Secure there."""
    return HOST not in LOOPBACK


def admin_cookie_name() -> str:
    """`__Host-admin_token` whenever the cookie is Secure. The __Host- prefix
    makes the browser refuse that name from anything but this exact host,
    with no Domain attribute: another *.sslip.io site (or any sibling
    subdomain) can't plant or overwrite it ("cookie tossing"), which could
    otherwise lock the admin out or swap sessions. Plain http on loopback
    can't use the prefix (it requires Secure), hence the plain name there."""
    return "__Host-admin_token" if _cookie_secure() else "admin_token"


def admin_token_from(cookies: Any) -> Optional[str]:
    return cookies.get(admin_cookie_name())


def require_auth(request: Request) -> None:
    if not _token_is_valid(admin_token_from(request.cookies)):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _credentials_ok(body: LoginBody) -> bool:
    """Password (and, when enabled, a fresh authenticator code). Both are
    always checked, so the response time doesn't say which one was wrong."""
    global _last_totp_step
    # Bytes, not str: compare_digest rejects non-ASCII str with a TypeError.
    password_ok = secrets.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    if not ADMIN_TOTP_SECRET:
        return password_ok
    step = totp.matching_counter(ADMIN_TOTP_SECRET, body.code)
    code_ok = step is not None and step > _last_totp_step
    if password_ok and code_ok:
        _last_totp_step = step
        return True
    return False


@app.get("/api/login-options")
async def api_login_options() -> dict[str, Any]:
    """Tells the sign-in screen whether to ask for an authenticator code."""
    return {"totp": bool(ADMIN_TOTP_SECRET)}


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request) -> JSONResponse:
    ip = _client_ip(request)
    now = _now()
    failures = _recent_failures(ip, now)
    if len(failures) >= LOGIN_MAX_FAILURES:
        retry_after = int(failures[0] + LOGIN_FAILURE_WINDOW_SECONDS - now) + 1
        log.warning("Login from %s refused: too many recent wrong passwords.", ip)
        raise HTTPException(
            status_code=429,
            detail=f"Too many wrong passwords from your address. "
                   f"Try again in {(retry_after + 59) // 60} minute(s).",
            headers={"Retry-After": str(retry_after)},
        )
    if not _credentials_ok(body):
        if len(_login_failures) >= MAX_TRACKED_IPS:
            # Many addresses (an IPv6 range, say): drop the ones whose window is over.
            for other in list(_login_failures):
                _recent_failures(other, now)
        failures.append(now)
        _login_failures[ip] = failures
        log.warning("Failed admin login from %s (%d/%d).", ip, len(failures), LOGIN_MAX_FAILURES)
        raise HTTPException(
            status_code=401,
            detail="Wrong password or code" if ADMIN_TOTP_SECRET else "Wrong password",
        )
    _login_failures.pop(ip, None)

    # Drop tokens that expired without ever being presented again, so the
    # dict doesn't grow by one entry per login forever.
    for stale in [t for t, expires in _valid_tokens.items() if expires <= now]:
        del _valid_tokens[stale]
    token = secrets.token_urlsafe(32)
    _valid_tokens[token] = now + SESSION_TTL_SECONDS
    response = JSONResponse({"ok": True})
    # A token already in the browser is retired, not left valid beside the new one.
    previous = admin_token_from(request.cookies)
    if previous:
        _valid_tokens.pop(previous, None)
    response.set_cookie(
        # strict: the cookie never rides along on a request another site
        # starts, not even a top-level link into the panel.
        admin_cookie_name(), token, httponly=True, samesite="strict",
        secure=_cookie_secure(), path="/",
    )
    return response


@app.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    admin_token = admin_token_from(request.cookies)
    if admin_token:
        _valid_tokens.pop(admin_token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(admin_cookie_name(), path="/", httponly=True, samesite="strict",
                           secure=_cookie_secure())
    return response


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------


def _lease_is_live(row: dict[str, Any]) -> bool:
    if not row.get("lease_worker_id") or not row.get("lease_expires_at"):
        return False
    expires = datetime.fromisoformat(row["lease_expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def api_sessions() -> list[dict[str, Any]]:
    rows = await registry.list()
    out = []
    for row in rows:
        live = _lease_is_live(row)
        out.append({
            **row,
            # Field names kept from the earlier version for the frontend's
            # sake: "running_here" now means "running somewhere in the
            # fleet" (this process holds no runtimes to be "here" about),
            # and status is derived from the registry row's own state
            # rather than an in-process object.
            "running_here": live,
            "status": {
                "telegram_connected": live and row.get("state") == "running",
                "state": row.get("state"),
                "state_reason": row.get("state_reason"),
            } if live else None,
        })
    return out


# ---------------------------------------------------------------------------
# Adding a Telegram account — login_flow.LoginFlow behind the panel's
# sign-in screen. One flow per panel process (one operator). A completed
# login is saved, given its DeepSeek key and marked active; manager.py's
# workers pick up active, unleased sessions on their own, so nothing here
# starts a runtime.
# ---------------------------------------------------------------------------


class AuthStartBody(BaseModel):
    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    deepseek_api_key: str = ""
    label: str = ""
    # Optional: socks5://user:pass@host:port (proxies.py). The sign-in and
    # the account then both go through it.
    proxy_url: str = ""


class AuthCodeBody(BaseModel):
    code: str = ""


class AuthPasswordBody(BaseModel):
    password: str = ""


login_flow: Optional[LoginFlow] = None
# Held until the login completes, so an abandoned sign-in never stores a key.
_pending_deepseek_key = ""


def session_id_for_phone(phone: str) -> str:
    """One row per phone number: signing the same number in again updates
    that session instead of creating a second one for the same account."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        raise HTTPException(status_code=400, detail="Phone number is required.")
    return f"tg{digits}"


def auth_state(signed_in: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    state = login_flow.state()
    if signed_in is not None:
        state["step"] = "done"
        state["session_id"] = signed_in["session_id"]
    return state


async def finish_login(row: dict[str, Any], me: Any) -> dict[str, Any]:
    global _pending_deepseek_key
    session_id = row["session_id"]
    if _pending_deepseek_key:
        await registry.set_deepseek_key(session_id, _pending_deepseek_key)
    _pending_deepseek_key = ""
    await registry.set_active(session_id, True)
    log.info(
        "[%s] Signed in as user %s; marked active for the manager to pick up.",
        session_id, getattr(me, "id", "?"),
    )
    return auth_state(signed_in=row)


@app.get("/api/auth", dependencies=[Depends(require_auth)])
async def api_auth() -> dict[str, Any]:
    return auth_state()


@app.post("/api/auth/start", dependencies=[Depends(require_auth)])
async def api_auth_start(body: AuthStartBody) -> dict[str, Any]:
    global _pending_deepseek_key
    api_id_raw = body.api_id.strip()
    api_hash = body.api_hash.strip()
    phone = body.phone.strip()
    deepseek_key = body.deepseek_api_key.strip()

    if not api_id_raw.isdigit():
        raise HTTPException(
            status_code=400,
            detail="API ID must be the number from my.telegram.org (7-8 digits).",
        )
    if not api_hash:
        raise HTTPException(status_code=400, detail="API hash is required.")
    session_id = session_id_for_phone(phone)

    existing = await registry.get(session_id)
    if existing is not None and _lease_is_live(existing):
        raise HTTPException(
            status_code=409,
            detail=f"{phone} is already signed in and running ({session_id}). "
                   "Stop it before signing it in again.",
        )
    if not deepseek_key and not (existing and await registry.load_deepseek_key(session_id)):
        raise HTTPException(
            status_code=400,
            detail="DeepSeek API key is required (from platform.deepseek.com).",
        )

    try:
        await login_flow.start(
            session_id, int(api_id_raw), api_hash, phone, label=body.label.strip() or phone,
            proxy_url=body.proxy_url.strip(),
        )
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _pending_deepseek_key = deepseek_key
    return auth_state()


@app.post("/api/auth/code", dependencies=[Depends(require_auth)])
async def api_auth_code(body: AuthCodeBody) -> dict[str, Any]:
    try:
        result = await login_flow.submit_code(body.code)
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:  # two-step verification password next
        return auth_state()
    return await finish_login(*result)


@app.post("/api/auth/password", dependencies=[Depends(require_auth)])
async def api_auth_password(body: AuthPasswordBody) -> dict[str, Any]:
    try:
        result = await login_flow.submit_password(body.password)
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await finish_login(*result)


@app.post("/api/auth/resend", dependencies=[Depends(require_auth)])
async def api_auth_resend() -> dict[str, Any]:
    try:
        await login_flow.resend()
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return auth_state()


@app.post("/api/auth/cancel", dependencies=[Depends(require_auth)])
async def api_auth_cancel() -> dict[str, Any]:
    global _pending_deepseek_key
    _pending_deepseek_key = ""
    await login_flow.reset()
    return auth_state()


# ---------------------------------------------------------------------------
# Per-session routes
# ---------------------------------------------------------------------------


class SendBody(BaseModel):
    text: str = Field(min_length=1)


class PauseBody(BaseModel):
    paused: bool


class GlobalPauseBody(BaseModel):
    global_pause: bool


class LinkBody(BaseModel):
    source_id: int


class ApproveBody(BaseModel):
    text: Optional[str] = None


class OutreachBody(BaseModel):
    chat_ids: list[int] = Field(default_factory=list)
    goal: str = ""


class MediaDescribeBody(BaseModel):
    description: str = ""


class SendMediaBody(BaseModel):
    media_id: int


@app.get("/api/sessions/{session_id}/status", dependencies=[Depends(require_auth)])
async def api_status(session_id: str) -> dict[str, Any]:
    row = await registry.get(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    live = _lease_is_live(row)
    return {
        "session_id": session_id,
        "telegram_connected": live and row.get("state") == "running",
        "telegram_error": row.get("state_reason") if row.get("state") == "error" else None,
        "state": row.get("state"),
        **(await _controls_status(session_id)),
        **(await _tenant_status(session_id)),
    }


async def _controls_status(session_id: str) -> dict[str, Any]:
    """The kill switches for the top bar (controls.py). `global_pause` is
    the manual hold, i.e. the "Pause all" button."""
    tenant = await tenants.TenantStore(pool).by_session(session_id)
    if tenant is None:
        return {"global_pause": False, "off_reason": "", "holds": []}
    holds = await controls.holds(pool, tenant["id"])
    return {
        "global_pause": any(h["kind"] == controls.MANUAL for h in holds),
        "off_reason": await controls.off_reason(pool, tenant["id"]),
        "holds": holds,
        "billing_status": tenant["status"],
    }


async def _tenant_status(session_id: str) -> dict[str, Any]:
    """What the top bar shows about the tenant: its id, auto-send, quiet
    hours, and whether the business sections of the prompt say anything."""
    try:
        bundle = await tenants.TenantStore(pool).bundle_for_session(session_id)
    except Exception as exc:  # a broken config must not break the status line
        log.warning("[%s] Could not load tenant for status: %s", session_id, exc)
        return {"tenant_id": None}
    cfg = bundle.config
    return {
        "tenant_id": bundle.tenant["id"],
        "tenant_name": bundle.tenant["name"],
        "auto_send": cfg["auto_send"],
        "quiet_hours": cfg["quiet_hours"],
        "timezone": cfg["timezone"],
        "persona_configured": bool(bundle.prompt.business_text.strip()),
    }


@app.get("/api/sessions/{session_id}/config", dependencies=[Depends(require_auth)])
async def api_get_config(session_id: str) -> dict[str, Any]:
    return await config_store.load(pool, session_id)


@app.put("/api/sessions/{session_id}/config", dependencies=[Depends(require_auth)])
async def api_put_config(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """This account's own settings (session_config): the per-contact style
    overrides from the Style sheet. How the bot behaves is the tenant's
    config, edited under Clients (platform_api.py)."""
    stored = await config_store.load(pool, session_id)
    # The device identity is assigned by the runtime and the pause switch has
    # its own route; neither is edited here, so a save can't blank them.
    payload = {**payload, "identity": stored["identity"],
               "behavior": {**stored["behavior"], "global_pause": stored["behavior"]["global_pause"]}}
    try:
        new_config = await config_store.save(pool, session_id, payload)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not save config: {exc}") from exc

    await best_effort_dispatch(session_id, "reload_config")
    await publish(session_id, {"type": "config", "config": new_config})
    log.info("[%s] Account settings updated from the admin panel.", session_id)
    return new_config


@app.get("/api/sessions/{session_id}/conversations", dependencies=[Depends(require_auth)])
async def api_conversations(session_id: str) -> list[dict[str, Any]]:
    return await db_for(session_id).list_conversations()


@app.get(
    "/api/sessions/{session_id}/conversations/{chat_id}/messages",
    dependencies=[Depends(require_auth)],
)
async def api_messages(session_id: str, chat_id: int) -> dict[str, Any]:
    db = db_for(session_id)
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    return {
        "conversation": conversation,
        "messages": await db.get_messages(chat_id),
        "links": await db.get_links(chat_id),
    }


@app.post(
    "/api/sessions/{session_id}/conversations/{chat_id}/read",
    dependencies=[Depends(require_auth)],
)
async def api_mark_read(session_id: str, chat_id: int) -> dict[str, Any]:
    conversation = await db_for(session_id).mark_read(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    await publish(session_id, {"type": "conversation", "conversation": conversation})
    return conversation


@app.get(
    "/api/sessions/{session_id}/conversations/{chat_id}/links",
    dependencies=[Depends(require_auth)],
)
async def api_links(session_id: str, chat_id: int) -> dict[str, Any]:
    db = db_for(session_id)
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    links = await db.get_links(chat_id)
    linked = {link["source_id"] for link in links}
    suggestions = [
        {
            "chat_id": other["chat_id"],
            "display_name": other["display_name"],
            "username": other["username"],
            "confidence": score,
            "reason": reason,
        }
        for other, score, reason in await context_link.find_candidates(
            db, conversation, min_score=context_link.SINGLE_NAME_SCORE
        )
        if other["chat_id"] not in linked
    ]
    return {"links": links, "suggestions": suggestions}


@app.post(
    "/api/sessions/{session_id}/conversations/{chat_id}/links",
    dependencies=[Depends(require_auth)],
)
async def api_link(session_id: str, chat_id: int, body: LinkBody) -> dict[str, Any]:
    db = db_for(session_id)
    if body.source_id == chat_id:
        raise HTTPException(status_code=400, detail="A chat cannot be linked to itself.")
    for wanted in (chat_id, body.source_id):
        if await db.get_conversation(wanted) is None:
            raise HTTPException(status_code=404, detail="Unknown conversation")
    for link in await context_link.link_by_hand(db, chat_id, body.source_id):
        await publish(session_id, {"type": "chat_link", "link": link})
    log.info("[%s] Chat %s linked to chat %s by hand.", session_id, chat_id, body.source_id)
    return {"links": await db.get_links(chat_id)}


@app.delete(
    "/api/sessions/{session_id}/conversations/{chat_id}/links/{source_id}",
    dependencies=[Depends(require_auth)],
)
async def api_unlink(session_id: str, chat_id: int, source_id: int) -> dict[str, Any]:
    db = db_for(session_id)
    if not await db.unlink_chats(chat_id, source_id):
        raise HTTPException(status_code=404, detail="These chats are not linked")
    await publish(session_id, {"type": "chat_unlink", "chat_id": chat_id, "source_id": source_id})
    log.info("[%s] Chat %s unlinked from chat %s.", session_id, chat_id, source_id)
    return {"links": await db.get_links(chat_id)}


@app.post(
    "/api/sessions/{session_id}/conversations/{chat_id}/pause",
    dependencies=[Depends(require_auth)],
)
async def api_pause(session_id: str, chat_id: int, body: PauseBody) -> dict[str, Any]:
    conversation = await db_for(session_id).set_paused(chat_id, body.paused)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    if body.paused:
        await best_effort_dispatch(session_id, "cancel_draft", {"chat_id": chat_id})
    await publish(session_id, {"type": "conversation", "conversation": conversation})
    return conversation


@app.post("/api/sessions/{session_id}/global-pause", dependencies=[Depends(require_auth)])
async def api_global_pause(session_id: str, body: GlobalPauseBody) -> dict[str, Any]:
    """"Pause all": the tenant's manual soft-off hold (controls.py). It
    lifts only the manual hold; a billing, anomaly or other hold stays."""
    tenant = await tenants.TenantStore(pool).by_session(session_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    if body.global_pause:
        await controls.add_hold(pool, tenant["id"], controls.MANUAL, "Pause all, from the panel", actor=audit.ADMIN)
        log.warning("[%s] Automation PAUSED from the admin panel.", session_id)
    else:
        await controls.remove_hold(pool, tenant["id"], controls.MANUAL, actor=audit.ADMIN,
                                   reason="Resumed from the panel")
        log.info("[%s] Automation resumed from the admin panel.", session_id)
    await best_effort_dispatch(session_id, "reload_controls")
    state = await _controls_status(session_id)
    await publish(session_id, {"type": "controls", **state})
    return state


class TakeoverBody(BaseModel):
    active: bool


@app.post(
    "/api/sessions/{session_id}/conversations/{chat_id}/takeover",
    dependencies=[Depends(require_auth)],
)
async def api_takeover(session_id: str, chat_id: int, body: TakeoverBody) -> dict[str, Any]:
    """Hand a chat back to the bot before takeover_hours are over. (A
    takeover starts by itself when someone writes in the chat by hand.)"""
    if body.active:
        raise HTTPException(status_code=400, detail="A takeover starts by writing in the chat.")
    db = db_for(session_id)
    before = await db.get_conversation(chat_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    conversation = await db.set_takeover(chat_id, None)
    if before.get("human_takeover_until"):
        await audit.record(pool, tenant_id=await db.tenant_id(), actor=audit.ADMIN, event=audit.TAKEOVER_ENDED,
                           reason="handed back to the bot from the panel", payload={"chat_id": chat_id})
    await publish(session_id, {"type": "conversation", "conversation": conversation})
    return conversation


@app.post("/api/sessions/{session_id}/conversations/{chat_id}/send", dependencies=[Depends(require_auth)])
async def api_send(session_id: str, chat_id: int, body: SendBody) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    if await db_for(session_id).get_conversation(chat_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")

    await best_effort_dispatch(session_id, "cancel_draft", {"chat_id": chat_id})
    return await _dispatch_live(session_id, "send", {"chat_id": chat_id, "text": text})


@app.post("/api/sessions/{session_id}/drafts/{draft_id}/approve", dependencies=[Depends(require_auth)])
async def api_approve(session_id: str, draft_id: int, body: ApproveBody) -> dict[str, Any]:
    sent = await _dispatch_live(
        session_id, "approve_draft", {"draft_id": draft_id, "text": body.text},
        not_found_detail="Unknown draft",
    )
    return sent


@app.post("/api/sessions/{session_id}/drafts/{draft_id}/reject", dependencies=[Depends(require_auth)])
async def api_reject(session_id: str, draft_id: int) -> dict[str, Any]:
    db = db_for(session_id)
    draft = await db.get_message(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Unknown draft")
    row = await db.update_message(draft_id, status=STATUS_REJECTED)
    if row is not None:
        await publish(session_id, {"type": "message", "message": row, "conversation": await db.get_conversation(draft["chat_id"])})
    item = await db.outreach_for_draft(draft_id)
    if item is not None and item["status"] == "drafted":
        await db.update_outreach(item["id"], status=OUT_CANCELLED)
        await publish(session_id, {"type": "outreach", "items": await db.list_outreach()})
    return row or {}


@app.get("/api/sessions/{session_id}/contacts", dependencies=[Depends(require_auth)])
async def api_contacts(session_id: str) -> list[dict[str, Any]]:
    return await _dispatch_live(session_id, "list_contacts", {}, timeout=LIVE_ACTION_TIMEOUT)


@app.get("/api/sessions/{session_id}/outreach", dependencies=[Depends(require_auth)])
async def api_outreach_list(session_id: str) -> list[dict[str, Any]]:
    return await db_for(session_id).list_outreach()


@app.post("/api/sessions/{session_id}/outreach", dependencies=[Depends(require_auth)])
async def api_outreach_queue(session_id: str, body: OutreachBody) -> dict[str, Any]:
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Say what the message should achieve")
    if not body.chat_ids:
        raise HTTPException(status_code=400, detail="Pick at least one contact")
    bundle = await tenants.TenantStore(pool).bundle_for_session(session_id)
    if not bundle.config["outreach"]["enabled"]:
        raise HTTPException(
            status_code=400,
            detail="Outreach is off for this client. Turn on outreach.enabled in its settings first.",
        )

    try:
        contacts = await _dispatch_live(session_id, "list_contacts", {}, timeout=LIVE_ACTION_TIMEOUT)
    except HTTPException:
        raise
    allowed = {c["chat_id"]: c["display_name"] for c in contacts}

    unknown = [cid for cid in body.chat_ids if cid not in allowed]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"{len(unknown)} of those are not in your Telegram contacts. "
                   "Outreach only goes to people you already have as contacts.",
        )

    recipients = [(cid, allowed[cid]) for cid in body.chat_ids]
    db = db_for(session_id)
    queued = await db.queue_outreach(recipients, goal)
    await best_effort_dispatch(session_id, "ensure_outreach_worker")
    await publish(session_id, {"type": "outreach", "items": await db.list_outreach()})
    return {"queued": len(queued), "skipped": len(recipients) - len(queued)}


@app.post("/api/sessions/{session_id}/outreach/cancel", dependencies=[Depends(require_auth)])
async def api_outreach_cancel(session_id: str) -> dict[str, Any]:
    db = db_for(session_id)
    cancelled = await db.cancel_queued_outreach()
    await best_effort_dispatch(session_id, "cancel_outreach")
    await publish(session_id, {"type": "outreach", "items": await db.list_outreach()})
    return {"cancelled": cancelled}


# ---------------------------------------------------------------------------
# Media library — still local files per session, same as bookings; no
# live client involved except actually sending one into a chat.
# ---------------------------------------------------------------------------


@app.get("/api/sessions/{session_id}/media", dependencies=[Depends(require_auth)])
async def api_media_list(session_id: str) -> list[dict[str, Any]]:
    library = await media_library_for(session_id)
    library.refresh()
    return library.all()


@app.put("/api/sessions/{session_id}/media/upload", dependencies=[Depends(require_auth)])
async def api_media_upload(session_id: str, request: Request, name: str, description: str = "") -> dict[str, Any]:
    library = await media_library_for(session_id)
    # Checked on the name the file will actually get (folders and odd
    # characters stripped), not on the raw one: "x.png/" or "..png" would
    # pass on the raw name and then be stored with no usable extension.
    if media.kind_for(media.safe_filename(name)) is None:
        raise HTTPException(
            status_code=400,
            detail="Only photos (jpg, png, webp, gif) and videos (mp4, mov, mkv, webm) are accepted.",
        )
    filename = library.unique_name(name)
    target = library.dir / filename
    library.dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + filename + ".part")
    written = 0
    try:
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                written += len(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="The file is empty.")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    item = library.add_file(filename, description)
    log.info("[%s] Media added: %s (%s bytes).", session_id, media.label(item), written)
    await publish(session_id, {"type": "media", "media": library.all()})
    return item


@app.patch("/api/sessions/{session_id}/media/{item_id}", dependencies=[Depends(require_auth)])
async def api_media_describe(session_id: str, item_id: int, body: MediaDescribeBody) -> dict[str, Any]:
    library = await media_library_for(session_id)
    item = library.describe(item_id, body.description)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    await publish(session_id, {"type": "media", "media": library.all()})
    return item


class MediaRoleBody(BaseModel):
    role: Optional[str] = Field(None, pattern="^arrival_reference$")


@app.patch("/api/sessions/{session_id}/media/{item_id}/role", dependencies=[Depends(require_auth)])
async def api_media_role(session_id: str, item_id: int, body: MediaRoleBody) -> dict[str, Any]:
    """Mark a photo as the entrance reference for the arrival photo check."""
    library = await media_library_for(session_id)
    try:
        item = library.set_role(item_id, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    await publish(session_id, {"type": "media", "media": library.all()})
    return item


@app.delete("/api/sessions/{session_id}/media/{item_id}", dependencies=[Depends(require_auth)])
async def api_media_delete(session_id: str, item_id: int) -> dict[str, Any]:
    library = await media_library_for(session_id)
    if not library.remove(item_id):
        raise HTTPException(status_code=404, detail="Unknown media item")
    await publish(session_id, {"type": "media", "media": library.all()})
    return {"ok": True}


@app.get("/api/sessions/{session_id}/media/{item_id}/file", dependencies=[Depends(require_auth)])
async def api_media_file(session_id: str, item_id: int) -> FileResponse:
    path = (await media_library_for(session_id)).path(item_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    return FileResponse(str(path))


@app.post(
    "/api/sessions/{session_id}/conversations/{chat_id}/send-media",
    dependencies=[Depends(require_auth)],
)
async def api_send_media(session_id: str, chat_id: int, body: SendMediaBody) -> dict[str, Any]:
    if await db_for(session_id).get_conversation(chat_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    if (await media_library_for(session_id)).get(body.media_id) is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    await best_effort_dispatch(session_id, "cancel_draft", {"chat_id": chat_id})
    return await _dispatch_live(session_id, "send_media", {"chat_id": chat_id, "media_id": body.media_id})


# ---------------------------------------------------------------------------
# Shared helper for routes that need the live client
# ---------------------------------------------------------------------------


async def _dispatch_live(
    session_id: str, action: str, args: dict[str, Any],
    *, timeout: float = LIVE_ACTION_TIMEOUT, not_found_detail: Optional[str] = None,
) -> Any:
    try:
        return await bus.dispatch(session_id, action, args, timeout=timeout)
    except commands.CommandTimeout as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Session {session_id!r} is not running anywhere right now, so this needs "
                   "its live Telegram connection and can't be done: " + str(exc),
        ) from exc
    except commands.CommandError as exc:
        detail = str(exc)
        if not_found_detail and "Unknown" in detail:
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=502, detail=detail) from exc


# ---------------------------------------------------------------------------
# WebSocket — one connection per session_id. Live updates arrive by
# subscribing to the same Redis channel the owning worker publishes to;
# this process never holds the SessionRuntime doing the publishing.
# ---------------------------------------------------------------------------


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str) -> None:
    # The Origin check happened in RequestGuard; this is the login check.
    if not _token_is_valid(admin_token_from(ws.cookies)):
        await ws.close(code=4401)
        return

    await ws.accept()
    db = db_for(session_id)
    try:
        await ws.send_json({
            "type": "hello",
            "conversations": await db.list_conversations(),
            "config": await config_store.load(pool, session_id),
            "tenant_config": (await tenants.TenantStore(pool).bundle_for_session(session_id)).config,
            "status": await api_status(session_id),
            "bookings": [booking_store.public(b) for b in await (await booking_store_for(session_id)).awaiting_owner()],
            "media": (await media_library_for(session_id)).all(),
        })
    except Exception:
        log.exception("[%s] Failed to send websocket hello", session_id)
        await ws.close(code=1011)
        return

    async def _forward_events() -> None:
        async with bus.subscribe_events(session_id) as pubsub:
            while True:
                # Short repeated polls, not one long wait: this is a real
                # limitation of at least one Redis client library's async
                # pub/sub under a single long timeout, worth keeping in
                # mind if this is ever ported to a different client.
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None:
                    await ws.send_text(message["data"])

    import asyncio

    forward_task = asyncio.create_task(_forward_events())
    try:
        while True:
            await ws.receive_text()  # client sends keepalives; nothing to parse
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        forward_task.cancel()
        from contextlib import suppress
        with suppress(asyncio.CancelledError, Exception):
            await forward_task


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """The full error goes to the log. Only a logged-in admin sees its text
    in the answer (it helps on the panel's toasts); anyone else, a client on
    /api/owner/* included, gets a bare message, since an exception's text
    can carry a query, a connection string or a stored value."""
    log.exception("Unhandled error in %s %s", request.method, request.url.path)
    if _token_is_valid(admin_token_from(request.cookies)):
        detail = f"{type(exc).__name__}: {exc}"
    else:
        detail = "Something went wrong on the server."
    return JSONResponse(status_code=500, content={"detail": detail})


# Industries, tenants, prompt layers, the config helper and the audit log.
platform_api.bind(get_pool=lambda: pool, get_bus=lambda: bus)
app.include_router(platform_api.router, dependencies=[Depends(require_auth)])
booking_api.bind(get_pool=lambda: pool, get_bus=lambda: bus)
app.include_router(booking_api.router, dependencies=[Depends(require_auth)])
# Kill switches, billing, alerts and health: admin only, like everything here.
safety_api.bind(get_pool=lambda: pool, get_bus=lambda: bus)
app.include_router(safety_api.router, dependencies=[Depends(require_auth)])
# Phase 4. Admin only: client logins, the unanswered queue, review batches.
for _module in (owner_admin_api, unanswered_api, review_api):
    _module.bind(get_pool=lambda: pool, get_bus=lambda: bus)
    app.include_router(_module.router, dependencies=[Depends(require_auth)])
# The client dashboard's API has its own login (owner_auth.py), never the
# admin's; every route there checks it.
owner_api.bind(get_pool=lambda: pool, get_bus=lambda: bus)
app.include_router(owner_api.router)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# Startup / shutdown — connect to Postgres and Redis. No SessionRuntimes
# are constructed here; see the module docstring for why.
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    global pool, registry, bus, login_flow
    problems = check_public_setup()
    if problems:
        raise RuntimeError("Refusing to serve a public panel (PANEL_DOMAIN is set): " + "; ".join(problems))
    if ADMIN_TOTP_SECRET:
        # Fail at boot, not at the first login attempt, if it can't work.
        try:
            totp.validate_secret(ADMIN_TOTP_SECRET)
        except ValueError as exc:
            raise RuntimeError(f"ADMIN_TOTP_SECRET is unusable: {exc}") from exc
        log.info("Admin login requires an authenticator code (ADMIN_TOTP_SECRET is set).")
    pool = await pg.create_pool(DATABASE_URL)
    await pg.assert_version(pool, pg.latest_version())
    registry = SessionRegistry(pool)
    login_flow = LoginFlow(pool)
    bus = await commands.CommandBus.connect(REDIS_URL)

    if HOST not in LOOPBACK:
        log.warning(
            "Admin panel bound to %s, not loopback. Password-protected, but only do "
            "this behind a firewall or inside a container published to 127.0.0.1.",
            HOST,
        )
    session_count = len(await registry.list())
    log.info(
        "Admin panel: http://%s:%s (%d session(s) registered; live status comes "
        "from whichever manager.py workers are actually running them)",
        "127.0.0.1" if HOST in LOOPBACK else HOST, PORT, session_count,
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if login_flow is not None:
        await login_flow.reset()  # drop a half-finished sign-in's connection
    if bus is not None:
        await bus.close()
    if pool is not None:
        await pool.close()


if __name__ == "__main__":
    import uvicorn

    # proxy_headers + forwarded_allow_ips="*": take the client IP (and
    # scheme) from X-Forwarded-For/-Proto, which the optional Caddy front
    # door sets to the real visitor's address — the login rate limit keys
    # on it. Trusting those headers from anyone is acceptable only because
    # nothing untrusted can reach this port: docker-compose.yml publishes it
    # on the host's 127.0.0.1 alone, so the only other senders are the
    # compose network (Caddy, which overwrites any client-supplied
    # X-Forwarded-For) and someone already on the server. If you ever
    # publish this port more widely, narrow forwarded_allow_ips first.
    uvicorn.run(
        app, host=HOST, port=PORT, log_level="warning", access_log=False,
        proxy_headers=True, forwarded_allow_ips="*",
    )
