"""Context review: how a reply is built for one account, and what its real
chats show (Finetune → Context review). For the admin, who tunes the prompt
boxes by hand.

- How a reply is built (context_view): every piece the model is given for a
  reply, in the order it reads them (session_runtime.draft_worker, then
  ai_responder.generate_reply, then prompt_layers.render), where each one
  comes from, its size and where to edit it; what can stop a reply before
  the model is called and what happens to the model's text before it goes
  out; and warnings about the mistakes that have cost replies before (an
  account's own text that hides every fix made to the industry's box, an
  old flow in the writing samples, the address typed into a box). No model
  call and no chat text.
- What the chats show (a review): one DeepSeek call on the platform key
  reads the account's real chats of the last days, and those of any other
  accounts the admin adds, masked like the finetune runs, and answers in
  JSON (REVIEW_PROMPT): what clients ask and how she and the bot answer it,
  what gets the booking, where the bot goes wrong, the lines it repeats and
  where the boxes disagree. parse_report keeps only what checks out. The
  numbers beside it (outcomes, lines by who, repeated lines) are counted in
  code, never by the model. Report only: nothing is applied to any prompt.

A chat reads like a hand-typed finetune run: one line per message that
crossed the wire (finetune.chat_lines), with its time in the account's
timezone, and who wrote it by the rule of stats._SENT: the model (the
message has its llm_model; a person may have edited the draft first, then
messages.draft_text holds what the bot had written), the bot or the system
without the model (a message_sent audit row with that actor: the address,
the way in, booking notices, reminders), or else a person, on the phone or
by hand in the panel.

Pure functions only; the database reads, the run and the storage live in
context_review_api.py.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Any, Callable, Collection, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import ai_responder
import booking_states as bs
import bookings
import examples
import finetune
import media
import prompt_layers
import rules
import transport
import variation
import whatsapp_transport

HEADINGS = dict(prompt_layers.SECTIONS)
# What a fix may point at: a section, the business's notes, the operator's rules.
BOX_KEYS = (*prompt_layers.SECTION_KEYS, "addendum", "rules")
BOX_HEADINGS = {**HEADINGS, "addendum": prompt_layers.ADDENDUM_HEADER.rstrip(":"),
                "rules": "RULES FROM THE OPERATOR"}
NONE = "none"

# ------------------------------------------------------------ edit links
# Where the admin panel edits each thing (admin-web: Clients and Finetune).


def _link(label: str, href: str) -> dict[str, str]:
    return {"label": label, "href": href}


def _tenant_link(tenant: dict[str, Any], tab: str, what: str) -> dict[str, str]:
    return _link(f"Clients → {tenant['name']} → {what}", f"/clients?node=tenant-{tenant['id']}&tab={tab}")


def _industry_link(industry: dict[str, Any]) -> dict[str, str]:
    return _link(f"Clients → {industry['name']} → Template",
                 f"/clients?node=industry-{industry['id']}&tab=template")


BASE_LINK = _link("Clients → Platform → Rules", "/clients?node=base&tab=rules")


def _rules_link(tenant: dict[str, Any]) -> dict[str, str]:
    return _link("Finetune → Rules", f"/finetune?tab=rules&tenant={tenant['id']}")


def _dialog_link(tenant: dict[str, Any], dialog: str, what: str) -> dict[str, str]:
    """The Media and Style dialogs of the conversations view: they have no
    address of their own, so the page opens the account, then the dialog."""
    session = f"&session={tenant['session_id']}" if tenant.get("session_id") else ""
    return _link(f"Conversations → {tenant['name']} → {what}", f"/?modal={dialog}{session}")


# ------------------------------------------------------- how a reply is built

# One sentence per box: what it does, or what to know about it.
SECTION_NOTES = {
    "about": "Who she is and what the business is: the model's picture of who it writes as.",
    "services": "What she offers and her rates: the only place the model may take a price from.",
    "hours_location": "When and where she works. The street address itself belongs in booking.address, "
                      "written [address] here.",
    "booking": "The steps of a booking in order: what to ask, when the rates and photos go out, how a time is "
               "confirmed.",
    "faq": "Answers to the questions clients ask most.",
    "tone": "How she sounds: length, emojis, warmth.",
    "boundaries": "What she never does. An account can only add to the industry's, never replace it.",
    "sign_off": "How she ends a message.",
    "writing_samples": "Rendered last of the boxes: the model follows it most.",
}
# Empty, these cost replies at once (warn); the others are only worth knowing (info).
_MUST_HAVE = ("services", "hours_location")
_GOOD_TO_HAVE = ("faq", "tone", "writing_samples")
# Warnings past these.
MAX_SYSTEM_CHARS = 40_000
RULES_NEAR_CAP = 0.8
# A rough count for DeepSeek's tokenizer on chat text in Latin and Cyrillic.
CHARS_PER_TOKEN = 4

SYSTEM, HISTORY, CLOSING = "system", "history", "closing"


@dataclass(frozen=True)
class Notes:
    """What the view needs besides the bundle: what the runtime works out
    on its own when it binds (the style card, the media library) and the
    state of the account's switches and rules, read by context_review_api."""
    # The style card's note (stylecard.card: the preview even while it is
    # off), whether it is on, and how many typed replies it was measured from.
    style_note: str = ""
    style_enabled: bool = False
    style_replies: int = 0
    # The media note exactly as SessionRuntime.media_prompt builds it
    # ("" with media off or no files), and the library's files it lists,
    # the greeting files and the threesome girl's (media.MediaLibrary).
    media_note: str = ""
    media_files: int = 0
    greeting: tuple[dict[str, Any], ...] = ()
    threesome: tuple[dict[str, Any], ...] = ()
    # Active operator rules of the industry and of the account (rules.counts).
    rules_industry: int = 0
    rules_account: int = 0
    # Chats with notes of their own (the chat's style sheet in the panel).
    per_chat: int = 0
    # Why the account may not send now (controls.off_reason), "" when it may.
    off_reason: str = ""


@dataclass
class _Piece:
    key: str
    part: str
    title: str
    source: str
    source_label: str
    enabled: bool
    text: str
    note: str
    edit: Optional[dict[str, str]]
    # Made for each chat: no size of its own, and one line in system_text.
    dynamic: bool = False
    inherited: Optional[str] = None
    placeholder: str = ""
    # What goes into every reply, when `text` also shows lines only some
    # replies get: the size and system_text count this part alone.
    static: Optional[str] = None

    def every_reply(self) -> str:
        return self.text if self.static is None else self.static

    def view(self) -> dict[str, Any]:
        out = {"key": self.key, "part": self.part, "title": self.title, "source": self.source,
               "source_label": self.source_label, "enabled": self.enabled,
               "chars": 0 if self.dynamic else len(self.every_reply()), "text": self.text, "note": self.note,
               "edit": self.edit}
        if self.inherited is not None:
            out["inherited"] = self.inherited
        return out


def _adapter(network: str) -> Callable[[str], str]:
    """The prompt layers as the account's network reads them: on WhatsApp,
    "Telegram" is "WhatsApp" (whatsapp_transport.adapt_prompt)."""
    if network == transport.WHATSAPP:
        return lambda text: text.replace("Telegram", "WhatsApp")
    return lambda text: text


def _on(value: Any) -> str:
    return "on" if value else "off"


def sections(bundle: Any) -> list[dict[str, Any]]:
    """Each box of the account as render() reads it: key, heading, the final
    text, where it comes from (prompt_layers.effective_sections: "industry",
    "client", "industry+client" or "" when empty) and the industry's text."""
    industry = prompt_layers.validate_industry(bundle.layers["industry"])
    client = prompt_layers.validate_client(bundle.layers["client"] or {}, strict=False)
    return [{"key": key, "heading": HEADINGS[key], **section}
            for key, section in prompt_layers.effective_sections(industry, client).items()]


def _section_source(section: dict[str, Any], industry_label: str) -> tuple[str, str]:
    source = section["source"] or "empty"
    if source == "industry":
        return source, industry_label
    if source == "client":
        return source, ("Own text — replaces the industry's" if section["inherited"]
                        else "Own text (the industry has none)")
    if source == "industry+client":
        return source, "Industry + own addition"
    return "empty", "Not set"


def _base_version(version_tag: str) -> str:
    match = re.match(r"b(\d+)/", version_tag or "")
    return f" v{match.group(1)}" if match else ""


