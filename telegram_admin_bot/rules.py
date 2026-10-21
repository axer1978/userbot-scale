"""Operator corrections as rules (Step 2 of learning from real chats).

A rule is one short sentence the bot must follow, proposed from a draft an
operator edited, from a correction on a flagged chat, or written by hand. The
admin accepts it (Finetune → Rules); active rules go into every reply's
prompt for the account or the whole industry, and once a week they are folded
into the prompt itself through an ordinary finetune run the admin applies.

Scaffold: the rules branch fills this in.
"""

from __future__ import annotations

from typing import Any


async def tick(pool: Any, bus: Any) -> None:
    """The scheduler's round: the weekly fold of active rules into the prompt."""
