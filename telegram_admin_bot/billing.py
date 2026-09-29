"""Billing state per tenant: active → grace → suspended.

- `tenants.billing_next_due` is the date the next payment is due; the
  operator sets it (none = billing is not tracked for that tenant).
- The day after it (in the tenant's timezone), if no payment was recorded,
  the tenant goes into **grace**: the owner gets a message from the
  tenant's own account (to `booking.provider`, the owner's Telegram), the
  operator gets an alert, and the bot keeps working.
- `grace_hours` later (48 by default) it is **suspended**: a 'billing'
  soft-off hold (controls.py). Nothing is deleted; data stays.
- Recording a payment (with the next due date) makes it active again and
  lifts the hold. The operator can also set any status by hand, at any
  time, with a reason; every change is audited.

The grace length and the owner's message are platform settings (Safety →
Billing in the panel). The message may use {business}, {due} and {until}.

`tick()` runs on the scheduler once a minute; each step is conditional on
the state it moves from, so running it twice changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

import alerts
import audit
import commands
import controls
import tenant_config

log = logging.getLogger("billing")

ACTIVE = "active"
GRACE = "grace"
SUSPENDED = "suspended"
STATUSES = (ACTIVE, GRACE, SUSPENDED)

NOTICE_TIMEOUT = 45.0
DEFAULT_SETTINGS = {
    "grace_hours": 48,
    "notice": "Payment for {business} was due on {due}. The assistant will pause on {until} "
              "unless the payment is recorded before then.",
}


async def settings(pool: asyncpg.Pool) -> dict[str, Any]:
    value = await pool.fetchval("SELECT value FROM platform_settings WHERE key = 'billing'")
    value = json.loads(value) if isinstance(value, str) else (value or {})
    return {**DEFAULT_SETTINGS, **value}


async def save_settings(pool: asyncpg.Pool, *, grace_hours: int, notice: str, actor: str) -> dict[str, Any]:
    if not 1 <= int(grace_hours) <= 24 * 60:
        raise ValueError("grace_hours must be between 1 and 1440.")
    notice = notice.strip()
    if not notice:
        raise ValueError("The message to the owner can't be empty.")
    if len(notice) > 2000:
        raise ValueError("The message to the owner is limited to 2000 characters.")
    try:
        notice.format(business="x", due="x", until="x")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"The message may only use {{business}}, {{due}} and {{until}} ({exc}).") from None
    before = await settings(pool)
    value = {"grace_hours": int(grace_hours), "notice": notice}
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            "INSERT INTO platform_settings (key, value, updated_by) VALUES ('billing', $1::jsonb, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()",
            json.dumps(value), actor,
        )
        await audit.record(con, tenant_id=None, actor=actor, event=audit.CONFIG_CHANGED,
                           reason="Billing settings changed", payload={"before": before, "after": value})
    return value


# ------------------------------------------------------------------ changes


async def _set(con: asyncpg.Connection, tenant_id: int, *, actor: str, reason: str, **fields: Any) -> dict[str, Any]:
    before = await con.fetchrow(
        "SELECT status, billing_next_due, grace_until FROM tenants WHERE id = $1 FOR UPDATE", tenant_id,
    )
    if before is None:
        raise LookupError(f"no tenant {tenant_id}")
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(fields))
    await con.execute(f"UPDATE tenants SET {sets}, updated_at = now() WHERE id = $1", tenant_id, *fields.values())
    after = await con.fetchrow("SELECT status, billing_next_due, grace_until FROM tenants WHERE id = $1", tenant_id)
    await audit.record(con, tenant_id=tenant_id, actor=actor, event=audit.BILLING_CHANGED, reason=reason,
                       payload={"from": dict(before), "to": dict(after)})
    return dict(after)


async def set_due(pool: asyncpg.Pool, tenant_id: int, due: Optional[date], *, actor: str, reason: str = "") -> None:
    async with pool.acquire() as con, con.transaction():
        await _set(con, tenant_id, actor=actor, reason=reason or "due date set", billing_next_due=due)


async def mark_paid(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], tenant_id: int, *,
                    next_due: Optional[date], actor: str, reason: str = "") -> None:
    """A payment was recorded: active again, the hold lifted, and the next
    due date set (None = stop tracking)."""
    async with pool.acquire() as con, con.transaction():
        await _set(con, tenant_id, actor=actor, reason=reason or "payment recorded", status=ACTIVE,
                   billing_next_due=next_due, grace_until=None, billing_notice_sent_at=None)
    await _after_status(pool, bus, tenant_id, ACTIVE, actor=actor, reason=reason or "payment recorded")


async def set_status(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], tenant_id: int, status: str, *,
                     actor: str, reason: str) -> None:
    """The manual override: any status, any time, with a reason."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if not reason.strip():
        raise ValueError("Say why the status is being changed by hand.")
    fields: dict[str, Any] = {"status": status}
    if status == GRACE:
        hours = (await settings(pool))["grace_hours"]
        fields.update(grace_until=datetime.now(timezone.utc) + timedelta(hours=hours), billing_notice_sent_at=None)
    elif status == ACTIVE:
        fields.update(grace_until=None, billing_notice_sent_at=None)
    async with pool.acquire() as con, con.transaction():
        await _set(con, tenant_id, actor=actor, reason=reason, **fields)
    await _after_status(pool, bus, tenant_id, status, actor=actor, reason=reason)


