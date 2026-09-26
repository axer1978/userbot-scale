"""Time-based one-time codes (RFC 6238) for the admin panel's second factor.

SHA-1, 30-second steps, 6 digits — the defaults every authenticator app
(Google Authenticator, Authy, 1Password, ...) uses when given a bare secret.
Standard library only; the secret is base32, as those apps expect.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def _key(secret: str) -> bytes:
    cleaned = "".join(secret.split()).upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def validate_secret(secret: str) -> None:
    """Raises ValueError for a secret an authenticator app couldn't use."""
    try:
        key = _key(secret)
    except Exception as exc:
        raise ValueError("not valid base32") from exc
    if len(key) < 10:
        raise ValueError("too short (need at least 16 base32 characters)")


def code_at(secret: str, counter: int, *, digits: int = DIGITS) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**digits).zfill(digits)


def matching_counter(
    secret: str, code: str, *, now: Optional[float] = None, window: int = 1
) -> Optional[int]:
    """The time step `code` belongs to, or None. Accepts one step either
    side of now, for clock drift and codes typed just as they rolled over."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    for counter in range(current - window, current + window + 1):
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def provisioning_uri(secret: str, *, account: str = "admin", issuer: str = "Userbot panel") -> str:
    """otpauth:// link an authenticator app can import (e.g. rendered as a QR code)."""
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SECONDS}"
    )


if __name__ == "__main__":
    # Run on the server: prints a fresh secret and the link to add it to an app.
    s = new_secret()
    print(f"ADMIN_TOTP_SECRET={s}")
    print(provisioning_uri(s))
