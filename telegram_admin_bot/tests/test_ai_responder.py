"""Prompt assembly — especially the burst handling that answers a whole
run of messages instead of only the last one."""

from __future__ import annotations

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
