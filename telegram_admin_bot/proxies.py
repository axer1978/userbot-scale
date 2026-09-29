"""Per-account proxy for the Telegram connection.

Each Telegram account can reach Telegram through its own proxy, so it
connects from an address in its own country (a residential or mobile proxy)
instead of from the server's datacenter IP. The sign-in (login_flow.py) and
the running account (session_runtime.py) use the same proxy, so Telegram
sees one consistent address from the first login on.

A proxy URL is `socks5://user:pass@host:port` (or `socks5h://`, or
`http://…` for an HTTP CONNECT proxy). It is stored AES-GCM encrypted in
telegram_sessions.proxy_url_enc (database.SessionRegistry.set_proxy) and
never shown again in full: the panel only gets `describe()`, which leaves
out the password.

Telethon hands the tuple from `telethon_tuple()` to python-socks.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import unquote, urlparse

SCHEMES = {"socks5": "socks5", "socks5h": "socks5", "http": "http"}
CHECK_TIMEOUT_SECONDS = 6.0


class ProxyError(ValueError):
    """The proxy URL is not usable; the message says why."""


def parse(url: str) -> dict[str, Any]:
    """Validate a proxy URL. Raises ProxyError with a sentence for the panel."""
    url = (url or "").strip()
    if not url:
        raise ProxyError("The proxy address is empty.")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SCHEMES:
        raise ProxyError("Use socks5://user:password@host:port (or http://… for an HTTP proxy).")
    try:
        port = parsed.port
    except ValueError:
        raise ProxyError("The port is not a number between 1 and 65535.") from None
    if not parsed.hostname or not port:
        raise ProxyError("The proxy address needs a host and a port, like socks5://user:pass@1.2.3.4:1080.")
    return {
        "type": SCHEMES[scheme],
        "host": parsed.hostname,
        "port": port,
        # socks5h and plain socks5 both resolve names through the proxy
        # (rdns): Telegram's addresses are IPs anyway, and a name must never
        # be looked up from the server itself.
        "rdns": True,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }


def telethon_tuple(url: Optional[str]) -> Optional[tuple]:
    """What TelegramClient(proxy=...) takes, or None for a direct connection."""
    if not url:
        return None
    p = parse(url)
    return (p["type"], p["host"], p["port"], p["rdns"], p["username"], p["password"])


def describe(url: Optional[str]) -> Optional[dict[str, Any]]:
    """The proxy as the panel may show it: no password."""
    if not url:
        return None
    try:
        p = parse(url)
    except ProxyError:
        return {"type": "invalid", "host": "", "port": None, "username": None}
    return {"type": p["type"], "host": p["host"], "port": p["port"], "username": p["username"]}


async def reachable(url: str, timeout: float = CHECK_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Can the server open a TCP connection to the proxy at all? Catches a
    wrong host or port before the account tries to use it. It does not log
    in to the proxy or reach Telegram through it."""
    p = parse(url)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(p["host"], p["port"]), timeout=timeout)
    except asyncio.TimeoutError:
        return False, f"no answer from {p['host']}:{p['port']} within {timeout:g} s"
    except OSError as exc:
        return False, f"cannot connect to {p['host']}:{p['port']} ({exc.strerror or type(exc).__name__})"
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True, ""
