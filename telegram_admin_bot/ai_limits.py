"""How much a client may use the AI, and how much the bot may say.

Two kinds of limit, both from the tenant config and both decided in code:

- AI usage (`limits.*`, `api_spend_cap_eur`): tokens and euros per day and
  per calendar month, in the tenant's timezone, summed from llm_usage. At a
  limit the bot stops calling the model for that client; messages are still
  received, stored and shown, just not answered, until the period rolls
  over or the limit is raised.
- Replies (`replies.*`): AI-written messages per chat per hour / day, a
  least gap between them, and bare acknowledgements ("ok", "thanks") that
  need no answer. Counted from the messages table: an AI-written message is
  one with llm_model set, sent or waiting for approval.

0 means no limit everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import asyncpg


@dataclass(frozen=True)
class Usage:
    tokens: int
    eur: float


def period_starts(now_local: datetime) -> tuple[datetime, datetime]:
    """Start of today and of this month in the tenant's zone, as UTC."""
    day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return day.astimezone(timezone.utc), month.astimezone(timezone.utc)


async def usage_since(pool: asyncpg.Pool, tenant_id: int, since: datetime) -> Usage:
    row = await pool.fetchrow(
        "SELECT COALESCE(sum(prompt_cache_hit_tokens + prompt_cache_miss_tokens + completion_tokens), 0) AS tokens, "
        "COALESCE(sum(cost_eur), 0) AS eur FROM llm_usage WHERE tenant_id = $1 AND created_at >= $2",
        tenant_id, since,
    )
    return Usage(int(row["tokens"]), float(row["eur"]))


async def limit_reached(pool: asyncpg.Pool, tenant_id: int, config: dict[str, Any], now_local: datetime) -> str:
    """Which AI limit this client has hit, as a sentence, or "" if none."""
    limits = config["limits"]
    monthly_eur = float(config.get("api_spend_cap_eur") or 0)
    if not (limits["daily_tokens"] or limits["monthly_tokens"] or limits["daily_spend_eur"] or monthly_eur):
        return ""
    day_start, month_start = period_starts(now_local)
    today = await usage_since(pool, tenant_id, day_start)
    if limits["daily_tokens"] and today.tokens >= limits["daily_tokens"]:
        return f"daily token limit reached ({today.tokens:,} of {limits['daily_tokens']:,})"
    if limits["daily_spend_eur"] and today.eur >= limits["daily_spend_eur"]:
        return f"daily AI spend limit reached (€{today.eur:.2f} of €{limits['daily_spend_eur']:.2f})"
    if limits["monthly_tokens"] or monthly_eur:
        month = await usage_since(pool, tenant_id, month_start)
        if limits["monthly_tokens"] and month.tokens >= limits["monthly_tokens"]:
            return f"monthly token limit reached ({month.tokens:,} of {limits['monthly_tokens']:,})"
        if monthly_eur and month.eur >= monthly_eur:
            return f"monthly AI spend limit reached (€{month.eur:.2f} of €{monthly_eur:.2f})"
    return ""


_PUNCT = re.compile(r"[\s.!?,;:)(]+")


def _normal(text: str) -> str:
    return _PUNCT.sub(" ", (text or "").lower()).strip()


def is_acknowledgement(text: str, words: Sequence[str]) -> bool:
    """The whole message is one of `words` (case, spaces and punctuation
    ignored), several of them ("ok thanks"), or one repeated ("👍👍")."""
    normal = _normal(text)
    if not normal:
        return False
    options = {_normal(w) for w in words if _normal(w)}
    if normal in options or all(token in options for token in normal.split()):
        return True
    squashed = normal.replace(" ", "")
    for option in options:
        unit = option.replace(" ", "")
        if unit and len(squashed) % len(unit) == 0 and squashed == unit * (len(squashed) // len(unit)):
            return True
    return False


async def reply_limit(pool: asyncpg.Pool, tenant_id: int, chat_id: int, replies: dict[str, Any]) -> str:
    """Why the bot must not write another message in this chat now, or "".
    Windows are counted on the database clock, the one that stamped the
    messages."""
    per_hour, per_day, gap = (
        replies["max_messages_per_chat_per_hour"], replies["max_messages_per_chat_per_day"], replies["min_gap_seconds"],
    )
    if not (per_hour or per_day or gap):
        return ""
    row = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 hour') AS hour, "
        "count(*) AS day, EXTRACT(EPOCH FROM now() - max(created_at)) AS since_last FROM messages "
        "WHERE tenant_id = $1 AND chat_id = $2 AND direction = 'out' AND llm_model IS NOT NULL "
        "AND status IN ('sent', 'pending_approval') AND created_at >= now() - interval '1 day'",
        tenant_id, chat_id,
    )
    if per_hour and row["hour"] >= per_hour:
        return f"{row['hour']} bot messages in this chat in the last hour (limit {per_hour})"
    if per_day and row["day"] >= per_day:
        return f"{row['day']} bot messages in this chat in the last 24 hours (limit {per_day})"
    if gap and row["since_last"] is not None and float(row["since_last"]) < gap:
        return f"the last bot message here was under {gap} s ago"
    return ""
