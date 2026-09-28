"""Anomalies that switch a tenant off by themselves (config `anomaly.*`).

Three triggers, each checked in code:

- a new Telegram login on the account: a login (authorization) that was
  not there at the last check. The first check only records what exists.
- send volume: the messages the account sent in the last hour against its
  own average hour over the last `volume_baseline_days` days. Trips at
  `volume_multiplier` times that average, and never below
  `volume_min_messages`. Every outgoing message counts, including ones
  typed by hand on a phone: a hijacked account sending spam shows up here.
- the outbound trip-wire: a reply the bot wrote links to a domain nobody
  allowed, or contains a wallet address or an IBAN the business never
  wrote (policy.py). The reply is already held; this also stops the rest.

A trip adds the tenant's 'anomaly' hold (soft-off, controls.py), writes an
`anomaly_detected` audit row with the reason, and alerts the operator. A
person looks and resumes; nothing resumes by itself.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import asyncpg

NEW_LOGIN = "new_login"
VOLUME = "volume"
TRIPWIRE = "tripwire"


def login_record(auth: Any) -> dict[str, Any]:
    """The parts of a Telegram Authorization worth keeping: enough to tell
    logins apart and to recognise one, nothing more (no IP address)."""
    created = getattr(auth, "date_created", None)
    return {
        "hash": int(getattr(auth, "hash", 0) or 0),
        "current": bool(getattr(auth, "current", False)),
        "device": getattr(auth, "device_model", "") or "",
        "platform": getattr(auth, "platform", "") or "",
        "app": " ".join(p for p in (getattr(auth, "app_name", ""), getattr(auth, "app_version", "")) if p),
        "country": getattr(auth, "country", "") or "",
        "created": created.isoformat(timespec="seconds") if created else None,
    }


def new_logins(known: Optional[Iterable[dict[str, Any]]], current: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Logins in `current` that were not in `known`. Nothing is new on the
    first check (known None). The account's own session (hash 0 / current)
    never counts."""
    if known is None:
        return []
    seen = {int(k["hash"]) for k in known}
    return [c for c in current if not c.get("current") and int(c["hash"]) and int(c["hash"]) not in seen]


def describe_login(login: dict[str, Any]) -> str:
    bits = [login.get("device") or "unknown device", login.get("platform"), login.get("app"), login.get("country")]
    return ", ".join(b for b in bits if b)


async def volume_spike(pool: asyncpg.Pool, tenant_id: int, settings: dict[str, Any]) -> str:
    """Why the last hour's send volume is anomalous, or ""."""
    multiplier = float(settings.get("volume_multiplier") or 0)
    if multiplier <= 0:
        return ""
    days = int(settings["volume_baseline_days"])
    row = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 hour') AS last_hour,
               count(*) FILTER (WHERE created_at <  now() - interval '1 hour') AS before,
               EXTRACT(EPOCH FROM (now() - interval '1 hour') - min(created_at)) / 3600 AS hours
          FROM messages
         WHERE tenant_id = $1 AND direction = 'out' AND status = 'sent'
           AND created_at >= now() - make_interval(days => $2)
        """,
        tenant_id, days,
    )
    last_hour = int(row["last_hour"])
    floor = int(settings["volume_min_messages"])
    if last_hour < floor:
        return ""
    hours = max(1.0, float(row["hours"] or 0))
    average = int(row["before"]) / hours if row["before"] else 0.0
    threshold = max(floor, multiplier * average)
    if last_hour < threshold:
        return ""
    return (f"{last_hour} messages sent in the last hour, against an average of {average:.1f} an hour "
            f"over the last {days} days (limit {multiplier:g}× and at least {floor})")
