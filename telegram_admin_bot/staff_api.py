"""Admin API for staff roles and the approval queue (staff.py). Mounted by
panel.py behind require_auth; staff.gate() refuses everything under
/api/staff/ to managers, so only the admin reaches it."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import staff

router = APIRouter()
ACTOR = audit.ADMIN

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any]) -> None:
    global _get_pool, _get_bus
    _get_pool, _get_bus = get_pool, get_bus


def _role(row: Any) -> dict[str, Any]:
    perms = row["permissions"]
    perms = json.loads(perms) if isinstance(perms, str) else (perms or {})
    return {"id": row["id"], "name": row["name"], "description": row["description"],
            "admin_panel": row["admin_panel"], "permissions": staff.clean_permissions(perms),
            "members": row["members"]}


_ROLES = ("SELECT r.*, (SELECT count(*) FROM managers m WHERE m.role_id = r.id) AS members "
          "FROM staff_roles r")


@router.get("/api/staff/roles")
async def api_roles() -> dict[str, Any]:
    rows = await _get_pool().fetch(_ROLES + " ORDER BY lower(r.name)")
    return {"roles": [_role(r) for r in rows], "catalogue": staff.catalogue(), "levels": list(staff.LEVELS)}


class RoleBody(BaseModel):
    name: str = Field(..., max_length=80)
    description: str = Field("", max_length=500)
    admin_panel: bool = False
    permissions: dict[str, str] = Field(default_factory=dict)


async def _get_role(executor: Any, role_id: int) -> dict[str, Any]:
    row = await executor.fetchrow(_ROLES + " WHERE r.id = $1", role_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown role")
    return _role(row)


@router.post("/api/staff/roles")
async def api_create_role(body: RoleBody) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the role a name.")
    pool = _get_pool()
    perms = staff.clean_permissions(body.permissions)
    try:
        role_id = await pool.fetchval(
            "INSERT INTO staff_roles (name, description, admin_panel, permissions) VALUES ($1, $2, $3, $4::jsonb) "
            "RETURNING id",
            name, body.description.strip(), body.admin_panel, json.dumps(perms),
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=409, detail="A role with that name exists.") from None
    await audit.record(pool, tenant_id=None, actor=ACTOR, event="staff_role_created", reason=name,
                       payload={"role_id": role_id, "admin_panel": body.admin_panel, "permissions": perms})
    return await _get_role(pool, role_id)


@router.put("/api/staff/roles/{role_id}")
async def api_update_role(role_id: int, body: RoleBody) -> dict[str, Any]:
    """Takes effect on the members' next request: roles are read each time."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the role a name.")
    pool = _get_pool()
    before = await _get_role(pool, role_id)
    perms = staff.clean_permissions(body.permissions)
    try:
        await pool.execute(
            "UPDATE staff_roles SET name = $2, description = $3, admin_panel = $4, permissions = $5::jsonb, "
            "updated_at = now() WHERE id = $1",
            role_id, name, body.description.strip(), body.admin_panel, json.dumps(perms),
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=409, detail="A role with that name exists.") from None
    await audit.record(pool, tenant_id=None, actor=ACTOR, event="staff_role_changed", reason=name,
                       payload={"role_id": role_id, "before": {k: before[k] for k in ("name", "admin_panel",
                                                                                      "permissions")},
                                "after": {"name": name, "admin_panel": body.admin_panel, "permissions": perms}})
    return await _get_role(pool, role_id)


@router.delete("/api/staff/roles/{role_id}")
async def api_delete_role(role_id: int) -> dict[str, Any]:
    pool = _get_pool()
    role = await _get_role(pool, role_id)
    if role["members"]:
        raise HTTPException(status_code=409, detail="Give its managers another role first.")
    await pool.execute("DELETE FROM staff_roles WHERE id = $1", role_id)
    await audit.record(pool, tenant_id=None, actor=ACTOR, event="staff_role_deleted", reason=role["name"],
                       payload={"role_id": role_id})
    return {"ok": True}


# -------------------------------------------------------------- the queue


@router.get("/api/staff/requests")
async def api_requests(status: Optional[str] = "pending", manager_id: Optional[int] = None,
                       limit: int = 200) -> dict[str, Any]:
    if status == "all":
        status = None
    if status not in (None, *(staff.PENDING, staff.APPLIED, staff.APPROVED, staff.REJECTED, staff.FAILED)):
        raise HTTPException(status_code=400, detail="Unknown status")
    pool = _get_pool()
    return {
        "requests": await staff.list_requests(pool, status=status, manager_id=manager_id,
                                              limit=max(1, min(limit, 1000))),
        "pending": await pool.fetchval("SELECT count(*) FROM staff_requests WHERE status = 'pending'"),
    }


class DecisionBody(BaseModel):
    note: str = Field("", max_length=500)


@router.post("/api/staff/requests/{request_id}/approve")
async def api_approve(request_id: int, body: DecisionBody) -> dict[str, Any]:
    """Runs the change now, exactly as the manager sent it."""
    return await staff.run_approved(request_id, actor=ACTOR, note=body.note.strip())


@router.post("/api/staff/requests/{request_id}/reject")
async def api_reject(request_id: int, body: DecisionBody) -> dict[str, Any]:
    return await staff.reject(request_id, actor=ACTOR, note=body.note.strip())
