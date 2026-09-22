"""Session config validation. Every value here arrives from a browser form,
so none of it can be trusted to be the right type or in range.

`normalize()` has no I/O and is unchanged from the file-based version, so
most of these run with no Postgres. Only the load/save round-trip tests at
the bottom need real Postgres (`requires_pg`, via the `db`/`pg_pool`
fixtures) since config now lives in a `session_config` row instead of a
JSON file.
"""

from __future__ import annotations

import pytest

import config_store


def test_blank_config_gets_full_defaults():
    cfg = config_store.normalize({})
    for section in ("persona", "timing", "behavior", "ai", "human",
                    "presence", "finetune", "outreach", "safety", "contacts"):
        assert section in cfg


def test_safety_defaults_are_conservative():
    """If these drift upwards, that is a decision, not an accident."""
    s = config_store.normalize({})["safety"]
    assert s["daily_send_limit"] == 150
    assert s["daily_peer_limit"] == 30
    assert s["halt_on_peer_flood"] is True
    assert s["known_contacts_only"] is True


def test_out_of_range_numbers_are_clamped_not_rejected():
    s = config_store.normalize({"safety": {"daily_send_limit": 10 ** 9}})["safety"]
    assert s["daily_send_limit"] == 10_000


def test_garbage_falls_back_to_the_default():
    s = config_store.normalize({"safety": {"daily_peer_limit": "not a number"}})["safety"]
    assert s["daily_peer_limit"] == 30


def test_string_booleans_from_a_form_are_understood():
    s = config_store.normalize({"safety": {"halt_on_peer_flood": "no"}})["safety"]
    assert s["halt_on_peer_flood"] is False


def test_concurrency_is_clamped_to_something_sane():
    assert config_store.normalize(
        {"ai": {"max_concurrent_requests": 0}})["ai"]["max_concurrent_requests"] == 1
    assert config_store.normalize(
        {"ai": {"max_concurrent_requests": 500}})["ai"]["max_concurrent_requests"] == 32


def test_a_backwards_max_delay_is_corrected():
    t = config_store.normalize(
        {"timing": {"min_delay_seconds": 90, "max_delay_seconds": 10}})["timing"]
    assert t["max_delay_seconds"] >= t["min_delay_seconds"]


def test_unknown_keys_are_dropped():
    assert "nonsense" not in config_store.normalize({"nonsense": 1})


def test_per_contact_overrides_survive_a_round_trip():
    """The generic merge iterates the defaults, and `contacts` defaults to {} —
    so it needs its own handling or saved contacts would vanish."""
    cfg = config_store.normalize({"contacts": {"123": {"style_notes": "keep it short"}}})
    assert cfg["contacts"]["123"]["style_notes"] == "keep it short"


def test_blank_contact_entries_are_not_kept():
    cfg = config_store.normalize({"contacts": {"123": {"style_notes": ""}}})
    assert cfg["contacts"] == {}


def test_identity_defaults_are_present():
    identity = config_store.normalize({})["identity"]
    assert identity["lang_code"] == "en"
    assert identity["tz_offset"] == 0


def test_identity_tz_offset_is_clamped():
    identity = config_store.normalize({"identity": {"tz_offset": 999_999}})["identity"]
    assert identity["tz_offset"] == 50_400


def test_identity_blank_fields_fall_back_to_defaults():
    identity = config_store.normalize({"identity": {"lang_code": ""}})["identity"]
    assert identity["lang_code"] == "en"


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_saving_normalises_and_persists(pg_pool):
    async with pg_pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, is_active) VALUES ($1, TRUE)", "acct01"
        )
    saved = await config_store.save(pg_pool, "acct01", {"safety": {"daily_send_limit": "abc"}})
    assert saved["safety"]["daily_send_limit"] == 150

    loaded = await config_store.load(pg_pool, "acct01")
    assert loaded["safety"]["daily_send_limit"] == 150


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_load_seeds_defaults_for_a_session_with_no_row_yet(pg_pool):
    async with pg_pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, is_active) VALUES ($1, TRUE)", "acct01"
        )
    loaded = await config_store.load(pg_pool, "acct01")
    assert loaded["safety"]["daily_send_limit"] == 150
    async with pg_pool.acquire() as con:
        row = await con.fetchrow("SELECT revision FROM session_config WHERE session_id = $1", "acct01")
    assert row["revision"] == 1


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_a_stale_revision_is_rejected_not_silently_overwritten(pg_pool):
    async with pg_pool.acquire() as con:
        await con.execute(
            "INSERT INTO telegram_sessions (session_id, is_active) VALUES ($1, TRUE)", "acct01"
        )
    _, rev1 = await config_store.save_with_revision(pg_pool, "acct01", {})
    # Someone else saves in between.
    await config_store.save(pg_pool, "acct01", {"persona": {"tone": "friendly"}})

    with pytest.raises(config_store.ConfigConflict):
        await config_store.save_with_revision(
            pg_pool, "acct01", {"persona": {"tone": "curt"}}, expected_revision=rev1
        )
    # The concurrent save's value is still there — nothing was clobbered.
    loaded = await config_store.load(pg_pool, "acct01")
    assert loaded["persona"]["tone"] == "friendly"
