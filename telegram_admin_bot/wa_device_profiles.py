"""Deterministic per-account browser identity for WhatsApp linked devices.

The WhatsApp analogue of device_profiles.py. A WhatsApp account runs here as
a *linked device* (WhatsApp Web's protocol, spoken by Baileys in the
wa-gateway), and every linked device announces a browser tuple
`[os, browser, version]` that shows up on the phone under Settings →
Linked devices ("Chrome (Mac OS)"). Baileys' default is the same tuple for
every socket, so a fleet on one box would present fifty identical
"computers"; a different tuple on every re-pair would look odder still.

`derive()` is a pure function of `session_id` (sha256, not hash(), which is
salted per process), so the same account always gets the same tuple even
before anything is stored. The panel then saves it once in the account's
`session_config` identity (`config_store`'s `identity.wa_browser`) and
reuses the stored value on every later pairing, which is what makes it
survive a change to the list below.

Each entry is a combination a real desktop can present; the third element
is the OS release, the way Baileys' own `Browsers.*` helpers fill it.
"""

from __future__ import annotations

import hashlib

PROFILES: tuple[tuple[str, str, str], ...] = (
    ("Mac OS", "Chrome", "14.4.1"),
    ("Mac OS", "Chrome", "13.6.7"),
    ("Mac OS", "Safari", "14.4.1"),
    ("Windows", "Chrome", "10.0.22631"),
    ("Windows", "Chrome", "10.0.19045"),
    ("Windows", "Edge", "10.0.22631"),
    ("Ubuntu", "Chrome", "22.04.4"),
)


def _index_for(session_id: str) -> int:
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % len(PROFILES)


def derive(session_id: str) -> list[str]:
    """This account's `[os, browser, version]`, as a list (the JSON shape
    stored in config and sent to the gateway)."""
    return list(PROFILES[_index_for(session_id)])
