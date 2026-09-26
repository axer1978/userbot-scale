"""LLM metering: three token kinds, three rates."""

from __future__ import annotations

import pytest

import llm_usage
from conftest import seed_session

PRICES = {
    "currency": "USD",
    "usd_to_eur": 0.5,
    "models": {
        "cheap": {"input_cache_hit": 0.1, "input_cache_miss": 1.0, "output": 2.0},
        "dear": {"input_cache_hit": 1.0, "input_cache_miss": 4.0, "output": 8.0},
    },
}


def test_each_token_kind_is_priced_at_its_own_rate():
    usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 2_000_000, "completion_tokens": 500_000}
    # (1 * 0.1 + 2 * 1.0 + 0.5 * 2.0) USD = 3.1 USD = 1.55 EUR
    assert llm_usage.cost_eur(PRICES, "cheap", usage) == pytest.approx(1.55)


def test_output_heavy_and_cache_heavy_calls_cost_differently():
    same_total = 1_000_000
    cached = {"prompt_cache_hit_tokens": same_total, "prompt_cache_miss_tokens": 0, "completion_tokens": 0}
    output = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "completion_tokens": same_total}
    assert llm_usage.cost_eur(PRICES, "cheap", output) == 20 * llm_usage.cost_eur(PRICES, "cheap", cached)


def test_an_unknown_model_is_costed_at_the_dearest_rates():
    usage = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert llm_usage.cost_eur(PRICES, "mystery", usage) == llm_usage.cost_eur(PRICES, "dear", usage)


def test_usage_without_a_cache_split_counts_as_cache_misses():
    assert llm_usage.parse_usage({"usage": {"prompt_tokens": 120, "completion_tokens": 30}}) == {
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 120, "completion_tokens": 30,
    }
    assert llm_usage.parse_usage({"usage": {
        "prompt_tokens": 120, "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 20, "completion_tokens": 5,
    }})["prompt_cache_hit_tokens"] == 100
    assert llm_usage.parse_usage({}) == {
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "completion_tokens": 0,
    }


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_calls_are_recorded_per_tenant_with_the_seeded_prices(pg_pool):
    llm_usage.reset_cache()
    tid = await seed_session(pg_pool, "acct")
    other = await seed_session(pg_pool, "other")
    usage = {"prompt_cache_hit_tokens": 1000, "prompt_cache_miss_tokens": 2000, "completion_tokens": 300}
    cost = await llm_usage.record(pg_pool, tenant_id=tid, purpose="reply", model="deepseek-chat", usage=usage)
    await llm_usage.record(pg_pool, tenant_id=other, purpose="reply", model="deepseek-chat", usage=usage)

    prices = await llm_usage.load_prices(pg_pool)
    assert cost == pytest.approx(llm_usage.cost_eur(prices, "deepseek-chat", usage)) and cost > 0
    assert await llm_usage.spend_since(pg_pool, tid, "2000-01-01T00:00:00+00:00") == pytest.approx(cost)