def context_view(bundle: Any, notes: Notes = Notes()) -> dict[str, Any]:
    """Part A: how a reply of this account is built (the ContextView the
    page shows). `bundle` is the account's tenants.Bundle."""
    tenant, industry, config = bundle.tenant, bundle.industry, bundle.config
    network = tenant.get("channel") or transport.TELEGRAM
    adapt = _adapter(network)
    pinned = tenant.get("prompt_pin_version")
    industry_version = bundle.layers.get("industry_version") or industry["template_version"]
    industry_label = f"Industry {industry['name']} v{industry_version}" + (" (pinned)" if pinned else "")
    prompt_link = _tenant_link(tenant, "prompt", "Prompt")
    config_link = _tenant_link(tenant, "config", "Config")
    pieces: list[_Piece] = []

    base = prompt_layers.validate_base(bundle.layers["base"])
    pieces.append(_Piece(
        "platform_rules", SYSTEM, "Platform rules", "platform",
        f"Platform{_base_version(bundle.prompt.version_tag)} — shared by every industry", True,
        adapt(f"{prompt_layers.BASE_HEADER}\n\n{base['rules']}"),
        "First in the system message, and restated after the business's boxes as PRECEDENCE: where anything "
        "below disagrees with them, the model follows these.", BASE_LINK))
    name = tenant["name"].strip() or "this business"
    pieces.append(_Piece("business", SYSTEM, "Business name", "config", "The account's name", True,
                         adapt(f"BUSINESS: {name}"), "Who the replies are written for.",
                         _link(f"Clients → {tenant['name']}", f"/clients?node=tenant-{tenant['id']}")))

    boxes = sections(bundle)
    for box in boxes:
        source, label = _section_source(box, industry_label)
        own = source in ("client", "industry+client")
        pieces.append(_Piece(
            f"section:{box['key']}", SYSTEM, box["heading"], source, label, bool(box["text"]),
            adapt(f"{box['heading']}:\n{box['text']}") if box["text"] else "",
            SECTION_NOTES[box["key"]] if box["text"] else "Empty: the model gets no such box.",
            prompt_link if own else _industry_link(industry),
            inherited=adapt(box["inherited"]) if own and box["inherited"] else None))

    policy = config["language_policy"]
    pieces.append(_Piece("language", SYSTEM, "LANGUAGE", "config", f"From config (language_policy: {policy})", True,
                         adapt("LANGUAGE:\n" + prompt_layers.language_rule(policy)),
                         "Written from the config, never by hand, so it cannot drift from it.", config_link))
    client = prompt_layers.validate_client(bundle.layers["client"] or {}, strict=False)
    addendum = client["addendum"]
    pieces.append(_Piece(
        "addendum", SYSTEM, "Additional notes from the business", "client" if addendum else "empty",
        "Own text" if addendum else "Not set", bool(addendum),
        adapt(f"{prompt_layers.ADDENDUM_HEADER}\n{addendum}") if addendum else "",
        f"Short notes of this account only, after every box (at most {prompt_layers.MAX_ADDENDUM_CHARS} "
        "characters).", prompt_link))
    active = [" ".join(rule.split()) for rule in bundle.layers.get("rules") or [] if rule.strip()]
    scopes = [f"{notes.rules_industry} for the industry"] if notes.rules_industry else []
    scopes += [f"{notes.rules_account} for this account"] if notes.rules_account else []
    rules_source = ("industry+client" if notes.rules_industry and notes.rules_account
                    else "client" if notes.rules_account else "industry" if active else "empty")
    pieces.append(_Piece(
        "rules", SYSTEM, "Rules from the operator", rules_source,
        ("Operator rules: " + ", ".join(scopes)) if scopes else (f"{len(active)} operator rules" if active
                                                                 else "None active"),
        bool(active), adapt(prompt_layers.RULES_HEADER + "\n" + "\n".join(f"- {r}" for r in active)) if active else "",
        "Corrections as short sentences, after everything the business wrote; once a week they are folded into "
        "the boxes and stop being added here.", _rules_link(tenant)))

    pieces.extend(_runtime_pieces(bundle, notes, config_link))

    after_layers = [p for p in pieces if p.part == SYSTEM and not _in_layers(p)]
    system_parts = [adapt(bundle.prompt.text)]
    system_parts += [p.placeholder or p.every_reply() for p in after_layers if p.enabled]
    system_text = "\n\n".join(system_parts)
    size = {"system_chars": len(system_text), "approx_tokens": -(-len(system_text) // CHARS_PER_TOKEN),
            "history_messages": _history_messages(config)}
    return {
        "tenant_id": tenant["id"], "account": tenant["name"], "network": network,
        "industry": {"id": industry["id"], "name": industry["name"], "template_version": industry["template_version"]},
        "version_tag": bundle.prompt.version_tag, "pinned": pinned,
        "pieces": [p.view() for p in pieces],
        "before": before_steps(bundle, notes),
        "after": after_steps(bundle, notes),
        "warnings": warnings(bundle, notes, boxes, size["system_chars"]),
        "system_text": system_text, "size": size,
    }


_LAYER_KEYS = {"platform_rules", "business", "language", "addendum", "rules"}


def _in_layers(piece: _Piece) -> bool:
    """Part of the rendered prompt layers (bundle.prompt.text)."""
    return piece.key in _LAYER_KEYS or piece.key.startswith("section:")


def _history_messages(config: dict[str, Any]) -> int:
    return int((config.get("ai") or {}).get("history_messages") or 60)


def _runtime_pieces(bundle: Any, notes: Notes, config_link: dict[str, str]) -> list[_Piece]:
    """What generate_reply adds after the prompt layers, in its order, then
    the conversation and the closing note."""
    config = bundle.config
    locked = config["language_policy"] != "mirror"
    pieces: list[_Piece] = []

    style_label = (f"Measured from {notes.style_replies} of her typed replies" if notes.style_enabled
                   else "Off (style_card.enabled)")
    style_on = notes.style_enabled and bool(notes.style_note)
    pieces.append(_Piece(
        "style_card", SYSTEM, "Style card", "system", style_label, style_on, notes.style_note,
        "How she types, in numbers, measured on every reload: lengths, capitals, full stops, emojis, openers."
        + ("" if style_on else " Off, or too few typed replies: the text shown is what it would add."),
        config_link))

    pieces.append(_Piece(
        "per_chat", SYSTEM, "Notes for this person", "per_chat",
        f"Set on {notes.per_chat} chat{'' if notes.per_chat == 1 else 's'}" if notes.per_chat else "Set on no chat",
        notes.per_chat > 0,
        "[EXTRA RULES FOR THIS PERSON, ABOUT THIS PERSON, HOW I HAVE WRITTEN TO THIS PERSON BEFORE and MESSAGE "
        "LENGTH: only in a chat where they are set on the chat's style sheet]",
        "Only in the chats they are set for.", _dialog_link(bundle.tenant, "style", "Style"), dynamic=True,
        placeholder="[notes for this person, in a chat where they are set]"))

    sample = ai_responder.describe_style([{"role": "user", "content": "hi"}] * 2, language_locked=locked)
    pieces.append(_Piece(
        "their_style", SYSTEM, "How this person writes", "per_chat", "Made for each chat",
        bool(config["human"]["adaptive_style"]),
        "HOW THIS PERSON WRITES: [how long their messages are, their emojis, capitals and full stops, measured "
        "from their own messages in this chat once there are two].\n" + sample.split("\n", 1)[1],
        "Mirrors each client's own way of writing (human.adaptive_style).", config_link, dynamic=True,
        placeholder="[HOW THIS PERSON WRITES: measured from their messages in this chat]"))

    link = config["context_link"]
    pieces.append(_Piece(
        "linked_chat", SYSTEM, "From a linked chat", "per_chat", "Made for each chat", bool(link["enabled"]),
        ai_responder.BACKGROUND_HEADER + "\n[a short brief of the same person's chat on another account, when the "
        "chats are linked]",
        "What the bot knows of this person from another account (context_link.enabled).", config_link, dynamic=True,
        placeholder="[WHAT YOU ALREADY KNOW ABOUT THIS PERSON: when a chat is linked]"))

    retrieval = config["retrieval"]
    shared = ", from the industry's other accounts too (none with figures)" if retrieval["share_in_industry"] else ""
    pieces.append(_Piece(
        "examples", SYSTEM, "Similar moments from earlier chats", "per_chat", "Made for each chat",
        bool(retrieval["enabled"]),
        examples.NOTE_HEADER + f"\n[up to {retrieval['k']} earlier moments like this one, at least "
        f"{retrieval['min_similarity']:g} alike, at most {retrieval['max_chars']} characters{shared}]",
        "Her real replies to moments like this one (retrieval.*); a reply that copies one is written again "
        "without them." if retrieval["copy_guard"] else "Her real replies to moments like this one (retrieval.*).",
        config_link, dynamic=True, placeholder="[SIMILAR MOMENTS FROM EARLIER CHATS: found for each message]"))

    booking = config["booking"]
    lines = ["[a line for each booking of this chat: asked for, waiting for her, confirmed, and any news to pass "
             "on now]"]
    if (booking.get("address") or "").strip():
        lines.append(bookings.address_tag_line(booking.get("address_timing", "with_prices")))
    pieces.append(_Piece(
        "appointments", SYSTEM, "APPOINTMENTS", "per_chat", "Made for each chat from its bookings",
        bool(booking["enabled"]), bookings.context_for_reply(lines),
        "The last box the model reads before the media list: what is booked in this chat, and the news it must "
        "pass on.", config_link, dynamic=True,
        placeholder="[APPOINTMENTS: only the business confirms, and a line for each booking of this chat]"))

    # Lines only some replies get: shown, never counted in the size.
    media_text = notes.media_note
    if media_text:
        if notes.greeting:
            media_text += "\n[only with the first reply in a chat:] " + media.greeting_line(list(notes.greeting))
        if notes.threesome:
            media_text += ("\n[only when he asks about a threesome or to see her:] "
                           + media.threesome_line(list(notes.threesome[:media.THREESOME_FILES])))
        media_text += "\n[only once files went out in the chat:] Already sent in this chat: photo #N, …"
    files = f"{notes.media_files} file{'' if notes.media_files == 1 else 's'}"
    pieces.append(_Piece(
        "media", SYSTEM, "Photos and videos", "system" if notes.media_note else "empty",
        f"From the media library ({files})" if notes.media_note else "No files, or media off", bool(notes.media_note),
        media_text, "The files the model may attach with a [send N] tag, as described in the library. The lines "
        "marked [only …] are added to the replies they name.", _dialog_link(bundle.tenant, "media", "Media"),
        static=notes.media_note))

    burst = config["burst"]
    pieces.append(_Piece(
        "burst", SYSTEM, "Sending as several messages", "config",
        f"From config (burst.max_messages: {burst['max_messages']}, keep_together: "
        f"{_on(burst.get('keep_together', True))})", True,
        ai_responder.burst_note(burst["max_messages"], burst.get("keep_together", True)),
        "Every line break of the reply goes out as a message of its own.", config_link))

    instruction = (config["replies"]["no_reply_instruction"] or "").strip()
    pieces.append(_Piece(
        "no_reply", SYSTEM, "When not to reply", "config" if instruction else "empty",
        "From config (replies.no_reply_instruction)" if instruction else "Not set", bool(instruction),
        ai_responder.no_reply_section(instruction) if instruction else "",
        "Left out of a reply that must pass on booking news, sends only photos or answers a threesome ask.",
        config_link))

    count = _history_messages(config)
    pieces.append(_Piece(
        "history", HISTORY, "The conversation", "per_chat", f"The chat's last {count} messages", True,
        f"[the chat's last {count} messages, both sides; messages in a row from one side are one turn; "
        f"⟦today 21:05⟧ in front of the first one and of any sent {_hours(ai_responder.TIME_GAP)} or more after "
        "the one before]",
        "Older messages are not seen at all (ai.history_messages).", config_link, dynamic=True))
    pieces.append(_Piece(
        "closing", CLOSING, "Closing note", "system", "Written for each reply", True,
        "[Now it is <day>, <time> (<timezone>). Their last message came <how long> ago.] " + ai_responder.CLOCK_NOTE
        + "\n[When the chat ends on our own message: " + ai_responder.OWN_INITIATIVE_NOTE + "]\n[Else, when the "
        "APPOINTMENTS carry news: " + ai_responder.NEWS_NOTE + " <the news lines>]",
        "The last thing the model reads, after the conversation: the time now, and the news it must pass on.",
        None, dynamic=True))
    return pieces


def _hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:g} hour{'' if hours == 1 else 's'}"


