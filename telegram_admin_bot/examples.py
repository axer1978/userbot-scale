"""Earlier chat moments like the one being answered (Step 3 of learning from
real chats; config retrieval).

Real pairs of a client's message and the business's reply, typed by hand or
corrected by an operator, masked, with the client's side worded several more
ways, all turned into vectors by the embedder (embeddings.py). For a new
message the closest ones are shown to the model as examples of how the
business handles it. The business's side is never generated.

Scaffold: the examples branch fills this in.
"""

from __future__ import annotations

from typing import Any


async def tick(pool: Any, bus: Any) -> None:
    """The scheduler's round: new pairs from the chats, and their wordings."""


async def note_for(runtime: Any, chat_id: int, history: list[dict[str, str]]) -> str:
    """The examples for the reply the runtime is about to write in this
    chat, as a prompt section, or "" (off, nothing alike, or the embedder
    is not running)."""
    return ""
