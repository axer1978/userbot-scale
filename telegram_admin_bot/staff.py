"""Staff roles: what each manager may do, and the admin's approval queue.

Every route a manager can reach (the admin panel's, for a role with
`admin_panel`, and the moderator panel's) belongs to one action of
CATALOGUE. A role sets each action to:

  off      refused (403); a view action hides that part of the panel
  allow    done at once
  approve  a change is stored in staff_requests and NOT done; the manager
           gets an ordinary success answer, so to them it looks done
           ("silent"). The admin approves (it runs then, as if the manager
           had done it) or rejects (it never runs).
           Protective changes (pausing a bot or a chat, disabling a login,
           cancelling outreach, rejecting a draft, the global stop) run at
           once even here, so a pause the manager believes is on really is.

Reads (GET) only know off/allow. A route missing from the catalogue, and
everything under ADMIN_ONLY, is never open to a manager. Every change a
manager makes is logged in staff_requests, whatever its level.

What this does not do: a change waiting for approval is not shown as done
when the manager reloads that screen; they see the real state.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

import asyncpg
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import alerts
import audit

log = logging.getLogger("staff")

OFF, ALLOW, APPROVE = "off", "allow", "approve"
LEVELS = (OFF, ALLOW, APPROVE)
ALERT_KIND = "staff_approval_pending"
INLINE_BODY_LIMIT = 256 * 1024
EVENT_DECIDED = "staff_request_decided"

PENDING, APPLIED, APPROVED, REJECTED, FAILED = "pending", "applied", "approved", "rejected", "failed"


@dataclass(frozen=True)
class Action:
    key: str
    group: str
    label: str
    view: bool = False
    # body -> True when this particular change only protects (it then runs
    # at once even when the role says 'approve').
    protective: Optional[Callable[[dict], bool]] = None


def _true(name: str) -> Callable[[dict], bool]:
    return lambda body: body.get(name) is True


def _always(body: dict) -> bool:
    return True


def _pausing(body: dict) -> bool:
    """"Pause all" with global_pause true, or a soft-off (body {reason}).
    Resuming goes through _resume / is_protective and never protects."""
    return body.get("global_pause") is True or ("global_pause" not in body and "kind" not in body)


ACTIONS = [
    # ------------------------------------------------------------- viewing
    Action("view.conversations", "See", "Accounts, conversations, messages, contacts, media", view=True),
    Action("view.config", "See", "Settings, prompts, industries, prices, onboarding", view=True),
    Action("view.bookings", "See", "Bookings, opening hours, waitlist, AI usage", view=True),
    Action("view.safety", "See", "Safety, alerts, health, billing", view=True),
    Action("view.clients", "See", "Client logins", view=True),
    Action("view.audit", "See", "Audit log", view=True),
    Action("view.unanswered", "See", "Unanswered messages", view=True),
    Action("view.training", "See", "Training review batches", view=True),
    Action("view.verification", "See", "Verification videos and client photos", view=True),
    Action("view.terms", "See", "Terms of service and sign-up setting", view=True),
    # -------------------------------------------------------- conversations
    Action("chat.manage", "Conversations", "Mark read, link chats, hand a chat back to the bot"),
    Action("chat.pause", "Conversations", "Pause / resume one chat", protective=_true("paused")),
    Action("chat.send", "Conversations", "Send messages and media as the business"),
    Action("drafts.approve", "Conversations", "Approve the bot's drafts (they are sent)"),
    Action("drafts.reject", "Conversations", "Reject the bot's drafts", protective=_always),
    Action("outreach.send", "Conversations", "Queue outreach to contacts"),
    Action("outreach.cancel", "Conversations", "Cancel queued outreach", protective=_always),
    Action("media.manage", "Conversations", "Upload, describe and delete media"),
    # ------------------------------------------------------------- bookings
    Action("bookings.act", "Bookings", "Confirm, decline, move or cancel bookings; scan a chat"),
    Action("bookings.settings", "Bookings", "Opening hours, waitlist, calendar link"),
    # --------------------------------------------------------- bot settings
    Action("config.account", "Settings", "An account's own settings (styles, identity)"),
    Action("config.client", "Settings", "A client's config, prompt, template pin, name and industry"),
    Action("config.platform", "Settings", "Platform rules, industries, LLM prices"),
    Action("accounts.connect", "Settings", "Log in Telegram accounts, pair WhatsApp numbers"),
    # --------------------------------------------------------------- safety
    Action("tenant.pause", "Safety", "Pause / resume a client's bot (manual hold)", protective=_pausing),
    Action("tenant.resume_other", "Safety", "Lift anomaly, spend-cap, Telegram or WhatsApp holds"),
    Action("alerts.ack", "Safety", "Acknowledge alerts"),
    Action("safety.global_stop", "Safety", "Global stop (every bot)", protective=_true("on")),
    Action("safety.hard_off", "Safety", "Hard-off: log an account out for good"),
    Action("safety.proxy", "Safety", "Change an account's proxy"),
    Action("billing.edit", "Safety", "Billing: due dates, payments, status, grace settings"),
    # -------------------------------------------------------------- clients
    Action("clients.approve", "Clients", "Approve or reject client sign-ups"),
    Action("clients.disable", "Clients", "Disable / enable client logins", protective=_true("disabled")),
    Action("clients.edit", "Clients", "Create client logins, rename, link businesses"),
    Action("clients.passwords", "Clients", "Reset client passwords and authenticators"),
    Action("clients.delete", "Clients", "Delete client logins"),
    # ---------------------------------------------------------------- other
    Action("unanswered.handle", "Other", "Mark unanswered messages reviewed, add answers to the FAQ"),
    Action("training.edit", "Other", "Training review batches"),
    Action("verification.decide", "Verification", "Approve / reject verification videos and photos"),
    Action("verification.request", "Verification", "Ask a client to verify again, re-check photos",
           protective=_always),
    Action("verification.industries", "Verification", "Mark industries as requiring verification"),
    Action("terms.edit", "Other", "Publish terms of service, open / close sign-up"),
]
ACTION_BY_KEY = {a.key: a for a in ACTIONS}

RouteKey = tuple[str, str]
Resolver = Union[str, Callable[[dict], str]]


def _owners_patch(body: dict) -> str:
    return "clients.disable" if set(body) <= {"disabled"} else "clients.edit"


def _resume(body: dict) -> str:
    return "tenant.pause" if body.get("kind") == "manual" else "tenant.resume_other"


# (method, route path) -> action. GET routes map to a view action.
ROUTES: dict[RouteKey, Resolver] = {
    # accounts and conversations
    ("GET", "/api/sessions"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/status"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/conversations"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/conversations/{chat_id}/messages"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/conversations/{chat_id}/links"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/contacts"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/outreach"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/media"): "view.conversations",
    ("GET", "/api/sessions/{session_id}/media/{item_id}/file"): "view.conversations",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/read"): "chat.manage",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/links"): "chat.manage",
    ("DELETE", "/api/sessions/{session_id}/conversations/{chat_id}/links/{source_id}"): "chat.manage",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/takeover"): "chat.manage",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/pause"): "chat.pause",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/send"): "chat.send",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/send-media"): "chat.send",
    ("POST", "/api/sessions/{session_id}/drafts/{draft_id}/approve"): "drafts.approve",
    ("POST", "/api/sessions/{session_id}/drafts/{draft_id}/reject"): "drafts.reject",
    ("POST", "/api/sessions/{session_id}/outreach"): "outreach.send",
    ("POST", "/api/sessions/{session_id}/outreach/cancel"): "outreach.cancel",
    ("PUT", "/api/sessions/{session_id}/media/upload"): "media.manage",
    ("PATCH", "/api/sessions/{session_id}/media/{item_id}"): "media.manage",
    ("PATCH", "/api/sessions/{session_id}/media/{item_id}/role"): "media.manage",
    ("PATCH", "/api/sessions/{session_id}/media/{item_id}/flags"): "media.manage",
    ("DELETE", "/api/sessions/{session_id}/media/{item_id}"): "media.manage",
    ("POST", "/api/sessions/{session_id}/global-pause"): "tenant.pause",
    ("GET", "/api/sessions/{session_id}/config"): "view.config",
    ("PUT", "/api/sessions/{session_id}/config"): "config.account",
    ("GET", "/api/auth"): "view.conversations",
    ("POST", "/api/auth/start"): "accounts.connect",
    ("POST", "/api/auth/code"): "accounts.connect",
    ("POST", "/api/auth/password"): "accounts.connect",
    ("POST", "/api/auth/resend"): "accounts.connect",
    ("POST", "/api/auth/cancel"): "accounts.connect",
    ("POST", "/api/wa/pair/start"): "accounts.connect",
    ("GET", "/api/wa/pair/{pair_id}"): "accounts.connect",
    ("POST", "/api/wa/pair/{pair_id}/cancel"): "accounts.connect",
    # platform, industries, clients' config
    ("GET", "/api/platform/tree"): "view.config",
    ("GET", "/api/platform/base"): "view.config",
    ("GET", "/api/industries/{industry_id}"): "view.config",
    ("GET", "/api/tenants/{tenant_id}"): "view.config",
    ("GET", "/api/tenants/by-session/{session_id}"): "view.config",
    ("GET", "/api/platform/prices"): "view.config",
    ("GET", "/api/onboarding"): "view.config",
    ("GET", "/api/onboarding/{tenant_id}"): "view.config",
    ("GET", "/api/audit"): "view.audit",
    ("PUT", "/api/platform/base"): "config.platform",
    ("POST", "/api/platform/base/rollback"): "config.platform",
    ("PUT", "/api/platform/prices"): "config.platform",
    ("POST", "/api/industries"): "config.platform",
    ("PUT", "/api/industries/{industry_id}/template"): "config.platform",
    ("POST", "/api/industries/{industry_id}/template/rollback"): "config.platform",
    ("PUT", "/api/industries/{industry_id}/config"): "config.platform",
    ("PATCH", "/api/tenants/{tenant_id}"): "config.client",
    ("PUT", "/api/tenants/{tenant_id}/config"): "config.client",
    ("PUT", "/api/tenants/{tenant_id}/prompt"): "config.client",
    ("POST", "/api/tenants/{tenant_id}/prompt/rollback"): "config.client",
    ("POST", "/api/tenants/{tenant_id}/pin"): "config.client",
    ("POST", "/api/tenants/{tenant_id}/config/propose"): "config.client",
    # bookings
    ("GET", "/api/sessions/{session_id}/bookings"): "view.bookings",
    ("GET", "/api/sessions/{session_id}/bookings/{booking_id}"): "view.bookings",
    ("GET", "/api/sessions/{session_id}/free-slots"): "view.bookings",
    ("GET", "/api/sessions/{session_id}/availability"): "view.bookings",
    ("GET", "/api/sessions/{session_id}/waitlist"): "view.bookings",
    ("GET", "/api/sessions/{session_id}/calendar-feed"): "view.bookings",
    ("GET", "/api/sessions/{session_id}/ai-usage"): "view.bookings",
    ("POST", "/api/sessions/{session_id}/bookings/{booking_id}/action"): "bookings.act",
    ("POST", "/api/sessions/{session_id}/conversations/{chat_id}/booking-scan"): "bookings.act",
    ("PUT", "/api/sessions/{session_id}/availability"): "bookings.settings",
    ("DELETE", "/api/sessions/{session_id}/waitlist/{entry_id}"): "bookings.settings",
    ("POST", "/api/sessions/{session_id}/calendar-feed/regenerate"): "bookings.settings",
    # safety and billing
    ("GET", "/api/safety"): "view.safety",
    ("GET", "/api/safety/summary"): "view.safety",
    ("GET", "/api/alerts"): "view.safety",
    ("GET", "/api/tenants/{tenant_id}/controls"): "view.safety",
    ("GET", "/api/platform/billing"): "view.safety",
    ("POST", "/api/safety/global-stop"): "safety.global_stop",
    ("POST", "/api/alerts/{alert_id}/ack"): "alerts.ack",
    ("POST", "/api/alerts/ack-all"): "alerts.ack",
    ("PUT", "/api/tenants/{tenant_id}/proxy"): "safety.proxy",
    ("POST", "/api/tenants/{tenant_id}/soft-off"): "tenant.pause",
    ("POST", "/api/tenants/{tenant_id}/resume"): _resume,
    ("POST", "/api/tenants/{tenant_id}/hard-off"): "safety.hard_off",
    ("PUT", "/api/tenants/{tenant_id}/billing/due"): "billing.edit",
    ("POST", "/api/tenants/{tenant_id}/billing/paid"): "billing.edit",
    ("POST", "/api/tenants/{tenant_id}/billing/status"): "billing.edit",
    ("PUT", "/api/platform/billing"): "billing.edit",
    # client logins
    ("GET", "/api/owners"): "view.clients",
    ("GET", "/api/owners/{owner_id}/terms"): "view.clients",
    ("POST", "/api/owners"): "clients.edit",
    ("PATCH", "/api/owners/{owner_id}"): _owners_patch,
    ("POST", "/api/owners/{owner_id}/reset-password"): "clients.passwords",
    ("DELETE", "/api/owners/{owner_id}/totp"): "clients.passwords",
    ("POST", "/api/owners/{owner_id}/approve"): "clients.approve",
    ("POST", "/api/owners/{owner_id}/reject"): "clients.approve",
    ("DELETE", "/api/owners/{owner_id}"): "clients.delete",
    # unanswered and training
    ("GET", "/api/unanswered"): "view.unanswered",
    ("GET", "/api/unanswered/count"): "view.unanswered",
    ("POST", "/api/unanswered/{item_id}/reviewed"): "unanswered.handle",
    ("POST", "/api/unanswered/{item_id}/reopen"): "unanswered.handle",
    ("POST", "/api/unanswered/{item_id}/promote"): "unanswered.handle",
    ("GET", "/api/review/batches"): "view.training",
    ("GET", "/api/review/batches/{batch_id}"): "view.training",
    ("GET", "/api/review/batches/{batch_id}/export.jsonl"): "view.training",
    ("POST", "/api/review/batches"): "training.edit",
    ("POST", "/api/review/items/{item_id}"): "training.edit",
    ("POST", "/api/review/batches/{batch_id}/done"): "training.edit",
    ("DELETE", "/api/review/batches/{batch_id}"): "training.edit",
    # verification
    ("GET", "/api/review/summary"): "view.verification",
    ("GET", "/api/review/verifications"): "view.verification",
    ("GET", "/api/review/verifications/{verification_id}/video"): "view.verification",
    ("GET", "/api/review/photos"): "view.verification",
    ("GET", "/api/review/photos/{submission_id}/file"): "view.verification",
    ("GET", "/api/review/industries"): "view.verification",
    ("POST", "/api/review/verifications/{verification_id}/approve"): "verification.decide",
    ("POST", "/api/review/verifications/{verification_id}/reject"): "verification.decide",
    ("POST", "/api/review/photos/{submission_id}/approve"): "verification.decide",
    ("POST", "/api/review/photos/{submission_id}/reject"): "verification.decide",
    ("POST", "/api/owners/{owner_id}/request-verification"): "verification.request",
    ("POST", "/api/tenants/{tenant_id}/recheck-media"): "verification.request",
    ("PUT", "/api/review/industries/{industry_id}"): "verification.industries",
    # terms
    ("GET", "/api/platform/terms"): "view.terms",
    ("POST", "/api/platform/terms"): "terms.edit",
    ("PUT", "/api/platform/signup"): "terms.edit",
    # the moderator panel (manager_api.py)
    ("GET", "/api/manager/overview"): "view.safety",
    ("POST", "/api/manager/tenants/{tenant_id}/pause"): "tenant.pause",
    ("POST", "/api/manager/tenants/{tenant_id}/resume"): "tenant.pause",
    ("GET", "/api/manager/tenants/{tenant_id}/conversations"): "view.conversations",
    ("GET", "/api/manager/tenants/{tenant_id}/conversations/{chat_id}/messages"): "view.conversations",
    ("POST", "/api/manager/tenants/{tenant_id}/conversations/{chat_id}/pause"): "chat.pause",
    ("GET", "/api/manager/alerts"): "view.safety",
    ("POST", "/api/manager/alerts/{alert_id}/ack"): "alerts.ack",
    ("GET", "/api/manager/clients"): "view.clients",
    ("POST", "/api/manager/clients/{owner_id}/approve"): "clients.approve",
    ("POST", "/api/manager/clients/{owner_id}/reject"): "clients.approve",
    ("POST", "/api/manager/clients/{owner_id}/disabled"): "clients.disable",
}
# Never open to a manager, whatever the role: staff and roles themselves,
# and finetuning (it rewrites a whole industry's prompt).
ADMIN_ONLY_PREFIXES = ("/api/managers", "/api/staff/", "/api/finetune/")

# A pause from the moderator panel's /pause and resume of a manual hold.
_MANAGER_PROTECTIVE = {("POST", "/api/manager/tenants/{tenant_id}/pause")}


def action_for(method: str, path: str, body: dict) -> Optional[Action]:
    resolver = ROUTES.get((method, path))
    if resolver is None:
        return None
    key = resolver(body) if callable(resolver) else resolver
    return ACTION_BY_KEY[key]


def is_protective(method: str, path: str, action: Action, body: dict) -> bool:
    if (method, path) in _MANAGER_PROTECTIVE:
        return True
    if path.endswith("/resume") or (method, path) == ("POST", "/api/manager/tenants/{tenant_id}/resume"):
        return False  # lifting a hold never protects
    return bool(action.protective and action.protective(body))


def clean_permissions(raw: Any) -> dict[str, str]:
    """Only known actions; views only off/allow; anything else dropped."""
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for key, level in raw.items():
        action = ACTION_BY_KEY.get(key)
        if action is None or level not in LEVELS or level == OFF:
            continue
        out[key] = ALLOW if action.view else level
    return out


def catalogue() -> list[dict[str, Any]]:
    return [{"key": a.key, "group": a.group, "label": a.label, "view": a.view,
             "protective": a.protective is not None} for a in ACTIONS]


# ------------------------------------------------------------- the staff


_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_app: Callable[[], Any] = lambda: None  # noqa: E731
_get_data_dir: Callable[[], Path] = lambda: Path("data")  # noqa: E731
# One-time secrets of approved requests being run now (run_approved).
_replay_secrets: set[str] = set()
REPLAY_HEADER = "x-staff-replay"


def bind(*, get_pool: Callable[[], Any], get_app: Callable[[], Any], get_data_dir: Callable[[], Path]) -> None:
    global _get_pool, _get_app, _get_data_dir
    _get_pool, _get_app, _get_data_dir = get_pool, get_app, get_data_dir


async def manager_with_role(pool: asyncpg.Pool, manager_id: int) -> Optional[dict[str, Any]]:
    row = await pool.fetchrow(
        "SELECT m.id, m.username, m.display_name, r.id AS role_id, r.name AS role_name, "
        "coalesce(r.admin_panel, false) AS admin_panel, coalesce(r.permissions, '{}'::jsonb) AS permissions "
        "FROM managers m LEFT JOIN staff_roles r ON r.id = m.role_id WHERE m.id = $1",
        manager_id,
    )
    if row is None:
        return None
    perms = row["permissions"]
    perms = json.loads(perms) if isinstance(perms, str) else (perms or {})
    return {**dict(row), "permissions": clean_permissions(perms)}


def level(staff: dict[str, Any], action: Action) -> str:
    return staff["permissions"].get(action.key, OFF)


class Queued(Exception):
    """Raised by gate(): the change is stored for the admin; the handler in
    panel.py answers with a plain success, as if it had been done."""

    def __init__(self, body: Any) -> None:
        super().__init__("queued for approval")
        self.body = body


def silent_answer(exc: Queued) -> JSONResponse:
    body = exc.body if isinstance(exc.body, dict) else {}
    return JSONResponse({**body, "ok": True})


def _tenant_hint(path: str) -> Optional[int]:
    match = re.search(r"/tenants/(\d+)", path)
    return int(match.group(1)) if match else None


async def _body(request: Request) -> tuple[bytes, dict]:
    raw = await request.body()
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return raw, parsed if isinstance(parsed, dict) else {}


async def _record(pool: asyncpg.Pool, staff: dict[str, Any], action: Action, request: Request, raw: bytes,
                  status: str, note: str = "") -> int:
    body_file = None
    inline: Optional[bytes] = raw
    if len(raw) > INLINE_BODY_LIMIT:
        folder = Path(_get_data_dir()) / "staff"
        folder.mkdir(parents=True, exist_ok=True)
        body_file = f"{uuid.uuid4().hex}.bin"
        (folder / body_file).write_bytes(raw)
        inline = None
    return await pool.fetchval(
        "INSERT INTO staff_requests (manager_id, username, role_name, action, method, path, query, content_type, "
        "body, body_file, tenant_id, status, note) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
        "RETURNING id",
        staff["id"], staff["username"], staff.get("role_name") or "", action.key, request.method,
        request.url.path, request.url.query, request.headers.get("content-type", ""), inline, body_file,
        _tenant_hint(request.url.path), status, note,
    )


async def gate(request: Request, staff: dict[str, Any]) -> None:
    """Checks one request of a manager (any panel). Returns to let it run,
    raises 403 when the role doesn't allow it, or Queued when it waits."""
    route = request.scope.get("route")
    path, method = getattr(route, "path", ""), request.method
    if path.startswith(ADMIN_ONLY_PREFIXES):
        raise HTTPException(status_code=403, detail="Only the admin can do this.")
    if method in ("GET", "HEAD"):
        action = action_for("GET", path, {})
        if action is None or level(staff, action) == OFF:
            raise HTTPException(status_code=403, detail="Your role does not include this.")
        return
    secret = request.headers.get(REPLAY_HEADER)
    if secret and secret in _replay_secrets:
        return  # the admin approved this one; it is running now
    raw, body = await _body(request)
    action = action_for(method, path, body)
    if action is None or level(staff, action) == OFF:
        raise HTTPException(status_code=403, detail="Your role does not include this.")
    pool = _get_pool()
    if level(staff, action) == ALLOW:
        await _record(pool, staff, action, request, raw, APPLIED)
        return
    if is_protective(method, path, action, body):
        await _record(pool, staff, action, request, raw, APPLIED, note="protective: done at once")
        return
    request_id = await _record(pool, staff, action, request, raw, PENDING)
    await audit.record(pool, tenant_id=_tenant_hint(request.url.path), actor=f"manager:{staff['username']}",
                       event="staff_request_queued", reason=action.label,
                       payload={"request_id": request_id, "path": request.url.path})
    count = await pool.fetchval("SELECT count(*) FROM staff_requests WHERE status = 'pending'")
    await alerts.raise_alert(pool, tenant_id=None, kind=ALERT_KIND, severity=alerts.INFO,
                             message=f"{count} moderator change(s) wait for your approval. Newest: "
                                     f"{staff['username']}: {action.label}",
                             payload={"request_id": request_id})
    raise Queued(body)


