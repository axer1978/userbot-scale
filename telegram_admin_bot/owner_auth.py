"""Client logins ("owners"): passwords, sessions, the second factor.

An owner is a business client's own login to the dashboard (owner_api.py).
It is a separate world from the admin login in panel.py on purpose:

- Different cookie (`owner_token`, not `admin_token`), different store
  (owner_sessions in Postgres, not panel.py's in-memory dict). The admin's
  `require_auth` never looks at this cookie and `current_owner` below never
  looks at the admin's, so neither login can stand in for the other.
- Only the SHA-256 of a session token is stored, so a database leak does
  not hand out live logins. A token lasts SESSION_TTL; logging out deletes
  the row; disabling the owner or resetting their password ends every
  session at once (the lookup joins `owners` and checks `disabled`).
- Passwords are scrypt hashes with a per-password salt, compared in
  constant time. An unknown username still costs one scrypt run, so the
  response time doesn't tell which usernames exist.
- Failed logins are limited per client IP and per username (in memory,
  like panel.py: one panel process, and a restart forgetting them is
  harmless). The error is the same whatever was wrong.
- The optional authenticator code is stored encrypted under the master key
  (crypto.py), bound to the owner's id, and each code works once.

Nothing here logs a password, a token or a TOTP secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import asyncpg
from fastapi import HTTPException, Request

import crypto
import terms
import totp

COOKIE = "owner_token"
SESSION_TTL = timedelta(hours=12)

# scrypt cost: 16 MiB of memory and ~50 ms per hash on a small server.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
MIN_PASSWORD_LENGTH = 10
# Hashing a megabyte "password" is work nobody needs to be able to ask for.
MAX_PASSWORD_LENGTH = 256

LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
WRONG_CREDENTIALS = "Wrong username or password"
# The detail of the 403 every owner route answers while the password must
# still be changed; the page recognises it and shows the change screen.
CHANGE_PASSWORD = "change_password"
# The detail of the 401 when the password was right and a code is needed.
CODE_REQUIRED = "code_required"
# The details of the 403 every owner route answers for a login that signed
# itself up and is not approved yet, was turned down, or has not accepted
# the current terms of service. The page shows the matching screen.
PENDING_APPROVAL = "pending_approval"
REJECTED = "rejected"
ACCEPT_TERMS = "accept_terms"
# Linked to a business whose industry requires review, or asked by the
# admin to verify again, and no approved verification video (review.py).
VERIFY_IDENTITY = "verify_identity"

PENDING, ACTIVE, REJECTED_STATUS = "pending", "active", "rejected"

LOOPBACK = {"127.0.0.1", "localhost", "::1"}

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731

# Recent failed attempts, keyed "ip:<address>" and "user:<lower username>".
_failures: dict[str, list[float]] = {}
# Per owner, the highest TOTP time step already used: a code works once.
_last_totp_step: dict[int, int] = {}


def bind(*, get_pool: Callable[[], Any]) -> None:
    global _get_pool
    _get_pool = get_pool


def _now() -> float:
    """The clock the failure window is measured on (monotonic, so a wall
    clock change can't shorten a lockout); a function so tests can move it."""
    return time.monotonic()


# ------------------------------------------------------------------ passwords


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>; the parameters travel with
    the hash so they can be raised later without breaking old ones."""
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=HASH_BYTES)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Checked against when the username is unknown, so that path costs the same.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_new_password(password: str) -> None:
    """Raises ValueError with a message for the person choosing it."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"The password must be at most {MAX_PASSWORD_LENGTH} characters.")


# ----------------------------------------------------------------- rate limit


def client_ip(request: Request) -> str:
    """Same rule as panel._client_ip: uvicorn's proxy_headers put the real
    visitor's address here when Caddy/nginx is in front."""
    return request.client.host if request.client else "unknown"


def _keys(ip: str, username: str) -> list[str]:
    return [f"ip:{ip}", f"user:{username.strip().lower()}"]


def _recent(key: str, now: float) -> list[float]:
    cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
    recent = [t for t in _failures.get(key, ()) if t > cutoff]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return recent


def check_rate_limit(ip: str, username: str) -> None:
    """429 with Retry-After once the IP or the username has had
    LOGIN_MAX_FAILURES failures inside the window (even with the right
    password: the point is to stop guessing, not to reward a lucky guess)."""
    now = _now()
    retry_after = 0
    for key in _keys(ip, username):
        recent = _recent(key, now)
        if len(recent) >= LOGIN_MAX_FAILURES:
            retry_after = max(retry_after, int(recent[0] + LOGIN_FAILURE_WINDOW_SECONDS - now) + 1)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {(retry_after + 59) // 60} minute(s).",
            headers={"Retry-After": str(retry_after)},
        )


def note_failure(ip: str, username: str) -> None:
    now = _now()
    for key in _keys(ip, username):
        recent = _recent(key, now)
        recent.append(now)
        _failures[key] = recent


def clear_failures(ip: str, username: str) -> None:
    for key in _keys(ip, username):
        _failures.pop(key, None)


# ----------------------------------------------------------------------- TOTP


def totp_aad(owner_id: int) -> bytes:
    return crypto.aad_for(f"owner:{owner_id}", "totp")


def encrypt_totp(owner_id: int, secret: str) -> bytes:
    return crypto.encrypt_text(secret, aad=totp_aad(owner_id))


def decrypt_totp(owner_id: int, blob: bytes) -> str:
    return crypto.decrypt_text(bytes(blob), aad=totp_aad(owner_id))


def use_code(owner_id: int, secret: str, code: str) -> bool:
    """True once per code: the step must be newer than the last one used."""
    step = totp.matching_counter(secret, code or "")
    if step is None or step <= _last_totp_step.get(owner_id, -1):
        return False
    _last_totp_step[owner_id] = step
    return True


def forget_totp(owner_id: int) -> None:
    _last_totp_step.pop(owner_id, None)


# ---------------------------------------------------------------------- login


class LoginFailed(Exception):
    """Wrong username or password, or the owner is disabled."""


class CodeRequired(Exception):
    """The password was right; an authenticator code is needed (or was wrong)."""

    def __init__(self, wrong: bool) -> None:
        super().__init__("wrong code" if wrong else "code required")
        self.wrong = wrong


async def authenticate(pool: asyncpg.Pool, username: str, password: str, code: str = "") -> dict[str, Any]:
    """The owner row for these credentials, or LoginFailed / CodeRequired.
    A disabled owner fails exactly like a wrong password."""
    row = await pool.fetchrow(
        "SELECT id, username, password_hash, totp_secret_enc, disabled FROM owners WHERE lower(username) = $1",
        username.strip().lower(),
    )
    if row is None:
        verify_password(password, _DUMMY_HASH)
        raise LoginFailed()
    if not verify_password(password, row["password_hash"]) or row["disabled"]:
        raise LoginFailed()
    if row["totp_secret_enc"] is not None:
        if not (code or "").strip():
            raise CodeRequired(wrong=False)
        if not use_code(row["id"], decrypt_totp(row["id"], row["totp_secret_enc"]), code):
            raise CodeRequired(wrong=True)
    return dict(row)


# ------------------------------------------------------------------- sessions


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(pool: asyncpg.Pool, owner_id: int, ip: str) -> str:
    """A new random token; only its hash is stored. Also sweeps sessions
    that expired, so the table doesn't grow by one row per login forever."""
    token = secrets.token_urlsafe(32)
    async with pool.acquire() as con:
        await con.execute("DELETE FROM owner_sessions WHERE expires_at <= now()")
        await con.execute(
            "INSERT INTO owner_sessions (token_hash, owner_id, expires_at, ip) VALUES ($1, $2, $3, $4)",
            token_hash(token), owner_id, datetime.now(timezone.utc) + SESSION_TTL, ip[:100],
        )
    return token


async def delete_session(pool: asyncpg.Pool, token: str) -> None:
    await pool.execute("DELETE FROM owner_sessions WHERE token_hash = $1", token_hash(token))


async def kill_sessions(executor: Any, owner_id: int, *, keep_token: Optional[str] = None) -> None:
    """End every session of this owner (but the one in `keep_token`)."""
    await executor.execute(
        "DELETE FROM owner_sessions WHERE owner_id = $1 AND token_hash <> $2",
        owner_id, token_hash(keep_token) if keep_token else "",
    )


async def session_owner(pool: asyncpg.Pool, token: Optional[str]) -> Optional[dict[str, Any]]:
    """The owner behind a cookie token, or None when it is unknown, expired,
    or the owner is disabled."""
    if not token:
        return None
    row = await pool.fetchrow(
        """
        SELECT o.id, o.username, o.display_name, o.must_change_password, o.status, o.review_reason,
               o.totp_secret_enc IS NOT NULL AS totp,
               array(SELECT tenant_id FROM owner_tenants WHERE owner_id = o.id ORDER BY tenant_id) AS tenant_ids,
               (SELECT max(version) FROM terms_versions WHERE requires_acceptance) AS terms_required,
               (SELECT max(version) FROM terms_acceptances WHERE owner_id = o.id) AS terms_accepted,
               (SELECT status FROM verifications v WHERE v.owner_id = o.id ORDER BY v.id DESC LIMIT 1)
                 AS verification_status,
               EXISTS (SELECT 1 FROM owner_tenants ot JOIN tenants t ON t.id = ot.tenant_id
                         JOIN industries i ON i.id = t.industry_id
                        WHERE ot.owner_id = o.id AND i.requires_review) AS review_required
          FROM owner_sessions s JOIN owners o ON o.id = s.owner_id
         WHERE s.token_hash = $1 AND s.expires_at > now() AND NOT o.disabled
        """,
        token_hash(token),
    )
    if row is None:
        return None
    return {
        "id": row["id"], "username": row["username"], "display_name": row["display_name"],
        "must_change_password": row["must_change_password"], "totp": row["totp"],
        "status": row["status"], "review_reason": row["review_reason"],
        "tenant_ids": list(row["tenant_ids"]),
        "terms": {"required": row["terms_required"], "accepted": row["terms_accepted"],
                  "ok": terms.is_current(row["terms_required"], row["terms_accepted"])},
        "verification": _verification(row["review_required"], row["verification_status"]),
    }


def _verification(review_required: bool, status: Optional[str]) -> dict[str, Any]:
    """Same rule as review.owner_state(), from the session query's columns."""
    required = bool(review_required) or (status is not None and status != "approved")
    return {"required": required, "status": status, "ok": not required or status == "approved"}


def cookie_secure() -> bool:
    """Same rule as panel.py's admin cookie: Secure unless the panel listens
    on loopback only (plain http through an SSH tunnel). Read from the
    environment rather than `import panel`, which runs as __main__."""
    return (os.getenv("ADMIN_HOST") or "127.0.0.1").strip() not in LOOPBACK


def cookie_name() -> str:
    """`__Host-owner_token` whenever the cookie is Secure (always, in Docker).
    The browser only accepts a __Host- cookie from this exact host, Secure,
    Path=/ and without a Domain, so a page on a sibling domain (any other
    *.sslip.io host, which counts as the same site) can't plant its own
    session in a client's browser or overwrite theirs. Plain http on
    loopback (SSH tunnel) can't carry the prefix, so it keeps the bare name."""
    return f"__Host-{COOKIE}" if cookie_secure() else COOKIE


def token_from(request: Any) -> Optional[str]:
    """The owner session token the request carries, under the current name."""
    return request.cookies.get(cookie_name())


def set_cookie(response: Any, token: str) -> None:
    response.set_cookie(
        cookie_name(), token, httponly=True, samesite="strict", secure=cookie_secure(),
        max_age=int(SESSION_TTL.total_seconds()), path="/",
    )


def clear_cookie(response: Any) -> None:
    response.delete_cookie(cookie_name(), path="/", httponly=True, samesite="strict", secure=cookie_secure())


# --------------------------------------------------------------- dependencies


async def any_owner(request: Request) -> dict[str, Any]:
    """The logged-in owner, even one who still has to change the password.
    Only the change-password and logout routes use this directly."""
    owner = await session_owner(_get_pool(), token_from(request))
    if owner is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return owner


def gate(owner: dict[str, Any]) -> Optional[str]:
    """Why this owner may not use the dashboard yet, in the order the page
    walks them through it; None when nothing stands in the way."""
    if owner["must_change_password"]:
        return CHANGE_PASSWORD
    if owner["status"] == PENDING:
        return PENDING_APPROVAL
    if owner["status"] == REJECTED_STATUS:
        return REJECTED
    if not owner["terms"]["ok"]:
        return ACCEPT_TERMS
    if not owner["verification"]["ok"]:
        return VERIFY_IDENTITY
    return None


async def current_owner(request: Request) -> dict[str, Any]:
    """{id, username, display_name, tenant_ids, ...} of the logged-in owner,
    401 without a valid owner cookie (the admin cookie counts for nothing
    here), 403 with gate()'s reason while a temporary password is in use,
    the login waits for approval or was turned down, or the current terms
    of service are not accepted yet."""
    owner = await any_owner(request)
    reason = gate(owner)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    return owner


def actor(owner: dict[str, Any]) -> str:
    """How an owner appears in audit_log and reviewed_by."""
    return f"owner:{owner['username']}"
