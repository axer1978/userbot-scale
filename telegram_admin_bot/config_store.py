"""Load/save per-session config, with defaults and validation.

Persona fields intentionally ship blank — they are filled in from the admin
panel's Settings tab, not pre-seeded with example content.

`normalize()` and everything it calls is unchanged from the single-account
version: it is the single source of truth for valid ranges, and it never
raises for a bad *value* (clamps or falls back) — it only ever needs to
reject at the JSON-decode boundary, which no longer exists here since the
value already arrives as a Python dict from JSONB. Only `load`/`save`
changed, from a per-instance `config.json` file to a `session_config` row.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

import asyncpg

DEFAULTS: dict[str, Any] = {
    "persona": {
        "purpose": "",
        "tone": "",
        "languages": "",
        "boundaries": "",
        "signature_style": "",
    },
    "timing": {
        "min_delay_seconds": 20,
        "max_delay_seconds": 90,
        "active_hours_enabled": False,
        "active_hours_start": "09:00",
        "active_hours_end": "21:00",
        "timezone": "UTC",
    },
    "behavior": {
        "auto_send": False,
        "log_all_messages": True,
        "global_pause": False,
    },
    "ai": {
        "model": "deepseek-chat",
        "max_tokens": 400,
        "temperature": 1.0,
        # Drafts run one task per chat, so a busy hour can fire many API calls
        # at the same instant. This caps how many are in flight at once; the
        # rest queue rather than all hitting the rate limit together.
        "max_concurrent_requests": 4,
    },
    # Behaviour that makes the account read like a person rather than a script.
    "human": {
        "adaptive_style": True,
        "typing_indicator": True,
        "typing_speed_cps": 12,
        "typing_max_seconds": 25,
        "mark_read": True,
    },
    # Online/offline presence, kept independent of typing so "online" doesn't
    # flip on at the exact instant a message goes out.
    "presence": {
        "enabled": True,
        "go_online_delay_min": 2,
        "go_online_delay_max": 8,
        "offline_delay_min": 15,
        "offline_delay_max": 90,
    },
    # Freeform writing samples fed to every reply, so it learns this account's
    # voice from real examples rather than only from the persona description.
    "finetune": {
        "writing_samples": "",
    },
    # Carrying context between two chats that are the same person — a second
    # account, a new number — so a conversation does not restart from nothing.
    "context_link": {
        "enabled": True,
        # Link chats on my behalf when the evidence names one specific person:
        # the same @username, or the same full name. Turned off, the only
        # links are the ones made by hand in the panel.
        "auto_detect": True,
        # Messages of the linked chat handed to the summariser.
        "history_limit": 60,
        # How many linked chats may feed one reply.
        "max_sources": 2,
        # New messages in a linked chat before its brief is rebuilt. Summaries
        # are cached because otherwise every reply would cost two API calls.
        "refresh_after_messages": 5,
    },
    # Messages the assistant starts, to people already in your contacts.
    "outreach": {
        # Telegram rate-limits and penalises bursts of new conversations, so
        # sends are spaced out and capped per day.
        "min_gap_seconds": 90,
        "max_gap_seconds": 300,
        "daily_limit": 20,
        "auto_send": False,
    },
    # Guard rails against the account itself getting limited or banned.
    # Telegram does not publish its thresholds, so these are deliberately
    # conservative: the cost of stopping early is a late reply, the cost of
    # going too far is losing the number.
    "safety": {
        # Total messages sent per day, replies included. Telegram counts all
        # outbound volume, not just conversations we started.
        "daily_send_limit": 150,
        # Distinct people written to per day. Breadth reads as spam much
        # faster than depth does, so this is the tighter of the two.
        "daily_peer_limit": 30,
        # PeerFloodError is Telegram explicitly warning that it considers this
        # account spammy. Continuing after it is what turns a warning into a
        # ban, so the default is to stop everything and wait for a human.
        "halt_on_peer_flood": True,
        # Never open a conversation with someone who is not in the account's
        # contacts and has never written first. Unsolicited messages to
        # strangers are the single biggest driver of spam reports.
        "known_contacts_only": True,
        # A FloodWaitError longer than this is not slept through — the chat is
        # left alone until the next message instead of holding a task open.
        "max_flood_wait_seconds": 300,
    },
    # Appointments: when a client settles on a date and time in chat, the
    # request is put to the provider on another Telegram account, who answers
    # YES or NO, and the client is told. Times are read in timing.timezone.
    "booking": {
        "enabled": False,
        # The account (a person or a bot) that confirms: @username or numeric id.
        "provider": "",
        # Used when the chat did not say how long the appointment is.
        "default_duration_minutes": 60,
        # How many recent messages are read when looking for an agreed time.
        "scan_messages": 20,
        # Blank = no calendar. Otherwise the calendar's ID from Google
        # Calendar -> Settings -> Integrate calendar; see google_calendar.py.
        "google_calendar_id": "",
        # This long before a confirmed appointment the client is asked whether
        # they are still coming. 0 turns the check-in off.
        "reminder_minutes_before": 120,
        # Sent word for word — address, floor, door code, how to get in — the
        # moment the client says they have arrived. Blank = nothing is sent.
        "arrival_instructions": "",
    },
    # Photos and videos in the media library (see media.py) that the AI may
    # attach when a contact asks for them.
    "media": {
        "enabled": True,
        # Videos are never attached the first time they come up: the AI asks
        # whether they want it and sends it once they say yes.
        "ask_before_video": True,
        # Even with auto-send on, a reply carrying a video waits for approval
        # in the panel.
        "videos_need_approval": True,
    },
    # Per-session device identity + proxy-adjacent knobs (Task 7). A blank
    # string means "not yet assigned" — session_runtime.py derives a
    # deterministic default (device_profiles.py) and persists it here the
    # first time the session starts, so it then stays stable forever.
    "identity": {
        "device_model": "",
        "system_version": "",
        "app_version": "",
        "lang_code": "en",
        "system_lang_code": "en-US",
        "lang_pack": "",
        "tz_offset": 0,  # seconds east of UTC
    },
    # Per-contact overrides, keyed by chat_id (as a string). Anything left
    # unset (empty string / null) falls back to the global settings above.
    "contacts": {},
}

MAX_SAMPLE_CHARS = 20_000
CONTACT_TEXT_FIELDS = ("persona_extra", "style_notes", "chat_samples")
CONTACT_LENGTH_CHOICES = ("auto", "short", "medium", "long")
CONTACT_INT_FIELDS = {
    # field: (lo, hi)
    "min_delay_seconds": (0, 86_400),
    "max_delay_seconds": (0, 86_400),
    "typing_speed_cps": (1, 100),
    "typing_max_seconds": (1, 300),
    "online_delay_min": (0, 120),
    "online_delay_max": (0, 120),
    "offline_delay_min": (0, 3600),
    "offline_delay_max": (0, 3600),
}


class ConfigConflict(RuntimeError):
    """save()/save_with_revision() was called with an expected_revision that
    no longer matches the stored row — someone else saved in between."""


def _merge(defaults: dict[str, Any], loaded: Any) -> dict[str, Any]:
    """Defaults, overlaid with whatever was actually stored. Unknown keys drop."""
    out = copy.deepcopy(defaults)
    if not isinstance(loaded, dict):
        return out
    for key, default_value in defaults.items():
        if key not in loaded:
            continue
        value = loaded[key]
        if isinstance(default_value, dict):
            out[key] = _merge(default_value, value)
        else:
            out[key] = value
    return out


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _as_int(value: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _as_float(value: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_sample_text(value: Any) -> str:
    text = _as_text(value)
    return text if len(text) <= MAX_SAMPLE_CHARS else text[:MAX_SAMPLE_CHARS]


def _opt_int(value: Any, lo: int, hi: int) -> Any:
    """An override that may be absent — None/''/null all mean 'use the global setting'."""
    if value is None or value == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _normalize_contact(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for field in CONTACT_TEXT_FIELDS:
        out[field] = _as_sample_text(raw.get(field))
    length = raw.get("message_length")
    out["message_length"] = length if length in CONTACT_LENGTH_CHOICES else "auto"
    for field, (lo, hi) in CONTACT_INT_FIELDS.items():
        out[field] = _opt_int(raw.get(field), lo, hi)
    if out["max_delay_seconds"] is not None and out["min_delay_seconds"] is not None:
        if out["max_delay_seconds"] < out["min_delay_seconds"]:
            out["max_delay_seconds"] = out["min_delay_seconds"]
    return out


def _normalize_contacts(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        try:
            chat_id = str(int(key))
        except (TypeError, ValueError):
            continue
        contact = _normalize_contact(value)
        # Drop entries that are entirely blank, so idle picks in the panel
        # don't pile up as empty rows.
        if any(contact[f] for f in CONTACT_TEXT_FIELDS) or contact["message_length"] != "auto" \
                or any(contact[f] is not None for f in CONTACT_INT_FIELDS):
            out[chat_id] = contact
    return out


def _as_time(value: Any, fallback: str) -> str:
    """Accept 'H:MM' / 'HH:MM', normalise to 'HH:MM'."""
    if not isinstance(value, str):
        return fallback
    parts = value.strip().split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def _normalize_identity(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    idd = DEFAULTS["identity"]
    out: dict[str, Any] = {}
    for field in ("device_model", "system_version", "app_version", "lang_code", "system_lang_code", "lang_pack"):
        out[field] = _as_text(raw.get(field)) or idd[field]
    out["tz_offset"] = _as_int(raw.get("tz_offset"), idd["tz_offset"], -43_200, 50_400)
    return out


def normalize(raw: Any) -> dict[str, Any]:
    # Captured before the generic merge: "contacts" defaults to {}, and the
    # merge only overlays keys the defaults dict actually iterates, so an
    # empty default would otherwise discard whatever was saved here.
    contacts_raw = raw.get("contacts") if isinstance(raw, dict) else None
    cfg = _merge(DEFAULTS, raw)

    persona = cfg["persona"]
    for field in DEFAULTS["persona"]:
        persona[field] = _as_text(persona.get(field))

    timing = cfg["timing"]
    d = DEFAULTS["timing"]
    timing["min_delay_seconds"] = _as_int(timing.get("min_delay_seconds"), d["min_delay_seconds"], 0, 86_400)
    timing["max_delay_seconds"] = _as_int(timing.get("max_delay_seconds"), d["max_delay_seconds"], 0, 86_400)
    if timing["max_delay_seconds"] < timing["min_delay_seconds"]:
        timing["max_delay_seconds"] = timing["min_delay_seconds"]
    timing["active_hours_enabled"] = _as_bool(timing.get("active_hours_enabled"), d["active_hours_enabled"])
    timing["active_hours_start"] = _as_time(timing.get("active_hours_start"), d["active_hours_start"])
    timing["active_hours_end"] = _as_time(timing.get("active_hours_end"), d["active_hours_end"])
    tz = _as_text(timing.get("timezone")) or d["timezone"]
    timing["timezone"] = tz

    behavior = cfg["behavior"]
    for field, default_value in DEFAULTS["behavior"].items():
        behavior[field] = _as_bool(behavior.get(field), default_value)

    human = cfg["human"]
    hd = DEFAULTS["human"]
    for flag in ("adaptive_style", "typing_indicator", "mark_read"):
        human[flag] = _as_bool(human.get(flag), hd[flag])
    human["typing_speed_cps"] = _as_int(
        human.get("typing_speed_cps"), hd["typing_speed_cps"], 1, 100
    )
    human["typing_max_seconds"] = _as_int(
        human.get("typing_max_seconds"), hd["typing_max_seconds"], 1, 300
    )

    out = cfg["outreach"]
    d = DEFAULTS["outreach"]
    out["min_gap_seconds"] = _as_int(out.get("min_gap_seconds"), d["min_gap_seconds"], 5, 86_400)
    out["max_gap_seconds"] = _as_int(out.get("max_gap_seconds"), d["max_gap_seconds"], 5, 86_400)
    if out["max_gap_seconds"] < out["min_gap_seconds"]:
        out["max_gap_seconds"] = out["min_gap_seconds"]
    out["daily_limit"] = _as_int(out.get("daily_limit"), d["daily_limit"], 1, 1000)
    out["auto_send"] = _as_bool(out.get("auto_send"), d["auto_send"])

    ai = cfg["ai"]
    ai["model"] = _as_text(ai.get("model")) or DEFAULTS["ai"]["model"]
    ai["max_tokens"] = _as_int(ai.get("max_tokens"), DEFAULTS["ai"]["max_tokens"], 1, 8192)
    ai["temperature"] = _as_float(ai.get("temperature"), DEFAULTS["ai"]["temperature"], 0.0, 2.0)
    ai["max_concurrent_requests"] = _as_int(
        ai.get("max_concurrent_requests"), DEFAULTS["ai"]["max_concurrent_requests"], 1, 32
    )

    presence = cfg["presence"]
    pd = DEFAULTS["presence"]
    presence["enabled"] = _as_bool(presence.get("enabled"), pd["enabled"])
    presence["go_online_delay_min"] = _as_int(
        presence.get("go_online_delay_min"), pd["go_online_delay_min"], 0, 120
    )
    presence["go_online_delay_max"] = _as_int(
        presence.get("go_online_delay_max"), pd["go_online_delay_max"], 0, 120
    )
    if presence["go_online_delay_max"] < presence["go_online_delay_min"]:
        presence["go_online_delay_max"] = presence["go_online_delay_min"]
    presence["offline_delay_min"] = _as_int(
        presence.get("offline_delay_min"), pd["offline_delay_min"], 0, 3600
    )
    presence["offline_delay_max"] = _as_int(
        presence.get("offline_delay_max"), pd["offline_delay_max"], 0, 3600
    )
    if presence["offline_delay_max"] < presence["offline_delay_min"]:
        presence["offline_delay_max"] = presence["offline_delay_min"]

    safety = cfg["safety"]
    sd = DEFAULTS["safety"]
    safety["daily_send_limit"] = _as_int(safety.get("daily_send_limit"), sd["daily_send_limit"], 1, 10_000)
    safety["daily_peer_limit"] = _as_int(safety.get("daily_peer_limit"), sd["daily_peer_limit"], 1, 10_000)
    safety["max_flood_wait_seconds"] = _as_int(
        safety.get("max_flood_wait_seconds"), sd["max_flood_wait_seconds"], 0, 86_400
    )
    for flag in ("halt_on_peer_flood", "known_contacts_only"):
        safety[flag] = _as_bool(safety.get(flag), sd[flag])

    finetune = cfg["finetune"]
    finetune["writing_samples"] = _as_sample_text(finetune.get("writing_samples"))

    link = cfg["context_link"]
    ld = DEFAULTS["context_link"]
    for flag in ("enabled", "auto_detect"):
        link[flag] = _as_bool(link.get(flag), ld[flag])
    link["history_limit"] = _as_int(link.get("history_limit"), ld["history_limit"], 5, 300)
    link["max_sources"] = _as_int(link.get("max_sources"), ld["max_sources"], 1, 5)
    link["refresh_after_messages"] = _as_int(
        link.get("refresh_after_messages"), ld["refresh_after_messages"], 1, 200
    )

    booking = cfg["booking"]
    bd = DEFAULTS["booking"]
    booking["enabled"] = _as_bool(booking.get("enabled"), bd["enabled"])
    booking["provider"] = _as_text(booking.get("provider"))
    booking["default_duration_minutes"] = _as_int(
        booking.get("default_duration_minutes"), bd["default_duration_minutes"], 5, 24 * 60
    )
    booking["scan_messages"] = _as_int(booking.get("scan_messages"), bd["scan_messages"], 2, 100)
    booking["google_calendar_id"] = _as_text(booking.get("google_calendar_id"))
    booking["reminder_minutes_before"] = _as_int(
        booking.get("reminder_minutes_before"), bd["reminder_minutes_before"], 0, 7 * 24 * 60
    )
    booking["arrival_instructions"] = _as_sample_text(booking.get("arrival_instructions"))

    md = DEFAULTS["media"]
    for field, default_value in md.items():
        cfg["media"][field] = _as_bool(cfg["media"].get(field), default_value)

    cfg["identity"] = _normalize_identity(cfg.get("identity"))

    cfg["contacts"] = _normalize_contacts(contacts_raw)

    return cfg


async def _seed(pool: asyncpg.Pool, session_id: str, clean: dict[str, Any]) -> None:
    payload = json.dumps(clean)
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO session_config (session_id, config, revision, updated_at)
            VALUES ($1, $2::jsonb, 1, now())
            ON CONFLICT (session_id) DO NOTHING
            """,
            session_id,
            payload,
        )