# --------------------------------------------------------------- deciding


def _row(row: Any) -> dict[str, Any]:
    body = row["body"]
    text = ""
    if body is not None:
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            text = f"(binary, {len(body)} bytes)"
    elif row["body_file"]:
        text = "(a file)"
    action = ACTION_BY_KEY.get(row["action"])
    return {
        "id": row["id"], "manager_id": row["manager_id"], "username": row["username"],
        "role_name": row["role_name"], "action": row["action"], "action_label": action.label if action else row["action"],
        "method": row["method"], "path": row["path"], "query": row["query"], "body": text[:20000],
        "tenant_id": row["tenant_id"], "tenant_name": row.get("tenant_name"), "status": row["status"],
        "note": row["note"], "decided_by": row["decided_by"],
        "decided_at": row["decided_at"].isoformat(timespec="seconds") if row["decided_at"] else None,
        "decision_note": row["decision_note"], "result_status": row["result_status"],
        "result_body": (row["result_body"] or "")[:2000],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
    }


async def list_requests(pool: asyncpg.Pool, *, status: Optional[str] = None, manager_id: Optional[int] = None,
                        limit: int = 200) -> list[dict[str, Any]]:
    where, args = [], []
    if status:
        args.append(status)
        where.append(f"s.status = ${len(args)}")
    if manager_id is not None:
        args.append(manager_id)
        where.append(f"s.manager_id = ${len(args)}")
    args.append(limit)
    rows = await pool.fetch(
        "SELECT s.*, t.name AS tenant_name FROM staff_requests s LEFT JOIN tenants t ON t.id = s.tenant_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + f" ORDER BY s.id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_row(dict(r)) for r in rows]


