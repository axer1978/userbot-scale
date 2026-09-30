"""Kill switches: soft-off per tenant, the global stop, hard-off.

Soft-off
    The tenant's account sends nothing on its own: no replies, reminders,
    owner messages or outreach. Messages keep arriving and are stored and
    shown. It is a set of *holds*, one per cause (tenant_holds, migration
    0004): the operator's pause, billing, an AI spend limit, an anomaly, or
    Telegram pushing back. The account is off while any hold is present,
    and each is lifted on its own, so resuming a manual pause does not also
    lift a billing suspension. Resuming replays nothing: replies that were
    waiting are dropped when the soft-off starts, reminders that fell due
    meanwhile are skipped, and messages received while off get no answer.
    The operator can still send by hand from the panel.

Global stop
    Every tenant soft-off at once, reachable only by the platform admin
    (the panel's admin login, or this file from a shell on the server:
    `python controls.py stop "reason"` / `python controls.py resume`).

Hard-off
    For a hijacked or leaked session: the account's Telegram session is
    logged out (its auth key stops working on Telegram's side), the key is
    deleted from Postgres and the account is deactivated. Signing it in
    again is a fresh login from the panel.

Every change writes an audit row; the running account is told at once over
the bus, and also rechecks on every send and every scheduler tick, so a
missed message delays nothing that matters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

import asyncpg

import alerts
import audit
import commands

log = logging.getLogger("controls")

MANUAL = "manual"
BILLING = "billing"
SPEND_CAP = "spend_cap"
ANOMALY = "anomaly"
TELEGRAM = "telegram"
WHATSAPP = "whatsapp"
KINDS = (MANUAL, BILLING, SPEND_CAP, ANOMALY, TELEGRAM, WHATSAPP)

LABELS = {
    MANUAL: "paused",
    BILLING: "suspended (billing)",
    SPEND_CAP: "AI limit reached",
    ANOMALY: "anomaly",
    TELEGRAM: "stopped after a Telegram error",
}

RELOAD_TIMEOUT = 5.0
HARD_OFF_TIMEOUT = 30.0


def describe(kind: str, reason: str) -> str:
    label = LABELS.get(kind, kind)
    return f"{label}: {reason}" if reason else label


def _hold(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "label": LABELS.get(row["kind"], row["kind"]),
        "reason": row["reason"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
    }


async def holds(pool: asyncpg.Pool, tenant_id: int) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT * FROM tenant_holds WHERE tenant_id = $1 ORDER BY created_at, kind", tenant_id,
    )
    return [_hold(r) for r in rows]


async def add_hold(pool: asyncpg.Pool, tenant_id: int, kind: str, reason: str, *, actor: str) -> bool:
    """Soft-off for this cause. True when it is new (audited then); a hold
    of the same kind that is already there is left as it is."""
    if kind not in KINDS:
        raise ValueError(f"unknown hold {kind!r}")
    async with pool.acquire() as con, con.transaction():
        added = await con.fetchval(
            "INSERT INTO tenant_holds (tenant_id, kind, reason, created_by) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tenant_id, kind) DO NOTHING RETURNING true",
            tenant_id, kind, reason, actor,
        )
        if added:
            await audit.record(con, tenant_id=tenant_id, actor=actor, event=audit.TENANT_SOFT_OFF,
                               reason=reason, payload={"kind": kind})
    return bool(added)


async def remove_hold(pool: asyncpg.Pool, tenant_id: int, kind: str, *, actor: str, reason: str = "") -> bool:
    async with pool.acquire() as con, con.transaction():
        removed = await con.fetchval(
            "DELETE FROM tenant_holds WHERE tenant_id = $1 AND kind = $2 RETURNING true", tenant_id, kind,
        )
        if removed:
            await audit.record(con, tenant_id=tenant_id, actor=actor, event=audit.TENANT_RESUMED,
                               reason=reason, payload={"kind": kind})
    return bool(removed)


async def global_stop(pool: asyncpg.Pool) -> dict[str, Any]:
    value = await pool.fetchval("SELECT value FROM platform_settings WHERE key = 'global_stop'")
    value = json.loads(value) if isinstance(value, str) else (value or {})
    return {"on": bool(value.get("on")), "reason": value.get("reason", ""),
            "by": value.get("by", ""), "at": value.get("at")}


async def set_global_stop(pool: asyncpg.Pool, on: bool, *, reason: str, actor: str) -> dict[str, Any]:
    if on and not reason.strip():
        raise ValueError("Say why everything is being stopped.")
    async with pool.acquire() as con, con.transaction():
        at = await con.fetchval("SELECT now()")
        value = {"on": on, "reason": reason.strip() if on else "", "by": actor if on else "",
                 "at": at.isoformat(timespec="seconds") if on else None}
        await con.execute(
            "INSERT INTO platform_settings (key, value, updated_by) VALUES ('global_stop', $1::jsonb, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
            "updated_at = now()",
            json.dumps(value), actor,
        )
        await audit.record(con, tenant_id=None, actor=actor, event=audit.GLOBAL_STOP if on else audit.GLOBAL_RESUMED,
                           reason=reason)
    return await global_stop(pool)


async def off_reason(pool: asyncpg.Pool, tenant_id: int) -> str:
    """Why this tenant's account may not send on its own, or "" when it
    may. One query: the global stop, then every hold."""
    row = await pool.fetchrow(
        """
        SELECT (SELECT value FROM platform_settings WHERE key = 'global_stop') AS stop,
               (SELECT json_agg(json_build_object('kind', kind, 'reason', reason) ORDER BY created_at, kind)
                  FROM tenant_holds WHERE tenant_id = $1) AS holds
        """,
        tenant_id,
    )
    stop = row["stop"]
    stop = json.loads(stop) if isinstance(stop, str) else (stop or {})
    if stop.get("on"):
        return "global stop" + (f": {stop['reason']}" if stop.get("reason") else "")
    found = row["holds"]
    found = json.loads(found) if isinstance(found, str) else (found or [])
    return "; ".join(describe(h["kind"], h["reason"]) for h in found)


async def overview(pool: asyncpg.Pool, tenant_id: int) -> dict[str, Any]:
    """What the panel shows about a tenant's switches."""
    tenant = await pool.fetchrow(
        "SELECT status, billing_next_due, grace_until, billing_notice_sent_at FROM tenants WHERE id = $1",
        tenant_id,
    )
    return {
        "holds": await holds(pool, tenant_id),
        "global_stop": await global_stop(pool),
        "off_reason": await off_reason(pool, tenant_id),
        "billing": {
            "status": tenant["status"],
            "next_due": tenant["billing_next_due"].isoformat() if tenant["billing_next_due"] else None,
            "grace_until": tenant["grace_until"].isoformat(timespec="seconds") if tenant["grace_until"] else None,
            "notice_sent_at": (tenant["billing_notice_sent_at"].isoformat(timespec="seconds")
                               if tenant["billing_notice_sent_at"] else None),
        } if tenant else None,
    }


