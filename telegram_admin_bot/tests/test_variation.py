"""Small differences between replies (config `variation`).

WhatsApp flags an account that sends many people the same text. The bot's
own lines (the greeting, the question, the small talk) come out a little
different every time; the one-piece messages (the rates, the extras, the
address, anything between [block] markers) never change at all.
"""

from __future__ import annotations

import random

import pytest

import ai_responder
import session_runtime
import tenant_config
import variation
from database import DIR_IN, STATUS_RECEIVED

pytestmark = pytest.mark.variation

PHRASES = [
    "Hi my love | Hey my love | Hi love | Hello my love",
    "around what hour you come? | what time would you like to come? | what time you want to come?",
]
PROMPT = (
    "Send the rates message as written:\n[block]\n30 min 100\n1 hour 200 ❤️\n[/block]\n"
    "Then the extras message:\n[block]\nevery extra +50\nI decide in person 😘\n[/block]\n"
)
FIXED = ai_responder.fixed_blocks(PROMPT)
RATES = "30 min 100\n1 hour 200 ❤️"
EXTRAS = "every extra +50\nI decide in person 😘"
FIRST_REPLY = (
    "Hi my love ❤️\naround what hour you come? 😘\n"
    f"[block]\n{RATES}\n[/block]\n[block]\n{EXTRAS}\n[/block]\n[block]\nganu street 4\n[/block]"
)


def settings(**changes) -> dict:
    return tenant_config.resolve(client={"variation": {"phrases": PHRASES, **changes}}).as_dict()["variation"]


def always(**changes) -> dict:
    """Only the changes a test names, each at the chance it gives, so a test
    sees just the one it is about."""
    base = {"phrases": [], "emoji_drop": 0.0, "emoji_swap": 0.0, "lowercase_start": 0.0,
            "drop_end_punctuation": 0.0, "merge_messages": 0.0}
    return settings(**{**base, **changes})


def vary(text: str, config: dict, seed: int = 0, fixed=FIXED) -> list[str]:
    return ai_responder.split_burst(variation.vary_reply(text, config, fixed, random.Random(seed)), 10, True, fixed)


# ----------------------------------------------------- what never changes

def test_the_rates_extras_and_address_never_change():
    for seed in range(300):
        parts = vary(FIRST_REPLY, settings(), seed)
        assert parts[-3:] == [RATES, EXTRAS, "ganu street 4"], seed


def test_the_prompts_blocks_stay_as_written_even_without_markers():
    """The model now and then writes the rates without [block]: still a
    block, still untouched (its emoji too)."""
    reply = "Hey love\n30 min 100\n1 hour 200 ❤️\nevery extra +50\nI decide in person 😘"
    for seed in range(100):
        assert vary(reply, settings(merge_messages=1.0), seed)[-2:] == [RATES, EXTRAS], seed


def test_a_one_line_prompt_block_stays_as_written_without_markers():
    prompt = "The address:\n[block]\nGanu street 4, ap. 12.\n[/block]\n"
    fixed = ai_responder.fixed_blocks(prompt, min_lines=1)
    config = always(lowercase_start=1.0, drop_end_punctuation=1.0, merge_messages=1.0)
    assert vary("See you!\nGanu street 4, ap. 12.", config, fixed=fixed) == ["see you", "Ganu street 4, ap. 12."]
    assert ai_responder.fixed_blocks(prompt) == ()  # split_burst's own list is unchanged


def test_off_the_reply_is_untouched():
    assert variation.vary_reply(FIRST_REPLY, settings(enabled=False), FIXED) == FIRST_REPLY


def test_numbers_never_change():
    reply = "yes love, 1 hour is 200.\nI'm free from 22:00 😘"
    for seed in range(100):
        out = "\n".join(vary(reply, settings(merge_messages=1.0), seed))
        assert "200" in out and "22:00" in out and "1 hour" in out.lower()


def test_the_same_parts_come_back_from_the_joined_pieces():
    """join_pieces is reply_pieces' way back: nothing varied, nothing moved."""
    for text in (FIRST_REPLY, "a|||b\n[block]\nx\ny\n[/block]\nc", "[block]\nopen to the end"):
        rebuilt = ai_responder.join_pieces(ai_responder.reply_pieces(text, FIXED))
        assert ai_responder.split_burst(rebuilt, 10, True, FIXED) == ai_responder.split_burst(text, 10, True, FIXED)


# ------------------------------------------------------- what does change

def test_the_first_reply_comes_out_many_ways():
    greetings = {tuple(vary(FIRST_REPLY, settings(), seed)[:-3]) for seed in range(60)}
    assert len(greetings) >= 30


def test_an_emoji_can_be_left_out():
    assert vary("Hi love ❤️\nsee you 😘", always(emoji_drop=1.0)) == ["Hi love", "see you"]


