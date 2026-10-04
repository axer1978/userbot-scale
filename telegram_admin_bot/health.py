"""Is every account well? The health record per tenant, and the watchdog.

The running account writes facts to sessions_health (migration 0004):
when it last confirmed it was connected, its last error, how long
Telegram asked it to wait, and which Telegram logins the account has.

The scheduler runs `check_all()` on every tick (once a minute) and turns
those facts, plus the lease and state in telegram_sessions, into one status
per tenant:

  ok            connected and seen in the last few minutes
  not_running   active, but no worker has held it for STALE_SECONDS
                (the manager is down, or it keeps failing to start)
  disconnected  a worker holds it, but it has not been connected to
                Telegram for STALE_SECONDS
  logged_out    Telegram no longer accepts the session (needs a new login)
  rate_limited  Telegram asked it to wait, and that wait is not over
  revoked       hard-off; no alert (a person did it)
  stopped       deactivated; no alert

A change to a bad status opens an alert (kind "health:<status>"), so the
operator hears within about STALE_SECONDS + one tick (under 5 minutes). A
change back to ok closes it and sends a short "back to normal".

Short FloodWaits that Telethon sleeps through by itself (under
safety.max_flood_wait_seconds) never reach this code and are not reported.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

import alerts

log = logging.getLogger("health")

STALE_SECONDS = 180

OK = "ok"
NOT_RUNNING = "not_running"
DISCONNECTED = "disconnected"
LOGGED_OUT = "logged_out"
RATE_LIMITED = "rate_limited"
REVOKED = "revoked"
STOPPED = "stopped"
UNKNOWN = "unknown"

BAD = {
    NOT_RUNNING: (alerts.CRITICAL, "The account is not running anywhere: no worker has picked it up for {m} min."),
    DISCONNECTED: (alerts.CRITICAL, "The account is not connected to Telegram ({m} min). Last error: {error}"),
    LOGGED_OUT: (alerts.CRITICAL, "Telegram no longer accepts this session: it was logged out or revoked. "
                                  "Sign it in again from the panel."),
    RATE_LIMITED: (alerts.WARNING, "Telegram is rate-limiting this account until {until}."),
}

_UPSERT = """
INSERT INTO sessions_health (tenant_id, session_id) VALUES ($1, $2)
ON CONFLICT (tenant_id) DO UPDATE SET session_id = EXCLUDED.session_id
"""


# --------------------------------------------------- written by the account


async def seen(pool: asyncpg.Pool, tenant_id: int, session_id: str) -> None:
    """The account is connected right now."""
    await pool.execute(_UPSERT, tenant_id, session_id)
    await pool.execute("UPDATE sessions_health SET last_seen_at = now() WHERE tenant_id = $1", tenant_id)


async def error(pool: asyncpg.Pool, tenant_id: int, session_id: str, text: str) -> None:
    await pool.execute(_UPSERT, tenant_id, session_id)
    await pool.execute(
        "UPDATE sessions_health SET last_error = $2, last_error_at = now() WHERE tenant_id = $1",
        tenant_id, text[:500],
    )


async def rate_limited(pool: asyncpg.Pool, tenant_id: int, session_id: str, seconds: int) -> None:
    await pool.execute(_UPSERT, tenant_id, session_id)
    await pool.execute(
        "UPDATE sessions_health SET rate_limited_until = GREATEST(COALESCE(rate_limited_until, now()), "
        "now() + make_interval(secs => $2)) WHERE tenant_id = $1",
        tenant_id, float(seconds),
    )


async def known_logins(pool: asyncpg.Pool, tenant_id: int) -> Optional[list[dict[str, Any]]]:
    """The account's Telegram logins as last seen; None before the first check."""
    value = await pool.fetchval("SELECT known_session_ids_json FROM sessions_health WHERE tenant_id = $1", tenant_id)
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


async def save_logins(pool: asyncpg.Pool, tenant_id: int, session_id: str, logins: list[dict[str, Any]]) -> None:
    await pool.execute(_UPSERT, tenant_id, session_id)
    await pool.execute(
        "UPDATE sessions_health SET known_session_ids_json = $2::jsonb, authorizations_checked_at = now() "
        "WHERE tenant_id = $1",
        tenant_id, json.dumps(logins, default=str),
    )


async def logins_checked_at(pool: asyncpg.Pool, tenant_id: int) -> Optional[datetime]:
    return await pool.fetchval("SELECT authorizations_checked_at FROM sessions_health WHERE tenant_id = $1",
                               tenant_id)


# --------------------------------------------------------------- the watchdog


