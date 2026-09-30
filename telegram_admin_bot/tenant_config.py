"""The tenant config schema, and how its three layers combine.

Everything that decides how a tenant's bot behaves at runtime (delays,
bursts, quiet hours, caps, filters, language) lives here as validated,
structured data. The LLM never decides any of it.

Layers, lowest precedence first:

1. platform defaults: the field defaults on `TenantConfig` below
2. industry:          `industries.default_config`, a partial override
3. client:            `tenants.config_json`, a partial override

An override is a nested dict with the same shape as `TenantConfig`, holding
only the fields that layer sets. A list field may instead be set to
`{"append": [...]}` to add to the inherited list rather than replace it.
Anything not in the schema is rejected, and so is any value outside its
range: unlike config_store.normalize(), nothing here is silently clamped,
because a saved config has to mean exactly what the operator typed.

The hard limits (the `Field(ge=..., le=...)` bounds) are the platform's own
rules; no layer can override them.
"""

from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any, Literal, Optional, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

PLATFORM = "platform"
INDUSTRY = "industry"
CLIENT = "client"

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LanguagePolicy = Literal["mirror", "fixed:lv", "fixed:ru", "fixed:en"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ReplyDelay(_Strict):
    """Wait between a customer's message and the reply being written."""
    min_s: float = Field(20, ge=0, le=86_400)
    max_s: float = Field(90, ge=0, le=86_400)
    # uniform: any value in [min_s, max_s] equally likely.
    # lognormal: clustered around the middle, with a tail towards max_s,
    # which is closer to how long people actually take to answer.
    distribution: Literal["uniform", "lognormal"] = "uniform"

    @model_validator(mode="after")
    def _ordered(self) -> "ReplyDelay":
        if self.max_s < self.min_s:
            raise ValueError("reply_delay.max_s must be at least reply_delay.min_s")
        return self


class GapMs(_Strict):
    min: int = Field(600, ge=0, le=60_000)
    max: int = Field(2200, ge=0, le=60_000)

    @model_validator(mode="after")
    def _ordered(self) -> "GapMs":
        if self.max < self.min:
            raise ValueError("gap_ms.max must be at least gap_ms.min")
        return self


class Burst(_Strict):
    """A reply may go out as several short messages in a row."""
    max_messages: int = Field(4, ge=1, le=10)
    gap_ms: GapMs = Field(default_factory=GapMs)


class QuietHours(_Strict):
    """A window, in the tenant's timezone, when nothing is sent. Replies due
    inside it wait until it ends. The window may cross midnight."""
    enabled: bool = False
    start: str = "21:00"
    end: str = "09:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("must be HH:MM, 24-hour")
        return value


class Human(_Strict):
    adaptive_style: bool = True
    typing_indicator: bool = True
    typing_speed_cps: int = Field(12, ge=1, le=100)
    typing_max_seconds: int = Field(25, ge=1, le=300)
    mark_read: bool = True


class Presence(_Strict):
    enabled: bool = True
    go_online_delay_min: int = Field(2, ge=0, le=120)
    go_online_delay_max: int = Field(8, ge=0, le=120)
    offline_delay_min: int = Field(15, ge=0, le=3600)
    offline_delay_max: int = Field(90, ge=0, le=3600)

    @model_validator(mode="after")
    def _ordered(self) -> "Presence":
        if self.go_online_delay_max < self.go_online_delay_min:
            raise ValueError("presence.go_online_delay_max must be at least go_online_delay_min")
        if self.offline_delay_max < self.offline_delay_min:
            raise ValueError("presence.offline_delay_max must be at least offline_delay_min")
        return self


class AI(_Strict):
    model: str = Field("deepseek-chat", min_length=1, max_length=64)
    max_tokens: int = Field(400, ge=1, le=8192)
    temperature: float = Field(1.0, ge=0.0, le=2.0)
    max_concurrent_requests: int = Field(4, ge=1, le=32)


class Safety(_Strict):
    """Guard rails that protect the Telegram account itself."""
    daily_peer_cap: int = Field(30, ge=1, le=10_000)
    halt_on_peer_flood: bool = True
    known_contacts_only: bool = True
    max_flood_wait_seconds: int = Field(300, ge=0, le=86_400)


class Outreach(_Strict):
    """Messages the bot starts. Off for tenants unless turned on."""
    enabled: bool = False
    min_gap_seconds: int = Field(90, ge=5, le=86_400)
    max_gap_seconds: int = Field(300, ge=5, le=86_400)
    daily_limit: int = Field(20, ge=1, le=1000)
    auto_send: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> "Outreach":
        if self.max_gap_seconds < self.min_gap_seconds:
            raise ValueError("outreach.max_gap_seconds must be at least min_gap_seconds")
        return self


class ContextLink(_Strict):
    """Borrowing context from another chat of the same person. Off for
    tenants: it stores free-text summaries about people."""
    enabled: bool = False
    auto_detect: bool = True
    history_limit: int = Field(60, ge=5, le=300)
    max_sources: int = Field(2, ge=1, le=5)
    refresh_after_messages: int = Field(5, ge=1, le=200)


class Media(_Strict):
    enabled: bool = True
    ask_before_video: bool = True
    videos_need_approval: bool = True


class Reminder(_Strict):
    """One reminder to the customer before a confirmed booking."""
    minutes_before: int = Field(ge=5, le=30 * 24 * 60)
    # What this reminder should say, as an instruction for the reply writer.
    # Empty: a short check that they are still coming.
    instruction: str = Field("", max_length=2000)


def _default_reminders() -> list["Reminder"]:
    return [Reminder(minutes_before=24 * 60), Reminder(minutes_before=120)]


class Booking(_Strict):
    enabled: bool = False
    # The owner's Telegram (username, phone or id). Requests go there from
    # this account; the owner answers YES n, NO n or with a new time.
    provider: str = Field("", max_length=64)
    # Where the e-mail record of each confirmation, change and cancellation
    # goes. Needs SMTP_* in .env; empty = no e-mail.
    owner_email: str = Field("", max_length=254)
    default_duration_minutes: int = Field(60, ge=5, le=24 * 60)
    scan_messages: int = Field(20, ge=2, le=100)
    google_calendar_id: str = Field("", max_length=256)
    reminders: list[Reminder] = Field(default_factory=_default_reminders, max_length=5)
    # Earliest and latest a booking may start, counted from now.
    min_notice_minutes: int = Field(60, ge=0, le=30 * 24 * 60)
    max_days_ahead: int = Field(90, ge=1, le=730)
    # Local dates (YYYY-MM-DD) with no bookings at all: holidays, days off.
    closed_dates: list[str] = Field(default_factory=list, max_length=366)
    # How many free times to offer when the requested one is taken.
    offer_alternatives: int = Field(3, ge=0, le=10)
    waitlist_enabled: bool = True
    # How long a freed slot is held for the first person on the waitlist
    # before it is offered to the next one.
    waitlist_offer_hours: int = Field(12, ge=1, le=168)
    # Sent word for word, once, when the customer has arrived.
    arrival_instructions: str = Field("", max_length=20_000)
    # Compare a photo the customer sends on arrival with the entrance
    # photos in the media library (marked as entrance reference). Needs
    # vision.enabled.
    arrival_photo_check: bool = False
    # With the photo check on: saying "I'm here" is not enough, the
    # arrival instructions wait for a photo that matches.
    arrival_requires_photo: bool = False
    arrival_photo_min_confidence: float = Field(0.7, ge=0.0, le=1.0)

    @field_validator("reminders")
    @classmethod
    def _distinct_reminders(cls, value: list[Reminder]) -> list[Reminder]:
        seen = set()
        for reminder in value:
            if reminder.minutes_before in seen:
                raise ValueError(f"two reminders at {reminder.minutes_before} minutes before")
            seen.add(reminder.minutes_before)
        return sorted(value, key=lambda r: -r.minutes_before)

    @field_validator("closed_dates")
    @classmethod
    def _iso_dates(cls, value: list[str]) -> list[str]:
        out = []
        for item in value:
            item = item.strip()
            try:
                date.fromisoformat(item)
            except ValueError:
                raise ValueError(f"{item!r} is not a date (YYYY-MM-DD)") from None
            if item not in out:
                out.append(item)
        return sorted(out)

    @field_validator("owner_email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip()
        if value and not _EMAIL.match(value):
            raise ValueError("not an e-mail address")
        return value


class Vision(_Strict):
    """Photos, through a separate vision model (DeepSeek cannot see images).
    The endpoint and key are VISION_API_URL / VISION_API_KEY in .env."""
    enabled: bool = False
    model: str = Field("", max_length=64)
    # Describe photos customers send so the reply can take them into account.
    describe_photos: bool = True


class Limits(_Strict):
    """How much AI this client may use. 0 = no limit. At a limit the bot
    stops writing replies (messages are still received and shown) until the
    period ends or the limit is raised."""
    daily_tokens: int = Field(0, ge=0, le=100_000_000)
    monthly_tokens: int = Field(0, ge=0, le=1_000_000_000)
    daily_spend_eur: float = Field(0.0, ge=0, le=10_000)


class Replies(_Strict):
    """Keeps the bot from answering too much. Counts AI-written messages
    (each part of a burst is one), sent or waiting for approval. 0 = no limit."""
    max_messages_per_chat_per_hour: int = Field(0, ge=0, le=200)
    max_messages_per_chat_per_day: int = Field(0, ge=0, le=2000)
    # Least time between two replies in one chat.
    min_gap_seconds: int = Field(0, ge=0, le=86_400)
    # Don't reply when the customer's message is only one of these
    # (compared ignoring case, spaces and trailing punctuation).
    skip_acknowledgements: bool = False
    acknowledgements: list[str] = Field(
        default_factory=lambda: ["ok", "okay", "thanks", "thank you", "paldies", "labi", "спасибо", "ок", "👍", "🙏"],
        max_length=200,
    )
    # When the bot should not answer, in your own words. Empty = it always
    # answers. When set, the reply writer may decline to answer a message
    # that matches; each time is noted in the chat and in the audit log.
    no_reply_instruction: str = Field("", max_length=2000)


class Anomaly(_Strict):
    """Switch the client off by itself, and alert the operator, when
    something looks wrong (anomaly.py). A person resumes it."""
    # A Telegram login appears on the account that was not there before.
    new_login_suspend: bool = True
    # Sent in the last hour at least this many times the account's own
    # average hour over the last volume_baseline_days days. 0 = off.
    volume_multiplier: float = Field(5.0, ge=0, le=100)
    # ...and never below this many messages in the hour.
    volume_min_messages: int = Field(30, ge=1, le=10_000)
    volume_baseline_days: int = Field(7, ge=1, le=60)
    # A reply the bot wrote links to a domain nobody allowed, or contains a
    # wallet address or an IBAN (policy.py). The reply is held either way.
    tripwire_suspend: bool = True


class Staging(_Strict):
    """Staging: the bot answers only the listed test chats (usernames with
    or without @, or numeric chat ids); everyone else is stored, not
    answered. Turning it off is "go live"."""
    enabled: bool = False
    test_chats: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("test_chats")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            item = item.strip().lstrip("@").lower()
            if item and item not in out:
                out.append(item)
        return out


class Digest(_Strict):
    """A weekly summary for the owner: by Telegram (booking.provider) and,
    with SMTP set up, by e-mail."""
    enabled: bool = True
    # 0 = Monday ... 6 = Sunday, and the hour, in the client's timezone.
    weekday: int = Field(0, ge=0, le=6)
    hour: int = Field(9, ge=0, le=23)
    # Empty = booking.owner_email.
    email: str = Field("", max_length=254)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip()
        if value and not _EMAIL.match(value):
            raise ValueError("not an e-mail address")
        return value


class Unanswered(_Strict):
    """What counts as unanswered, besides a message that got no reply at all:
    a reply containing one of these phrases (case ignored)."""
    fallback_phrases: list[str] = Field(default_factory=list, max_length=100)


class TenantConfig(_Strict):
    timezone: str = "Europe/Riga"
    # Off: every reply waits in the panel for approval. On: replies that pass
    # the policy checks (policy.py) go out by themselves.
    auto_send: bool = False
    reply_delay: ReplyDelay = Field(default_factory=ReplyDelay)
    burst: Burst = Field(default_factory=Burst)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    # A customer message containing one of these (a word or the start of
    # one, any case) pauses that chat and pings the owner (booking.provider).
    escalation_keywords: list[str] = Field(default_factory=list, max_length=200)
    # Someone wrote in a chat by hand (on the phone, or from the panel): the
    # bot stays quiet in that chat for this many hours, then carries on by
    # itself. 0 = off.
    takeover_hours: float = Field(12, ge=0, le=720)
    # A reply that mentions one of these is held for approval (policy.py).
    banned_topics: list[str] = Field(default_factory=list, max_length=200)
    # Service name -> lowest price in EUR the bot may state for it.
    price_floors: dict[str, float] = Field(default_factory=dict)
    # Web domains the bot may link to; any other link holds the reply.
    allowed_link_domains: list[str] = Field(default_factory=list, max_length=100)
    # Phone numbers / e-mail addresses the business allows the bot to share;
    # any other one in a reply holds it.
    shareable_contacts: list[str] = Field(default_factory=list, max_length=50)
    # Every message the account sends per day, replies included.
    daily_message_cap: int = Field(150, ge=1, le=5000)
    # Every message the account sends per hour. 0 = no limit.
    hourly_message_cap: int = Field(0, ge=0, le=1000)
    # AI spend per calendar month (tenant timezone), in EUR. 0 = no limit.
    # See also `limits`.
    api_spend_cap_eur: float = Field(10.0, ge=0, le=10_000)
    language_policy: LanguagePolicy = "mirror"
    human: Human = Field(default_factory=Human)
    presence: Presence = Field(default_factory=Presence)
    ai: AI = Field(default_factory=AI)
    safety: Safety = Field(default_factory=Safety)
    outreach: Outreach = Field(default_factory=Outreach)
    context_link: ContextLink = Field(default_factory=ContextLink)
    media: Media = Field(default_factory=Media)
    booking: Booking = Field(default_factory=Booking)
    vision: Vision = Field(default_factory=Vision)
    limits: Limits = Field(default_factory=Limits)
    replies: Replies = Field(default_factory=Replies)
    anomaly: Anomaly = Field(default_factory=Anomaly)
    staging: Staging = Field(default_factory=Staging)
    digest: Digest = Field(default_factory=Digest)
    unanswered: Unanswered = Field(default_factory=Unanswered)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        if ZoneInfo is not None:
            try:
                ZoneInfo(value)
            except Exception:
                raise ValueError(f"unknown IANA timezone {value!r}") from None
        return value

    @field_validator("price_floors")
    @classmethod
    def _non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        for service, price in value.items():
            if not service.strip():
                raise ValueError("price_floors has an empty service name")
            if price < 0:
                raise ValueError(f"price_floors[{service!r}] is negative")
        return value

    @field_validator("escalation_keywords", "banned_topics", "allowed_link_domains", "shareable_contacts")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            item = item.strip()
            if item and item not in out:
                out.append(item)
        return out


# The client layer a brand-new WhatsApp account starts with (the panel
# saves it, audited, when the number is first paired; a re-paired number
# keeps whatever its config says by then). WhatsApp bans numbers for volume
# and breadth much sooner than Telegram limits them, and a linked device
# that answers in seconds, around the clock, in long bursts, reads as a bot.
# So: fewer messages per day and per hour, fewer distinct people per day,
# slower replies with a human-shaped spread, shorter bursts with longer
# gaps, a quiet night, replies held for approval and no outreach.
WHATSAPP_CLIENT_DEFAULTS: dict[str, Any] = {
    "auto_send": False,
    "daily_message_cap": 60,
    "hourly_message_cap": 15,
    "safety": {"daily_peer_cap": 15},
    "reply_delay": {"min_s": 45, "max_s": 180, "distribution": "lognormal"},
    "burst": {"max_messages": 3, "gap_ms": {"min": 1200, "max": 3500}},
    "quiet_hours": {"enabled": True, "start": "21:00", "end": "09:00"},
    "outreach": {"enabled": False},
}


class ConfigError(ValueError):
    """An override or the config it resolves to is invalid. `errors` is a
    list of {"path", "message"} for the panel to show next to fields."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


# ------------------------------------------------------------------ merging


def _submodel(annotation: Any) -> Optional[type[BaseModel]]:
    return annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None


def _is_list(annotation: Any) -> bool:
    return get_origin(annotation) is list


def _apply(
    model: type[BaseModel],
    base: dict[str, Any],
    override: Any,
    layer: str,
    sources: dict[str, str],
    prefix: str,
    errors: list[dict[str, str]],
) -> None:
    """Overlay one layer's override onto `base` in place, walking the schema
    so only sub-models are merged field by field. Every other field (a
    number, a list, the price_floors map) is one leaf, replaced whole."""
    if not isinstance(override, dict):
        errors.append({"path": prefix or "(root)", "message": "must be an object"})
        return
    for key, value in override.items():
        path = f"{prefix}{key}"
        field = model.model_fields.get(key)
        if field is None:
            errors.append({"path": path, "message": "not a config field"})
            continue
        sub = _submodel(field.annotation)
        if sub is not None:
            _apply(sub, base[key], value, layer, sources, path + ".", errors)
            continue
        if _is_list(field.annotation) and isinstance(value, dict):
            if set(value) != {"append"} or not isinstance(value["append"], list):
                errors.append({"path": path, "message": 'a list override is a list or {"append": [...]}'})
                continue
            base[key] = list(base[key]) + [v for v in value["append"] if v not in base[key]]
        else:
            base[key] = copy.deepcopy(value)
        sources[path] = layer


def leaf_paths(model: type[BaseModel] = TenantConfig, prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, field in model.model_fields.items():
        sub = _submodel(field.annotation)
        if sub is not None:
            out.extend(leaf_paths(sub, f"{prefix}{key}."))
        else:
            out.append(prefix + key)
    return out


class Resolved:
    """The effective config for a tenant and where each leaf came from."""

    def __init__(self, config: TenantConfig, sources: dict[str, str]) -> None:
        self.config = config
        self.sources = sources

    def as_dict(self) -> dict[str, Any]:
        return self.config.model_dump(mode="json")


def resolve(industry: Optional[dict[str, Any]] = None, client: Optional[dict[str, Any]] = None) -> Resolved:
    """Platform defaults <- industry <- client. Raises ConfigError listing
    every problem, with paths, if an override is malformed or the merged
    result breaks a rule."""
    merged = TenantConfig().model_dump(mode="json")
    sources = {path: PLATFORM for path in leaf_paths()}
    errors: list[dict[str, str]] = []
    _apply(TenantConfig, merged, industry or {}, INDUSTRY, sources, "", errors)
    _apply(TenantConfig, merged, client or {}, CLIENT, sources, "", errors)
    if errors:
        raise ConfigError(errors)
    try:
        config = TenantConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError([
            {"path": ".".join(str(p) for p in e["loc"]) or "(root)", "message": e["msg"]}
            for e in exc.errors()
        ]) from None
    return Resolved(config, sources)


def get_path(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        node = node[part]
    return node


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Leaf-by-leaf changes between two full configs, for the audit log and
    for showing a proposed change before it is applied."""
    changes = []
    for path in leaf_paths():
        old, new = get_path(before, path), get_path(after, path)
        if old != new:
            changes.append({"path": path, "from": old, "to": new})
    return changes


def field_catalog(resolved: Resolved, inherited: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per leaf for the panel: its type, current value, the layer it
    comes from, and what it would be without the client layer (`inherited`,
    the industry-resolved config) so a reset can be previewed."""
    effective = resolved.as_dict()
    rows = []
    for path in leaf_paths():
        field = _field_for(path)
        rows.append({
            "path": path,
            "kind": _kind(field.annotation, field),
            "choices": list(get_args(field.annotation)) if get_origin(field.annotation) is Literal else None,
            "value": get_path(effective, path),
            "inherited_value": get_path(inherited, path),
            "source": resolved.sources[path],
            "description": field.description or "",
        })
    return rows


def _field_for(path: str):
    model: type[BaseModel] = TenantConfig
    parts = path.split(".")
    for part in parts[:-1]:
        model = _submodel(model.model_fields[part].annotation)  # type: ignore[assignment]
    return model.model_fields[parts[-1]]


def _kind(annotation: Any, field: Any = None) -> str:
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if get_origin(annotation) is Literal:
        return "choice"
    if _is_list(annotation):
        # A list of objects (booking.reminders) is edited as JSON.
        (item,) = get_args(annotation) or (str,)
        return "json" if _submodel(item) is not None else "list"
    if get_origin(annotation) is dict:
        return "map"
    if field is not None and any((getattr(m, "max_length", 0) or 0) > 256 for m in field.metadata):
        return "longtext"
    return "text"
