"""Prompt assembly — especially the burst handling that answers a whole
run of messages instead of only the last one."""

from __future__ import annotations

import pytest

import ai_responder


def test_burst_of_messages_becomes_one_turn():
    """Three messages typed in a row are one thought, not three."""
    merged = ai_responder._merge_consecutive_turns([
        {"role": "user", "content": "hey"},
        {"role": "user", "content": "so about tomorrow"},
        {"role": "user", "content": "can you still make it?"},
    ])
    assert len(merged) == 1
    assert merged[0]["content"] == "hey\nso about tomorrow\ncan you still make it?"


def test_alternating_turns_are_left_alone():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
        {"role": "user", "content": "you free?"},
    ]
    assert ai_responder._merge_consecutive_turns(history) == history


def test_merging_does_not_mutate_the_caller_s_history():
    """describe_style() runs on the original list and needs per-message rows."""
    history = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]
    ai_responder._merge_consecutive_turns(history)
    assert history == [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]


def test_empty_history_merges_to_nothing():
    assert ai_responder._merge_consecutive_turns([]) == []


def test_burst_note_only_appears_when_there_was_a_burst():
    single = ai_responder._merge_consecutive_turns(
        [{"role": "user", "content": "just one"}]
    )
    assert not any("\n" in m["content"] for m in single)


def test_style_brief_reads_only_their_messages():
    """Mirroring our own past replies would entrench whatever we did first."""
    brief = ai_responder.describe_style([
        {"role": "user", "content": "yo"},
        {"role": "user", "content": "sup"},
        {"role": "assistant", "content": "A" * 500},
    ])
    assert brief  # produced something
    assert "500" not in brief  # the long assistant message did not skew it


def test_style_brief_needs_a_sample_before_guessing():
    assert ai_responder.describe_style([{"role": "user", "content": "hi"}]) == ""


def test_persona_falls_back_when_unconfigured():
    blank = ai_responder.build_system_prompt(
        {"purpose": "", "tone": "", "languages": "", "boundaries": ""}
    )
    assert blank == ai_responder.FALLBACK_SYSTEM_PROMPT


def test_persona_sections_are_labelled():
    prompt = ai_responder.build_system_prompt({"tone": "warm and brief"})
    assert "TONE AND STYLE" in prompt and "warm and brief" in prompt


# --------------------------------------------------------------- language
#
# The persona's language setting used to lose to the adaptive-style brief,
# which ended with "always reply in the language they are writing in" and was
# appended *after* the persona. Someone writing in Russian got Russian back
# even with English pinned. These tests hold that boundary.


def test_pinned_language_is_stated_as_a_rule_not_a_label():
    """'LANGUAGE: english' alone reads as a label and loses to later text."""
    prompt = ai_responder.build_system_prompt({"languages": "english"})
    assert "english" in prompt
    assert "no other" in prompt, "the language setting is not phrased as an instruction"


def test_style_brief_does_not_ask_to_mirror_language_when_one_is_pinned():
    history = [
        {"role": "user", "content": "привет"},
        {"role": "user", "content": "как дела"},
    ]
    brief = ai_responder.describe_style(history, language_locked=True)
    assert brief
    assert "Reply in the language they are writing in" not in brief
    assert "NOT their language" in brief


def test_style_brief_still_mirrors_language_when_none_is_pinned():
    """With no language set, matching the sender is the sane default."""
    history = [
        {"role": "user", "content": "привет"},
        {"role": "user", "content": "как дела"},
    ]
    brief = ai_responder.describe_style(history, language_locked=False)
    assert "Reply in the language they are writing in" in brief


def test_language_pin_detected_only_when_actually_set():
    assert ai_responder.language_is_pinned({"languages": "english"})
    assert not ai_responder.language_is_pinned({"languages": "   "})
    assert not ai_responder.language_is_pinned({})


def test_prompt_never_contains_both_language_instructions_at_once():
    """The actual regression: two contradicting rules in one prompt."""
    persona = {"languages": "english"}
    history = [
        {"role": "user", "content": "привет как дела"},
        {"role": "user", "content": "ты тут?"},
    ]
    system = ai_responder.build_system_prompt(persona)
    system += "\n\n" + ai_responder.describe_style(
        history, language_locked=ai_responder.language_is_pinned(persona)
    )
    assert "Reply in the language they are writing in" not in system, \
        "prompt still tells the model to switch to the sender's language"
    assert "no other" in system


