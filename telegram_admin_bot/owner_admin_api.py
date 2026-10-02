"""Admin API for client logins: create, disable, reset, link tenants.
Mounted by panel.py behind the admin login (require_auth), so only the
platform admin reaches any of it.

A client login ("owner", owner_auth.py) is one username linked to one or
more tenants. The admin sets a temporary password; the owner must change it
at the first login. Nothing here ever returns a password hash or a TOTP
secret. Every change writes an audit row per linked tenant (a platform row
when none is linked), actor "admin".

Logins people made themselves (sign-up, owner_api.py) wait as 'pending'
until approve() or reject() here; manager_api.py calls the same two
functions with its own actor, but can't link businesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Iterable, Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import alerts
import audit
import mailer
import owner_auth
import terms

log = logging.getLogger("owner_admin_api")

router = APIRouter()
ACTOR = audit.ADMIN

EVENT_CREATED = "owner_created"
EVENT_UPDATED = "owner_updated"
EVENT_PASSWORD_RESET = "owner_password_reset"
EVENT_TOTP_REMOVED = "owner_totp_removed"
EVENT_DELETED = "owner_deleted"
EVENT_APPROVED = "owner_approved"
EVENT_REJECTED = "owner_rejected"

# The platform alert a sign-up raises (owner_api.py); closed once nobody waits.
ALERT_SIGNUP = "signup_pending"

# Letters, digits and . _ @ + -: fits an e-mail address or a short name, and
# nothing that looks different from what it is.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{3,64}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


_SELECT = """
SELECT o.id, o.username, o.display_name, o.must_change_password, o.disabled,
       o.totp_secret_enc IS NOT NULL AS totp, o.last_login_at, o.created_by, o.created_at, o.updated_at,
       o.status, o.email, o.company, o.phone, o.reviewed_by, o.reviewed_at, o.review_reason,
       (SELECT max(version) FROM terms_acceptances a WHERE a.owner_id = o.id) AS terms_accepted,
       (SELECT count(*) FROM owner_sessions s WHERE s.owner_id = o.id AND s.expires_at > now()) AS sessions,
       coalesce((SELECT json_agg(json_build_object('id', t.id, 'name', t.name) ORDER BY lower(t.name), t.id)
                   FROM owner_tenants ot JOIN tenants t ON t.id = ot.tenant_id
                  WHERE ot.owner_id = o.id), '[]'::json) AS tenants
  FROM owners o
