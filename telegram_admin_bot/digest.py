"""The weekly digest: a short summary of last week for each client's owner.

Once a week, at `digest.weekday` / `digest.hour` in the client's own
timezone (Monday 09:00 by default), the owner gets the previous full week
(Monday to Monday, stats.py) in numbers: bookings and no-shows, messages,
how many the bot answered, the unanswered queue, and what is confirmed for
the coming seven days. The text is fixed here in code; no model writes it.

It goes to the owner's Telegram (`booking.provider`, sent from the client's
own account over the bus, like billing's notice) and, when SMTP is set up
(mailer.py), by e-mail to `digest.email` or else `booking.owner_email`.

Idempotent, at most once: `tick()` runs on the scheduler once a minute and
claims the week by inserting its `digest_log` row *before* sending, so a
second tick, a restart or a second scheduler never sends it twice. The
price is that a send that fails is not retried; the row says how it went
(`sent_via`), and when neither channel reached the owner the operator gets
an alert (kind "digest", one open per client).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

import alerts
import commands
import mailer
import stats
import tenant_config
import unanswered

log = logging.getLogger("digest")

NOTICE_TIMEOUT = 45.0
ALERT_KIND = "digest"


def _parsed(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else (value or {})


async def effective_config(pool: asyncpg.Pool, tenant_id: int) -> Optional[tenant_config.TenantConfig]:
    """The client's effective config (industry defaults + its overrides),
    resolved the way billing._timezone does it. None when what is stored no
    longer validates (edited by hand): no digest rather than a wrong one."""
    row = await pool.fetchrow(
        "SELECT i.default_config, t.config_json FROM tenants t JOIN industries i ON i.id = t.industry_id "
        "WHERE t.id = $1", tenant_id,
    )
    if row is None:
        return None
    try:
        return tenant_config.resolve(_parsed(row["default_config"]), _parsed(row["config_json"])).config
    except tenant_config.ConfigError as exc:
        log.warning("Tenant %s: config does not validate, no digest: %s", tenant_id, exc)
        return None


def due_at(now: datetime, cfg: tenant_config.TenantConfig) -> datetime:
    """When this week's digest is due: the configured weekday and hour of
    the week containing `now`, in the client's zone (wall-clock time, so a
    DST change doesn't move it)."""
    return stats.week_start(now, cfg.timezone) + timedelta(days=cfg.digest.weekday, hours=cfg.digest.hour)


def digest_text(business: str, since: datetime, until: datetime, week: dict[str, Any], *,
                open_now: int, upcoming: int) -> str:
    """The message. Numbers only, fixed wording."""
    b, m = week["bookings"], week["messages"]
    last_day = until - timedelta(days=1)
    return (
        f"Weekly summary for {business}, {since:%d.%m}–{last_day:%d.%m}: "
        f"{b['booked']} bookings ({b['no_show']} no-shows, {b['cancelled']} cancelled), "
        f"{m['received']} messages from {m['conversations']} people, "
        f"the bot answered with {m['sent_by_bot']} messages, "
        f"{week['unanswered']} unanswered (open now: {open_now}). "
        f"Confirmed for the next 7 days: {upcoming}."
    )


async def _upcoming_confirmed(pool: asyncpg.Pool, tenant_id: int, now: datetime) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM bookings WHERE tenant_id = $1 AND state = 'confirmed' "
        "AND starts_at >= $2 AND starts_at < $3",
        tenant_id, now, now + timedelta(days=7),
    )


async def _running(pool: asyncpg.Pool, session_id: str) -> bool:
    return bool(await pool.fetchval(
        "SELECT lease_expires_at > now() FROM telegram_sessions WHERE session_id = $1", session_id,
    ))


async def _by_telegram(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], session_id: str,
                       text: str) -> str:
    """Send over the client's account. "" when sent, else why not."""
    if bus is None:
        return "no command bus"
    # One nobody runs would only time out, holding up the scheduler.
    if not await _running(pool, session_id):
        return "the account is not running"
    try:
        result = await bus.dispatch(session_id, "owner_notice", {"text": text, "reason": "weekly digest"},
                                    timeout=NOTICE_TIMEOUT)
    except commands.CommandError as exc:
        return f"the account did not answer ({type(exc).__name__})"
    if (result or {}).get("sent"):
        return ""
    return (result or {}).get("error") or "the owner could not be reached (booking.provider)"