def test_a_message_that_is_only_an_emoji_is_never_emptied():
    """It gets another one of its family instead."""
    kisses = set(variation.emoji_groups(["😘 😚 😙 💋 🥰"])[0])
    for seed in range(20):
        (part,) = vary("😘", always(emoji_drop=1.0), seed)
        assert part in kisses


def test_a_swapped_emoji_stays_in_its_family():
    hearts = set(variation.emoji_groups(["❤️ ♥️ 💕 💖 💗"])[0])
    seen = set()
    for seed in range(50):
        (part,) = vary("Hi love ❤️", always(emoji_swap=1.0), seed)
        emoji = part.removeprefix("Hi love ")
        assert emoji in hearts and emoji != "❤️"
        seen.add(emoji)
    assert len(seen) > 2


def test_an_emoji_in_no_family_is_kept_when_swapping():
    assert vary("on my way 🚕", always(emoji_swap=1.0)) == ["on my way 🚕"]


def test_a_phrase_is_swapped_for_one_of_its_group_keeping_the_capital():
    seen = set()
    for seed in range(60):
        (greeting,) = vary("Hi my love", always(phrases=PHRASES), seed)
        assert greeting in {"Hi my love", "Hey my love", "Hi love", "Hello my love"}
        seen.add(greeting)
    assert len(seen) == 4
    assert {vary("hi my love", always(phrases=PHRASES), seed)[0][0] for seed in range(20)} == {"h"}


def test_a_swapped_in_i_stays_a_capital_mid_sentence():
    config = always(phrases=["it's booked for | I booked you for"])
    outs = {vary("yes love, it's booked for 22:00", config, seed)[0] for seed in range(20)}
    assert outs == {"yes love, it's booked for 22:00", "yes love, I booked you for 22:00"}


def test_a_swap_keeps_capitals_where_they_belong():
    config = always(phrases=["ok | OK | okay"])
    assert {vary("ok love", config, seed)[0] for seed in range(30)} == {"ok love", "OK love", "okay love"}
    config = always(phrases=["it's booked for | I booked you for"])
    outs = {vary("Yes, I booked you for 22:00", config, seed)[0] for seed in range(20)}
    assert outs == {"Yes, I booked you for 22:00", "Yes, it's booked for 22:00"}


def test_a_phrase_is_not_found_inside_a_longer_word():
    config = always(phrases=["hi | hey"])
    assert {vary("this is his", config, seed)[0] for seed in range(20)} == {"this is his"}


def test_a_small_letter_start_but_not_for_capitals_or_codes():
    config = always(lowercase_start=1.0)
    assert vary("Hi love", config) == ["hi love"]
    for word in ("OK love", "iPhone?", "TQn9Y2abc is the wallet", "Room4 ok"):
        assert vary(word, config) == [word]


def test_the_end_punctuation_goes_but_not_an_ellipsis():
    config = always(drop_end_punctuation=1.0)
    assert vary("what time you come? 😘", config) == ["what time you come 😘"]
    assert vary("see you soon.", config) == ["see you soon"]
    assert vary("hmm...", config) == ["hmm..."]
    assert vary("really?!", config) == ["really?!"]
    assert vary("?", config) == ["?"]


def test_a_long_row_of_toned_emojis_is_quick():
    """The end of a message is found without a regex that backtracks."""
    import time
    text = "ok. " + "\U0001F44D\U0001F3FB" * 400 + " x"
    started = time.perf_counter()
    variation.drop_end_punctuation(text)
    variation.vary_emojis(text, [], 1.0, 0.0, random.Random(0))
    assert time.perf_counter() - started < 0.5


def test_a_joined_emoji_goes_whole():
    black_cat = "\U0001F408" + chr(0x200D) + chr(0x2B1B)
    assert vary(f"ok {black_cat} fine", always(emoji_drop=1.0)) == ["ok fine"]


def test_dingbat_digits_are_numbers_not_emoji():
    one = chr(0x2776)
    assert vary(f"option {one} is 100", always(emoji_drop=1.0)) == [f"option {one} is 100"]


def test_two_short_messages_can_go_out_as_one():
    config = always(merge_messages=1.0)
    assert vary("Hi love ❤️\nwhat time?", config) == ["Hi love ❤️ what time?"]
    assert vary("Hi love\nwhat time?", config) == ["Hi love, what time?"]


def test_a_message_is_never_merged_into_a_block_or_past_the_length():
    config = always(merge_messages=1.0)
    assert vary(f"ok\n[block]\n{RATES}\n[/block]\nbye", config) == ["ok", RATES, "bye"]
    long = "x" * 60
    assert vary(f"{long}\n{long}", config) == [long, long]


# ------------------------------------------------------------- the config

