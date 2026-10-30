"""The page where an account sets how the bot's messages to the booker
look (booking.booker_language, booking.booker_messages): what it shows, its
live preview, and the save. Shared by the admin panel (platform_api) and the
owner's own pages (owner_api), so both see and save exactly the same."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field

import booker_texts
import bookings
import tenants


class MessagesBody(BaseModel):
    """A save (or a preview) from the page: her language, and the account's
    own text by message (empty or missing: the default)."""
    language: str = booker_texts.DEFAULT_LANGUAGE
    messages: dict[str, str] = Field(default_factory=dict)
    expected_revision: Optional[int] = None
    reason: str = Field("", max_length=500)


def _name(bundle: tenants.Bundle) -> str:
    """Whose booking it is, as the messages to her say (booking.name, else
    the account's name)."""
    return (bundle.config["booking"].get("name") or "").strip() or bundle.tenant["name"]


def preview(bundle: tenants.Bundle, language: str, messages: Mapping[str, str], *,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Each main message as the bot would send it with these settings, for
    a made-up booking — and what is wrong with any text (that one then shows
    as its default, as the bot would send it)."""
    problems = {key: found for key, text in messages.items() if (found := booker_texts.check(key, text))}
    usable = {key: text for key, text in messages.items() if key not in problems and text.strip()}
    lang = language if language in booker_texts.LANGUAGES else booker_texts.DEFAULT_LANGUAGE
    texts = booker_texts.Texts(lang, usable)
    shown = bookings.preview_messages(texts, tz=bundle.config["timezone"], now=now or datetime.now(timezone.utc),
                                      name=_name(bundle), whatsapp=bundle.tenant.get("channel") == "whatsapp")
    return {"preview": shown, "problems": problems}


def view(bundle: tenants.Bundle, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything the page needs: the languages, each message with its
    blanks and its default in every language, the account's settings and
    their preview."""
    booking = bundle.config["booking"]
    language, messages = booking["booker_language"], dict(booking["booker_messages"])
    return {
        "language": language,
        "messages": messages,
        "languages": [{"key": key, "label": label} for key, label in booker_texts.LANGUAGES.items()],
        "templates": [
            {"key": key, "label": template.label, "about": template.about,
             "blanks": [{"name": name, "about": booker_texts.BLANKS[name]} for name in template.blanks],
             "defaults": {lang: booker_texts.DEFAULTS.get(lang, {}).get(key) or booker_texts.DEFAULTS["en"][key]
                          for lang in booker_texts.LANGUAGES},
             "kept": ["number"] + (["commands"] if template.commands else [])}
            for key, template in booker_texts.TEMPLATES.items()
        ],
        "max_length": booker_texts.MAX_TEMPLATE,
        "revision": bundle.tenant["config_revision"],
        **preview(bundle, language, messages, now=now),
    }


async def save(store: tenants.TenantStore, tenant_id: int, body: MessagesBody, *, actor: str) -> tenants.Bundle:
    """Only these two settings change; the rest of the account's own
    settings stay as they are. Checked like any config save (a text with an
    unknown blank is refused, per message), and refused when someone else
    saved the account's settings in between (expected_revision)."""
    bundle = await store.bundle(tenant_id)
    overrides = copy.deepcopy(bundle.tenant["config_json"] or {})
    booking = overrides.setdefault("booking", {})
    booking["booker_language"] = body.language
    own = {key: text for key, text in body.messages.items() if text.strip()}
    if own:
        booking["booker_messages"] = own
    else:
        booking.pop("booker_messages", None)
    reason = body.reason or "messages to the booker"
    return await store.save_config(tenant_id, overrides, actor=actor, reason=reason,
                                   expected_revision=body.expected_revision)