def _step(key: str, title: str, detail: str, active: bool, edit: Optional[dict[str, str]]) -> dict[str, Any]:
    return {"key": key, "title": title, "detail": detail, "active": bool(active), "edit": edit}


def before_steps(bundle: Any, notes: Notes = Notes()) -> list[dict[str, Any]]:
    """What can stop or delay a reply before the model is called, in the
    order draft_worker checks it, with this account's values."""
    config, tenant = bundle.config, bundle.tenant
    link = _tenant_link(tenant, "config", "Config")
    delay, quiet, replies = config["reply_delay"], config["quiet_hours"], config["replies"]
    limits, booking, staging = config["limits"], config["booking"], config["staging"]
    spend = float(config.get("api_spend_cap_eur") or 0)
    per_chat = [f"{replies['max_messages_per_chat_per_hour']} an hour" if replies["max_messages_per_chat_per_hour"]
                else "", f"{replies['max_messages_per_chat_per_day']} a day" if replies["max_messages_per_chat_per_day"]
                else "", f"{replies['min_gap_seconds']} s between replies" if replies["min_gap_seconds"] else ""]
    per_chat = [p for p in per_chat if p]
    hourly = int(config.get("hourly_message_cap") or 0)
    return [
        _step("reply_delay", "Reply delay",
              f"Waits {delay['min_s']:g}–{delay['max_s']:g} s ({delay['distribution']}) before writing; a chat's "
              "style sheet can set its own.", True, link),
        _step("quiet_hours", "Quiet hours",
              f"No replies {quiet['start']}–{quiet['end']} ({config['timezone']}): a reply due then waits until "
              "they end." if quiet["enabled"] else "Off: replies are written at any hour.", quiet["enabled"], link),
        _step("switches", "Pause and soft-off",
              f"Off now: {notes.off_reason}. Nothing is written; the client is queued as unanswered (soft_off)."
              if notes.off_reason else "Sending is on now. Pause all, an AI limit, an anomaly or the global stop "
              "would stop every reply (soft_off).", bool(notes.off_reason),
              _link("Safety", f"/safety?tab=client&tenant={tenant['id']}")),
        _step("chat_paused", "Paused chats and takeovers",
              f"A chat paused in the panel, or written in by hand (the bot then keeps out for "
              f"{config['takeover_hours']:g} h), gets no reply (paused).", True, link),
        _step("staging", "Staging",
              f"Only the {len(staging['test_chats'])} test chats are answered; everyone else is stored, not answered."
              if staging["enabled"] else "Off: every chat is answered.", staging["enabled"], link),
        _step("booker_wait", "Waiting for the booker",
              "While a time he asked for waits for her answer, nothing is written; only photos he asks for go out."
              if booking["enabled"] and booking["quiet_until_answered"] else
              "Off (booking.quiet_until_answered): replies go on while a time waits for her.",
              booking["enabled"] and booking["quiet_until_answered"], link),
        _step("ai_limits", "AI limits",
              f"Daily tokens {limits['daily_tokens'] or 'no limit'}, monthly tokens "
              f"{limits['monthly_tokens'] or 'no limit'}, €{limits['daily_spend_eur']:g} a day, €{spend:g} a month "
              "(0 = no limit). At a limit the client goes soft-off.",
              bool(limits["daily_tokens"] or limits["monthly_tokens"] or limits["daily_spend_eur"] or spend), link),
        _step("skip_acknowledgements", "Skip acknowledgements",
              f"A message that is only one of {len(replies['acknowledgements'])} words like ok or thanks gets no reply."
              if replies["skip_acknowledgements"] else "Off: an ok or a thanks gets a reply.",
              replies["skip_acknowledgements"], link),
        _step("skip_bot_questions", "Skip bot questions",
              "A message that only asks whether this is a bot gets no reply (not the first one)."
              if replies["skip_bot_questions"] else "Off: are you a bot? gets a reply.",
              replies["skip_bot_questions"], link),
        _step("reply_limits", "Reply limits per chat",
              ("At most " + ", ".join(per_chat) + "; past that the client is queued as unanswered (skipped).")
              if per_chat else "No limit per chat.", bool(per_chat), link),
        _step("send_quota", "Daily send quota",
              f"{config['daily_message_cap']} messages a day, " + (f"{hourly} an hour, " if hourly else "")
              + f"{config['safety']['daily_peer_cap']} different people a day; past one, sending stops until "
              "it rolls over.", True, link),
    ]