def status_of(row: dict[str, Any], now: datetime) -> tuple[str, str]:
    """(status, detail) for one tenant's account, from its telegram_sessions
    and sessions_health columns."""
    stale = timedelta(seconds=STALE_SECONDS)
    if row["state"] == "revoked":
        return REVOKED, ""
    if not row["is_active"]:
        return STOPPED, ""
    if row["state"] == "needs_login":
        return LOGGED_OUT, row["state_reason"] or ""
    lease_live = row["lease_expires_at"] is not None and row["lease_expires_at"] > now
    if not lease_live:
        since = row["lease_seen_at"] or row["activated_at"]
        if since is None or now - since > stale:
            minutes = int((now - since).total_seconds() // 60) if since else STALE_SECONDS // 60
            return NOT_RUNNING, str(minutes)
        return UNKNOWN, ""
    seen_at = row["last_seen_at"]
    if row["rate_limited_until"] is not None and row["rate_limited_until"] > now:
        return RATE_LIMITED, row["rate_limited_until"].isoformat(timespec="minutes")
    if seen_at is None or now - seen_at > stale:
        # A lease younger than STALE_SECONDS is still connecting.
        started = row["lease_seen_at"]
        if seen_at is None and started is not None and now - started <= stale and row["state"] != "running":
            return UNKNOWN, ""
        minutes = int((now - seen_at).total_seconds() // 60) if seen_at else STALE_SECONDS // 60
        return DISCONNECTED, str(minutes)
    return OK, ""


async def check_all(pool: asyncpg.Pool, now: Optional[datetime] = None) -> dict[int, str]:
    """One watchdog round. Returns tenant id -> status (for the log and tests)."""
    now = now or await pool.fetchval("SELECT now()")
    rows = await pool.fetch(
        """
        SELECT t.id AS tenant_id, t.session_id, s.is_active, s.state, s.state_reason,
               s.lease_expires_at, s.last_seen_at AS lease_seen_at, s.updated_at AS activated_at,
               h.last_seen_at, h.last_error, h.rate_limited_until, h.status AS old_status
          FROM tenants t
          JOIN telegram_sessions s ON s.session_id = t.session_id
          LEFT JOIN sessions_health h ON h.tenant_id = t.id
         WHERE s.auth_key_enc IS NOT NULL OR s.state IN ('needs_login', 'revoked')
            OR EXISTS (SELECT 1 FROM wa_auth_state a WHERE a.session_id = s.session_id AND a.kind = 'creds')
        """
    )
    out: dict[int, str] = {}
    for record in rows:
        row = dict(record)
        status, detail = status_of(row, now)
        out[row["tenant_id"]] = status
        if status == UNKNOWN:
            continue
        old = row["old_status"] or UNKNOWN
        if status == old:
            continue
        await pool.execute(_UPSERT, row["tenant_id"], row["session_id"])
        await pool.execute(
            "UPDATE sessions_health SET status = $2, status_reason = $3, status_since = now() WHERE tenant_id = $1",
            row["tenant_id"], status, detail,
        )
        await _announce(pool, row, old, status, detail)
    return out


async def _announce(pool: asyncpg.Pool, row: dict[str, Any], old: str, new: str, detail: str) -> None:
    tenant_id = row["tenant_id"]
    if old in BAD:
        await alerts.resolve(pool, tenant_id=tenant_id, kind=f"health:{old}",
                             by="system: " + ("recovered" if new == OK else f"now {new}"))
    if new in BAD:
        severity, template = BAD[new]
        message = template.format(m=detail, until=detail, error=row["last_error"] or "none recorded")
        await alerts.raise_alert(pool, tenant_id=tenant_id, kind=f"health:{new}", severity=severity,
                                 message=message, payload={"status": new, "previous": old})
    elif new == OK and old in BAD:
        await alerts.notify(pool, tenant_id=tenant_id, text=f"Back to normal (was {old.replace('_', ' ')}).")
    log.info("Tenant %s health: %s -> %s %s", tenant_id, old, new, detail)


async def overview(pool: asyncpg.Pool, tenant_id: int) -> dict[str, Any]:
    row = await pool.fetchrow("SELECT * FROM sessions_health WHERE tenant_id = $1", tenant_id)
    if row is None:
        return {"status": UNKNOWN, "logins": None}

    def iso(value):
        return value.isoformat(timespec="seconds") if value else None

    logins = row["known_session_ids_json"]
    return {
        "status": row["status"],
        "status_reason": row["status_reason"],
        "status_since": iso(row["status_since"]),
        "last_seen_at": iso(row["last_seen_at"]),
        "last_error": row["last_error"],
        "last_error_at": iso(row["last_error_at"]),
        "rate_limited_until": iso(row["rate_limited_until"]),
        "logins": json.loads(logins) if isinstance(logins, str) else logins,
        "logins_checked_at": iso(row["authorizations_checked_at"]),
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
