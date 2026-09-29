"""Alerts for the operator: something on the platform needs a person.

Raised by the running accounts (a Telegram error, an anomaly, a spend
cap), by the scheduler's watchdog (health.py, billing.py) and by the panel
(a hard-off). Every alert is stored in Postgres and shown in the panel under
Safety → Alerts. It is also delivered, when .env says where:

  ALERT_EMAIL        an e-mail address; needs the SMTP_* settings that the
                     booking e-mail record uses (mailer.py)
  ALERT_WEBHOOK_URL  an https URL that gets a JSON POST:
                     {"text", "content", "kind", "severity", "tenant_id",
                      "tenant", "alert_id"}  ("content" repeats "text" for
                     services that read that key instead)

There is at most one open alert per (tenant, kind). Raising it again while
it is open only counts the repeat (count, last_at): it is not delivered
again, so a failure that repeats every minute is one message, not sixty.
Acknowledging it in the panel closes it; the next occurrence is new.

Delivery never raises and never blocks the caller for long: it runs in the
background, and a failure is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import asyncpg
import httpx

import mailer

log = logging.getLogger("alerts")

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"

DELIVERY_TIMEOUT_SECONDS = 15.0

# Delivery tasks still running, so they are not garbage-collected half way
# and tests can wait for them (drain()).
_pending: set[asyncio.Task] = set()


def _row(row: asyncpg.Record) -> dict[str, Any]:
    payload = row["payload"]
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "kind": row["kind"],
        "severity": row["severity"],
        "message": row["message"],
        "payload": json.loads(payload) if isinstance(payload, str) else payload,
        "count": row["count"],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
        "last_at": row["last_at"].isoformat(timespec="seconds"),
        "acknowledged_at": row["acknowledged_at"].isoformat(timespec="seconds") if row["acknowledged_at"] else None,
        "acknowledged_by": row["acknowledged_by"],
    }


async def raise_alert(
    pool: asyncpg.Pool,
    *,
    tenant_id: Optional[int],
    kind: str,
    message: str,
    severity: str = WARNING,
    payload: Optional[dict[str, Any]] = None,
    deliver: bool = True,
) -> dict[str, Any]:
    """Open an alert, or count a repeat of the open one. Returns the row,
    with "new" True when it was opened now (and so delivered)."""
    row = await pool.fetchrow(
        """
        INSERT INTO alerts (tenant_id, kind, severity, message, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT ((COALESCE(tenant_id, 0)), kind) WHERE acknowledged_at IS NULL
        DO UPDATE SET count = alerts.count + 1, last_at = now()
        RETURNING *, (xmax = 0) AS inserted
        """,
        tenant_id, kind, severity, message, json.dumps(payload or {}, default=str),
    )
    alert = {**_row(row), "new": bool(row["inserted"])}
    if alert["new"]:
        log.warning("ALERT [%s] tenant=%s %s: %s", severity, tenant_id, kind, message)
        if deliver:
            _background(_deliver(pool, alert))
    return alert


async def resolve(pool: asyncpg.Pool, *, tenant_id: Optional[int], kind: str, by: str) -> Optional[dict[str, Any]]:
    """Close the open alert of this kind, when its cause went away by
    itself (the account reconnected). Returns it, or None if none was open."""
    row = await pool.fetchrow(
        "UPDATE alerts SET acknowledged_at = now(), acknowledged_by = $3 "
        "WHERE COALESCE(tenant_id, 0) = COALESCE($1::int, 0) AND kind = $2 AND acknowledged_at IS NULL RETURNING *",
        tenant_id, kind, by,
    )
    return _row(row) if row else None


async def acknowledge(pool: asyncpg.Pool, alert_id: int, *, by: str) -> Optional[dict[str, Any]]:
    row = await pool.fetchrow(
        "UPDATE alerts SET acknowledged_at = COALESCE(acknowledged_at, now()), "
        "acknowledged_by = COALESCE(acknowledged_by, $2) WHERE id = $1 RETURNING *",
        alert_id, by,
    )
    return _row(row) if row else None


async def acknowledge_all(pool: asyncpg.Pool, *, by: str, tenant_id: Optional[int] = None) -> int:
    if tenant_id is None:
        result = await pool.execute(
            "UPDATE alerts SET acknowledged_at = now(), acknowledged_by = $1 WHERE acknowledged_at IS NULL", by,
        )
    else:
        result = await pool.execute(
            "UPDATE alerts SET acknowledged_at = now(), acknowledged_by = $1 "
            "WHERE acknowledged_at IS NULL AND tenant_id = $2", by, tenant_id,
        )
    return int(result.split()[-1])


async def list_alerts(
    pool: asyncpg.Pool, *, open_only: bool = False, tenant_id: Optional[int] = None, limit: int = 200,
) -> list[dict[str, Any]]:
    where, args = [], []
    if open_only:
        where.append("acknowledged_at IS NULL")
    if tenant_id is not None:
        args.append(tenant_id)
        where.append(f"tenant_id = ${len(args)}")
    args.append(limit)
    rows = await pool.fetch(
        "SELECT * FROM alerts" + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY acknowledged_at IS NOT NULL, last_at DESC LIMIT ${len(args)}",
        *args,
    )
    return [_row(r) for r in rows]


async def open_count(pool: asyncpg.Pool) -> dict[str, int]:
    row = await pool.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE severity = 'critical') AS critical "
        "FROM alerts WHERE acknowledged_at IS NULL"
    )
    return {"total": row["total"], "critical": row["critical"]}


# ----------------------------------------------------------------- delivery


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for deliveries still running (tests, and a clean shutdown)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


async def notify(pool: asyncpg.Pool, *, tenant_id: Optional[int], text: str, severity: str = INFO,
                 kind: str = "notice") -> None:
    """Deliver a message without opening an alert (e.g. "back to normal")."""
    _background(_deliver(pool, {
        "id": None, "tenant_id": tenant_id, "kind": kind, "severity": severity, "message": text,
    }))


async def _tenant_name(pool: asyncpg.Pool, tenant_id: Optional[int]) -> str:
    if tenant_id is None:
        return "Platform"
    try:
        name = await pool.fetchval("SELECT name FROM tenants WHERE id = $1", tenant_id)
    except Exception:
        name = None
    return name or f"Client {tenant_id}"


def format_text(alert: dict[str, Any], tenant: str) -> str:
    return f"[{alert['severity'].upper()}] {tenant}: {alert['message']}"


async def _deliver(pool: asyncpg.Pool, alert: dict[str, Any]) -> None:
    try:
        tenant = await _tenant_name(pool, alert.get("tenant_id"))
        text = format_text(alert, tenant)
        await asyncio.gather(_email(text, alert, tenant), _webhook(text, alert, tenant))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Alert delivery failed")


async def _email(text: str, alert: dict[str, Any], tenant: str) -> None:
    to = (os.getenv("ALERT_EMAIL") or "").strip()
    if not to:
        return
    try:
        # Inside the try: SMTP settings that don't parse must not turn
        # into an exception nobody retrieves in this background task.
        settings = mailer.settings_from_env()
        if settings is None:
            return
        await mailer.send(
            settings, to=to, subject=f"[{alert['severity']}] {tenant}: {alert['kind']}", body=text + "\n",
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # mailer.MailError, or anything unexpected
        log.warning("Alert e-mail not sent: %s", exc)


async def _webhook(text: str, alert: dict[str, Any], tenant: str) -> None:
    url = (os.getenv("ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return
    body = {
        "text": text, "content": text, "kind": alert["kind"], "severity": alert["severity"],
        "tenant_id": alert.get("tenant_id"), "tenant": tenant, "alert_id": alert.get("id"),
    }
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=body)
        if response.status_code >= 400:
            log.warning("Alert webhook answered %s", response.status_code)
    except Exception as exc:
        log.warning("Alert webhook failed: %s", type(exc).__name__)