def after_steps(bundle: Any, notes: Notes = Notes()) -> list[dict[str, Any]]:
    """What happens to the model's text before it goes out, in the order
    draft_worker does it."""
    config, tenant = bundle.config, bundle.tenant
    link = _tenant_link(tenant, "config", "Config")
    instruction = (config["replies"]["no_reply_instruction"] or "").strip()
    booking, burst, variation_on = config["booking"], config["burst"], config["variation"]["enabled"]
    address = bool((booking.get("address") or "").strip())
    fallback = config["unanswered"]["fallback_phrases"]
    system_files = []
    if notes.greeting:
        system_files.append(f"{len(notes.greeting)} greeting file(s) with the first reply in a chat")
    if notes.threesome:
        system_files.append(f"the threesome girl's files ({len(notes.threesome)}) when he asks")
    holds = [f"{len(config['banned_topics'])} banned topics", f"{len(config['price_floors'])} price floors",
             f"{len(config['allowed_link_domains'])} allowed link domains",
             f"{len(config['shareable_contacts'])} shareable contacts"]
    return [
        _step("no_reply", "[NO_REPLY]",
              "The model may answer [NO_REPLY]: nothing goes out and the chat says why." if instruction
              else "Off: no no-reply instruction is set.", bool(instruction), link),
        _step("media_tags", "Media tags",
              (f"[send N] tags come out of the text and attach one of the {notes.media_files} files (at most "
               f"{media.MAX_ATTACHMENTS_PER_REPLY})." if notes.media_note else "No files to attach.")
              + (" The system adds " + " and ".join(system_files) + "." if system_files else ""),
              bool(notes.media_note), _dialog_link(tenant, "media", "Media")),
        _step("address", "[address] filled in",
              f"[address] becomes booking.address, word for word, as a message of its own "
              f"({booking.get('address_timing', 'with_prices')})." if booking["enabled"] and address
              else "No address to fill in: a tag is dropped.", booking["enabled"] and address, link),
        _step("fallback", "Fallback phrases",
              f"A reply with one of {len(fallback)} fallback phrases still goes out, and the client is queued as "
              "unanswered (fallback)." if fallback else "None set.", bool(fallback), link),
        _step("variation", "Variation",
              "The bot's own lines are varied a little (emojis, phrases, capitals, full stops); the [block] "
              "messages stay as written." if variation_on else "Off: replies go out exactly as the model wrote them.",
              variation_on, link),
        _step("burst", "Burst split",
              f"Split at line breaks into at most {burst['max_messages']} messages; price lists kept together: "
              f"{_on(burst.get('keep_together', True))}.", burst["max_messages"] > 1, link),
        _step("videos_need_approval", "Videos need approval",
              "A reply that attaches a video waits in the panel." if config["media"]["videos_need_approval"]
              else "Off: a video goes out like any file.", config["media"]["videos_need_approval"], link),
        _step("policy", "Policy check",
              "A reply against the platform rules or the account's " + ", ".join(holds)
              + " is held for approval (policy_hold).", True, link),
        _step("auto_send", "Auto send",
              "Replies that pass go out by themselves." if config["auto_send"]
              else "Off: every reply waits in the panel for approval.", config["auto_send"], link),
    ]


# A WhatsApp booker is a number or a WhatsApp group (whatsapp_transport.group_ref);
# a Telegram one is a @username, an id, a number, a t.me link or "group: <name>".
_PHONE_LIKE = re.compile(r"\+?[\d\s().\-]+")
_TELEGRAM_ONLY = re.compile(r"^@|(?:^|/)(?:t|telegram)\.me/", re.IGNORECASE)


def provider_problem(provider: str, network: str) -> str:
    """Why booking.provider cannot reach a booker on this network, or ""."""
    value = (provider or "").strip()
    if not value:
        return ""
    group = whatsapp_transport.group_ref(value)
    if network == transport.WHATSAPP:
        if group is not None:
            return ""
        if _TELEGRAM_ONLY.search(value) or not (_PHONE_LIKE.fullmatch(value)
                                                and sum(c.isdigit() for c in value) >= 6):
            return "looks like a Telegram username or link, but this is a WhatsApp account"
        return ""
    if group is not None and group[0] in ("jid", "invite"):
        return "is a WhatsApp group, but this is a Telegram account"
    return ""


def warnings(bundle: Any, notes: Notes = Notes(), boxes: Optional[list[dict[str, Any]]] = None,
             system_chars: int = 0) -> list[dict[str, Any]]:
    """The mistakes worth knowing about, warn first, then info. Never the
    address or the booker's number in the text: only where they are."""
    tenant, industry, config = bundle.tenant, bundle.industry, bundle.config
    name = tenant["name"]
    boxes = boxes if boxes is not None else sections(bundle)
    by_key = {box["key"]: box for box in boxes}
    prompt_link = _tenant_link(tenant, "prompt", "Prompt")
    config_link = _tenant_link(tenant, "config", "Config")
    found: list[dict[str, Any]] = []

    def add(level: str, piece: Optional[str], text: str, edit: Optional[dict[str, str]]) -> None:
        found.append({"level": level, "piece": piece, "text": text, "edit": edit})

    def box_link(key: str) -> dict[str, str]:
        return prompt_link if by_key[key]["source"] in ("client", "industry+client") else _industry_link(industry)

    for box in boxes:
        if box["source"] == "client" and box["inherited"]:
            add("warn", f"section:{box['key']}",
                f"{name}'s own text replaces the industry's {box['heading']}: changes to the industry box never "
                f"reach {name}.", prompt_link)
    if by_key["writing_samples"]["text"]:
        add("info", "section:writing_samples",
            f"{HEADINGS['writing_samples']} is rendered last of the boxes and the model follows it most: if it shows "
            f"an old flow, the bot follows the old flow whatever {HEADINGS['booking']} says.",
            box_link("writing_samples"))
    booking = config["booking"]
    for key in _MUST_HAVE:
        if not by_key[key]["text"]:
            add("warn", f"section:{key}", f"{HEADINGS[key]} is empty: the model has nothing to answer from and "
                "makes it up or dodges.", _industry_link(industry))
    if booking["enabled"] and not by_key["booking"]["text"]:
        add("warn", "section:booking", f"{HEADINGS['booking']} is empty while bookings are on: the model does not "
            "know the steps to take a booking.", _industry_link(industry))
    for key in _GOOD_TO_HAVE:
        if not by_key[key]["text"]:
            add("info", f"section:{key}", f"{HEADINGS[key]} is empty.", _industry_link(industry))

    if booking["enabled"]:
        provider = (booking.get("provider") or "").strip()
        if not provider:
            add("warn", "appointments", "Bookings are on but booking.provider is empty: booking requests reach "
                "nobody.", config_link)
        elif problem := provider_problem(provider, tenant.get("channel") or transport.TELEGRAM):
            add("warn", "appointments", f"booking.provider {problem}: booking requests cannot reach the booker.",
                config_link)
    address = (booking.get("address") or "").strip()
    if booking["enabled"] and not address:
        add("warn", "appointments", "Bookings are on but booking.address is empty: [address] in a reply cannot be "
            "filled, so it is dropped.", config_link)
    if address:
        literals = [literal.lower() for literal, _ in finetune.masks_for(address=address)]
        where = [box["heading"] for box in boxes if any(lit in box["text"].lower() for lit in literals)]
        client = prompt_layers.validate_client(bundle.layers["client"] or {}, strict=False)
        if any(lit in client["addendum"].lower() for lit in literals):
            where.append(prompt_layers.ADDENDUM_HEADER.rstrip(":"))
        if where:
            add("info", None, f"The address is typed in {', '.join(where)}; use [address] so it follows "
                "booking.address.", prompt_link)

    pinned = tenant.get("prompt_pin_version")
    if pinned:
        add("warn", None, f"{name}'s prompt is pinned to {industry['name']} v{pinned}: newer saves of the industry's "
            f"boxes (now v{industry['template_version']}) are not used for {name}.",
            _tenant_link(tenant, "versions", "Versions"))
    for count, scope in ((notes.rules_industry, f"the {industry['name']} industry"), (notes.rules_account, name)):
        if count >= RULES_NEAR_CAP * rules.CAP:
            add("info", "rules", f"{count} of {rules.CAP} operator rules are active for {scope}: fold them into the "
                "prompt before the cap stops new ones.", _rules_link(tenant))
    if system_chars > MAX_SYSTEM_CHARS:
        add("warn", None, f"The system message is {system_chars:,} characters (about "
            f"{-(-system_chars // CHARS_PER_TOKEN):,} tokens) before the chat: every reply costs more, and the "
            "model attends less to each part.", prompt_link)

    if not config["style_card"]["enabled"]:
        add("info", "style_card", "The style card is off: the model gets no measured note of how she types "
            "(style_card.enabled).", config_link)
    if not config["retrieval"]["enabled"]:
        add("info", "examples", "Retrieval is off: the model sees no earlier moments with her real replies "
            "(retrieval.enabled).", config_link)
    if not config["human"]["adaptive_style"]:
        add("info", "their_style", "Adaptive style is off: the model does not mirror how each client writes "
            "(human.adaptive_style).", config_link)
    if not config["variation"]["enabled"]:
        add("info", None, "Variation is off: every reply goes out as the model wrote it, so repeated lines stay "
            "identical (variation.enabled).", config_link)
    return [w for w in found if w["level"] == "warn"] + [w for w in found if w["level"] == "info"]