"""


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _owner(row: asyncpg.Record) -> dict[str, Any]:
    linked = row["tenants"]
    linked = json.loads(linked) if isinstance(linked, str) else (linked or [])
    return {
        "id": row["id"], "username": row["username"], "display_name": row["display_name"],
        "must_change_password": row["must_change_password"], "disabled": row["disabled"],
        "totp": row["totp"], "sessions": row["sessions"],
        "last_login_at": _iso(row["last_login_at"]), "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
        "tenants": linked, "tenant_ids": sorted(t["id"] for t in linked),
        "status": row["status"], "email": row["email"], "company": row["company"], "phone": row["phone"],
        "reviewed_by": row["reviewed_by"], "reviewed_at": _iso(row["reviewed_at"]),
        "review_reason": row["review_reason"], "terms_accepted": row["terms_accepted"],
    }


async def _get(executor: Any, owner_id: int) -> dict[str, Any]:
    row = await executor.fetchrow(_SELECT + " WHERE o.id = $1", owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown client login")
    return _owner(row)


async def _check_tenants(executor: Any, tenant_ids: Iterable[int]) -> list[int]:
    """The ids, deduplicated and sorted; 400 naming any that don't exist."""
    ids = sorted(set(tenant_ids))
    if not ids:
        return ids
    found = {r["id"] for r in await executor.fetch("SELECT id FROM tenants WHERE id = ANY($1)", ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown client(s): {', '.join(map(str, missing))}")
    return ids


async def _link(con: asyncpg.Connection, owner_id: int, tenant_ids: list[int]) -> None:
    await con.execute("DELETE FROM owner_tenants WHERE owner_id = $1 AND NOT (tenant_id = ANY($2))",
                      owner_id, tenant_ids)
    await con.executemany(
        "INSERT INTO owner_tenants (owner_id, tenant_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        [(owner_id, t) for t in tenant_ids],
    )


async def _audit(con: Any, tenant_ids: Iterable[int], event: str, reason: str, payload: dict[str, Any]) -> None:
    for tenant_id in sorted(set(tenant_ids)) or [None]:
        await audit.record(con, tenant_id=tenant_id, actor=ACTOR, event=event, reason=reason, payload=payload)


def _check_password(password: str) -> None:
    try:
        owner_auth.check_new_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ------------------------------------------------- approving sign-ups

# Background e-mail tasks, kept so they are not garbage-collected half way.
_mail_tasks: set[asyncio.Task] = set()


async def _mail(to: str, subject: str, body: str) -> None:
    """Best effort, when SMTP is set up (mailer.py); never raises."""
    try:
        settings = mailer.settings_from_env()
        if settings is None or not to:
            return
        await mailer.send(settings, to=to, subject=subject, body=body, timeout=15.0)
    except Exception as exc:  # mailer.MailError, or anything unexpected
        log.warning("Sign-up e-mail not sent: %s", exc)


def _mail_later(to: str, subject: str, body: str) -> None:
    task = asyncio.create_task(_mail(to, subject, body))
    _mail_tasks.add(task)
    task.add_done_callback(_mail_tasks.discard)


async def _close_signup_alert(pool: Any, actor: str) -> None:
    if not await pool.fetchval("SELECT count(*) FROM owners WHERE status = 'pending'"):
        await alerts.resolve(pool, tenant_id=None, kind=ALERT_SIGNUP, by=actor)


async def approve(pool: Any, owner_id: int, *, actor: str, reason: str = "",
                  tenant_ids: Optional[list[int]] = None) -> dict[str, Any]:
    """A pending (or earlier rejected) login becomes active; with
    tenant_ids, also linked to those businesses."""
    async with pool.acquire() as con, con.transaction():
        owner = await _get(con, owner_id)
        if owner["status"] == owner_auth.ACTIVE:
            raise HTTPException(status_code=409, detail="This login is already approved.")
        ids = await _check_tenants(con, tenant_ids) if tenant_ids is not None else owner["tenant_ids"]
        await con.execute(
            "UPDATE owners SET status = 'active', reviewed_by = $2, reviewed_at = now(), review_reason = $3, "
            "updated_at = now() WHERE id = $1",
            owner_id, actor, reason.strip(),
        )
        if tenant_ids is not None:
            await _link(con, owner_id, ids)
        for tenant_id in ids or [None]:
            await audit.record(con, tenant_id=tenant_id, actor=actor, event=EVENT_APPROVED,
                               reason=reason.strip() or "sign-up approved",
                               payload={"owner_id": owner_id, "username": owner["username"], "tenant_ids": ids})
    await _close_signup_alert(pool, actor)
    _mail_later(owner["email"], "Your account is approved",
                f"Hello {owner['display_name'] or owner['username']},\n\nyour account ({owner['username']}) "
                "is approved. You can sign in to your dashboard now.\n")
    return await _get(pool, owner_id)


async def reject(pool: Any, owner_id: int, *, actor: str, reason: str) -> dict[str, Any]:
    """A pending login is turned down; it can still sign in to read why."""
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Give a reason: the applicant sees it.")
    async with pool.acquire() as con, con.transaction():
        owner = await _get(con, owner_id)
        if owner["status"] != owner_auth.PENDING:
            raise HTTPException(status_code=409, detail="Only a login waiting for approval can be rejected.")
        await con.execute(
            "UPDATE owners SET status = 'rejected', reviewed_by = $2, reviewed_at = now(), review_reason = $3, "
            "updated_at = now() WHERE id = $1",
            owner_id, actor, reason,
        )
        await audit.record(con, tenant_id=None, actor=actor, event=EVENT_REJECTED, reason=reason,
                           payload={"owner_id": owner_id, "username": owner["username"]})
    await _close_signup_alert(pool, actor)
    _mail_later(owner["email"], "Your account request",
                f"Hello {owner['display_name'] or owner['username']},\n\nwe could not approve your account "
                f"({owner['username']}).\n\nReason: {reason}\n")
    return await _get(pool, owner_id)


async def set_disabled(pool: Any, owner_id: int, disabled: bool, *, actor: str, reason: str = "") -> dict[str, Any]:
    """Disabling ends every session of the owner at once."""
    async with pool.acquire() as con, con.transaction():
        before = await _get(con, owner_id)
        if before["disabled"] != disabled:
            await con.execute("UPDATE owners SET disabled = $2, updated_at = now() WHERE id = $1", owner_id, disabled)
            if disabled:
                await owner_auth.kill_sessions(con, owner_id)
            await _audit_as(con, actor, before["tenant_ids"], EVENT_UPDATED, reason or "client login changed",
                            {"owner_id": owner_id, "username": before["username"],
                             "changes": {"disabled": disabled}})
    return await _get(pool, owner_id)


async def _audit_as(con: Any, actor: str, tenant_ids: Iterable[int], event: str, reason: str,
                    payload: dict[str, Any]) -> None:
    for tenant_id in sorted(set(tenant_ids)) or [None]:
        await audit.record(con, tenant_id=tenant_id, actor=actor, event=event, reason=reason, payload=payload)


# ------------------------------------------------------------------ routes


@router.get("/api/owners")
async def api_list() -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(_SELECT + " ORDER BY o.status <> 'pending', lower(o.username)")
    return [_owner(r) for r in rows]


class CreateBody(BaseModel):
    username: str = Field(..., max_length=64)
    display_name: str = Field("", max_length=200)
    password: str = Field(..., max_length=1000)
    tenant_ids: list[int] = Field(default_factory=list)


@router.post("/api/owners")
async def api_create(body: CreateBody) -> dict[str, Any]:
    """The password is temporary: the owner must change it on first login."""
    username = body.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400,
                            detail="Username: 3 to 64 letters, digits or . _ @ - (an e-mail address works).")
    _check_password(body.password)
    pool = _get_pool()
    try:
        async with pool.acquire() as con, con.transaction():
            ids = await _check_tenants(con, body.tenant_ids)
            owner_id = await con.fetchval(
                "INSERT INTO owners (username, display_name, password_hash, must_change_password, created_by) "
                "VALUES ($1, $2, $3, true, $4) RETURNING id",
                username, body.display_name.strip(), owner_auth.hash_password(body.password), ACTOR,
            )
            await _link(con, owner_id, ids)
            await _audit(con, ids, EVENT_CREATED, "client login created",
                         {"owner_id": owner_id, "username": username, "tenant_ids": ids})
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=409, detail="That username is taken.") from None
    return await _get(pool, owner_id)