async def _after_status(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], tenant_id: int, status: str, *,
                        actor: str, reason: str) -> None:
    if status == SUSPENDED:
        await controls.add_hold(pool, tenant_id, controls.BILLING, reason, actor=actor)
    else:
        await controls.remove_hold(pool, tenant_id, controls.BILLING, actor=actor, reason=reason)
    if status == ACTIVE:
        await alerts.resolve(pool, tenant_id=tenant_id, kind="billing", by=actor)
    session_id = await pool.fetchval("SELECT session_id FROM tenants WHERE id = $1", tenant_id)
    await controls.reload_controls(pool, bus, [session_id] if session_id else [])


# --------------------------------------------------------------------- tick


def _local_today(timezone_name: str, now: datetime) -> date:
    from bookings import tzinfo_for

    return now.astimezone(tzinfo_for(timezone_name)).date()


async def _timezone(pool: asyncpg.Pool, tenant_id: int) -> str:
    row = await pool.fetchrow(
        "SELECT i.default_config, t.config_json FROM tenants t JOIN industries i ON i.id = t.industry_id "
        "WHERE t.id = $1", tenant_id,
    )

    def parsed(value):
        return json.loads(value) if isinstance(value, str) else (value or {})

    if row is None:
        return "UTC"
    try:
        return tenant_config.resolve(parsed(row["default_config"]), parsed(row["config_json"])).config.timezone
    except (tenant_config.ConfigError, ValueError, TypeError, AttributeError):
        # Edited by hand into something that no longer validates (or isn't
        # even JSON): UTC rather than no billing at all.
        log.warning("Tenant %s: config does not validate; billing uses UTC.", tenant_id)
        return "UTC"


def notice_text(template: str, *, business: str, due: date, until: datetime, timezone_name: str) -> str:
    from bookings import tzinfo_for

    local_until = until.astimezone(tzinfo_for(timezone_name))
    return template.format(business=business, due=due.strftime("%d.%m.%Y"),
                           until=local_until.strftime("%d.%m.%Y %H:%M"))


async def tick(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], now: Optional[datetime] = None) -> list[str]:
    """Move tenants along active → grace → suspended. Returns what changed."""
    now = now or datetime.now(timezone.utc)
    conf = await settings(pool)
    changed: list[str] = []

    # Every step for every tenant is guarded on its own: one tenant whose
    # row or account misbehaves (or the bus being down) must not hold up
    # the others, and above all not their suspension. Each step is
    # conditional on the state it moves from, so the next tick finishes
    # whatever an error left undone.
    async def guarded(what: str, tenant_id: int, step) -> None:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Billing: %s for tenant %s failed; retried next tick", what, tenant_id)

    # Due yesterday or earlier (in UTC, with a day's margin for timezones;
    # the exact day is checked in the tenant's own zone below).
    for row in await pool.fetch(
        "SELECT id, name FROM tenants WHERE status = 'active' AND billing_next_due IS NOT NULL "
        "AND billing_next_due < $1::date", (now + timedelta(days=1)).date(),
    ):
        await guarded("grace", row["id"], lambda row=row: _to_grace(pool, row, conf, now, changed))

    # Tell the owner, until it has been done.
    for row in await pool.fetch(
        "SELECT id, name, session_id, billing_next_due, grace_until FROM tenants "
        "WHERE status = 'grace' AND billing_notice_sent_at IS NULL AND grace_until > $1", now,
    ):
        async def notify(row=row) -> None:
            if await _notify_owner(pool, bus, row, conf):
                changed.append(f"{row['id']}:notified")

        await guarded("owner notice", row["id"], notify)

    for row in await pool.fetch(
        "SELECT id FROM tenants WHERE status = 'grace' AND grace_until IS NOT NULL AND grace_until <= $1", now,
    ):
        await guarded("suspension", row["id"], lambda row=row: _suspend(pool, bus, row["id"], changed))
    return changed


