"""AI usage limits per client, and the reply limits per chat."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import ai_limits
import tenant_config
from conftest import seed_session

WORDS = tenant_config.TenantConfig().replies.acknowledgements


@pytest.mark.parametrize("text", ["ok", "OK!", "  thanks.  ", "Paldies!", "спасибо", "👍", "👍👍👍", "ok thanks",
                                  "thank you", "ok ok"])
def test_acknowledgements(text):
    assert ai_limits.is_acknowledgement(text, WORDS)


@pytest.mark.parametrize("text", ["ok, what time tomorrow?", "thanks, and the price?", "", "okay then 15:00",
                                  "no", "👍 but can I bring my dog"])
def test_not_acknowledgements(text):
    assert not ai_limits.is_acknowledgement(text, WORDS)


@pytest.mark.parametrize("text", [
    "are you a bot? 😅", "is this really you typing or some auto reply?", "wait that message looks automatic 😳 r u a bot?",
    "hmm is this an AI answering?", "am i talking to a real person?", "real?", "who am i talking to?", "ты бот?",
    "это бот отвечает или ты?", "ты настоящая или фейк?", "vai tu esi bots?", "vai tu esi īsta?", "be honest, are you human"])
def test_bot_questions(text):
    assert ai_limits.is_bot_question(text)


@pytest.mark.parametrize("text", [
    "before i say a time, is this a real person or a bot answering?", "whats ur real name tho? nikol is fake right",
    "how do i even know ur real then. at least the district?", "u look the same in person?",
    "is that really you in the photos?", "are you real? 22:00 ok?", "are you real? how much for an hour?",
    "are you free tonight?", "are those photos real?", ""])
def test_not_only_bot_questions(text):
    assert not ai_limits.is_bot_question(text)


def test_a_bot_question_is_skipped_only_after_the_first_reply():
    first = [{"role": "user", "content": "are you a bot?"}]
    assert not ai_limits.bot_question_only(first)
    later = [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "ok"},
             {"role": "assistant", "content": "what time?"}, {"role": "user", "content": "are you a bot?"},
             {"role": "user", "content": "r u real"}]
    assert ai_limits.bot_question_only(later)
    later.append({"role": "user", "content": "and how much?"})
    assert not ai_limits.bot_question_only(later)


def test_periods_start_at_local_midnight_and_the_first():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo("Europe/Riga"))  # the clocks went back at 04:00
    day, month = ai_limits.period_starts(now)
    assert day == datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)
    assert month == datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)


def config(**limits):
    cfg = tenant_config.TenantConfig().model_dump(mode="json")
    cfg["api_spend_cap_eur"] = limits.pop("monthly_eur", 0)
    cfg["limits"].update(limits)
    return cfg


async def usage(pool, tenant, tokens, eur):
    await pool.execute(
        "INSERT INTO llm_usage (tenant_id, purpose, model, prompt_cache_miss_tokens, completion_tokens, cost_eur) "
        "VALUES ($1, 'reply', 'deepseek-chat', $2, 0, $3)", tenant, tokens, eur,
    )


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_limits_count_only_this_clients_usage(pg_pool):
    a = await seed_session(pg_pool, "a")
    b = await seed_session(pg_pool, "b")
    now = datetime.now(ZoneInfo("Europe/Riga"))
    await usage(pg_pool, b, 5000, 5.0)
    assert await ai_limits.limit_reached(pg_pool, a, config(daily_tokens=1000), now) == ""
    await usage(pg_pool, a, 999, 0.01)
    assert await ai_limits.limit_reached(pg_pool, a, config(daily_tokens=1000), now) == ""
    await usage(pg_pool, a, 1, 0.01)
    assert "daily token limit" in await ai_limits.limit_reached(pg_pool, a, config(daily_tokens=1000), now)
    assert "daily AI spend" in await ai_limits.limit_reached(pg_pool, a, config(daily_spend_eur=0.02), now)
    assert "monthly AI spend" in await ai_limits.limit_reached(pg_pool, a, config(monthly_eur=0.02), now)
    assert "monthly token" in await ai_limits.limit_reached(pg_pool, a, config(monthly_tokens=1000), now)
    assert await ai_limits.limit_reached(pg_pool, a, config(), now) == ""


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_reply_limits_count_ai_written_messages_in_this_chat(pg_pool):
    tenant = await seed_session(pg_pool, "a")
    replies = tenant_config.TenantConfig().replies.model_dump()
    replies["max_messages_per_chat_per_hour"] = 2

    async def message(chat, model="deepseek-chat", status="sent"):
        await pg_pool.execute(
            "INSERT INTO conversations (session_id, chat_id) VALUES ('a', $1) ON CONFLICT DO NOTHING", chat)
        await pg_pool.execute(
            "INSERT INTO messages (session_id, chat_id, direction, status, text, llm_model) "
            "VALUES ('a', $1, 'out', $2, 'x', $3)", chat, status, model)

    await message(7)
    await message(7, model=None)  # typed by a person: not counted
    await message(8)
    assert await ai_limits.reply_limit(pg_pool, tenant, 7, replies) == ""
    await message(7, status="pending_approval")
    assert "limit 2" in await ai_limits.reply_limit(pg_pool, tenant, 7, replies)
    assert await ai_limits.reply_limit(pg_pool, tenant, 8, replies) == ""
    replies.update(max_messages_per_chat_per_hour=0, min_gap_seconds=600)
    assert "under 600 s" in await ai_limits.reply_limit(pg_pool, tenant, 8, replies)
