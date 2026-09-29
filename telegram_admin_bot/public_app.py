"""The only part of the platform that faces the internet: two kinds of
unguessable-token URLs, and nothing else.

- ``GET /cal/<calendar_token>.ics`` — a business's bookings as a calendar
  feed the owner subscribes to (Google/Apple/Outlook).
- ``/b/<customer_token>`` — a read-only page for one booking, given to the
  customer, with "I'm coming" and "cancel" buttons.

The admin panel (panel.py) stays private behind the SSH tunnel; this is a
separate process so exposing it exposes none of the panel's routes.

Decisions:
- Holds no Telegram connection. A customer's button press is sent over the
  command bus (commands.py) to the worker that runs the business's account
  (the booking's session_id); that worker changes the booking and tells the
  owner. If no worker answers, the page says so — nothing is queued here,
  because a customer who taps "cancel" and sees success must be able to
  trust it happened.
- Tenant isolation: the token is the only input. Every query is keyed on
  it, and the calendar's booking query is scoped by the tenant id the token
  resolved to, so one token can never reach another tenant's rows.
- Tokens must match [A-Za-z0-9_-]{16,128}; anything else is a 404 before
  the database is touched. Unknown tokens get the same bare 404, so the
  response never tells a guesser whether they were close.
- GET never changes anything: Telegram, WhatsApp, mail scanners and chat
  apps fetch link previews, and a preview must not cancel a booking. The
  buttons are POST forms; "cancel" goes through a confirmation page first.
- Tokens are secrets, so request paths are never logged (uvicorn's access
  log is off, and this module doesn't log paths).
- The customer page shows nothing about the customer (not even their own
  name): whoever holds the link, e.g. from a forwarded message, learns the
  business, the time and the state, and nothing else.
"""

from __future__ import annotations

import html
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

import commands
import ics
import pg

PUBLIC_HOST = (os.getenv("PUBLIC_HOST") or "localhost").strip()
PUBLIC_PORT = int(os.getenv("PUBLIC_PORT") or 8788)
PUBLIC_BIND = (os.getenv("PUBLIC_BIND") or "127.0.0.1").strip()

# How long a button press waits for the account's worker. Read at call
# time so tests can shorten it.
ACTION_TIMEOUT = 10.0

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")

# Feed window: live and past bookings from the last 90 days; cancelled ones
# for 30 days after they were cancelled, so subscribed calendars see the
# CANCELLED status and drop the event.
FEED_PAST_DAYS = 90
FEED_CANCELLED_DAYS = 30

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
}

STATE_LABELS = {
    "requested": "Waiting for confirmation",
    "pending": "Waiting for confirmation",
    "confirmed": "Confirmed",
    "cancelled": "Cancelled",
    "completed": "Completed",
    "no_show": "Missed",
}
CANCELLABLE_STATES = {"requested", "pending", "confirmed"}

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

log = logging.getLogger("public_app")


# ---------------------------------------------------------------------------
# App + dependency injection
# ---------------------------------------------------------------------------