async def _to_grace(pool: asyncpg.Pool, row: asyncpg.Record, conf: dict[str, Any], now: datetime,
                    changed: list[str]) -> None:
    due = await pool.fetchval("SELECT billing_next_due FROM tenants WHERE id = $1", row["id"])
    if due is None or _local_today(await _timezone(pool, row["id"]), now) <= due:
        return
    until = now + timedelta(hours=int(conf["grace_hours"]))
    async with pool.acquire() as con, con.transaction():
        moved = await con.fetchval(
            "UPDATE tenants SET status = 'grace', grace_until = $2, billing_notice_sent_at = NULL, "
            "updated_at = now() WHERE id = $1 AND status = 'active' RETURNING true", row["id"], until,
        )
        if not moved:
            return
        await audit.record(con, tenant_id=row["id"], actor=audit.SYSTEM, event=audit.BILLING_CHANGED,
                           reason=f"payment due {due.isoformat()} not recorded",
                           payload={"from": {"status": ACTIVE}, "to": {"status": GRACE,
                                                                         "grace_until": until.isoformat()}})
    changed.append(f"{row['id']}:grace")
    await alerts.raise_alert(pool, tenant_id=row["id"], kind="billing", severity=alerts.WARNING,
                             message=f"Payment due {due.isoformat()} not recorded: in grace until "
                                     f"{until.isoformat(timespec='minutes')}, then suspended.")


async def _suspend(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], tenant_id: int,
                   changed: list[str]) -> None:
    async with pool.acquire() as con, con.transaction():
        moved = await con.fetchval(
            "UPDATE tenants SET status = 'suspended', updated_at = now() "
            "WHERE id = $1 AND status = 'grace' RETURNING true", tenant_id,
        )
        if not moved:
            return
        await audit.record(con, tenant_id=tenant_id, actor=audit.SYSTEM, event=audit.BILLING_CHANGED,
                           reason="grace period ended without a payment",
                           payload={"from": {"status": GRACE}, "to": {"status": SUSPENDED}})
        # In the same transaction as the status: a crash in between must not
        # leave a suspended tenant that still sends.
        # (controls.add_hold's insert and audit row, on this connection.)
        if await con.fetchval(
            "INSERT INTO tenant_holds (tenant_id, kind, reason, created_by) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tenant_id, kind) DO NOTHING RETURNING true",
            tenant_id, controls.BILLING, "grace period ended without a payment", audit.SYSTEM,
        ):
            await audit.record(con, tenant_id=tenant_id, actor=audit.SYSTEM, event=audit.TENANT_SOFT_OFF,
                               reason="grace period ended without a payment", payload={"kind": controls.BILLING})
    changed.append(f"{tenant_id}:suspended")
    await alerts.raise_alert(pool, tenant_id=tenant_id, kind="billing_suspended", severity=alerts.CRITICAL,
                             message="Suspended for non-payment: the bot sends nothing until a payment "
                                     "is recorded.")
    session_id = await pool.fetchval("SELECT session_id FROM tenants WHERE id = $1", tenant_id)
    await controls.reload_controls(pool, bus, [session_id] if session_id else [])


async def _notify_owner(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], row: asyncpg.Record,
                        conf: dict[str, Any]) -> bool:
    if bus is None or not row["session_id"] or row["billing_next_due"] is None:
        return False
    text = notice_text(conf["notice"], business=row["name"], due=row["billing_next_due"],
                       until=row["grace_until"], timezone_name=await _timezone(pool, row["id"]))
    try:
        result = await bus.dispatch(row["session_id"], "owner_notice",
                                    {"text": text, "reason": "billing notice to the owner"}, timeout=NOTICE_TIMEOUT)
        sent = bool((result or {}).get("sent"))
        problem = "" if sent else (result or {}).get("error") or "the owner could not be reached (booking.provider)"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # CommandError (incl. the bus being down), or anything unexpected
        sent, problem = False, f"the account did not answer ({type(exc).__name__})"
    if sent:
        await pool.execute("UPDATE tenants SET billing_notice_sent_at = now() WHERE id = $1", row["id"])
        await alerts.resolve(pool, tenant_id=row["id"], kind="billing_notice", by="system: sent")
        return True
    await alerts.raise_alert(pool, tenant_id=row["id"], kind="billing_notice", severity=alerts.WARNING,
                             message=f"The owner was not told about the missed payment yet: {problem}. "
                                     "Retrying every minute.")
    return False