async def load(pool: asyncpg.Pool, session_id: str) -> dict[str, Any]:
    async with pool.acquire() as con:
        raw = await con.fetchval(
            "SELECT config FROM session_config WHERE session_id = $1", session_id
        )
    if raw is None:
        clean = normalize({})
        await _seed(pool, session_id, clean)
        return clean
    if isinstance(raw, str):
        raw = json.loads(raw)
    return normalize(raw)


async def load_many(pool: asyncpg.Pool, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One query for every session a worker owns, used at boot."""
    if not session_ids:
        return {}
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT session_id, config FROM session_config WHERE session_id = ANY($1)",
            session_ids,
        )
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = row["config"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        found[row["session_id"]] = normalize(raw)
    for session_id in session_ids:
        if session_id not in found:
            clean = normalize({})
            await _seed(pool, session_id, clean)
            found[session_id] = clean
    return found


async def save_with_revision(
    pool: asyncpg.Pool,
    session_id: str,
    cfg: dict[str, Any],
    *,
    expected_revision: Optional[int] = None,
) -> tuple[dict[str, Any], int]:
    """Validate then write. `expected_revision`, when given, makes this an
    optimistic-concurrency update: if the stored row's revision has moved on,
    nothing is written and `ConfigConflict` is raised (the panel turns that
    into an HTTP 409) instead of silently clobbering a concurrent change.
    A first save (no row yet) always succeeds regardless of the value passed."""
    clean = normalize(cfg)
    payload = json.dumps(clean)
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            INSERT INTO session_config (session_id, config, revision, updated_at)
            VALUES ($1, $2::jsonb, 1, now())
            ON CONFLICT (session_id) DO UPDATE SET
                config     = $2::jsonb,
                revision   = session_config.revision + 1,
                updated_at = now()
            WHERE $3::int IS NULL OR session_config.revision = $3
            RETURNING config, revision
            """,
            session_id,
            payload,
            expected_revision,
        )
    if row is None:
        raise ConfigConflict(
            f"session_config for {session_id!r} was changed by someone else; reload and retry"
        )
    stored = row["config"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    return stored, row["revision"]


async def save(
    pool: asyncpg.Pool,
    session_id: str,
    cfg: dict[str, Any],
    *,
    expected_revision: Optional[int] = None,
) -> dict[str, Any]:
    """Validate then write; same normalize-then-persist contract as the old
    file-based `save()`. Kept to a single-dict return so ported call sites
    (`config_store.save(...)`) don't need to change shape."""
    clean, _revision = await save_with_revision(
        pool, session_id, cfg, expected_revision=expected_revision
    )
    return clean