# ------------------------------------------------------------ the running accounts


async def reload_controls(pool: asyncpg.Pool, bus: Optional[commands.CommandBus],
                          session_ids: Optional[list[str]] = None) -> None:
    """Tell running accounts to re-read their switches now (all of them
    when session_ids is None). Best effort: they recheck on every send and
    every tick anyway."""
    if bus is None:
        return
    try:
        if session_ids is None:
            rows = await pool.fetch("SELECT session_id FROM telegram_sessions WHERE lease_expires_at > now()")
        else:
            rows = await pool.fetch(
                "SELECT session_id FROM telegram_sessions WHERE session_id = ANY($1) AND lease_expires_at > now()",
                [s for s in session_ids if s],
            )
    except Exception:
        log.warning("Could not list the running accounts to reload their switches; they recheck within a minute.",
                    exc_info=True)
        return

    async def one(session_id: str) -> None:
        try:
            await bus.dispatch(session_id, "reload_controls", {}, timeout=RELOAD_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # CommandError, BusUnavailable, or anything unexpected
            log.warning("[%s] Did not confirm reload_controls (%s); it rechecks within a minute.",
                        session_id, type(exc).__name__)

    await asyncio.gather(*(one(r["session_id"]) for r in rows))


async def hard_off(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], tenant_id: int, *,
                   reason: str, actor: str) -> dict[str, Any]:
    """Log the tenant's Telegram session out, delete its key and deactivate
    the account. Returns {"logged_out": bool, "how": ...}: logged_out False
    means Telegram could not be told (the key is deleted here all the same);
    the owner should then end the session under Settings → Devices."""
    if not reason.strip():
        raise ValueError("Say why the session is being revoked.")
    session_id = await pool.fetchval("SELECT session_id FROM tenants WHERE id = $1", tenant_id)
    if not session_id:
        raise LookupError("This client has no Telegram account.")
    logged_out, how = False, "not running"
    if bus is not None:
        try:
            result = await bus.dispatch(session_id, "hard_off", {"reason": reason}, timeout=HARD_OFF_TIMEOUT)
            logged_out, how = bool((result or {}).get("logged_out")), "by the running account"
        except (commands.CommandTimeout, commands.BusUnavailable):
            # Nobody answered, or the bus is down: log out from here. If a
            # worker does run it, its lease stops this (LeaseLost), and the
            # deactivation below fences that worker within seconds.
            pass
        except commands.CommandError as exc:
            how = f"the running account failed: {exc}"
    if not logged_out and how == "not running":
        # Nobody runs it: log out from here, holding its lease meanwhile so
        # no worker can pick it up half way.
        import session_runtime

        try:
            logged_out = await session_runtime.log_out_session(pool, session_id)
            how = "directly"
        except Exception as exc:
            how = f"could not connect to log out: {type(exc).__name__}"
    async with pool.acquire() as con, con.transaction():
        await con.execute(
            """
            UPDATE telegram_sessions
               SET is_active = false, auth_key_enc = NULL, dc_id = NULL, server_address = NULL, port = NULL,
                   state = 'revoked', state_reason = $2, updated_at = now()
             WHERE session_id = $1
            """,
            session_id, reason,
        )
        await audit.record(con, tenant_id=tenant_id, actor=actor, event=audit.HARD_OFF, reason=reason,
                           payload={"session_id": session_id, "logged_out": logged_out, "how": how})
    await alerts.raise_alert(
        pool, tenant_id=tenant_id, kind="hard_off", severity=alerts.CRITICAL,
        message=f"Session revoked ({reason}). "
                + ("Telegram logged it out." if logged_out else
                   "Telegram could NOT be told: end the session under Settings → Devices on the phone."),
        payload={"session_id": session_id, "how": how},
    )
    return {"session_id": session_id, "logged_out": logged_out, "how": how}


# ---------------------------------------------------------------- the shell


async def _cli(argv: list[str]) -> int:
    import pg

    if len(argv) < 1 or argv[0] not in ("stop", "resume", "status"):
        print('usage: python controls.py stop "reason" | resume | status', file=sys.stderr)
        return 2
    pool = await pg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    bus = None
    try:
        if argv[0] == "status":
            print(json.dumps(await global_stop(pool)))
            return 0
        if os.getenv("REDIS_URL"):
            bus = await commands.CommandBus.connect(os.environ["REDIS_URL"])
        on = argv[0] == "stop"
        reason = " ".join(argv[1:]).strip() or ("" if on else "resumed from the shell")
        state = await set_global_stop(pool, on, reason=reason, actor="admin (shell)")
        await reload_controls(pool, bus)
        print(json.dumps(state))
        return 0
    finally:
        if bus is not None:
            await bus.close()
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_cli(sys.argv[1:])))