def test_on_by_default_with_no_phrases():
    config = tenant_config.resolve().as_dict()["variation"]
    assert config["enabled"] is True
    assert config["phrases"] == []
    assert len(variation.emoji_groups(config["emoji_families"])) == len(tenant_config.DEFAULT_EMOJI_FAMILIES)


def test_a_phrase_line_needs_two_phrases():
    with pytest.raises(tenant_config.ConfigError):
        tenant_config.resolve(client={"variation": {"phrases": ["Hi my love"]}})


def test_a_phrase_group_cannot_change_a_number():
    with pytest.raises(tenant_config.ConfigError):
        tenant_config.resolve(client={"variation": {"phrases": ["at 10 | at 11"]}})
    tenant_config.resolve(client={"variation": {"phrases": ["text me 15 mins before | write me 15 min before"]}})


def test_an_emoji_family_is_read_like_the_code_reads_it():
    config = tenant_config.resolve(client={"variation": {"emoji_families": ["❤️💕"]}}).as_dict()["variation"]
    assert variation.emoji_groups(config["emoji_families"]) == [["❤️", "💕"]]
    with pytest.raises(tenant_config.ConfigError):
        tenant_config.resolve(client={"variation": {"emoji_families": ["love hearts"]}})


def test_drop_and_swap_together_are_at_most_certain():
    with pytest.raises(tenant_config.ConfigError):
        tenant_config.resolve(client={"variation": {"emoji_drop": 0.6, "emoji_swap": 0.6}})


# ------------------------------------------------------------ the runtime

@pytest.fixture
def outbox(app, monkeypatch):
    """Capture what would go to Telegram, without the delays."""
    sent: list[str] = []

    async def fake_resolve_peer(chat_id):
        return chat_id

    async def fake_deliver(peer, chat_id, text, typing):
        sent.append(text)
        return type("Sent", (), {"id": 1000 + len(sent)})()

    monkeypatch.setattr(app, "resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(app, "deliver", fake_deliver)
    app.config["burst"]["gap_ms"] = {"min": 0, "max": 0}
    return sent


@pytest.fixture
def drafting(app, monkeypatch):
    """draft_worker without the waits, and with a scripted model reply."""
    script: dict = {"reply": ""}

    async def no_sleep(_s):
        return None

    async def fake_generate_reply(**kwargs):
        return script["reply"]

    async def nothing(*a, **k):
        return None

    async def no_context(chat_id):
        return ""

    monkeypatch.setattr(session_runtime.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ai_responder, "generate_reply", fake_generate_reply)
    monkeypatch.setattr(app, "go_online_for", nothing)
    monkeypatch.setattr(app, "mark_read", nothing)
    monkeypatch.setattr(app, "borrowed_context", no_context)
    monkeypatch.setattr(app, "schedule_go_offline", lambda chat_id: None)
    monkeypatch.setattr(app, "deepseek_key", "k")
    return script


@pytest.mark.asyncio
async def test_the_draft_waiting_for_approval_is_the_varied_text(app, db, drafting):
    app.config["variation"] = always(lowercase_start=1.0, drop_end_punctuation=1.0)
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "hi", telegram_id=1)
    drafting["reply"] = f"Hi there.\n[block]\n{RATES}\n[/block]"

    await app.draft_worker(7)

    (draft,) = await db.pending_drafts(7)
    assert ai_responder.split_burst(draft["text"]) == ["hi there", RATES]


@pytest.mark.asyncio
async def test_an_auto_sent_reply_goes_out_varied(app, db, drafting, outbox):
    app.config["auto_send"] = True
    app.config["variation"] = always(lowercase_start=1.0, merge_messages=1.0)
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "hi", telegram_id=1)
    drafting["reply"] = f"Hi there\nWhat time?\n[block]\n{RATES}\n[/block]"

    await app.draft_worker(7)

    assert outbox == ["hi there, what time?", RATES]


@pytest.mark.asyncio
async def test_a_fallback_phrase_is_found_in_the_models_own_words(app, db, drafting, monkeypatch):
    """The operator's phrase ends in a full stop the varied reply lost: the
    reply is still queued as unanswered."""
    queued = []

    async def record(chat_id, reason, detail="", message_id=None):
        queued.append(detail)

    monkeypatch.setattr(app, "queue_unanswered", record)
    app.config["variation"] = always(drop_end_punctuation=1.0)
    app.config["unanswered"]["fallback_phrases"] = ["let me ask the owner."]
    await db.upsert_conversation(7, "J", None, False, 1)
    await db.record_message(7, DIR_IN, STATUS_RECEIVED, "can I bring a friend?", telegram_id=1)
    drafting["reply"] = "Let me ask the owner."

    await app.draft_worker(7)

    (draft,) = await db.pending_drafts(7)
    assert draft["text"] == "Let me ask the owner"
    assert queued == ["the reply contains “let me ask the owner.”"]
