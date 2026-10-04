"""Manager logins: staff below the admin who moderate when the admin can't.

A third login world, separate from the admin's (panel.py) and the clients'
(owner_auth.py) the same way those two are separate from each other:

- Its own cookie (`manager_token`, `__Host-` prefixed whenever Secure) and
  its own sessions table (manager_sessions, SHA-256 of the token only).
  Neither other cookie counts here, and this one counts nowhere else.
- Passwords, the failed-login limit and authenticator codes work exactly
  like owner_auth's and reuse its functions. Failure counts share its
  table under "manager:<name>" keys, so the IP limit covers both logins.
- The admin creates a manager with a temporary password. The manager must
  choose their own, then set up an authenticator app, before any manager
  route opens: a manager can read every client's conversations, so a
  password alone is never enough. Only the admin can remove the app.
- Disabling a manager, resetting their password or deleting them ends
  every session at once.

Nothing here logs a password, a token or a TOTP secret.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import asyncpg
from fastapi import HTTPException, Request

import crypto
import owner_auth
import totp

COOKIE = "manager_token"
SESSION_TTL = owner_auth.SESSION_TTL

WRONG_CREDENTIALS = owner_auth.WRONG_CREDENTIALS
CHANGE_PASSWORD = owner_auth.CHANGE_PASSWORD
CODE_REQUIRED = owner_auth.CODE_REQUIRED
# The detail of the 403 every manager route answers until an authenticator is set up.
SETUP_TOTP = "setup_totp"

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
# Per manager, the highest TOTP time step already used: a code works once.
_last_totp_step: dict[int, int] = {}


def bind(*, get_pool: Callable[[], Any]) -> None:
    global _get_pool
    _get_pool = get_pool


def limit_key(username: str) -> str:
    """The name the shared failed-login limit counts this login under."""
    return f"manager:{username.strip().lower()}"


# ----------------------------------------------------------------------- TOTP


def totp_aad(manager_id: int) -> bytes:
    return crypto.aad_for(f"manager:{manager_id}", "totp")


def encrypt_totp(manager_id: int, secret: str) -> bytes:
    return crypto.encrypt_text(secret, aad=totp_aad(manager_id))


def decrypt_totp(manager_id: int, blob: bytes) -> str:
    return crypto.decrypt_text(bytes(blob), aad=totp_aad(manager_id))


def use_code(manager_id: int, secret: str, code: str) -> bool:
    """True once per code: the step must be newer than the last one used."""
    step = totp.matching_counter(secret, code or "")
    if step is None or step <= _last_totp_step.get(manager_id, -1):
        return False
    _last_totp_step[manager_id] = step
    return True


def forget_totp(manager_id: int) -> None:
    _last_totp_step.pop(manager_id, None)


# ---------------------------------------------------------------------- login


async def authenticate(pool: asyncpg.Pool, username: str, password: str, code: str = "") -> dict[str, Any]:
    """The manager row for these credentials, or owner_auth.LoginFailed /
    CodeRequired. A disabled manager fails exactly like a wrong password."""
    row = await pool.fetchrow(
        "SELECT id, username, password_hash, totp_secret_enc, disabled FROM managers WHERE lower(username) = $1",
        username.strip().lower(),
    )
    if row is None:
        owner_auth.verify_password(password, owner_auth._DUMMY_HASH)
        raise owner_auth.LoginFailed()
    if not owner_auth.verify_password(password, row["password_hash"]) or row["disabled"]:
        raise owner_auth.LoginFailed()
    if row["totp_secret_enc"] is not None:
        if not (code or "").strip():
            raise owner_auth.CodeRequired(wrong=False)
        if not use_code(row["id"], decrypt_totp(row["id"], row["totp_secret_enc"]), code):
            raise owner_auth.CodeRequired(wrong=True)
    return dict(row)


# ------------------------------------------------------------------- sessions


async def create_session(pool: asyncpg.Pool, manager_id: int, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    async with pool.acquire() as con:
        await con.execute("DELETE FROM manager_sessions WHERE expires_at <= now()")
        await con.execute(
            "INSERT INTO manager_sessions (token_hash, manager_id, expires_at, ip) VALUES ($1, $2, $3, $4)",
            owner_auth.token_hash(token), manager_id, datetime.now(timezone.utc) + SESSION_TTL, ip[:100],
        )
    return token


async def delete_session(pool: asyncpg.Pool, token: str) -> None:
    await pool.execute("DELETE FROM manager_sessions WHERE token_hash = $1", owner_auth.token_hash(token))


async def kill_sessions(executor: Any, manager_id: int, *, keep_token: Optional[str] = None) -> None:
    await executor.execute(
        "DELETE FROM manager_sessions WHERE manager_id = $1 AND token_hash <> $2",
        manager_id, owner_auth.token_hash(keep_token) if keep_token else "",
    )


async def session_manager(pool: asyncpg.Pool, token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    row = await pool.fetchrow(
        """
        SELECT m.id, m.username, m.display_name, m.must_change_password, m.totp_secret_enc IS NOT NULL AS totp
          FROM manager_sessions s JOIN managers m ON m.id = s.manager_id
         WHERE s.token_hash = $1 AND s.expires_at > now() AND NOT m.disabled
        """,
        owner_auth.token_hash(token),
    )
    return dict(row) if row else None


def cookie_name() -> str:
    return f"__Host-{COOKIE}" if owner_auth.cookie_secure() else COOKIE


def token_from(request: Any) -> Optional[str]:
    return request.cookies.get(cookie_name())


def set_cookie(response: Any, token: str) -> None:
    response.set_cookie(
        cookie_name(), token, httponly=True, samesite="strict", secure=owner_auth.cookie_secure(),
        max_age=int(SESSION_TTL.total_seconds()), path="/",
    )


def clear_cookie(response: Any) -> None:
    response.delete_cookie(cookie_name(), path="/", httponly=True, samesite="strict",
                           secure=owner_auth.cookie_secure())


# --------------------------------------------------------------- dependencies


async def any_manager(request: Request) -> dict[str, Any]:
    """The logged-in manager, whatever is still to do. Only the password,
    authenticator setup, account and logout routes use this directly."""
    manager = await session_manager(_get_pool(), token_from(request))
    if manager is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return manager


def gate(manager: dict[str, Any]) -> Optional[str]:
    if manager["must_change_password"]:
        return CHANGE_PASSWORD
    if not manager["totp"]:
        return SETUP_TOTP
    return None


async def current_manager(request: Request) -> dict[str, Any]:
    """401 without a valid manager cookie, 403 with gate()'s reason while
    the temporary password is in use or no authenticator is set up."""
    manager = await any_manager(request)
    reason = gate(manager)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    return manager


def actor(manager: dict[str, Any]) -> str:
    """How a manager appears in audit_log, alerts and reviewed_by."""
    return f"manager:{manager['username']}"