async def _by_email(cfg: tenant_config.TenantConfig, business: str, since: datetime, until: datetime,
                    text: str) -> str:
    """Send by e-mail. "" when sent, else why not."""
    to = cfg.digest.email or cfg.booking.owner_email
    if not to:
        return "no e-mail address (digest.email / booking.owner_email)"
    try:
        settings = mailer.settings_from_env()
    except mailer.MailError as exc:
        return f"SMTP is misconfigured: {exc}"
    if settings is None:
        return "SMTP is not set up"
    last_day = until - timedelta(days=1)
    try:
        await mailer.send(settings, to=to, subject=f"Weekly summary: {business}, {since:%d.%m}–{last_day:%d.%m}",
                          body=text)
    except mailer.MailError as exc:
        return f"e-mail failed: {exc}"
    return ""


async def send_for(pool: asyncpg.Pool, bus: Optional[commands.CommandBus], tenant: asyncpg.Record,
                   now: datetime) -> Optional[str]:
    """This client's digest, if it is due and not sent yet. Returns
    `sent_via` when one was sent (or tried), None when nothing was due."""
    cfg = await effective_config(pool, tenant["id"])
    if cfg is None or not cfg.digest.enabled or now < due_at(now, cfg):
        return None
    this_week = stats.week_start(now, cfg.timezone)
    # The same wall-clock Monday a week earlier.
    last_week = this_week - timedelta(days=7)
    claimed = await pool.fetchval(
        "INSERT INTO digest_log (tenant_id, week_start) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING true",
        tenant["id"], last_week.date(),
    )
    if not claimed:
        return None

    week = await stats.period(pool, tenant["id"], last_week, this_week)
    text = digest_text(
        tenant["name"], last_week, this_week, week,
        open_now=await unanswered.open_count(pool, tenant["id"]),
        upcoming=await _upcoming_confirmed(pool, tenant["id"], now),
    )
    problems: list[str] = []
    via: list[str] = []
    for channel, problem in (
        ("telegram", await _by_telegram(pool, bus, tenant["session_id"], text)),
        ("email", await _by_email(cfg, tenant["name"], last_week, this_week, text)),
    ):
        if problem:
            problems.append(f"{channel}: {problem}")
        else:
            via.append(channel)
    sent_via = ",".join(via) if via else "none: " + "; ".join(problems)
    await pool.execute(
        "UPDATE digest_log SET sent_via = $3 WHERE tenant_id = $1 AND week_start = $2",
        tenant["id"], last_week.date(), sent_via[:500],
    )
    if via:
        log.info("Tenant %s: weekly digest sent (%s).", tenant["id"], sent_via)
        await alerts.resolve(pool, tenant_id=tenant["id"], kind=ALERT_KIND, by="system: digest sent")
    else:
        await alerts.raise_alert(
            pool, tenant_id=tenant["id"], kind=ALERT_KIND, severity=alerts.WARNING,
            message=f"The weekly summary for {last_week:%d.%m}–{(this_week - timedelta(days=1)):%d.%m} "
                    f"reached the owner by neither Telegram nor e-mail ({'; '.join(problems)}). "
                    "It is not retried; the next one goes out next week.",
        )
    return sent_via


async def tick(pool: asyncpg.Pool, bus: Optional[commands.CommandBus],
               now: Optional[datetime] = None) -> dict[int, str]:
    """Every client with an account whose digest is due. Returns tenant id
    -> sent_via for the ones handled now. One client failing does not stop
    the others."""
    now = now or datetime.now(timezone.utc)
    done: dict[int, str] = {}
    for tenant in await pool.fetch(
        "SELECT id, name, session_id FROM tenants WHERE session_id IS NOT NULL ORDER BY id"
    ):
        try:
            sent_via = await send_for(pool, bus, tenant, now)
        except Exception:
            log.exception("Weekly digest for tenant %s failed", tenant["id"])
            continue
        if sent_via is not None:
            done[tenant["id"]] = sent_via
    return done