# ------------------------------------------------- our own initiative

async def _sent_messages(monkeypatch, history, booking_note=""):
    seen: dict = {}

    async def fake_complete(*, api_key, messages, ai_config, client=None):
        seen["messages"] = messages
        return "ok"

    monkeypatch.setattr(ai_responder, "_complete", fake_complete)
    await ai_responder.generate_reply(api_key="k", history=history, persona={}, ai_config={},
                                      booking_note=booking_note)
    return seen["messages"]


@pytest.mark.asyncio
async def test_a_message_we_start_ends_with_a_note_to_write_it(monkeypatch):
    """A booking confirmed after "let me check": the chat ends on our turn,
    and without a closing turn the model repeated it or wrote nonsense."""
    messages = await _sent_messages(monkeypatch, [
        {"role": "user", "content": "22:00 ok?"},
        {"role": "assistant", "content": "let me check love, one sec"},
    ])
    assert messages[-1] == {"role": "system", "content": ai_responder.OWN_INITIATIVE_NOTE}


@pytest.mark.asyncio
async def test_the_news_itself_closes_a_message_we_start(monkeypatch):
    """A second confirmation, after a change and another "let me check", was
    answered "still checking": the news is repeated where the model reads last."""
    news = "NEWS TO PASS ON IN THIS REPLY: the appointment on Tue 06 Oct 18:15 has just been CONFIRMED."
    messages = await _sent_messages(monkeypatch, [
        {"role": "user", "content": "can I come in 20 min instead?"},
        {"role": "assistant", "content": "let me check love, one sec"},
    ], booking_note="APPOINTMENTS:\nOnly the business confirms appointments.\n" + news)
    assert messages[-1]["role"] == "system"
    assert messages[-1]["content"].startswith(ai_responder.OWN_INITIATIVE_NOTE)
    assert messages[-1]["content"].endswith(news)
    assert "Only the business confirms" not in messages[-1]["content"]


@pytest.mark.asyncio
async def test_news_for_a_reply_to_them_is_repeated_last(monkeypatch):
    """Their time went unanswered until the owner confirmed: the chat ends on
    their message, and the confirmation was answered as if it were absent."""
    news = "NEWS TO PASS ON IN THIS REPLY: the appointment on Tue 06 Oct 23:00 has just been CONFIRMED."
    messages = await _sent_messages(monkeypatch, [
        {"role": "assistant", "content": "hi love"},
        {"role": "user", "content": "at 23?"},
    ], booking_note="APPOINTMENTS:\nOnly the business confirms appointments.\n" + news)
    assert messages[-2] == {"role": "user", "content": "at 23?"}
    assert messages[-1] == {"role": "system", "content": ai_responder.NEWS_NOTE + "\n" + news}


@pytest.mark.asyncio
async def test_a_reply_to_them_ends_with_their_message(monkeypatch):
    messages = await _sent_messages(monkeypatch, [
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "22:00 ok?"},
    ])
    assert messages[-1] == {"role": "user", "content": "22:00 ok?"}


def test_the_no_reply_marker_never_goes_out_as_text():
    assert ai_responder.is_no_reply(" [no_reply] ")
    assert ai_responder.without_no_reply("here love 😘|||[NO_REPLY]") == "here love 😘|||"
    assert ai_responder.split_burst(ai_responder.without_no_reply("here love 😘|||[NO_REPLY]"), 4) == ["here love 😘"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status, ok, why", [
    (200, True, ""),
    (401, False, "DeepSeek does not accept this key"),
    (503, False, "DeepSeek answered HTTP 503 when it was checked"),
])
async def test_check_key_asks_the_model_list(status, ok, why):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await ai_responder.check_key("sk-test", client=client) == (ok, why)
    assert str(seen[0].url) == ai_responder.MODELS_URL and seen[0].method == "GET"
    assert seen[0].headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_check_key_says_when_deepseek_cannot_be_reached():
    import httpx

    def handler(request):
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok, why = await ai_responder.check_key("sk-test", client=client)
    assert not ok and why == "could not reach DeepSeek to check it (ConnectError)" and "sk-test" not in why