class UpdateBody(BaseModel):
    display_name: Optional[str] = Field(None, max_length=200)
    disabled: Optional[bool] = None
    tenant_ids: Optional[list[int]] = None


@router.patch("/api/owners/{owner_id}")
async def api_update(owner_id: int, body: UpdateBody) -> dict[str, Any]:
    """Disabling ends every session of the owner at once."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        before = await _get(con, owner_id)
        changes: dict[str, Any] = {}
        if body.display_name is not None and body.display_name.strip() != before["display_name"]:
            changes["display_name"] = body.display_name.strip()
        if body.disabled is not None and body.disabled != before["disabled"]:
            changes["disabled"] = body.disabled
        if changes:
            await con.execute(
                "UPDATE owners SET display_name = coalesce($2, display_name), disabled = coalesce($3, disabled), "
                "updated_at = now() WHERE id = $1",
                owner_id, changes.get("display_name"), changes.get("disabled"),
            )
        if changes.get("disabled"):
            await owner_auth.kill_sessions(con, owner_id)
        touched = set(before["tenant_ids"])
        if body.tenant_ids is not None:
            ids = await _check_tenants(con, body.tenant_ids)
            if ids != before["tenant_ids"]:
                await _link(con, owner_id, ids)
                changes["tenant_ids"] = {"before": before["tenant_ids"], "after": ids}
                touched |= set(ids)
        if changes:
            await _audit(con, touched, EVENT_UPDATED, "client login changed",
                         {"owner_id": owner_id, "username": before["username"], "changes": changes})
    return await _get(pool, owner_id)


class ResetBody(BaseModel):
    password: str = Field(..., max_length=1000)


@router.post("/api/owners/{owner_id}/reset-password")
async def api_reset_password(owner_id: int, body: ResetBody) -> dict[str, Any]:
    """A new temporary password; every session ends and the owner must
    choose their own at the next login."""
    _check_password(body.password)
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        owner = await _get(con, owner_id)
        await con.execute(
            "UPDATE owners SET password_hash = $2, must_change_password = true, updated_at = now() WHERE id = $1",
            owner_id, owner_auth.hash_password(body.password),
        )
        await owner_auth.kill_sessions(con, owner_id)
        await _audit(con, owner["tenant_ids"], EVENT_PASSWORD_RESET, "temporary password set by the admin",
                     {"owner_id": owner_id, "username": owner["username"]})
    return await _get(pool, owner_id)


@router.delete("/api/owners/{owner_id}/totp")
async def api_remove_totp(owner_id: int) -> dict[str, Any]:
    """For a lost phone: the owner logs in with the password alone again
    and can set up a new authenticator."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        owner = await _get(con, owner_id)
        await con.execute("UPDATE owners SET totp_secret_enc = NULL, updated_at = now() WHERE id = $1", owner_id)
        await _audit(con, owner["tenant_ids"], EVENT_TOTP_REMOVED, "authenticator removed by the admin",
                     {"owner_id": owner_id, "username": owner["username"]})
    owner_auth.forget_totp(owner_id)
    return await _get(pool, owner_id)


