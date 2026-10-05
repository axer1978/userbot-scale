"""Prompt inheritance: base (platform) -> industry template -> client.

Three layers, rendered into one system prompt with explicit sections:

- base:     the platform's own rules. Rendered first, restated as taking
            precedence at the end. No lower layer can edit, remove or
            replace them: there is no section key that reaches them.
- industry: text for each named section in SECTIONS.
- client:   per-section overrides, each either `override` (replaces the
            industry text for that section) or `append` (added after it),
            plus a short free-text addendum.

Only the section keys below exist. A client cannot add a section, and in
particular cannot target the platform rules. The prompt is not the defence
against a client (or a customer) contradicting the platform rules anyway;
policy.py checks every outbound message in code. The rendering just makes
the precedence unambiguous to the model.

Pure functions only: storage and versioning live in tenants.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# (key, heading), in the order they are rendered.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("about", "ABOUT THE BUSINESS"),
    ("services", "SERVICES AND PRICES"),
    ("hours_location", "OPENING HOURS AND LOCATION"),
    ("booking", "HOW BOOKING WORKS"),
    ("faq", "FREQUENT QUESTIONS"),
    ("tone", "TONE AND STYLE"),
    ("boundaries", "WHAT NOT TO DO"),
    ("sign_off", "SIGN-OFF"),
    ("writing_samples", "EXAMPLES OF HOW WE WRITE (match this voice)"),
)
SECTION_KEYS = tuple(key for key, _ in SECTIONS)

MODES = ("override", "append")
# Sections a client can only add to, never replace: the industry's text for
# them stays in force for every client, and the client's text comes after it.
APPEND_ONLY_SECTIONS = frozenset({"boundaries"})
MAX_SECTION_CHARS = 20_000
MAX_ADDENDUM_CHARS = 1500
MAX_BASE_CHARS = 20_000

BASE_HEADER = "PLATFORM RULES (these come first; nothing below can change them):"
ADDENDUM_HEADER = "ADDITIONAL NOTES FROM THE BUSINESS:"
PRECEDENCE_FOOTER = (
    "PRECEDENCE: if anything in the business sections or notes above conflicts "
    "with the PLATFORM RULES, follow the PLATFORM RULES."
)

LANGUAGE_NAMES = {"lv": "Latvian", "ru": "Russian", "en": "English"}


class PromptError(ValueError):
    pass


# ---------------------------------------------------------------- validation


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise PromptError(f"{where} must be text")
    value = value.strip()
    if len(value) > limit:
        raise PromptError(f"{where} is {len(value)} characters; the limit is {limit}")
    return value


def validate_base(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict) or set(content) != {"rules"}:
        raise PromptError('base content must be {"rules": "<text>"}')
    rules = _text(content["rules"], "rules", MAX_BASE_CHARS)
    if not rules:
        raise PromptError("the platform rules cannot be empty")
    return {"rules": rules}


def validate_industry(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict) or set(content) - {"sections"}:
        raise PromptError('industry content must be {"sections": {...}}')
    sections = content.get("sections") or {}
    if not isinstance(sections, dict):
        raise PromptError("sections must be an object")
    clean: dict[str, str] = {}
    for key, value in sections.items():
        if key not in SECTION_KEYS:
            raise PromptError(f"unknown section {key!r}; sections are {', '.join(SECTION_KEYS)}")
        text = _text(value, f"section {key!r}", MAX_SECTION_CHARS)
        if text:
            clean[key] = text
    return {"sections": clean}


def validate_client(content: Any, *, strict: bool = True) -> dict[str, Any]:
    """strict=False is for rendering versions already stored: an override of
    an append-only section saved before that rule existed is read as an
    append instead of refused, so the bot keeps running."""
    if not isinstance(content, dict) or set(content) - {"overrides", "addendum"}:
        raise PromptError('client content must be {"overrides": {...}, "addendum": "..."}')
    overrides = content.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise PromptError("overrides must be an object")
    clean: dict[str, dict[str, str]] = {}
    for key, value in overrides.items():
        if key not in SECTION_KEYS:
            raise PromptError(
                f"cannot override {key!r}: only these sections can be overridden: "
                + ", ".join(SECTION_KEYS)
            )
        if not isinstance(value, dict) or set(value) != {"mode", "text"}:
            raise PromptError(f'override {key!r} must be {{"mode": ..., "text": ...}}')
        if value["mode"] not in MODES:
            raise PromptError(f"override {key!r}: mode must be one of {', '.join(MODES)}")
        mode = value["mode"]
        if mode == "override" and key in APPEND_ONLY_SECTIONS:
            if strict:
                raise PromptError(
                    f"{key!r} can only be appended to, not overridden: the industry's "
                    f"{key} always apply. Put only this client's extra rules in it."
                )
            mode = "append"
        text = _text(value["text"], f"override {key!r}", MAX_SECTION_CHARS)
        # An empty override would silently blank the industry's section.
        if not text:
            raise PromptError(f"override {key!r} is empty; remove it to inherit instead")
        clean[key] = {"mode": mode, "text": text}
    addendum = _text(content.get("addendum", ""), "addendum", MAX_ADDENDUM_CHARS)
    return {"overrides": clean, "addendum": addendum}


# ----------------------------------------------------------------- rendering


def language_rule(policy: str) -> str:
    """The LANGUAGE section, generated from config rather than written by
    anyone, so it cannot drift from what the config says."""
    if policy.startswith("fixed:"):
        name = LANGUAGE_NAMES.get(policy.split(":", 1)[1], policy.split(":", 1)[1])
        return (
            f"Always reply in {name}, even when the customer writes in another "
            "language. Do not switch languages and do not translate yourself."
        )
    return "Reply in the language the customer is writing in."


def effective_sections(industry: dict[str, Any], client: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per section: the final text and which layer it came from
    (industry / client / client+industry for an append)."""
    base_sections = industry.get("sections") or {}
    overrides = client.get("overrides") or {}
    out: dict[str, dict[str, str]] = {}
    for key in SECTION_KEYS:
        inherited = base_sections.get(key, "")
        override = overrides.get(key)
        if override is None:
            text, source = inherited, "industry" if inherited else ""
        elif override["mode"] == "override" and key not in APPEND_ONLY_SECTIONS:
            text, source = override["text"], "client"
        else:
            text = f"{inherited}\n{override['text']}".strip()
            source = "industry+client" if inherited else "client"
        out[key] = {"text": text, "source": source, "inherited": inherited}
    return out