def create_app(pool: Any = None, bus: Optional[commands.CommandBus] = None) -> FastAPI:
    """Build the app. With `pool` and `bus` given (tests), nothing is
    connected at startup and nothing is closed at shutdown; otherwise both
    are created from DATABASE_URL / REDIS_URL when the server starts."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = app.state.pool is None
        if owned:
            app.state.pool = await pg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=8)
            await pg.assert_version(app.state.pool, pg.latest_version())
            app.state.bus = await commands.CommandBus.connect(os.environ["REDIS_URL"])
        try:
            yield
        finally:
            if owned:
                await app.state.bus.close()
                await app.state.pool.close()

    # No /docs or /openapi.json: nothing about this service needs describing
    # to the internet.
    application = FastAPI(
        title="Bookings (public)", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    application.state.pool = pool
    application.state.bus = bus
    _register(application)
    return application


def _register(application: FastAPI) -> None:
    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # The feed sets its own (short, private) caching; everything else is
        # a per-person page that must not sit in a shared cache.
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @application.exception_handler(StarletteHTTPException)
    async def plain_errors(request: Request, exc: StarletteHTTPException) -> Response:
        # A bare status line instead of FastAPI's JSON detail.
        return _not_found() if exc.status_code == 404 else PlainTextResponse(
            "Error", status_code=exc.status_code
        )

    @application.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @application.get("/cal/{token}.ics")
    async def calendar_feed(token: str, request: Request) -> Response:
        if not _valid(token):
            return _not_found()
        pool = request.app.state.pool
        async with pool.acquire() as con:
            tenant = await con.fetchrow(
                "SELECT id, name FROM tenants WHERE calendar_token = $1", token
            )
            if tenant is None:
                return _not_found()
            rows = await con.fetch(
                f"""
                SELECT id, number, state, starts_at, ends_at, service, customer_name,
                       updated_at, tz
                  FROM bookings
                 WHERE tenant_id = $1
                   AND ((state IN ('requested', 'pending', 'confirmed', 'completed', 'no_show')
                         AND starts_at >= now() - interval '{FEED_PAST_DAYS} days')
                        OR (state = 'cancelled'
                            AND updated_at >= now() - interval '{FEED_CANCELLED_DAYS} days'))
                 ORDER BY starts_at, id
                """,
                tenant["id"],
            )
        body = ics.calendar(
            [dict(r) for r in rows], name=tenant["name"], host=PUBLIC_HOST,
            now=datetime.now(timezone.utc),
        )
        return Response(
            body, media_type="text/calendar; charset=utf-8",
            headers={"Cache-Control": "private, max-age=300"},
        )

    @application.get("/b/{token}")
    async def booking_page(token: str, request: Request) -> Response:
        booking = await _booking(request, token)
        if booking is None:
            return _not_found()
        return _html(_booking_html(token, booking))

    @application.get("/b/{token}/cancel")
    async def cancel_page(token: str, request: Request) -> Response:
        booking = await _booking(request, token)
        if booking is None:
            return _not_found()
        if not _can_cancel(booking):
            return _back(token)
        t = _e(token)
        body = f"""
<h1>Cancel this booking?</h1>
<p class="muted">{_e(booking['business'])} &middot; #{_e(booking['number'])}</p>
<p>{_e(booking['service'] or 'Booking')}<br>{_e(format_when(booking))}</p>
<p>The business will be told that you cancelled.</p>
<form method="post" action="/b/{t}/cancel"><button class="danger" type="submit">Yes, cancel it</button></form>
<p><a href="/b/{t}">No, keep it</a></p>
"""
        return _html(_layout("Cancel booking", body))

    @application.post("/b/{token}/confirm")
    async def confirm_attendance(token: str, request: Request) -> Response:
        booking = await _booking(request, token)
        if booking is None:
            return _not_found()
        if not _can_confirm(booking):
            # A stale page or a replayed POST: nothing to do, just show
            # where things stand.
            return _back(token)
        return await _dispatch(request, token, booking, "confirm_attendance")

    @application.post("/b/{token}/cancel")
    async def cancel_booking(token: str, request: Request) -> Response:
        booking = await _booking(request, token)
        if booking is None:
            return _not_found()
        if not _can_cancel(booking):
            return _back(token)
        return await _dispatch(request, token, booking, "cancel")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid(token: str) -> bool:
    return TOKEN_RE.fullmatch(token) is not None


def _not_found() -> Response:
    return PlainTextResponse("Not found", status_code=404)


def _back(token: str) -> Response:
    return RedirectResponse(f"/b/{token}", status_code=303)


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


async def _booking(request: Request, token: str) -> Optional[Mapping[str, Any]]:
    if not _valid(token):
        return None
    async with request.app.state.pool.acquire() as con:
        # The tenant comes from the booking row itself, never from anything
        # else in the request.
        return await con.fetchrow(
            """
            SELECT b.id, b.session_id, b.number, b.state, b.service, b.starts_at,
                   b.ends_at, b.tz, b.attendance_confirmed_at, t.name AS business
              FROM bookings b
              JOIN tenants t ON t.id = b.tenant_id
             WHERE b.customer_token = $1
            """,
            token,
        )


def _is_future(booking: Mapping[str, Any]) -> bool:
    return booking["starts_at"] > datetime.now(timezone.utc)


def _can_confirm(booking: Mapping[str, Any]) -> bool:
    return (
        booking["state"] == "confirmed"
        and _is_future(booking)
        and booking["attendance_confirmed_at"] is None
    )


def _can_cancel(booking: Mapping[str, Any]) -> bool:
    return booking["state"] in CANCELLABLE_STATES and _is_future(booking)


async def _dispatch(request: Request, token: str, booking: Mapping[str, Any], action: str) -> Response:
    bus: commands.CommandBus = request.app.state.bus
    try:
        await bus.dispatch(
            booking["session_id"], "booking_customer_action",
            {"booking_id": booking["id"], "action": action, "via": "page"},
            timeout=ACTION_TIMEOUT,
        )
    except (commands.CommandError, commands.CommandTimeout):
        # CommandTimeout is a CommandError; named for the reader. Logged by
        # booking id, never by token.
        log.warning("booking %s: %s could not be delivered to its account", booking["id"], action)
        body = f"""
