"""The tenant config schema and its three-layer resolution."""

from __future__ import annotations

import pytest

import tenant_config as tc
import tenants


def test_defaults_are_a_valid_config():
    resolved = tc.resolve()
    assert resolved.config.daily_message_cap == 150
    assert set(resolved.sources.values()) == {tc.PLATFORM}
    # The two features that conflict with the platform rules start off.
    assert resolved.config.context_link.enabled is False
    assert resolved.config.outreach.enabled is False


def test_client_beats_industry_beats_platform_and_sources_say_so():
    resolved = tc.resolve(
        {"reply_delay": {"min_s": 5, "max_s": 30}, "daily_message_cap": 80},
        {"reply_delay": {"max_s": 60}},
    )
    cfg = resolved.config
    assert (cfg.reply_delay.min_s, cfg.reply_delay.max_s) == (5, 60)
    assert cfg.daily_message_cap == 80
    assert resolved.sources["reply_delay.min_s"] == tc.INDUSTRY
    assert resolved.sources["reply_delay.max_s"] == tc.CLIENT
    assert resolved.sources["daily_message_cap"] == tc.INDUSTRY
    assert resolved.sources["reply_delay.distribution"] == tc.PLATFORM


def test_a_list_can_be_replaced_or_appended_to():
    industry = {"escalation_keywords": ["complaint", "refund"]}
    assert tc.resolve(industry, {"escalation_keywords": ["lawyer"]}).config.escalation_keywords == ["lawyer"]
    appended = tc.resolve(industry, {"escalation_keywords": {"append": ["lawyer", "refund"]}})
    assert appended.config.escalation_keywords == ["complaint", "refund", "lawyer"]


def test_price_floors_map_is_one_leaf():
    resolved = tc.resolve({"price_floors": {"haircut": 20}}, {"price_floors": {"colour": 50}})
    assert resolved.config.price_floors == {"colour": 50}


@pytest.mark.parametrize("override, path", [
    ({"nonsense": 1}, "nonsense"),
    ({"reply_delay": {"median": 3}}, "reply_delay.median"),
    ({"daily_message_cap": 0}, "daily_message_cap"),              # below the hard limit
    ({"daily_message_cap": 999_999}, "daily_message_cap"),        # above it: rejected, not clamped
    ({"language_policy": "fixed:de"}, "language_policy"),
    ({"quiet_hours": {"start": "25:00"}}, "quiet_hours.start"),
    ({"timezone": "Mars/Olympus"}, "timezone"),
    ({"price_floors": {"haircut": -1}}, "price_floors"),
    ({"reply_delay": "fast"}, "reply_delay"),
    ({"escalation_keywords": {"add": ["x"]}}, "escalation_keywords"),
])
def test_invalid_overrides_are_rejected_with_a_path(override, path):
    with pytest.raises(tc.ConfigError) as info:
        tc.resolve(None, override)
    assert any(e["path"].startswith(path) for e in info.value.errors), info.value.errors


def test_cross_field_rules_hold_across_layers():
    # Each layer alone is fine; together max < min, which must be refused.
    with pytest.raises(tc.ConfigError):
        tc.resolve({"reply_delay": {"min_s": 60}}, {"reply_delay": {"max_s": 30}})


def test_diff_lists_changed_leaves_only():
    before = tc.resolve().as_dict()
    after = tc.resolve(None, {"auto_send": True, "burst": {"max_messages": 2}}).as_dict()
    assert tc.diff(before, after) == [
        {"path": "auto_send", "from": False, "to": True},
        {"path": "burst.max_messages", "from": 4, "to": 2},
    ]


def test_field_catalog_marks_inherited_and_overridden_fields():
    inherited = tc.resolve({"daily_message_cap": 80}, None)
    resolved = tc.resolve({"daily_message_cap": 80}, {"auto_send": True})
    rows = {r["path"]: r for r in tc.field_catalog(resolved, inherited.as_dict())}
    assert rows["auto_send"]["source"] == tc.CLIENT and rows["auto_send"]["inherited_value"] is False
    assert rows["daily_message_cap"]["source"] == tc.INDUSTRY and rows["daily_message_cap"]["value"] == 80
    assert rows["language_policy"]["kind"] == "choice"
    assert "fixed:lv" in rows["language_policy"]["choices"]
    assert rows["escalation_keywords"]["kind"] == "list"
    assert rows["price_floors"]["kind"] == "map"


# Pre-platform persona 'languages' text -> language_policy (tenants.legacy_language).
@pytest.mark.parametrize("text, policy, note", [
    ("", "mirror", ""),
    ("english", "fixed:en", ""),
    ("Always reply in Russian.", "fixed:ru", ""),
    ("latviešu", "fixed:lv", ""),
    ("Reply in whatever language they write in", "mirror", ""),
    ("English or Latvian", "mirror", "Language: English or Latvian"),
    ("Deutsch", "mirror", "Language: Deutsch"),
])
def test_legacy_language_mapping(text, policy, note):
    assert tenants.legacy_language(text) == (policy, note)