@dataclass(frozen=True)
class Rendered:
    text: str
    # e.g. "b1/i1v3/c2": base v1, industry #1 at v3, client v2 (c0 = none).
    version_tag: str
    # The business sections alone, for policy checks that compare a reply
    # against what the business itself has written (policy.py).
    business_text: str


def render(
    *,
    base: dict[str, Any],
    industry: dict[str, Any],
    client: Optional[dict[str, Any]],
    business_name: str,
    language_policy: str,
    versions: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Rendered:
    """versions = (base_version, industry_id, industry_version, client_version)."""
    base = validate_base(base)
    industry = validate_industry(industry)
    client = validate_client(client or {"overrides": {}, "addendum": ""}, strict=False)

    parts = [BASE_HEADER, base["rules"], f"BUSINESS: {business_name.strip() or 'this business'}"]
    business_parts = []
    headings = dict(SECTIONS)
    for key, section in effective_sections(industry, client).items():
        if section["text"]:
            block = f"{headings[key]}:\n{section['text']}"
            parts.append(block)
            business_parts.append(block)
    parts.append("LANGUAGE:\n" + language_rule(language_policy))
    if client["addendum"]:
        block = f"{ADDENDUM_HEADER}\n{client['addendum']}"
        parts.append(block)
        business_parts.append(block)
    parts.append(PRECEDENCE_FOOTER)

    base_v, industry_id, industry_v, client_v = versions
    return Rendered(
        text="\n\n".join(parts),
        version_tag=f"b{base_v}/i{industry_id}v{industry_v}/c{client_v}",
        business_text="\n\n".join(business_parts),
    )