# ------------------------------------------------------------ the chats

BOOKED = "booked"
BOOKED_NO_SHOW = "booked_no_show"
NOT_BOOKED = "not_booked"
WAITING = "waiting"
WENT_QUIET = "went_quiet"
UNANSWERED = "unanswered"
OPEN = "open"
OUTCOME_LABELS = {
    BOOKED: "booked",
    BOOKED_NO_SHOW: "booked, did not come",
    NOT_BOOKED: "asked for a time, not booked (declined, cancelled or lapsed)",
    WAITING: "a time waits for her answer",
    WENT_QUIET: "went quiet after our reply",
    UNANSWERED: "his last message got no answer",
    OPEN: "still going",
}
OUTCOMES = tuple(OUTCOME_LABELS)
# The side of the comparison that booked, or is about to.
BOOKED_ISH = (BOOKED, BOOKED_NO_SHOW, WAITING)
QUIET_AFTER = timedelta(hours=24)
UNANSWERED_AFTER = timedelta(hours=1)

CLIENT, BOT, BOT_EDITED, BY_HAND, SYSTEM_SENT, EVENT = "client", "bot", "bot_edited", "by_hand", "system", "event"
LINE_KINDS = (CLIENT, BOT, BOT_EDITED, BY_HAND, SYSTEM_SENT)
LABELS = {CLIENT: "Client", BOT: "Bot", BY_HAND: "Her (typed by hand)", SYSTEM_SENT: "System"}
EDITED = "Bot (edited by her before sending; the bot had written: «{draft}»)"
# The other messages of a reply she edited: its later parts and its files.
EDITED_MORE = "Bot (edited by her before sending, same reply)"
# The reasons a bot reply's messages go out with (their message_sent audit
# rows): the automatic reply, or the draft approved in the panel.
AUTOMATIC_REPLY = "automatic reply"
APPROVED_DRAFT = "draft approved in the panel"
BOT_REPLY_REASONS = (AUTOMATIC_REPLY, APPROVED_DRAFT)
MAX_DRAFT_CHARS = 600
LEFT_OUT = "[… {n} messages left out …]"