class ApproveBody(BaseModel):
    reason: str = Field("", max_length=500)
    # None = leave the links as they are.
    tenant_ids: Optional[list[int]] = None


@router.post("/api/owners/{owner_id}/approve")
async def api_approve(owner_id: int, body: ApproveBody) -> dict[str, Any]:
    return await approve(_get_pool(), owner_id, actor=ACTOR, reason=body.reason, tenant_ids=body.tenant_ids)


class RejectBody(BaseModel):
    reason: str = Field(..., max_length=500)


@router.post("/api/owners/{owner_id}/reject")
async def api_reject(owner_id: int, body: RejectBody) -> dict[str, Any]:
    return await reject(_get_pool(), owner_id, actor=ACTOR, reason=body.reason)


@router.get("/api/owners/{owner_id}/terms")
async def api_owner_terms(owner_id: int) -> list[dict[str, Any]]:
    """Every terms version this login accepted: when and from where."""
    await _get(_get_pool(), owner_id)
    return await terms.acceptances(_get_pool(), owner_id)


@router.delete("/api/owners/{owner_id}")
async def api_delete(owner_id: int) -> dict[str, Any]:
    """Removes the login, its links and its sessions (ON DELETE CASCADE).
    The businesses and their data are untouched."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        owner = await _get(con, owner_id)
        await con.execute("DELETE FROM owners WHERE id = $1", owner_id)
        await _audit(con, owner["tenant_ids"], EVENT_DELETED, "client login deleted",
                     {"owner_id": owner_id, "username": owner["username"]})
    owner_auth.forget_totp(owner_id)
    return {"ok": True}
