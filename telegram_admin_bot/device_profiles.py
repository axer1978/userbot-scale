"""Deterministic per-session device identity.

Telethon's defaults are the problem this solves. Left alone it reports
`device_model='PC 64bit'`, `system_version` from the host kernel release,
and `app_version` set to the Telethon version string — so every session in
a fleet running on one box announces byte-identical client info, and one of
those fields names the library outright. Fifty separate client businesses
that all claim to be the same PC running the same Telethon build are
trivially correlated with each other.

What this module gives each session instead is one coherent, *stable*
profile: a real device paired with an OS version that device actually runs
and a Telegram client version that exists for that platform. Stability is
the point as much as plausibility — an account whose device model changes
on every process restart looks stranger than one that has always been a
slightly unusual device, and Telegram tracks client info per session.

`derive()` is a pure function of `session_id`, so the same session always
gets the same profile even before anything is persisted. `config_store`'s
`identity` block then stores it on first start (see session_runtime), which
is what makes it survive a change to this file's profile list.

Locale and timezone default to Latvia because that is where this fleet's
accounts and their ISP proxy IPs are; a session whose reported tz_offset
contradicts its exit IP's country is self-inconsistent in exactly the way
the rest of this is trying to avoid.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone as _timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_TIMEZONE = "Europe/Riga"
DEFAULT_LANG = "lv"
DEFAULT_SYSTEM_LANG = "lv-LV"

# Each entry is internally coherent: the OS version is one that model
# actually shipped/updated to, and the app version is a real Telegram
# client release for that platform. Mixing these across entries (an iPhone
# reporting an Android SDK level, say) is the kind of contradiction that
# makes a profile worse than no profile at all.
PROFILES: tuple[dict[str, str], ...] = (
    # --- Android ---
    {"device_model": "Samsung SM-G991B", "system_version": "SDK 33", "app_version": "10.14.5 (4746)"},
    {"device_model": "Samsung SM-A536B", "system_version": "SDK 33", "app_version": "10.12.0 (4670)"},
    {"device_model": "Samsung SM-S911B", "system_version": "SDK 34", "app_version": "10.14.5 (4746)"},
    {"device_model": "Xiaomi 22071219CG", "system_version": "SDK 33", "app_version": "10.13.0 (4700)"},
    {"device_model": "Xiaomi 2201117TY", "system_version": "SDK 32", "app_version": "10.12.0 (4670)"},
    {"device_model": "Google Pixel 7", "system_version": "SDK 34", "app_version": "10.14.5 (4746)"},
    {"device_model": "Google Pixel 6a", "system_version": "SDK 33", "app_version": "10.13.0 (4700)"},
    {"device_model": "OnePlus CPH2451", "system_version": "SDK 33", "app_version": "10.12.0 (4670)"},
    # --- iOS ---
    {"device_model": "iPhone 13", "system_version": "17.5.1", "app_version": "10.14"},
    {"device_model": "iPhone 14", "system_version": "17.5.1", "app_version": "10.14"},
    {"device_model": "iPhone 12", "system_version": "17.4.1", "app_version": "10.13"},
    {"device_model": "iPhone 15", "system_version": "17.5.1", "app_version": "10.14"},
    {"device_model": "iPhone SE 3", "system_version": "17.4.1", "app_version": "10.12"},
    # --- Desktop ---
    {"device_model": "Desktop", "system_version": "Windows 10", "app_version": "5.3.1 x64"},
    {"device_model": "Desktop", "system_version": "Windows 11", "app_version": "5.3.1 x64"},
    {"device_model": "Desktop", "system_version": "macOS 14.5", "app_version": "10.14"},
)


def tz_offset_seconds(tz_name: str = DEFAULT_TIMEZONE, *, at: Optional[datetime] = None) -> int:
    """Seconds east of UTC for `tz_name` right now (config_store's unit).

    Computed rather than hardcoded because Latvia observes DST: EET (+2) in
    winter, EEST (+3) in summer. A fixed value would be wrong half the year,
    and a client whose reported offset disagrees with its own locale is the
    sort of small contradiction worth not introducing.
    """
    moment = at or datetime.now(_timezone.utc)
    if ZoneInfo is None:
        return 0
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return 0
    offset = moment.astimezone(tz).utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def _index_for(session_id: str) -> int:
    """Stable index from the session id. sha256 rather than hash() because
    Python salts hash() per process — the whole point is that this survives
    restarts."""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % len(PROFILES)


def derive(
    session_id: str,
    *,
    tz_name: str = DEFAULT_TIMEZONE,
    lang_code: str = DEFAULT_LANG,
    system_lang_code: str = DEFAULT_SYSTEM_LANG,
) -> dict[str, Any]:
    """The full `identity` config block for a session, shaped exactly like
    `config_store.DEFAULTS["identity"]` so it can be stored straight back."""
    profile = PROFILES[_index_for(session_id)]
    return {
        "device_model": profile["device_model"],
        "system_version": profile["system_version"],
        "app_version": profile["app_version"],
        "lang_code": lang_code,
        "system_lang_code": system_lang_code,
        # Telethon has no lang_pack parameter; it is carried in config for
        # completeness and is not sent anywhere by this codebase.
        "lang_pack": "",
        "tz_offset": tz_offset_seconds(tz_name),
    }
