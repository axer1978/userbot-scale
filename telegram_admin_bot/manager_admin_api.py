"""Admin API for manager (moderator) logins. Mounted by panel.py behind the
admin login, so only the platform admin creates, disables or removes a
manager. Same rules as client logins (owner_admin_api.py): the password set
here is temporary, nothing returns a hash or a secret, and every change is
an audit row (a platform row, actor "admin"). What a manager can do once
signed in is listed in manager_api.py.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import manager_auth
import owner_admin_api
import owner_auth

router = APIRouter()
ACTOR = audit.ADMIN

EVENT_CREATED = "manager_created"
EVENT_UPDATED = "manager_updated"
EVENT_PASSWORD_RESET = "manager_password_reset"
EVENT_TOTP_REMOVED = "manager_totp_removed"
EVENT_DELETED = "manager_deleted"

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


_SELECT = """
SELECT m.id, m.username, m.display_name, m.must_change_password, m.disabled,
       m.totp_secret_enc IS NOT NULL AS totp, m.last_login_at, m.created_by, m.created_at, m.updated_at,
       m.role_id, (SELECT name FROM staff_roles r WHERE r.id = m.role_id) AS role_name,
       (SELECT count(*) FROM manager_sessions s WHERE s.manager_id = m.id AND s.expires_at > now()) AS sessions
  FROM managers m
"""


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _manager(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"], "username": row["username"], "display_name": row["display_name"],
        "must_change_password": row["must_change_password"], "disabled": row["disabled"],
        "totp": row["totp"], "sessions": row["sessions"], "last_login_at": _iso(row["last_login_at"]),
        "created_by": row["created_by"], "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]), "role_id": row["role_id"], "role_name": row["role_name"],
    }


async def _get(executor: Any, manager_id: int) -> dict[str, Any]:
    row = await executor.fetchrow(_SELECT + " WHERE m.id = $1", manager_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown manager")
    return _manager(row)


async def _audit(con: Any, event: str, reason: str, manager: dict[str, Any], **extra: Any) -> None:
    await audit.record(con, tenant_id=None, actor=ACTOR, event=event, reason=reason,
                       payload={"manager_id": manager["id"], "username": manager["username"], **extra})


def _check_password(password: str) -> None:
    try:
        owner_auth.check_new_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/managers")
async def api_list() -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(_SELECT + " ORDER BY lower(m.username)")
    return [_manager(r) for r in rows]


class CreateBody(BaseModel):
    username: str = Field(..., max_length=64)
    display_name: str = Field("", max_length=200)
    password: str = Field(..., max_length=1000)
    # None = the "Moderator" role.
    role_id: Optional[int] = None


async def _check_role(executor: Any, role_id: Optional[int]) -> int:
    if role_id is None:
        role_id = await executor.fetchval("SELECT id FROM staff_roles WHERE name = 'Moderator'")
    if role_id is None or await executor.fetchval("SELECT id FROM staff_roles WHERE id = $1", role_id) is None:
        raise HTTPException(status_code=400, detail="Unknown role")
    return role_id


@router.post("/api/managers")
async def api_create(body: CreateBody) -> dict[str, Any]:
    """The password is temporary: the manager changes it and sets up an
    authenticator app at the first sign-in."""
    username = body.username.strip()
    if not owner_admin_api.USERNAME_RE.match(username):
        raise HTTPException(status_code=400,
                            detail="Username: 3 to 64 letters, digits or . _ @ + - (an e-mail address works).")
    _check_password(body.password)
    pool = _get_pool()
    try:
        async with pool.acquire() as con, con.transaction():
            role_id = await _check_role(con, body.role_id)
            manager_id = await con.fetchval(
                "INSERT INTO managers (username, display_name, password_hash, must_change_password, created_by, "
                "role_id) VALUES ($1, $2, $3, true, $4, $5) RETURNING id",
                username, body.display_name.strip(), owner_auth.hash_password(body.password), ACTOR, role_id,
            )
            await _audit(con, EVENT_CREATED, "manager login created", {"id": manager_id, "username": username})
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=409, detail="That username is taken.") from None
    return await _get(pool, manager_id)


class UpdateBody(BaseModel):
    display_name: Optional[str] = Field(None, max_length=200)
    disabled: Optional[bool] = None
    role_id: Optional[int] = None


@router.patch("/api/managers/{manager_id}")
async def api_update(manager_id: int, body: UpdateBody) -> dict[str, Any]:
    """Disabling ends every session of the manager at once."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        before = await _get(con, manager_id)
        changes: dict[str, Any] = {}
        if body.display_name is not None and body.display_name.strip() != before["display_name"]:
            changes["display_name"] = body.display_name.strip()
        if body.disabled is not None and body.disabled != before["disabled"]:
            changes["disabled"] = body.disabled
        if body.role_id is not None and body.role_id != before["role_id"]:
            changes["role_id"] = await _check_role(con, body.role_id)
            await con.execute("UPDATE managers SET role_id = $2 WHERE id = $1", manager_id, changes["role_id"])
        if changes:
            await con.execute(
                "UPDATE managers SET display_name = coalesce($2, display_name), disabled = coalesce($3, disabled), "
                "updated_at = now() WHERE id = $1",
                manager_id, changes.get("display_name"), changes.get("disabled"),
            )
            if changes.get("disabled"):
                await manager_auth.kill_sessions(con, manager_id)
            await _audit(con, EVENT_UPDATED, "manager login changed", before, changes=changes)
    return await _get(pool, manager_id)


class ResetBody(BaseModel):
    password: str = Field(..., max_length=1000)


@router.post("/api/managers/{manager_id}/reset-password")
async def api_reset_password(manager_id: int, body: ResetBody) -> dict[str, Any]:
    _check_password(body.password)
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        manager = await _get(con, manager_id)
        await con.execute(
            "UPDATE managers SET password_hash = $2, must_change_password = true, updated_at = now() WHERE id = $1",
            manager_id, owner_auth.hash_password(body.password),
        )
        await manager_auth.kill_sessions(con, manager_id)
        await _audit(con, EVENT_PASSWORD_RESET, "temporary password set by the admin", manager)
    return await _get(pool, manager_id)


@router.delete("/api/managers/{manager_id}/totp")
async def api_remove_totp(manager_id: int) -> dict[str, Any]:
    """For a lost phone: at the next sign-in they must set up a new app
    before any manager route opens. Their sessions end now."""
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        manager = await _get(con, manager_id)
        await con.execute("UPDATE managers SET totp_secret_enc = NULL, updated_at = now() WHERE id = $1", manager_id)
        await manager_auth.kill_sessions(con, manager_id)
        await _audit(con, EVENT_TOTP_REMOVED, "authenticator removed by the admin", manager)
    manager_auth.forget_totp(manager_id)
    return await _get(pool, manager_id)


@router.delete("/api/managers/{manager_id}")
async def api_delete(manager_id: int) -> dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        manager = await _get(con, manager_id)
        await con.execute("DELETE FROM managers WHERE id = $1", manager_id)
        await _audit(con, EVENT_DELETED, "manager login deleted", manager)
    manager_auth.forget_totp(manager_id)
    return {"ok": True}