def _when(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _flat(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def review_lines(rows: Sequence[dict[str, Any]],
                 approvals: Iterable[tuple[int, int]] = ()) -> list[dict[str, Any]]:
    """The chat's lines, all of them (finetune.chat_lines: what crossed the
    wire, and the booking system's events), each with its `kind`: client,
    bot, bot_edited, by_hand, system or event.

    A row carries the message's llm_model and draft_text, and from its
    message_sent audit row: `machine` (the bot or the system sent it),
    `sent_reason` and `sent_audit` (the row's id). `approvals` are this
    chat's drafts a person changed before approving them: (the id of the
    draft_approved audit row, the draft's message id). Who wrote what:

    - The bot, when the model wrote it (llm_model), or when it is a file a
      bot reply attached: send_burst sends those after the reply's text,
      without llm_model, with the reply's reason (automatic reply, or draft
      approved in the panel).
    - The bot, edited by her: the draft she changed (draft_text holds what
      the bot had written), and every other message that same approval
      sent. approve_draft sends the reply's parts, the draft's own row
      first, then its files, each with a message_sent row of its reason,
      and only then writes draft_approved: so a message whose message_sent
      row lies between the draft's and that draft_approved row is part of
      the edited reply.
    - The system: a message_sent row of the bot or the system and no model
      (the address, the way in, booking notices, reminders).
    - Else her, typed by hand (on the phone, or from the panel)."""
    by_id = {row["id"]: row for row in rows}
    edited_parts: set[int] = set()
    for approved_at, draft_id in approvals:
        start = (by_id.get(draft_id) or {}).get("sent_audit")
        if start is None:
            continue
        edited_parts |= {row["id"] for row in rows if row["id"] != draft_id and row.get("sent_reason") == APPROVED_DRAFT
                         and row.get("sent_audit") is not None and start < row["sent_audit"] < approved_at}
    lines = []
    for line in finetune.chat_lines(rows, limit=None):
        row = by_id[line["id"]]
        draft = None
        if line["who"] != "business":
            kind = CLIENT if line["who"] == "client" else EVENT
        elif row.get("llm_model") or row.get("sent_reason") in BOT_REPLY_REASONS:
            if row.get("llm_model") and row.get("draft_text") is not None:
                kind, draft = BOT_EDITED, row["draft_text"]
            else:
                kind = BOT_EDITED if row["id"] in edited_parts else BOT
        else:
            kind = SYSTEM_SENT if row.get("machine") else BY_HAND
        line = {**line, "kind": kind}
        if kind == BOT_EDITED:
            line["draft"] = draft
        lines.append(line)
    return lines


def line_counts(lines: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(line["kind"] for line in lines)
    return {kind: counts.get(kind, 0) for kind in LINE_KINDS}


def outcome(states: Collection[str], lines: Sequence[dict[str, Any]], now: datetime) -> str:
    """How the chat stands: from its bookings made in the window (`states`),
    the best of them, else from who wrote last and how long ago."""
    if states:
        if any(state in (bs.CONFIRMED, bs.COMPLETED) for state in states):
            return BOOKED
        if bs.NO_SHOW in states:
            return BOOKED_NO_SHOW
        if any(state in (bs.REQUESTED, bs.PENDING) for state in states):
            return WAITING
        return NOT_BOOKED
    said = [line for line in lines if line["kind"] != EVENT]
    if not said:
        return OPEN
    last, at = said[-1], _when(said[-1].get("at"))
    if at is None:
        return OPEN
    if last["kind"] != CLIENT and now - at > QUIET_AFTER:
        return WENT_QUIET
    if last["kind"] == CLIENT and now - at > UNANSWERED_AFTER:
        return UNANSWERED
    return OPEN


def first_answer(lines: Sequence[dict[str, Any]]) -> Optional[timedelta]:
    """From the client's first message in the window to our first message
    after it; None when there is no answer."""
    start = None
    for line in lines:
        if line["kind"] == CLIENT and start is None:
            start = _when(line.get("at"))
        elif start is not None and line["kind"] not in (CLIENT, EVENT):
            at = _when(line.get("at"))
            return max(timedelta(0), at - start) if at else None
    return None


def took(delta: Optional[timedelta]) -> str:
    """"4 min", "2 h 10 min", "3 days"."""
    if delta is None:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} h" + (f" {rest} min" if rest else "")
    return f"{minutes // (24 * 60)} days"


def chat_header(*, ref: Optional[str], account: str, messages: int, outcome: str, answer: Optional[timedelta],
                taken_over: bool) -> str:
    """"### C4 — pam — 12 messages — outcome: went_quiet (went quiet after our
    reply) — first answer after 4 min"; "outcome: booked" alone."""
    label = OUTCOME_LABELS[outcome]
    parts = [f"### {ref or '(not in this review)'}", account, f"{messages} messages",
             f"outcome: {outcome}" if label == outcome else f"outcome: {outcome} ({label})",
             f"first answer after {took(answer)}" if answer is not None else "no answer to his first message"]
    if taken_over:
        parts.append("taken over by a person")
    return " — ".join(parts)


def _who(masks: Sequence[tuple[str, str]]) -> Callable[[dict[str, Any]], str]:
    def who(line: dict[str, Any]) -> str:
        if line["kind"] == BOT_EDITED:
            if line.get("draft") is None:
                return EDITED_MORE
            return EDITED.format(draft=_flat(finetune.mask(line["draft"], masks), MAX_DRAFT_CHARS))
        return LABELS[line["kind"]]
    return who


def _rendered(lines: Sequence[dict[str, Any]], masks: Sequence[tuple[str, str]],
              tz: Optional[ZoneInfo]) -> list[str]:
    """One chunk per line: a day line when the day changes, then the message."""
    return ["\n".join(out) for _, out in finetune.rendered_lines(lines, masks, tz, _who(masks))]


def render_chat(lines: Sequence[dict[str, Any]], *, header: str, masks: Sequence[tuple[str, str]] = (),
                tz: Optional[ZoneInfo] = None, max_chars: int = finetune.MAX_HAND_CHAT_CHARS) -> str:
    """The chat as the model reads it: the header, then a line per message
    with its time, masked. Longer than `max_chars`, its start and its end
    are kept, about half each, with a line saying how many messages were
    left out between them."""
    chunks = _rendered(lines, masks, tz)
    whole = "\n".join([header, *chunks])
    if len(whole) <= max_chars:
        return whole
    # Room for the cut line, and for the day line the end starts with.
    budget = max_chars - len(header) - len(LEFT_OUT.format(n=len(lines))) - 40
    head = size = 0
    while head < len(chunks) and size + len(chunks[head]) + 1 <= budget // 2:
        size += len(chunks[head]) + 1
        head += 1
    tail = 0
    while tail < len(chunks) - head and size + len(chunks[-1 - tail]) + 1 <= budget:
        size += len(chunks[-1 - tail]) + 1
        tail += 1
    left = sum(1 for line in lines[head:len(lines) - tail] if line["kind"] != EVENT)
    # The end rendered on its own, so it starts with its day.
    end = _rendered(lines[len(lines) - tail:], masks, tz) if tail else []
    return "\n".join([header, *chunks[:head], LEFT_OUT.format(n=left), *end])


def plan(chats: Sequence[dict[str, Any]], accounts: Sequence[tuple[int, Any]], *,
         max_chats: int = finetune.MAX_CHATS, max_chars: int = finetune.MAX_TRANSCRIPT_CHARS) -> list[dict[str, Any]]:
    """Which chats a review reads, and their refs.

    `chats` carry "tenant_id", "outcome", "last_at" and "chars"; `accounts`
    are (tenant id, name), the account under review first. Inside each
    account, the chats that booked (or wait for her) and the others take
    turns, newest first, so both sides of the comparison are in; the
    accounts take turns too, as finetune.plan_hand_run does, so one busy
    account does not crowd the others out. As many are picked as the
    limits take (one that does not fit is skipped, a smaller one after it
    may still be). Returns copies in that order, with "picked" and "ref":
    C1, C2… for the picked ones, in the order the model reads them, else
    None."""
    turns: list[tuple[int, int, dict[str, Any]]] = []
    for turn, (tenant_id, _) in enumerate(accounts):
        own = sorted((dict(c) for c in chats if c["tenant_id"] == tenant_id),
                     key=lambda c: str(c.get("last_at") or ""), reverse=True)
        booked = [c for c in own if c["outcome"] in BOOKED_ISH]
        other = [c for c in own if c["outcome"] not in BOOKED_ISH]
        mixed = [c for pair in zip_longest(booked, other) for c in pair if c is not None]
        turns += [(rank, turn, chat) for rank, chat in enumerate(mixed)]
    turns.sort(key=lambda item: item[:2])
    planned, count, total = [], 0, 0
    for _, _, chat in turns:
        chat["picked"] = count < max_chats and total + chat["chars"] <= max_chars
        chat["ref"] = None
        if chat["picked"]:
            count, total = count + 1, total + chat["chars"]
            chat["ref"] = f"C{count}"
        planned.append(chat)
    return planned


# --------------------------------------------------------- the model's input

_SHAPE = """{
  "summary": "3-6 sentences",
  "questions": [{"question": "...", "chats": ["C1", "C4"], "wordings": ["..."], "her_answer": "...",
                 "bot_answer": "...", "covered_by": "<box key>|none", "problem": "",
                 "suggested": {"box": "<box key>", "line": "..."}}],
  "booking": {"works": [{"point": "...", "chats": ["C3"]}], "loses": [{"point": "...", "chats": ["C7"]}],
              "speed": "..."},
  "wrong": [{"what": "...", "chats": ["C2"], "why": "...", "fix": {"box": "<box key>", "line": "..."}}],
  "phrases": [{"text": "...", "note": "...", "chats": ["C1", "C9"]}],
  "boxes": [{"boxes": ["booking", "writing_samples"], "problem": "...", "fix": "..."}]
}"""

REVIEW_PROMPT = (
    "You review the real chats of an AI assistant that answers clients for independent adult companions (escorts "
    "working where this is legal) on Telegram and WhatsApp. The admin who tunes the assistant's prompt reads your "
    "review to decide what to change in its boxes. The review is about chat quality: what clients ask, how they "
    "are answered, what gets the booking and where the assistant goes wrong. Write about the adult context "
    "plainly and neutrally.\n\n"
    "WHAT YOU GET\n"
    "- The business's boxes: the sections of the assistant's prompt, each headed \"## <box key> — <heading> "
    "(<where it comes from>)\", then the business's additional notes (addendum) and the operator's rules. This "
    "is what the assistant is told for every reply.\n"
    "- The chats. Each starts with a header \"### C<n> — <account> — <k> messages — outcome: <outcome> — first "
    "answer after <time>\", ending \" — taken over by a person\" where a person took the chat over. C<n> is the "
    "chat's ref: use it to point at the chat. Chats may come from other accounts of the same kind (the header "
    "names the account): they show the shared voice and pipeline, never this business's prices or places.\n"
    "- Outcomes: booked; booked_no_show (booked, did not come); not_booked (asked for a time, declined, "
    "cancelled or lapsed); waiting (a time waits for her answer); went_quiet (we wrote last, over a day ago); "
    "unanswered (the client wrote last and got no answer); open (still going).\n"
    "- One line per message, with its time; a line \"— <day> —\" when the day changes. \"Client:\" the client. "
    "\"Bot:\" a reply the assistant wrote that went out as written. \"Bot (edited by her before sending; the bot "
    "had written: «...»):\" a reply she corrected before it went out, after what the assistant had written. "
    "\"Her (typed by hand):\" a reply she typed herself. \"System:\" a fixed message the system sent (the address, "
    "the way in, booking notices, reminders). Lines \"— booking: ... —\" are what the booking system did. A "
    "message can run over several lines (the rates). \"[… n messages left out …]\" means the middle of a long "
    "chat is not shown.\n"
    "- Personal data is masked: [client], [number], [username], [email], [address], [way in]. Keep it masked.\n\n"
    "WHAT TO WRITE\n"
    "Answer with one JSON object and nothing else, shaped like this:\n" + _SHAPE + "\n\n"
    "- \"summary\": 3 to 6 sentences: what the chats show overall, and the 3 changes to the boxes that would help "
    "most.\n"
    "- \"questions\": the 15 or so questions clients ask most, the one asked in the most chats first. "
    "\"question\": short, in English. \"chats\": every chat ref where it came up. \"wordings\": up to 3 ways "
    "clients ask it, quoted as given, in their own language. \"her_answer\": how she answers it when she types "
    "by hand, \"\" if she never did in these chats. \"bot_answer\": how the bot answers it, \"\" if it never did. "
    "Compare her answers with the bot's to the same question. \"covered_by\": the box that covers it, or \"none\" "
    "when no box does. \"problem\": \"\", or what is wrong with how it is answered. \"suggested\": a line to add "
    "to or change in a box, or null.\n"
    "- \"booking\": compare the chats that booked with those that went_quiet or were not_booked: what was done "
    "differently, and how fast (the \"first answer after\" times). \"works\": what the chats that booked did; "
    "\"loses\": what the lost ones did; \"speed\": a sentence or two on reply speed.\n"
    "- \"wrong\": where the bot goes wrong: the replies she edited (what she changed, and why), the chats a person "
    "took over, the chats that went quiet right after a bot message, and answers that contradict the boxes. "
    "\"why\": why it went wrong. \"fix\": a line for a box, or null.\n"
    "- \"phrases\": lines the bot sends near-identical to many clients, or that sound like a bot (messaging apps "
    "flag a number that sends many people the same words). \"note\": what to do about it.\n"
    "- \"boxes\": where the boxes contradict each other or what the chats show, or are out of date (for example "
    "the booking box says no prices in the first reply, but the writing samples give the rates in the first "
    "reply). \"boxes\": the box keys involved. \"fix\": what to change.\n\n"
    "LENGTH: the answer must end within the space you have, so keep it short. At most 15 questions, and at most "
    "8 items in each other list. Each answer, problem, point, why, note and fix at most 200 characters; each line "
    "for a box at most 300; at most 3 wordings of at most 100 characters each; the summary at most 800 "
    "characters.\n\n"
    "RULES\n"
    "- Only what the chats show. Do not guess; write \"\" when you do not know.\n"
    "- \"chats\" lists every chat ref where the point came up, and only refs from the headers.\n"
    f"- A box key is one of: {', '.join(BOX_KEYS)}.\n"
    "- Never write a client's name, a phone number or an address, even when you see one.\n"
    "- Quote clients' wordings as given, masked; write everything else in English."
)


def review_boxes(bundle: Any) -> list[dict[str, str]]:
    """The account's boxes for the model: key, heading, where it comes from,
    and its text with the address and the way in masked."""
    booking = bundle.config.get("booking") or {}
    masks = finetune.masks_for(address=booking.get("address", ""), way_in=booking.get("arrival_instructions", ""))
    words = {"industry": "industry text", "client": "own text, replaces the industry's",
             "industry+client": "industry text + own addition", "": "empty"}
    out = [{"key": box["key"], "heading": box["heading"],
            "source": words[box["source"]] if box["inherited"] or box["source"] != "client" else "own text",
            "text": finetune.mask(box["text"], masks)} for box in sections(bundle)]
    client = prompt_layers.validate_client(bundle.layers["client"] or {}, strict=False)
    out.append({"key": "addendum", "heading": BOX_HEADINGS["addendum"], "source": "own text",
                "text": finetune.mask(client["addendum"], masks)})
    active = [" ".join(rule.split()) for rule in bundle.layers.get("rules") or [] if rule.strip()]
    out.append({"key": "rules", "heading": BOX_HEADINGS["rules"], "source": "operator rules",
                "text": finetune.mask("\n".join(f"- {rule}" for rule in active), masks)})
    return out


def review_messages(*, account: str, boxes: Sequence[dict[str, str]], chats: Sequence[str],
                    others: Sequence[str] = ()) -> list[dict[str, str]]:
    """The model's input: REVIEW_PROMPT, then the account's boxes and the
    chats, in the order of their refs."""
    parts = [f"THE BUSINESS'S BOXES ({account}), in the order the assistant reads them:"]
    parts += [f"## {box['key']} — {box['heading']} ({box['source']})\n{box['text'] or '(empty)'}" for box in boxes]
    origin = account + (f", and from {', '.join(others)}" if others else "")
    parts.append(f"THE CHATS ({len(chats)}, from {origin}):")
    parts += list(chats)
    return [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}]


# ----------------------------------------------------- checking the answer

TEXT_CHARS = 600
LINE_CHARS = 400
SHORT_CHARS = 300
MAX_QUESTIONS = 20
MAX_ITEMS = 15
MAX_WORDINGS = 3
_FENCE = re.compile(r"```[a-zA-Z]*")
_REF = re.compile(r"C(\d{1,4})", re.IGNORECASE)
_KEY_LIKE = re.compile(r"[a-z_]{1,40}")


def _object(raw: str) -> Optional[dict[str, Any]]:
    """The JSON object in the model's answer: a ```json fence and text
    around it are tolerated. None when there is none."""
    text = _FENCE.sub("", raw or "")
    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


CUT_OFF = "The answer stops before its end."


def cut_off(raw: str) -> bool:
    """Whether the answer stops inside its JSON object, as when the model
    ran out of room: after the first {, a bracket or a string still open at
    the end. DeepSeek's finish_reason does not get past vision._complete."""
    text = _FENCE.sub("", raw or "")
    start = text.find("{")
    if start < 0:
        return False
    depth, in_string, escaped = 0, False, False
    for ch in text[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return False
    return True


class _Check:
    """Reads one answer, noting in `errors` everything it drops. An error
    names the place in the answer, never what the model wrote there."""

    def __init__(self, refs: Collection[str]) -> None:
        self.refs = set(refs)
        self.errors: list[str] = []

    def text(self, value: Any, limit: int = TEXT_CHARS) -> str:
        return _flat(value, limit) if isinstance(value, str) else ""

    def strings(self, value: Any, where: str, what: str) -> list[Any]:
        """A list of texts: a single text is a list of one; anything else
        that is not text is left out, and said."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            self.errors.append(f"{where}: its {what} are not a list; left out.")
            return []
        texts = [item for item in value if isinstance(item, str)]
        if len(texts) < len(value):
            self.errors.append(f"{where}: {len(value) - len(texts)} of its {what} are not text; left out.")
        return texts

    def chats(self, value: Any, where: str) -> list[str]:
        out: list[str] = []
        for item in self.strings(value, where, "chat refs"):
            match = _REF.fullmatch(item.strip())
            ref = f"C{int(match.group(1))}" if match else None
            if ref is None or ref not in self.refs:
                self.errors.append(f"{where}: dropped chat ref {ref} (not in this review)." if ref
                                   else f"{where}: dropped a chat ref that is not of the form C<n>.")
            elif ref not in out:
                out.append(ref)
        return out

    def box(self, value: Any, where: str, allowed: Collection[str]) -> str:
        if isinstance(value, str) and value.strip().lower() in allowed:
            return value.strip().lower()
        shown = f" {value.strip()!r}" if isinstance(value, str) and _KEY_LIKE.fullmatch(value.strip()) else ""
        self.errors.append(f"{where}: unknown box{shown}, read as none.")
        return NONE

    def fix(self, value: Any, where: str) -> Optional[dict[str, str]]:
        if not isinstance(value, dict):
            return None
        line = self.text(value.get("line"), LINE_CHARS)
        if not line:
            return None
        return {"box": self.box(value.get("box"), where, (*BOX_KEYS, NONE)), "line": line}

    def items(self, value: Any, where: str, limit: int,
              read: Callable[[dict[str, Any], str], Optional[dict[str, Any]]],
              order: Optional[Callable[[dict[str, Any]], Any]] = None) -> list[dict[str, Any]]:
        """The list's items as `read` reads them (None: left out), in
        `order` if given, then capped at `limit`."""
        if value is None:
            self.errors.append(f"The answer has no {where}.")
            return []
        if not isinstance(value, list):
            self.errors.append(f"{where} is not a list; left out.")
            return []
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                self.errors.append(f"{where} #{i + 1} is not an object; left out.")
                continue
            read_item = read(item, f"{where} #{i + 1}")
            if read_item is not None:
                out.append(read_item)
        if order is not None:
            out.sort(key=order)
        if len(out) > limit:
            self.errors.append(f"{where}: {len(out) - limit} past the first {limit} left out.")
        return out[:limit]

    def with_chats(self, item: dict[str, Any], where: str) -> Optional[dict[str, Any]]:
        """Its chats and their count; None when it named chats and none of
        them is in this review: it is about chats nobody can open."""
        given = item.get("chats")
        refs = self.chats(given, where)
        if not refs and given not in (None, "", []):
            self.errors.append(f"{where}: left out, none of its chat refs is in this review.")
            return None
        return {"chats": refs, "chat_count": len(refs)}


def parse_report(raw: str, refs: Collection[str]) -> tuple[Optional[dict[str, Any]], list[str]]:
    """The model's answer as a report (ReviewReport), and what was dropped
    from it. Only known keys are kept, texts are clipped, chat refs not in
    this review (`refs`) are dropped, a box that is not a section key,
    "addendum", "rules" or "none" is read as "none", and the lists are
    capped. A list given as one text is read as a list of one; anything
    else of the wrong kind is left out. Every item with chats gets its
    chat_count, and one whose chats were all refs not in this review is
    left out; the questions come most chats first. (None, errors) when no
    JSON object can be read: errors is [CUT_OFF] when the answer stops
    inside it."""
    found = _object(raw)
    if found is None:
        return None, [CUT_OFF if cut_off(raw) else "The answer holds no JSON object."]
    check = _Check(refs)

    def question(item: dict[str, Any], where: str) -> Optional[dict[str, Any]]:
        chats = check.with_chats(item, where)
        if chats is None:
            return None
        wordings = [check.text(w, SHORT_CHARS) for w in check.strings(item.get("wordings"), where, "wordings")]
        wordings = [w for w in dict.fromkeys(wordings) if w]
        return {"question": check.text(item.get("question"), SHORT_CHARS), **chats,
                "wordings": wordings[:MAX_WORDINGS], "her_answer": check.text(item.get("her_answer")),
                "bot_answer": check.text(item.get("bot_answer")),
                "covered_by": check.box(item.get("covered_by"), where, (*BOX_KEYS, NONE)),
                "problem": check.text(item.get("problem")), "suggested": check.fix(item.get("suggested"), where)}

    def point(item: dict[str, Any], where: str) -> Optional[dict[str, Any]]:
        chats = check.with_chats(item, where)
        return None if chats is None else {"point": check.text(item.get("point")), **chats}

    def wrong(item: dict[str, Any], where: str) -> Optional[dict[str, Any]]:
        chats = check.with_chats(item, where)
        if chats is None:
            return None
        return {"what": check.text(item.get("what")), **chats, "why": check.text(item.get("why")),
                "fix": check.fix(item.get("fix"), where)}

    def phrase(item: dict[str, Any], where: str) -> Optional[dict[str, Any]]:
        chats = check.with_chats(item, where)
        if chats is None:
            return None
        return {"text": check.text(item.get("text"), LINE_CHARS), "note": check.text(item.get("note")), **chats}

    def boxes(item: dict[str, Any], where: str) -> dict[str, Any]:
        keys = [check.box(key, where, BOX_KEYS) for key in check.strings(item.get("boxes"), where, "boxes")]
        return {"boxes": [k for k in dict.fromkeys(keys) if k != NONE], "problem": check.text(item.get("problem")),
                "fix": check.text(item.get("fix"))}

    booking = found.get("booking")
    if not isinstance(booking, dict):
        check.errors.append("The answer has no booking comparison." if booking is None
                            else "booking is not an object; left out.")
        booking = {}
    report = {
        "summary": check.text(found.get("summary"), 2 * TEXT_CHARS),
        # Most chats first, then capped: the commonest are the ones kept.
        "questions": check.items(found.get("questions"), "questions", MAX_QUESTIONS, question,
                                 order=lambda q: -q["chat_count"]),
        "booking": {"works": check.items(booking.get("works", []), "booking works", MAX_ITEMS, point),
                    "loses": check.items(booking.get("loses", []), "booking loses", MAX_ITEMS, point),
                    "speed": check.text(booking.get("speed"))},
        "wrong": check.items(found.get("wrong"), "wrong", MAX_ITEMS, wrong),
        "phrases": check.items(found.get("phrases"), "phrases", MAX_ITEMS, phrase),
        "boxes": check.items(found.get("boxes"), "boxes", MAX_ITEMS, boxes),
    }
    if not report["summary"]:
        check.errors.append("The answer has no summary.")
    return report, check.errors


# What the stored report never shows as written: the ids it points with.
_NOT_TEXT = frozenset({"chats", "chat_count", "covered_by", "box", "boxes"})


def mask_report(value: Any, masks: Sequence[tuple[str, str]]) -> Any:
    """Every text of a report, or the raw answer, through `masks` (those of
    the chats read) and finetune.mask's number, e-mail and handle masks: a
    name, a number or an address the model wrote anyway is hidden before it
    is stored. The refs and box keys are left as they are."""
    if isinstance(value, str):
        return finetune.mask(value, masks)
    if isinstance(value, list):
        return [mask_report(item, masks) for item in value]
    if isinstance(value, dict):
        return {key: item if key in _NOT_TEXT and not _has_items(item) else mask_report(item, masks)
                for key, item in value.items()}
    return value


def _has_items(value: Any) -> bool:
    """A list of the report's items (its "boxes" list), not of ids."""
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


# ------------------------------------------------------- repeated bot lines
# WhatsApp flags a number that sends many people the same words. Counted
# over every line the bot wrote in the window, not only the chats a review
# reads. Two lines are the same when they are equal once normalised (case,
# emojis, punctuation and spacing left out, every number "#"), which already
# undoes what variation.py changes. Then, among the 400 commonest lines of
# four words or more, near-duplicates (the same words but for one or two,
# token sets at least 85% alike) are merged into the commoner one: cheap,
# and it catches a phrase group swapped in the middle of a line.

MIN_PHRASE_CHATS = 3
MAX_PHRASES = 20
# Shorter lines ("ok love", "yes") are what any person sends many people.
MIN_PHRASE_WORDS = 3
MERGE_MIN_WORDS = 4
MERGE_SIMILARITY = 0.85
MERGE_CANDIDATES = 400
_NOT_WORD = re.compile(r"[^\w#]+|_")


def normalise(text: str) -> str:
    """A line as compared with others: lower-case, no emojis or
    punctuation, every number "#", single spaces."""
    text = variation.EMOJI.sub(" ", (text or "").lower())
    text = re.sub(r"\d+", "#", text)
    return " ".join(_NOT_WORD.sub(" ", text).split())


def _one_line(text: str) -> bool:
    """A line of the bot's own: not a block (the rates), not several lines,
    not a placeholder ([sent photo #3: ...])."""
    text = (text or "").strip()
    return bool(text) and "\n" not in text and "[block]" not in text.lower() and not (
        text.startswith("[") and text.endswith("]"))


def repeated_phrases(messages: Iterable[dict[str, Any]], *, prompts: dict[int, str],
                     account_masks: dict[int, Sequence[tuple[str, str]]],
                     chat_masks: dict[tuple[int, int], Sequence[tuple[str, str]]]) -> list[dict[str, Any]]:
    """The bot's lines sent in at least MIN_PHRASE_CHATS different chats,
    most chats first, at most MAX_PHRASES: {text, chats, times}.

    `messages` are the bot's messages ({tenant_id, chat_id, text}); a line
    that appears in its account's prompt (`prompts`: a fixed text, meant to
    be the same every time) is not counted. The text shown is the commonest
    wording that names nobody, masked with its account's masks (the
    address, the way in; numbers and e-mails always). A wording that names
    the client it went to (his name, as `chat_masks` hide it) still counts
    but is never shown; a line whose every wording names one is left out."""
    groups: dict[str, dict[str, Any]] = {}
    for message in messages:
        text = (message.get("text") or "").strip()
        key = normalise(text)
        if not _one_line(text) or len(key.split()) < MIN_PHRASE_WORDS:
            continue
        chat = (message["tenant_id"], message["chat_id"])
        group = groups.setdefault(key, {"wordings": Counter(), "chats": set(), "times": 0, "tenants": set()})
        group["chats"].add(chat)
        group["tenants"].add(message["tenant_id"])
        group["times"] += 1
        # Only the wordings that name nobody can be shown.
        if "[client]" not in finetune.mask(text, chat_masks.get(chat, ())):
            group["wordings"][(message["tenant_id"], text)] += 1
    _merge_near(groups)

    in_prompt: dict[int, set[str]] = {tenant: {normalise(line) for line in (text or "").splitlines()}
                                      for tenant, text in prompts.items()}
    prompt_text = {tenant: "\n".join(lines) for tenant, lines in in_prompt.items()}
    found = []
    for key, group in groups.items():
        if len(group["chats"]) < MIN_PHRASE_CHATS or not group["wordings"]:
            continue
        if any(key in in_prompt.get(t, ()) or re.search(rf"(?:^| ){re.escape(key)}(?: |$)", prompt_text.get(t, ""),
                                                         re.MULTILINE) for t in group["tenants"]):
            continue
        (tenant, text), _ = group["wordings"].most_common(1)[0]
        found.append({"text": finetune.mask(text, account_masks.get(tenant, ())), "chats": len(group["chats"]),
                      "times": group["times"]})
    found.sort(key=lambda p: (-p["chats"], -p["times"], p["text"]))
    return found[:MAX_PHRASES]


def _merge_near(groups: dict[str, dict[str, Any]]) -> None:
    """Fold near-duplicate lines into the commoner one, in place."""
    candidates = sorted((key for key in groups if len(key.split()) >= MERGE_MIN_WORDS),
                        key=lambda k: (-len(groups[k]["chats"]), -groups[k]["times"], k))[:MERGE_CANDIDATES]
    leaders: list[tuple[str, set[str]]] = []
    for key in candidates:
        tokens = set(key.split())
        into = next((lead for lead, lead_tokens in leaders
                     if len(tokens & lead_tokens) / len(tokens | lead_tokens) >= MERGE_SIMILARITY), None)
        if into is None:
            leaders.append((key, tokens))
            continue
        lead, group = groups[into], groups.pop(key)
        lead["wordings"].update(group["wordings"])
        lead["chats"] |= group["chats"]
        lead["tenants"] |= group["tenants"]
        lead["times"] += group["times"]
