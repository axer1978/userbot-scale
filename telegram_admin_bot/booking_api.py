"""Admin API for bookings: the calendar, booking actions, opening hours, the
waitlist, the calendar feed link and the AI usage of a client.

Mounted by panel.py behind the admin login, per account like the rest of
the panel's /api/sessions/... routes. Reads and the opening-hours save go
straight to Postgres (booking_store, scoped to the account's tenant). A
booking action needs the live account (it messages the customer and the
owner), so it goes over the command bus to the worker running it
(`booking_action`, session_runtime.handle_command -> booking_flow).
"""

from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import ai_limits
import audit
import availability
import booking_store
import bookings
import commands
import tenant_config
import tenants
from booking_store import public

router = APIRouter()
ACTION_TIMEOUT = 30.0

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


async def _bundle(session_id: str) -> tenants.Bundle:
    try:
        return await tenants.TenantStore(_get_pool()).bundle_for_session(session_id)
    except tenants.NotFound:
        raise HTTPException(status_code=404, detail="Unknown session") from None


async def _store(session_id: str) -> tuple[booking_store.BookingStore, tenants.Bundle]:
    bundle = await _bundle(session_id)
    return booking_store.BookingStore(_get_pool(), bundle.tenant["id"], session_id), bundle


async def _live(session_id: str, action: str, args: dict[str, Any]) -> Any:
    """A command for the worker running this account, with its errors as
    HTTP answers the panel can show."""
    try:
        return await _get_bus().dispatch(session_id, action, args, timeout=ACTION_TIMEOUT)
    except commands.CommandTimeout:
        raise HTTPException(status_code=503, detail="This account is not running right now, so the customer "
                                                    "and owner can't be told. Start it and try again.") from None
    except commands.CommandError as exc:
        kind, _, message = str(exc).partition(": ")
        if kind in ("IllegalTransition", "SlotTaken", "StaleBooking"):
            raise HTTPException(status_code=409, detail=message or kind) from None
        if kind in ("BookingNotFound", "KeyError"):
            raise HTTPException(status_code=404, detail="Unknown booking") from None
        raise HTTPException(status_code=502, detail=str(exc)) from None


def _parse_day(value: Optional[str], default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{value!r} is not a date (YYYY-MM-DD)") from None


# ------------------------------------------------------------ bookings


@router.get("/api/sessions/{session_id}/bookings")
async def list_bookings(session_id: str, start: Optional[str] = None, days: int = 14,
                        states: Optional[str] = None) -> dict[str, Any]:
    """Bookings touching [start, start + days) in the client's zone, plus
    what is waiting for an answer. `states` is a comma list."""
    store, bundle = await _store(session_id)
    tz = bundle.config["timezone"]
    zone = bookings.tzinfo_for(tz)
    today = datetime.now(zone).date()
    day = _parse_day(start, today - timedelta(days=today.weekday()))
    days = max(1, min(days, 92))
    begin = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=days), datetime.min.time(), tzinfo=zone)
    wanted = [s for s in (states or "").split(",") if s] or None
    rows = await store.between(begin, end, wanted)
    return {
        "timezone": tz,
        "start": day.isoformat(),
        "days": days,
        "bookings": [public(b) for b in rows],
        "awaiting": [public(b) for b in await store.awaiting_owner()],
        "enabled": bundle.config["booking"]["enabled"],
    }


@router.get("/api/sessions/{session_id}/bookings/{booking_id}")
async def get_booking(session_id: str, booking_id: int) -> dict[str, Any]:
    store, _ = await _store(session_id)
    try:
        booking = await store.get(booking_id)
    except booking_store.BookingNotFound:
        raise HTTPException(status_code=404, detail="Unknown booking") from None
    return {"booking": public(booking), "events": [public(e) for e in await store.events(booking_id)]}


class ActionBody(BaseModel):
    action: str = Field(pattern="^(confirm|decline|cancel|complete|no_show|propose|reschedule|resend)$")
    starts_at: Optional[str] = None
    minutes: Optional[int] = Field(None, ge=5, le=24 * 60)
    reason: str = Field("", max_length=300)


@router.post("/api/sessions/{session_id}/bookings/{booking_id}/action")
async def booking_action(session_id: str, booking_id: int, body: ActionBody) -> dict[str, Any]:
    store, _ = await _store(session_id)
    try:
        await store.get(booking_id)  # 404 before bothering the worker
    except booking_store.BookingNotFound:
        raise HTTPException(status_code=404, detail="Unknown booking") from None
    if body.action in ("propose", "reschedule") and not body.starts_at:
        raise HTTPException(status_code=400, detail="Give the new time")
    return await _live(session_id, "booking_action", {"booking_id": booking_id, **body.model_dump()})


@router.post("/api/sessions/{session_id}/conversations/{chat_id}/booking-scan")
async def booking_scan(session_id: str, chat_id: int) -> dict[str, Any]:
    return await _live(session_id, "booking_scan", {"chat_id": chat_id})


