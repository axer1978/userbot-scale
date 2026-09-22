"""Carrying one chat's context into another when they are the same person.

Telegram hands the same human a new chat_id on a second account, so a
conversation can start from nothing with someone who is already half-known.
This module spots those pairs, links them, and hands the drafting path a short
brief of what the other chat already established.

Two chats are only ever linked automatically on a signal that identifies a
specific person — the same @username, or the same full name. Everything
weaker is scored and left below the threshold on purpose: the cost of a wrong
link is one person's private conversation leaking into a reply to someone
else, which is far worse than a reply that simply doesn't know something.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

import httpx

import ai_responder
from database import LINK_AUTO, LINK_MANUAL, Database

log = logging.getLogger(__name__)

# How sure a guess has to be before it links two chats on its own.
AUTO_LINK_THRESHOLD = 0.8

# A single given name ("alex") is not an identity — plenty of people share one.
# Scored, so the panel can still show it as a suggestion, but never auto-linked.
SINGLE_NAME_SCORE = 0.45

# A full name has to have some substance to it: two initials matching is not
# the same evidence as two matching names.
_MIN_FULL_NAME_CHARS = 5

_NOT_NAME_CHARS = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_name(name: Optional[str]) -> str:
    """Casefold and strip decoration, so "Anna K." and "anna k" are one name.

    Display names carry emoji, dots and zero-width padding that mean nothing
    about who the person is; comparing them raw would miss obvious matches.
    """
    text = unicodedata.normalize("NFKC", name or "")
    text = _NOT_NAME_CHARS.sub(" ", text.casefold())
    return " ".join(text.split())


def name_tokens(name: Optional[str]) -> list[str]:
    return normalize_name(name).split()


def match_score(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
    """How sure we are that two conversations are the same person.

    Returns the score and a plain-language reason to show in the panel, so a
    link is never something the app did for reasons I can't see.
    """
    if a.get("chat_id") == b.get("chat_id"):
        return 0.0, ""
    # A bot is not a person with a second account, and its chat is not context
    # for anyone else's.
    if a.get("is_bot") or b.get("is_bot"):
        return 0.0, ""

    username_a = (a.get("username") or "").strip().casefold()
    username_b = (b.get("username") or "").strip().casefold()
    if username_a and username_a == username_b:
        return 1.0, f"same @{username_a}"

    tokens_a, tokens_b = name_tokens(a.get("display_name")), name_tokens(b.get("display_name"))
    if tokens_a and tokens_a == tokens_b:
        joined = " ".join(tokens_a)
        if len(tokens_a) >= 2 and len(joined.replace(" ", "")) >= _MIN_FULL_NAME_CHARS:
            return 0.85, f'same full name "{joined}"'
        return SINGLE_NAME_SCORE, f'both called "{joined}"'

    return 0.0, ""


async def find_candidates(
    db: Database,
    conversation: dict[str, Any],
    min_score: float = AUTO_LINK_THRESHOLD,
) -> list[tuple[dict[str, Any], float, str]]:
    """Existing chats that look like the same person, best match first."""
    if conversation.get("is_bot"):
        return []
    blocked = await db.blocked_sources(conversation["chat_id"])
    matches = []
    for other in await db.list_conversations():
        if other["chat_id"] in blocked:
            continue
        score, reason = match_score(conversation, other)
        if score >= min_score:
            matches.append((other, score, reason))
    matches.sort(key=lambda m: (-m[1], m[0]["chat_id"]))
    return matches


async def autolink(
    db: Database, conversation: dict[str, Any], settings: dict[str, Any]
) -> list[dict[str, Any]]:
    """Link a chat to any confident match. Returns the links newly created."""
    if not settings.get("enabled", True) or not settings.get("auto_detect", True):
        return []

    chat_id = conversation["chat_id"]
    existing = {link["source_id"] for link in await db.get_links(chat_id)}
    max_sources = max(1, int(settings.get("max_sources", 2) or 2))
    # The common case, once a pair is linked: nothing to look for, and this
    # runs on every incoming message.
    if len(existing) >= max_sources:
        return []

    created: list[dict[str, Any]] = []
    for other, score, reason in await find_candidates(db, conversation):
        if other["chat_id"] in existing:
            continue
        if len(existing) >= max_sources:
            break
        await db.link_chats(chat_id, other["chat_id"], LINK_AUTO, reason, score)
        existing.add(other["chat_id"])
        link = await db.get_link(chat_id, other["chat_id"])
        if link is not None:
            created.append(link)
        log.info(
            "Linked chat %s to chat %s for context (%s, confidence %.2f).",
            chat_id, other["chat_id"], reason, score,
        )
    return created


async def link_by_hand(
    db: Database, chat_id: int, source_id: int
) -> list[dict[str, Any]]:
    """A link I chose myself, which also lifts any earlier unlink of the pair."""
    return await db.link_chats(chat_id, source_id, LINK_MANUAL, "linked by hand", 1.0)


async def ensure_summary(
    db: Database,
    chat_id: int,
    *,
    api_key: str,
    ai_config: dict[str, Any],
    history_limit: int = 60,
    refresh_after: int = 5,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """The cached brief for one chat, rebuilt only once that chat has moved on.

    Summarising on every message would mean a second API call per reply for no
    new information, so the cache holds until `refresh_after` new messages have
    landed in the source chat.
    """
    latest = await db.last_message_id(chat_id)
    if not latest:
        return ""

    cached = await db.get_summary(chat_id)
    if cached and latest - cached["last_message_id"] < max(1, refresh_after):
        return cached["summary"]

    history = await db.get_history_for_ai(chat_id, limit=max(1, history_limit))
    if not history:
        return ""

    conversation = await db.get_conversation(chat_id)
    try:
        summary = await ai_responder.summarize_conversation(
            api_key=api_key,
            history=history,
            subject_name=(conversation or {}).get("display_name", ""),
            ai_config=ai_config,
            client=client,
        )
    except ai_responder.AIResponderError as exc:
        # A failed summary must not cost the reply itself. Fall back to the
        # last good one; a slightly stale brief beats none.
        log.warning("Could not summarise chat %s for context: %s", chat_id, exc)
        return cached["summary"] if cached else ""

    # Stored even when it comes back empty, so a chat with nothing worth
    # carrying over is not re-summarised on every single message.
    await db.save_summary(chat_id, summary, latest)
    return summary


async def build_background(
    db: Database,
    chat_id: int,
    *,
    api_key: str,
    ai_config: dict[str, Any],
    settings: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """What this chat should already know from the chats linked to it, or ""."""
    if not settings.get("enabled", True):
        return ""
    links = await db.get_links(chat_id)
    if not links:
        return ""

    max_sources = max(1, int(settings.get("max_sources", 2) or 2))
    history_limit = int(settings.get("history_limit", 60) or 60)
    refresh_after = int(settings.get("refresh_after_messages", 5) or 5)

    briefs = []
    for link in links[:max_sources]:
        brief = await ensure_summary(
            db,
            link["source_id"],
            api_key=api_key,
            ai_config=ai_config,
            history_limit=history_limit,
            refresh_after=refresh_after,
            client=client,
        )
        if brief:
            briefs.append(brief)
    return "\n\n".join(briefs)
