"""config_assist: turning a model's proposal into a checked, reviewable change."""

from __future__ import annotations

import pytest

import config_assist


def test_merge_patch_merges_sections_and_replaces_leaves():
    current = {"reply_delay": {"min_s": 5}, "banned_topics": ["a"]}
    patch = {"reply_delay": {"max_s": 50}, "banned_topics": {"append": ["b"]}}
    assert config_assist.merge_patch(current, patch) == {
        "reply_delay": {"min_s": 5, "max_s": 50}, "banned_topics": {"append": ["b"]},
    }


def test_a_valid_patch_reports_exactly_what_changes():
    result = config_assist.evaluate({"auto_send": True}, industry_config={}, client_overrides={"daily_message_cap": 80})
    assert result["valid"] is True
    assert result["overrides"] == {"daily_message_cap": 80, "auto_send": True}
    assert result["changes"] == [{"path": "auto_send", "from": False, "to": True}]


@pytest.mark.parametrize("patch, path", [
    ({"daily_message_cap": 99999}, "daily_message_cap"),
    ({"mood": "cheerful"}, "mood"),
    ({"reply_delay": {"min_s": 100, "max_s": 10}}, "reply_delay"),
])
def test_an_invalid_patch_is_reported_not_repaired(patch, path):
    result = config_assist.evaluate(patch, industry_config={}, client_overrides={})
    assert result["valid"] is False and result["changes"] == []
    assert any(e["path"].startswith(path) for e in result["errors"])


def test_a_non_object_answer_is_invalid():
    assert config_assist.evaluate(["x"], industry_config={}, client_overrides={})["valid"] is False


@pytest.mark.parametrize("text, expected", [
    ('{"auto_send": true}', {"auto_send": True}),
    ('Here you go:\n```json\n{"auto_send": false}\n```', {"auto_send": False}),
])
def test_json_is_found_inside_chatter(text, expected):
    assert config_assist.extract_json(text) == expected


def test_no_json_is_an_error():
    with pytest.raises(ValueError):
        config_assist.extract_json("I cannot do that.")