@router.get("/api/sessions/{session_id}/free-slots")
async def free_slots(session_id: str, start: Optional[str] = None, days: int = 7,
                     minutes: Optional[int] = None) -> dict[str, Any]:
    store, bundle = await _store(session_id)
    tz = bundle.config["timezone"]
    cfg = bundle.config["booking"]
    zone = bookings.tzinfo_for(tz)
    day = _parse_day(start, datetime.now(zone).date())
    days = max(1, min(days, 31))
    now = datetime.now(timezone.utc)
    begin = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    busy = await store.busy(begin - timedelta(days=1), begin + timedelta(days=days + 1))
    slots = availability.free_slots(
        day_from=day, days=days, duration_minutes=minutes or cfg["default_duration_minutes"],
        rules=await store.rules(), busy=busy, tz=tz, now=now,
        limits=availability.Limits(cfg["min_notice_minutes"], cfg["max_days_ahead"],
                                   frozenset(date.fromisoformat(d) for d in cfg["closed_dates"])),
    )
    return {"timezone": tz, "slots": [s.isoformat() for s in slots]}


# -------------------------------------------------------- opening hours


class RuleBody(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    slot_minutes: int = Field(60, ge=5, le=1440)
    buffer_minutes: int = Field(0, ge=0, le=1440)


class RulesBody(BaseModel):
    rules: list[RuleBody] = Field(max_length=100)


def _rule_json(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "start_time": row["start_time"].strftime("%H:%M"), "end_time": row["end_time"].strftime("%H:%M")}


@router.get("/api/sessions/{session_id}/availability")
async def get_availability(session_id: str) -> dict[str, Any]:
    store, bundle = await _store(session_id)
    return {"timezone": bundle.config["timezone"], "rules": [_rule_json(r) for r in await store.rule_rows()]}


@router.put("/api/sessions/{session_id}/availability")
async def put_availability(session_id: str, body: RulesBody) -> dict[str, Any]:
    store, bundle = await _store(session_id)
    try:
        rows = await store.save_rules([r.model_dump() for r in body.rules], actor="admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"timezone": bundle.config["timezone"], "rules": [_rule_json(r) for r in rows]}


# ------------------------------------------------------------- waitlist


@router.get("/api/sessions/{session_id}/waitlist")
async def get_waitlist(session_id: str) -> list[dict[str, Any]]:
    store, _ = await _store(session_id)
    return [public(e) for e in await store.waitlist()]


@router.delete("/api/sessions/{session_id}/waitlist/{entry_id}")
async def remove_waitlist(session_id: str, entry_id: int) -> dict[str, Any]:
    store, _ = await _store(session_id)
    row = await store.set_waitlist_state(entry_id, "removed", reason="removed in the panel", actor=audit.ADMIN)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown waitlist entry")
    return public(row)


# -------------------------------------------------------- calendar feed


def _feed(token: str) -> dict[str, Any]:
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return {"url": f"{base}/cal/{token}.ics" if base else "", "public_base_url_set": bool(base)}


@router.get("/api/sessions/{session_id}/calendar-feed")
async def calendar_feed(session_id: str) -> dict[str, Any]:
    bundle = await _bundle(session_id)
    token = await _get_pool().fetchval("SELECT calendar_token FROM tenants WHERE id = $1", bundle.tenant["id"])
    return _feed(token)


@router.post("/api/sessions/{session_id}/calendar-feed/regenerate")
async def regenerate_feed(session_id: str) -> dict[str, Any]:
    """A new secret link; the old one stops working at once."""
    bundle = await _bundle(session_id)
    pool = _get_pool()
    token = secrets.token_urlsafe(32)
    async with pool.acquire() as con, con.transaction():
        await con.execute("UPDATE tenants SET calendar_token = $2, updated_at = now() WHERE id = $1",
                          bundle.tenant["id"], token)
        await audit.record(con, tenant_id=bundle.tenant["id"], actor=audit.ADMIN, event=audit.TENANT_UPDATED,
                           reason="calendar feed link replaced", payload={"field": "calendar_token"})
    return _feed(token)


# -------------------------------------------------------------- AI usage


@router.get("/api/sessions/{session_id}/ai-usage")
async def ai_usage(session_id: str) -> dict[str, Any]:
    """Today's and this month's AI use against the client's limits."""
    bundle = await _bundle(session_id)
    pool, cfg = _get_pool(), bundle.config
    now_local = datetime.now(bookings.tzinfo_for(cfg["timezone"]))
    day_start, month_start = ai_limits.period_starts(now_local)
    today = await ai_limits.usage_since(pool, bundle.tenant["id"], day_start)
    month = await ai_limits.usage_since(pool, bundle.tenant["id"], month_start)
    return {
        "today": {"tokens": today.tokens, "eur": round(today.eur, 4)},
        "month": {"tokens": month.tokens, "eur": round(month.eur, 4)},
        "limits": {**cfg["limits"], "monthly_spend_eur": cfg["api_spend_cap_eur"]},
        "reached": await ai_limits.limit_reached(pool, bundle.tenant["id"], cfg, now_local),
    }
