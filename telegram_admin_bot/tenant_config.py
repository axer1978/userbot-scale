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


class Booking(_Strict):
    enabled: bool = False
    provider: str = Field("", max_length=64)
    default_duration_minutes: int = Field(60, ge=5, le=24 * 60)
    scan_messages: int = Field(20, ge=2, le=100)
    google_calendar_id: str = Field("", max_length=256)
    reminder_minutes_before: int = Field(120, ge=0, le=7 * 24 * 60)
    arrival_instructions: str = Field("", max_length=20_000)


class TenantConfig(_Strict):
    timezone: str = "Europe/Riga"
    # Off: every reply waits in the panel for approval. On: replies that pass
    # the policy checks (policy.py) go out by themselves.
    auto_send: bool = False
    reply_delay: ReplyDelay = Field(default_factory=ReplyDelay)
    burst: Burst = Field(default_factory=Burst)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    # Phase 2: a free slot is confirmed at once instead of waiting for the owner.
    auto_confirm: bool = False
    # Phase 3: a customer message containing one of these pauses the chat
    # and pings the owner.
    escalation_keywords: list[str] = Field(default_factory=list, max_length=200)
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
    # Phase 3 enforces this; usage is metered from phase 1 (llm_usage.py).
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
            "kind": _kind(field.annotation),
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


def _kind(annotation: Any) -> str:
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if get_origin(annotation) is Literal:
        return "choice"
    if _is_list(annotation):
        return "list"
    if get_origin(annotation) is dict:
        return "map"
    return "text"
