"""LLM usage metering: tokens and cost per call, per tenant.

DeepSeek bills three kinds of token at three rates: input that hit its
context cache, input that missed it, and output. A single per-token price
would make the EUR spend caps wrong in whichever direction the mix leans,
so every row keeps all three counts and the cost uses all three rates.

Prices come from platform_settings 'llm_prices' (seeded by migration 0002,
USD per 1M tokens as DeepSeek publishes them, plus a usd_to_eur rate).
A model missing from the table is costed at the most expensive rates
listed, and logged: undercounting is the failure that lets spend run past
a cap, so unknown means expensive, never free.

Enforcing the cap (api_spend_cap_eur) is phase 3; this records the spend.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import asyncpg

log = logging.getLogger("llm_usage")

PRICES_TTL_SECONDS = 300
_prices_cache: tuple[float, Optional[dict[str, Any]]] = (0.0, None)


def parse_usage(data: dict[str, Any]) -> dict[str, int]:
    """The three token counts from a chat-completions response body. When
    the cache split is missing, all input counts as a cache miss (the
    dearer of the two)."""
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    miss = int(miss) if miss is not None else max(0, prompt - hit)
    return {
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


def rates_for(prices: dict[str, Any], model: str) -> tuple[dict[str, float], bool]:
    """(rates, known). Unknown models get the highest rate of each kind."""
    models = prices.get("models") or {}
    if model in models:
        return models[model], True
    worst = {
        kind: max((float(m.get(kind, 0)) for m in models.values()), default=0.0)
        for kind in ("input_cache_hit", "input_cache_miss", "output")
    }
    return worst, False


def cost_eur(prices: dict[str, Any], model: str, usage: dict[str, int]) -> float:
    rates, known = rates_for(prices, model)
    if not known:
        log.warning("No price listed for model %r; costing it at the highest listed rates.", model)
    per_million = (
        usage["prompt_cache_hit_tokens"] * float(rates.get("input_cache_hit", 0))
        + usage["prompt_cache_miss_tokens"] * float(rates.get("input_cache_miss", 0))
        + usage["completion_tokens"] * float(rates.get("output", 0))
    )
    amount = per_million / 1_000_000
    if (prices.get("currency") or "USD").upper() == "USD":
        amount *= float(prices.get("usd_to_eur", 1.0))
    return amount


async def load_prices(pool: asyncpg.Pool) -> dict[str, Any]:
    global _prices_cache
    fetched_at, cached = _prices_cache
    if cached is not None and time.monotonic() - fetched_at < PRICES_TTL_SECONDS:
        return cached
    raw = await pool.fetchval("SELECT value FROM platform_settings WHERE key = 'llm_prices'")
    prices = json.loads(raw) if isinstance(raw, str) else (raw or {})
    _prices_cache = (time.monotonic(), prices)
    return prices


def reset_cache() -> None:
    global _prices_cache
    _prices_cache = (0.0, None)


async def record(
    pool: asyncpg.Pool, *, tenant_id: Optional[int], purpose: str, model: str, usage: dict[str, int],
) -> float:
    """Store one call; returns its cost in EUR."""
    cost = cost_eur(await load_prices(pool), model, usage)
    await pool.execute(
        """
        INSERT INTO llm_usage (tenant_id, purpose, model, prompt_cache_hit_tokens,
                               prompt_cache_miss_tokens, completion_tokens, cost_eur)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        tenant_id, purpose, model, usage["prompt_cache_hit_tokens"],
        usage["prompt_cache_miss_tokens"], usage["completion_tokens"], cost,
    )
    return cost


async def spend_since(pool: asyncpg.Pool, tenant_id: int, since_iso: str) -> float:
    from database import _ts

    value = await pool.fetchval(
        "SELECT COALESCE(sum(cost_eur), 0) FROM llm_usage WHERE tenant_id = $1 AND created_at >= $2",
        tenant_id, _ts(since_iso),
    )
    return float(value or 0)
