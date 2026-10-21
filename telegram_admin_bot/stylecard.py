"""How the business types, measured from the replies a person typed by hand,
and given to the model as a short note (config style_card).

Scaffold: the rules branch fills this in.
"""

from __future__ import annotations

from typing import Any


async def note_for(pool: Any, tenant_id: int, config: dict[str, Any]) -> str:
    """The style note for this account's replies, or "" (off, or too few
    replies typed by hand to measure)."""
    return ""