<h1>That didn't go through</h1>
<p>We couldn't do that right now. Please message {_e(booking['business'])} directly.</p>
<p><a href="/b/{_e(token)}">Back to your booking</a></p>
"""
        return _html(_layout("Not available right now", body), status_code=503)
    return _back(token)


def _zone(name: str) -> tuple[Any, str]:
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc, "UTC"


def _day(dt: datetime) -> str:
    # Fixed English names rather than strftime("%a %b"), which follows the
    # server's locale.
    return f"{_DAYS[dt.weekday()]} {dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year}"


def format_when(booking: Mapping[str, Any]) -> str:
    """'Fri 03 Oct 2026, 14:00–15:00 (Europe/Riga)', in the booking's zone."""
    zone, zone_name = _zone(booking["tz"])
    start = booking["starts_at"].astimezone(zone)
    end = booking["ends_at"].astimezone(zone)
    if start.date() == end.date():
        return f"{_day(start)}, {start:%H:%M}–{end:%H:%M} ({zone_name})"
    return f"{_day(start)}, {start:%H:%M} – {_day(end)}, {end:%H:%M} ({zone_name})"


def _booking_html(token: str, booking: Mapping[str, Any]) -> str:
    t = _e(token)
    state = booking["state"]
    actions = ""
    if _can_cancel(booking):
        if state == "confirmed":
            if booking["attendance_confirmed_at"] is not None:
                actions += '<p class="ok">You have confirmed that you are coming.</p>'
            else:
                actions += (
                    f'<form method="post" action="/b/{t}/confirm">'
                    '<button type="submit">I&#x27;m coming</button></form>'
                )
        actions += f'<p><a class="quiet" href="/b/{t}/cancel">Cancel booking</a></p>'
    body = f"""
<p class="muted">{_e(booking['business'])}</p>
<h1>Booking #{_e(booking['number'])}</h1>
<p class="state state-{_e(state)}">{_e(STATE_LABELS.get(state, state))}</p>
<dl>
  <dt>Service</dt><dd>{_e(booking['service'] or 'Booking')}</dd>
  <dt>When</dt><dd>{_e(format_when(booking))}</dd>
</dl>
{actions}
"""
    return _layout(f"Booking #{booking['number']}", body)


_CSS = """
:root { color-scheme: light dark; --bg: #f6f6f4; --card: #fff; --fg: #1d1d1f; --muted: #6b6b70;
  --accent: #1a73e8; --accent-fg: #fff; --danger: #c5221f; --ok: #188038; --line: #e2e2e0; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #131314; --card: #1e1f20; --fg: #e8e8ea; --muted: #a0a0a8;
    --accent: #8ab4f8; --accent-fg: #131314; --danger: #f28b82; --ok: #81c995; --line: #333437; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 28rem; margin: 2rem auto; padding: 1.5rem 1.25rem; background: var(--card);
  border: 1px solid var(--line); border-radius: 12px; }
@media (max-width: 30rem) { main { margin: 0; border: 0; border-radius: 0; min-height: 100vh; } }
h1 { font-size: 1.4rem; margin: 0 0 .5rem; }
.muted { color: var(--muted); margin: 0 0 .25rem; }
.state { font-weight: 600; }
.state-confirmed, .state-completed, .ok { color: var(--ok); }
.state-cancelled, .state-no_show { color: var(--danger); }
dl { display: grid; grid-template-columns: auto 1fr; gap: .25rem 1rem; margin: 1rem 0; }
dt { color: var(--muted); } dd { margin: 0; }
form { margin: 1rem 0 .5rem; }
button { width: 100%; padding: .8rem 1rem; font: inherit; font-weight: 600; border: 0;
  border-radius: 8px; background: var(--accent); color: var(--accent-fg); cursor: pointer; }
button.danger { background: var(--danger); color: var(--card); }
a { color: var(--accent); } a.quiet { color: var(--muted); }
"""


def _layout(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # access_log=False: every path here contains a secret token.
    uvicorn.run(app, host=PUBLIC_BIND, port=PUBLIC_PORT, log_level="warning", access_log=False)