async def _settle_alert(pool: asyncpg.Pool, actor: str) -> None:
    if not await pool.fetchval("SELECT count(*) FROM staff_requests WHERE status = 'pending'"):
        await alerts.resolve(pool, tenant_id=None, kind=ALERT_KIND, by=actor)


async def run_approved(request_id: int, *, actor: str, note: str = "") -> dict[str, Any]:
    """Runs a pending request now, through the app itself, as the manager
    who made it would have (an admin-panel route with an admin token, a
    moderator-panel route with the manager's own session). Its outcome is
    stored with it."""
    import httpx

    import manager_auth

    pool = _get_pool()
    exists = await pool.fetchval("SELECT id FROM staff_requests WHERE id = $1", request_id)
    claimed = await pool.fetchrow(
        "UPDATE staff_requests SET status = 'approved', decided_by = $2, decided_at = now(), decision_note = $3 "
        "WHERE id = $1 AND status = 'pending' RETURNING *",
        request_id, actor, note,
    )
    if claimed is None:
        raise HTTPException(status_code=409 if exists else 404, detail="This change is no longer waiting.")
    body = bytes(claimed["body"]) if claimed["body"] is not None else b""
    if claimed["body_file"]:
        body = (Path(_get_data_dir()) / "staff" / claimed["body_file"]).read_bytes()
    secret = secrets.token_urlsafe(24)
    headers = {REPLAY_HEADER: secret}
    if claimed["content_type"]:
        headers["content-type"] = claimed["content_type"]
    if claimed["path"].startswith("/api/manager/"):
        if claimed["manager_id"] is None:
            raise HTTPException(status_code=409, detail="The manager who asked was deleted.")
        token = await manager_auth.create_session(pool, claimed["manager_id"], "approval")
        headers["cookie"] = f"{manager_auth.cookie_name()}={token}"

        async def cleanup() -> None:
            await manager_auth.delete_session(pool, token)
    else:
        import panel

        token = panel.issue_internal_admin_token()
        headers["cookie"] = f"{panel.admin_cookie_name()}={token}"

        async def cleanup() -> None:
            panel.revoke_admin_token(token)
    _replay_secrets.add(secret)
    try:
        transport = httpx.ASGITransport(app=_get_app(), client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://panel") as client:
            url = claimed["path"] + (f"?{claimed['query']}" if claimed["query"] else "")
            response = await client.request(claimed["method"], url, content=body, headers=headers)
    finally:
        _replay_secrets.discard(secret)
        await cleanup()
    ok = response.status_code < 400
    await pool.execute(
        "UPDATE staff_requests SET status = $2, result_status = $3, result_body = $4 WHERE id = $1",
        request_id, APPROVED if ok else FAILED, response.status_code, response.text[:4000],
    )
    await audit.record(pool, tenant_id=claimed["tenant_id"], actor=actor, event=EVENT_DECIDED,
                       reason=f"approved {claimed['username']}: {claimed['action']}" + ("" if ok else " (failed)"),
                       payload={"request_id": request_id, "status": response.status_code})
    await _settle_alert(pool, actor)
    out = await pool.fetchrow("SELECT s.*, t.name AS tenant_name FROM staff_requests s "
                              "LEFT JOIN tenants t ON t.id = s.tenant_id WHERE s.id = $1", request_id)
    return _row(dict(out))


async def reject(request_id: int, *, actor: str, note: str = "") -> dict[str, Any]:
    pool = _get_pool()
    row = await pool.fetchrow(
        "UPDATE staff_requests SET status = 'rejected', decided_by = $2, decided_at = now(), decision_note = $3 "
        "WHERE id = $1 AND status = 'pending' RETURNING *",
        request_id, actor, note,
    )
    if row is None:
        raise HTTPException(status_code=409, detail="This change is no longer waiting.")
    if row["body_file"]:
        (Path(_get_data_dir()) / "staff" / row["body_file"]).unlink(missing_ok=True)
    await audit.record(pool, tenant_id=row["tenant_id"], actor=actor, event=EVENT_DECIDED,
                       reason=f"rejected {row['username']}: {row['action']}", payload={"request_id": request_id})
    await _settle_alert(pool, actor)
    return _row({**dict(row), "tenant_name": None})

