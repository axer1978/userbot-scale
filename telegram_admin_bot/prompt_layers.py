"""Prompt inheritance: base (platform) -> industry template -> client.

Three layers, rendered into one system prompt with explicit sections:

- base:     the platform's own rules. Rendered first, restated as taking
            precedence at the end. No lower layer can edit, remove or
            replace them: there is no section key that reaches them.
- industry: text for each named section in SECTIONS. An industry can hold
            sub-folders, any levels deep; a sub-folder's template holds
            only its changes to its parent's (validate_folder), and its
            clients get the result (apply_folder).
- client:   per-section overrides, each either `override` (replaces the
            industry text for that section) or `append` (added after it),
            plus a short free-text addendum.
- rules:    the operator's corrections as short sentences (rules.py), for
            the industry or the account, after everything the business
            wrote. Not a layer of their own: once a week they are folded
            into the industry and client layers and stop being added here.

Only the section keys below exist. A client cannot add a section, and in
particular cannot target the platform rules. The prompt is not the defence
against a client (or a customer) contradicting the platform rules anyway;
policy.py checks every outbound message in code. The rendering just makes
the precedence unambiguous to the model.

Pure functions only: storage and versioning live in tenants.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Optional, Sequence

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
RULES_HEADER = "RULES FROM THE OPERATOR (corrections; the PLATFORM RULES above still come first):"
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


def _validate_overrides(overrides: Any, *, strict: bool, who: str) -> dict[str, dict[str, str]]:
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
                    f"{key} always apply. Put only this {who}'s extra rules in it."
                )
            mode = "append"
        text = _text(value["text"], f"override {key!r}", MAX_SECTION_CHARS)
        # An empty override would silently blank the industry's section.
        if not text:
            raise PromptError(f"override {key!r} is empty; remove it to inherit instead")
        clean[key] = {"mode": mode, "text": text}
    return clean


def validate_client(content: Any, *, strict: bool = True) -> dict[str, Any]:
    """strict=False is for rendering versions already stored: an override of
    an append-only section saved before that rule existed is read as an
    append instead of refused, so the bot keeps running."""
    if not isinstance(content, dict) or set(content) - {"overrides", "addendum"}:
        raise PromptError('client content must be {"overrides": {...}, "addendum": "..."}')
    clean = _validate_overrides(content.get("overrides") or {}, strict=strict, who="client")
    addendum = _text(content.get("addendum", ""), "addendum", MAX_ADDENDUM_CHARS)
    return {"overrides": clean, "addendum": addendum}


def validate_folder(content: Any, *, strict: bool = True) -> dict[str, Any]:
    """A sub-folder's template: only what it changes in its parent's, per
    section, the way a client layer does (no addendum: that is the
    client's own). The industry's boundaries stay in force here too."""
    if not isinstance(content, dict) or set(content) - {"overrides"}:
        raise PromptError('a sub-folder template must be {"overrides": {...}}')
    return {"overrides": _validate_overrides(content.get("overrides") or {}, strict=strict, who="folder")}


def apply_folder(sections: dict[str, str], folder: dict[str, Any]) -> dict[str, str]:
    """The sections a sub-folder's clients inherit: its parent's (`sections`)
    with the sub-folder's own changes on top."""
    out = dict(sections)
    for key, override in validate_folder(folder, strict=False)["overrides"].items():
        if override["mode"] == "override":
            out[key] = override["text"]
        else:
            out[key] = f"{out.get(key, '')}\n{override['text']}".strip()
    return {key: out[key] for key in SECTION_KEYS if out.get(key)}


def folder_changes(parent: dict[str, str], wanted: dict[str, str]) -> dict[str, Any]:
    """The sub-folder template that turns the parent's sections into
    `wanted` (a whole standard, as a finetune run writes one): a section the
    same as the parent's is inherited, one that only adds to the end of it
    is an append, anything else an override. Boundaries can only be added
    to, so only what is new in them is kept, line by line."""
    overrides: dict[str, dict[str, str]] = {}
    for key in SECTION_KEYS:
        have, want = (parent.get(key) or "").strip(), (wanted.get(key) or "").strip()
        if not want or want == have:
            continue
        if have and want.startswith(have) and want[len(have):].strip():
            overrides[key] = {"mode": "append", "text": want[len(have):].strip()}
        elif key in APPEND_ONLY_SECTIONS:
            known = {line.strip() for line in have.splitlines()}
            new = [line for line in want.splitlines() if line.strip() and line.strip() not in known]
            if new:
                overrides[key] = {"mode": "append", "text": "\n".join(new).strip()}
        else:
            overrides[key] = {"mode": "override", "text": want}
    return validate_folder({"overrides": overrides})


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


def layers_tag(versions: tuple[int, int, int, int], parents: Sequence[tuple[int, int]] = ()) -> str:
    """"b1/i1v3/c2": base v1, industry #1 at v3, client v2 (c0 = none). In
    a sub-folder the folders above it come first, top one first:
    "b1/i2v10/i5v3/c2" is sub-folder #5 at v3 inside industry #2 at v10."""
    base_v, industry_id, industry_v, client_v = versions
    above = "".join(f"/i{folder_id}v{folder_v}" for folder_id, folder_v in parents)
    return f"b{base_v}{above}/i{industry_id}v{industry_v}/c{client_v}"


def rules_tag(rules: Sequence[str]) -> str:
    """"/r3-1a2b": three operator rules, and a short hash of their text, so
    a reply's tag tells which rules it was written under; "" without any."""
    if not rules:
        return ""
    digest = sha256("\n".join(rules).encode("utf-8")).hexdigest()
    return f"/r{len(rules)}-{digest[:4]}"


def _rules(rules: Optional[Sequence[str]]) -> list[str]:
    if rules is None:
        return []
    if isinstance(rules, str) or not all(isinstance(rule, str) for rule in rules):
        raise PromptError("rules must be a list of texts")
    return [" ".join(rule.split()) for rule in rules if rule.strip()]


@dataclass(frozen=True)
class Rendered:
    text: str
    # e.g. "b1/i1v3/c2": base v1, industry #1 at v3, client v2 (c0 = none);
    # with operator rules, "b1/i1v3/c2/r3-1a2b" (rules_tag).
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
    rules: Optional[Sequence[str]] = None,
    parents: Sequence[tuple[int, int]] = (),
) -> Rendered:
    """versions = (base_version, industry_id, industry_version, client_version).
    `industry` is the template the client's folder hands down, its parent
    folders' already applied; `parents` = (folder id, version) of those
    folders, top one first, for the version tag.

    `rules` are the operator's active rules (rules.active_for), one line
    each, after everything the business wrote and before the precedence
    footer: they correct the business sections, never the platform rules."""
    base = validate_base(base)
    industry = validate_industry(industry)
    client = validate_client(client or {"overrides": {}, "addendum": ""}, strict=False)
    rules = _rules(rules)

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
    if rules:
        block = RULES_HEADER + "\n" + "\n".join(f"- {rule}" for rule in rules)
        parts.append(block)
        business_parts.append(block)
    parts.append(PRECEDENCE_FOOTER)

    return Rendered(
        text="\n\n".join(parts),
        version_tag=layers_tag(versions, parents) + rules_tag(rules),
        business_text="\n\n".join(business_parts),
    )
