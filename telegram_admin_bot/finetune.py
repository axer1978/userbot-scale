"""Finetune a business's prompt layers from screenshots of its real chats.

The admin writes one finetune template per industry (finetune_api.py keeps
it in platform_settings). A run fills the template's placeholders, sends it
with the screenshots to the vision model (vision.py's endpoint) and reads
back three blocks:

    === BUSINESS LAYER ===          {"overrides": {...}, "addendum": "..."}
    === INDUSTRY STANDARD (...) === {"sections": {...}}
    === NOTES FOR OPERATOR ===      free text

Without a vision key the chats can be transcribed instead: the same
template goes to the text model (DeepSeek) with the transcripts and a note
that says how to read them in place of screenshots.

Or the admin flags real chats from the conversations view (good / bad, what
kind of client, how it ended, why, corrections on single lines): those go
to the text model the same way, each headed by that judgement, with the
client's personal data and the business's address masked.

Or the run picks, by itself, the chats where a person typed the business's
replies (on the phone, or by hand in the panel): the business's own way of
working, which outranks the standard's. Each reply is marked typed by hand
or written by the assistant, and the chats can come from several accounts.

Both JSON blocks are checked exactly as a manual save would check them
(prompt_layers.validate_client / validate_industry), plus one rule of this
module: an industry standard written by a run must have a boundaries
section, since it replaces the whole template when applied.

Pure functions and one model call; storage and applying live in
finetune_api.py.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx

import ai_responder
import prompt_layers
import vision
from ai_responder import UsageSink

# What a template may contain; each is replaced as plain text (the template
# is full of JSON braces, so str.format is out).
BUSINESS_NAME = "{BUSINESS_NAME}"
BUSINESSES_SO_FAR = "{N}"
CURRENT_STANDARD = "{CURRENT_INDUSTRY_SECTIONS_JSON}"
PLACEHOLDERS = (BUSINESS_NAME, BUSINESSES_SO_FAR, CURRENT_STANDARD)

# Starting points the admin can load into the template editor: one file per
# industry, named after it ("escort.txt" for an industry called "Escort"),
# and generic.txt for any other, with {INDUSTRY} filled in on loading.
DEFAULTS_DIR = Path(__file__).resolve().parent / "finetune_templates"
GENERIC_DEFAULT = "generic"
INDUSTRY_NAME = "{INDUSTRY}"

MAX_TEMPLATE_CHARS = 50_000
MAX_IMAGES = 40
MAX_TOTAL_IMAGE_BYTES = 60 * 1024 * 1024
# Two full prompt layers in JSON: far longer than any chat reply.
DEFAULT_MAX_TOKENS = 16_000
# Reading 40 screenshots and writing that much takes minutes, not seconds.
REQUEST_TIMEOUT_SECONDS = 600.0

_HEADER = re.compile(r"^[ \t]*=+[ \t]*([A-Za-z][^=\n]*?)[ \t]*=+[ \t]*$", re.MULTILINE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class FinetuneError(ValueError):
    pass


# ------------------------------------------------------------------ input


def check_template(template: str) -> str:
    template = (template or "").strip()
    if not template:
        raise FinetuneError("The finetune template is empty.")
    if len(template) > MAX_TEMPLATE_CHARS:
        raise FinetuneError(f"The template is {len(template)} characters; the limit is {MAX_TEMPLATE_CHARS}.")
    return template


def _default_key(industry_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", industry_name.strip().lower()).strip("_")


def default_template(industry_name: str) -> str:
    """The shipped starting template for this industry: its own file if
    there is one, else the generic one. Empty if neither exists."""
    key = _default_key(industry_name)
    path = DEFAULTS_DIR / f"{key}.txt"
    if not key or key == GENERIC_DEFAULT or not path.is_file():
        path = DEFAULTS_DIR / f"{GENERIC_DEFAULT}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.replace(INDUSTRY_NAME, industry_name.strip() or "this").strip()


def fill_template(template: str, *, business_name: str, businesses_so_far: int,
                  industry_sections: dict[str, str]) -> str:
    """The template with its placeholders filled. An industry with no
    sections yet is "none", which the template tells the model to treat as
    the first business."""
    current = (json.dumps({"sections": industry_sections}, ensure_ascii=False, indent=2)
               if industry_sections else "none")
    return (check_template(template)
            .replace(BUSINESS_NAME, business_name.strip() or "this business")
            .replace(BUSINESSES_SO_FAR, str(businesses_so_far))
            .replace(CURRENT_STANDARD, current))


def screenshot_order(name: str) -> list[Any]:
    """Natural order, so 2 comes before 10 and 03a, 03b, 03c stay together."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def check_images(images: Sequence[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Sorted by file name, each a readable image of a size the provider
    takes. Raises FinetuneError naming the file that is wrong."""
    if not images:
        raise FinetuneError("Add at least one screenshot.")
    if len(images) > MAX_IMAGES:
        raise FinetuneError(f"{len(images)} screenshots; at most {MAX_IMAGES} per run.")
    total = sum(len(data) for _, data in images)
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise FinetuneError(f"The screenshots add up to {total // (1024 * 1024)} MB; "
                            f"at most {MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MB per run.")
    for name, data in images:
        if len(data) > vision.MAX_IMAGE_BYTES:
            raise FinetuneError(f"{name} is larger than {vision.MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        if vision.sniff_mime(data) is None:
            raise FinetuneError(f"{name} is not a JPEG, PNG, WebP or GIF image.")
    return sorted(images, key=lambda item: screenshot_order(item[0]))


def build_messages(prompt: str, images: Sequence[tuple[str, bytes]]) -> list[dict[str, Any]]:
    """One user message: the filled template, then each screenshot after a
    line naming its file, since the names carry the conversation order and
    the operator's good/bad marks."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for name, data in images:
        content.append({"type": "text", "text": f"Screenshot {name}:"})
        content.append(vision._image_part(data, name))
    return [{"role": "user", "content": content}]


# ------------------------------------------------------------- transcripts

MAX_CHATS = 100
# The template plus this must fit the text model's context with room for a
# long answer.
MAX_TRANSCRIPT_CHARS = 100_000
DEFAULT_TEXT_MODEL = "deepseek-chat"
# DeepSeek's chat model writes at most 8K tokens per answer.
DEFAULT_TEXT_MAX_TOKENS = 8_000

# Comes between the template and the transcripts, so a template written for
# screenshots is read correctly without being edited.
TRANSCRIPT_NOTE = (
    "THE CHATS BELOW ARE TEXT, NOT SCREENSHOTS. The operator transcribed them by hand. Wherever the "
    "instructions above talk about screenshots, read them as these transcripts:\n"
    "- Each conversation starts with a line \"### <name>\". The name works like a screenshot's file name: "
    "the same number is one conversation, in order, and a name ending in \"-good\" or \"-bad\" carries "
    "the operator's mark.\n"
    "- Lines starting \"Client:\" are the client, lines starting \"Business:\" are the business. Bubble "
    "colours and sides do not apply.\n"
    "- Timestamps, voice notes and photos appear only where the operator wrote them, e.g. [10:42] or "
    "[voice note].\n"
    "- If a line does not say who wrote it, or a conversation is unclear, list it in the operator notes. "
    "Do not guess."
)


def check_chats(chats: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Non-empty conversations in natural order, within the size limits."""
    chats = [(name.strip(), text.strip()) for name, text in chats if text.strip()]
    if not chats:
        raise FinetuneError("Add at least one conversation.")
    if len(chats) > MAX_CHATS:
        raise FinetuneError(f"{len(chats)} conversations; at most {MAX_CHATS} per run.")
    names = [name for name, _ in chats]
    if any(not name for name in names):
        raise FinetuneError("Every conversation needs a name, e.g. 03 or 03-good.")
    if len(set(names)) != len(names):
        raise FinetuneError("Two conversations have the same name.")
    total = sum(len(text) for _, text in chats)
    if total > MAX_TRANSCRIPT_CHARS:
        raise FinetuneError(f"The transcripts are {total:,} characters; at most {MAX_TRANSCRIPT_CHARS:,} per run.")
    return sorted(chats, key=lambda item: screenshot_order(item[0]))


def _transcripts(chats: Sequence[tuple[str, str]]) -> str:
    return "\n\n".join(f"### {name}\n{text}" for name, text in chats)


def build_text_messages(prompt: str, chats: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": f"{prompt}\n\n{TRANSCRIPT_NOTE}\n\n{_transcripts(chats)}"}]


def text_model() -> str:
    return (os.getenv("FINETUNE_TEXT_MODEL") or "").strip() or DEFAULT_TEXT_MODEL


# ----------------------------------------------------------- flagged chats
# Real chats the admin flagged from the conversations view. They go to the
# text model like typed transcripts, each headed by the admin's judgement.

VERDICTS = {
    "good": "GOOD: handled the way the business wants. Learn from it first.",
    "bad": "BAD: shows replies NOT to repeat. Never use them as writing samples.",
    "plain": "an ordinary example, neither marked good nor bad.",
}
CLIENT_KINDS = {
    "serious": "SERIOUS client",
    "time_waster": "TIME-WASTER",
    "safety_risk": "SAFETY RISK",
    "regular": "REGULAR (a returning client)",
}
OUTCOMES = {
    "came": "he booked and came",
    "no_show": "he booked but did not come (no-show)",
    "cancelled": "he booked, then cancelled",
    "never_booked": "he never booked",
    "blocked": "the business stopped answering him or blocked him",
}
MAX_NOTE_CHARS = 4_000
MAX_CORRECTION_CHARS = 1_000
MAX_CORRECTIONS = 100
# From the first message on: first contact is what matters most.
MAX_EXAMPLE_MESSAGES = 300

# After TRANSCRIPT_NOTE, in a run from flagged chats.
EXAMPLES_NOTE = (
    "THESE ARE THE BUSINESS'S REAL CHATS, FLAGGED BY THE OPERATOR. Most business replies were written by the "
    "assistant you are tuning, so a \"-bad\" chat shows the assistant's own mistakes. Each conversation starts "
    "with the operator's judgement, which is not part of the chat:\n"
    "- \"Handled:\" good = learn from it first (like a \"-good\" screenshot); bad = replies not to repeat (like "
    "\"-bad\"); ordinary = an ordinary example.\n"
    "- \"Client:\" what kind of client he was: serious, time-waster, safety risk or regular. This is the "
    "operator's verdict and it stands. Learn from the chat the SIGNS that show this kind of client early, and "
    "write them into the Qualify step. If you read the client differently, say so in the operator notes.\n"
    "- \"Ended:\" what really happened afterwards (came, no-show, cancelled, never booked, blocked). A client "
    "who looked serious and did not come teaches warning signs too.\n"
    "- \"Why:\" the operator's explanation of how to read this chat. Follow it, and turn it into a rule where "
    "it fits (qualify, booking, boundaries, tone).\n"
    "- A line \"ANNOTATION FOR MODEL: ...\" right under a message is the operator's comment on that message. "
    "Under a business reply: what was wrong, or what the business should have written; treat it like a "
    "\"-bad\" lesson and turn it into a rule. Under a client message: what that message shows about him (a "
    "sign of a serious client, a time-waster or a risk); use it in the Qualify step.\n"
    "- A message can run over several lines (the rates and extras messages do): it lasts until the next line "
    "that starts with a time, \"ANNOTATION FOR MODEL:\" or \"—\". Lines \"— booking: ... —\" are what the booking "
    "system did (requested, confirmed, declined, address sent), not messages. \"[sent photo #N: ...]\" is a "
    "photo the business sent.\n"
    "- Personal data is already masked: [client], [number], [username], [email], and the business's own "
    "[address] and [way in]. Keep them masked."
)


class ExampleError(FinetuneError):
    pass


def check_example(*, verdict: str, client_kind: str, outcome: str, note: str,
                  corrections: dict[str, str], message_ids: set[int]) -> dict[str, Any]:
    """The admin's judgement of one chat, cleaned. Corrections must point at
    messages of the chat as it was flagged; empty ones are dropped."""
    if verdict not in VERDICTS:
        raise ExampleError("Mark the chat good, bad or ordinary.")
    if client_kind and client_kind not in CLIENT_KINDS:
        raise ExampleError(f"Unknown kind of client: {client_kind}.")
    if outcome and outcome not in OUTCOMES:
        raise ExampleError(f"Unknown outcome: {outcome}.")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise ExampleError(f"The explanation is {len(note)} characters; at most {MAX_NOTE_CHARS}.")
    clean: dict[str, str] = {}
    for key, text in (corrections or {}).items():
        text = (text or "").strip()
        if not text:
            continue
        try:
            message_id = int(key)
        except (TypeError, ValueError):
            raise ExampleError(f"A correction points at {key!r}, which is not a message.") from None
        if message_id not in message_ids:
            raise ExampleError("A correction points at a message that is not in this chat.")
        if len(text) > MAX_CORRECTION_CHARS:
            raise ExampleError(f"A correction is {len(text)} characters; at most {MAX_CORRECTION_CHARS}.")
        clean[str(message_id)] = text
    if len(clean) > MAX_CORRECTIONS:
        raise ExampleError(f"{len(clean)} corrections; at most {MAX_CORRECTIONS} per chat.")
    return {"verdict": verdict, "client_kind": client_kind or "", "outcome": outcome or "",
            "note": note, "corrections": clean}


def example_name(example_id: int, verdict: str) -> str:
    """Like a screenshot's file name: "07-good", "12-bad", "15"."""
    return f"{example_id:02d}" + (f"-{verdict}" if verdict in ("good", "bad") else "")


def chat_lines(messages: Sequence[dict[str, Any]],
               limit: Optional[int] = MAX_EXAMPLE_MESSAGES) -> list[dict[str, Any]]:
    """The chat as the model reads it: what crossed the wire, and the
    booking system's events. Drafts never sent, rejected drafts, errors and
    the panel's other notes are left out. A business message that says
    whether a person typed it ("typed") keeps that mark. At most `limit`
    lines from the start (None: all of them; the context review cuts a long
    chat itself, keeping its end too)."""
    lines: list[dict[str, Any]] = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if m["direction"] == "in" and m["status"] == "received":
            who = "client"
        elif m["direction"] == "out" and m["status"] == "sent":
            who = "business"
            if not text and m.get("attachments"):
                text = "[sent a file]"
        elif m["status"] == "note" and "Booking #" in text:
            who = "event"
        else:
            continue
        if text:
            line = {"id": m["id"], "who": who, "text": text, "at": m.get("created_at")}
            if who == "business" and "typed" in m:
                line["typed"] = bool(m["typed"])
            lines.append(line)
    return lines if limit is None else lines[:limit]


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A + and 7 or more digits, or 8 or more digits in a row (dashes allowed);
# spaces only after a +, so "100 150 200" in a price list and a date like
# 12.10.2026 are left alone.
_PHONE = re.compile(r"\+\d(?:[ \-().]?\d){6,}|(?<!\d)\d(?:-?\d){7,}(?!\d)")
_HANDLE = re.compile(r"(?<![\w.@])@[A-Za-z0-9_]{3,}")
# A client's name is hidden in any case ("janis", "JANIS"), except that a
# contact named like an everyday word ("Love", "Baby") must not eat "my
# love": such a word written in small letters is left as it is.
EVERYDAY_WORDS = frozenset({
    "love", "baby", "babe", "honey", "darling", "dear", "sweet", "sweetie", "sweetheart", "angel", "king", "queen",
    "prince", "princess", "boss", "friend", "bro", "man", "guy", "boy", "girl", "mister", "sir", "daddy", "papi",
    "handsome", "beautiful", "sexy", "kitty", "bunny", "tiger", "client", "me", "you",
})


def masks_for(*, display_name: str = "", username: str = "", address: str = "",
              way_in: str = "", person_name: str = "") -> list[tuple[str, str]]:
    """What to hide from the model in one chat: the client's name, and the
    business's address and way in (the system sends those, they must never
    turn up in a prompt). `person_name`: the name a WhatsApp person gave
    themselves, when the chat is labelled with their number. Longest first,
    so a whole text is replaced before a line of it."""
    found: list[tuple[str, str]] = []
    for text, label in ((address, "[address]"), (way_in, "[way in]")):
        text = (text or "").strip()
        if text:
            found.append((text, label))
            found += [(line.strip(), label) for line in text.splitlines() if len(line.strip()) >= 6]
    for name in (display_name, person_name):
        name = (name or "").strip()
        if len(name) < 3 or _PHONE.fullmatch(name):
            continue  # a number label is hidden as [number] by mask()
        found.append((name, "[client]"))
        words = name.split()
        # Only a name-like first or last word: "love bug" as a contact name must not eat "my love".
        for word in {words[0], words[-1]} if len(words) > 1 else ():
            if len(word) >= 3 and word[0].isupper():
                found.append((word, "[client]"))
    if username and len(username.lstrip("@")) >= 3:
        found.append(("@" + username.lstrip("@"), "[username]"))
    return sorted(set(found), key=lambda item: -len(item[0]))


def _name(literal: str, label: str) -> Callable[["re.Match[str]"], str]:
    everyday = literal.lower() in EVERYDAY_WORDS

    def hide(match: "re.Match[str]") -> str:
        return match.group(0) if everyday and match.group(0).islower() else label
    return hide


def mask(text: str, masks: Sequence[tuple[str, str]]) -> str:
    for literal, label in masks:
        if label == "[client]":
            # Not inside an e-mail address or a handle: those have masks of their own.
            text = re.sub(rf"(?<![\w@.]){re.escape(literal)}(?![\w@])", _name(literal, label), text,
                          flags=re.IGNORECASE)
        else:
            text = re.sub(re.escape(literal), label, text, flags=re.IGNORECASE)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[number]", text)
    return _HANDLE.sub("[username]", text)


def _local(at: Any, tz: Optional[ZoneInfo]) -> Optional[datetime]:
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at)
        except ValueError:
            return None
    if not isinstance(at, datetime):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(tz) if tz else at


def render_example(example: dict[str, Any], lines: Sequence[dict[str, Any]], *,
                   masks: Sequence[tuple[str, str]] = (), tz: Optional[ZoneInfo] = None) -> str:
    """One flagged chat as a transcript: the operator's judgement, then the
    chat with times, a line for each new day, and each correction under the
    message it corrects."""
    head = ["OPERATOR'S JUDGEMENT (not part of the chat):",
            f"- Handled: {VERDICTS.get(example['verdict'], VERDICTS['plain'])}"]
    if example.get("client_kind") in CLIENT_KINDS:
        head.append(f"- Client: {CLIENT_KINDS[example['client_kind']]}")
    if example.get("outcome") in OUTCOMES:
        head.append(f"- Ended: {OUTCOMES[example['outcome']]}")
    note = mask(example.get("note") or "", masks).strip()
    if note:
        head.append("- Why: " + note.replace("\n", "\n  "))

    corrections = example.get("corrections") or {}
    body: list[str] = []
    for line, rendered in rendered_lines(lines, masks, tz,
                                         lambda line: "Client" if line["who"] == "client" else "Business"):
        body += rendered
        correction = corrections.get(str(line["id"]))
        if correction:
            body.append("ANNOTATION FOR MODEL: " + mask(correction, masks).replace("\n", " "))
    return "\n".join(head) + "\n\n" + "\n".join(body)


def rendered_lines(lines: Sequence[dict[str, Any]], masks: Sequence[tuple[str, str]], tz: Optional[ZoneInfo],
                   who: Callable[[dict[str, Any]], str]) -> Iterator[tuple[dict[str, Any], list[str]]]:
    """Each chat line as transcript lines, masked: a "— <day> —" line when
    the day changes, then the message with its time and `who(line)`, or the
    booking event."""
    day = None
    for line in lines:
        out: list[str] = []
        at = _local(line.get("at"), tz)
        if at and at.date() != day:
            day = at.date()
            out.append(f"— {at:%a %d.%m.%Y} —")
        text = mask(line["text"], masks)
        if line["who"] == "event":
            out.append(f"— booking: {text} —")
        else:
            out.append((f"[{at:%H:%M}] " if at else "") + f"{who(line)}: {text}")
        yield line, out


def build_example_messages(prompt: str, chats: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": f"{prompt}\n\n{TRANSCRIPT_NOTE}\n\n{EXAMPLES_NOTE}\n\n{_transcripts(chats)}"}]


# -------------------------------------------------------- hand-typed chats
# The chats where a person typed the business's replies, picked by the
# system rather than flagged: the business's own way of working. The
# assistant's replies in the same chats stay in, marked, so the model sees
# where a person took over and what it did differently.

TYPED = "Business (typed by hand)"
ASSISTANT = "Assistant"
# One long chat must not take a run's whole budget; its start, with the
# first contact, matters most.
MAX_HAND_CHAT_CHARS = 12_000
CUT = "— (the rest of this chat is left out) —"

# After TRANSCRIPT_NOTE, in a run from hand-typed chats.
HAND_NOTE = (
    "THESE ARE REAL CHATS IN WHICH A PERSON TYPED THE BUSINESS'S REPLIES, on the phone or by hand in the panel. "
    "The system picked them; nobody judged them. They show how the work really goes, and on the voice and the "
    "pipeline they outrank everything else you were told about this business:\n"
    f"- \"{TYPED}:\" a reply a person typed. This is the voice and the way of working to learn: how the "
    "business opens, what it says first and what it leaves for later, how long its messages are, emojis, "
    "spelling and capitals, when the rates, photos and address go out, how it asks for a time, how it answers "
    "the usual questions, how it closes the booking.\n"
    f"- \"{ASSISTANT}:\" a reply the assistant you are tuning wrote. It is NOT the voice to learn. Where a person "
    "took over from the assistant, or the typed replies do something the assistant does not, the person is "
    "right: write that into the standard.\n"
    "- Where the typed replies work differently from the current industry standard (the first reply, the order "
    "of the steps, wording, length, emojis), the typed way wins: rewrite the standard's booking pipeline, faq, "
    "tone, sign_off and writing_samples to match it. This overrides any instruction above to only add to the "
    "existing standard. The boundaries section, and every rule the instructions above call fixed, always stay. "
    "In the operator notes, list each change as \"CHANGED: <old> → <new> (from <conversation names>)\".\n"
    "- Each conversation starts with a line \"FROM:\", the account it comes from. Only conversations from THIS "
    "business count for the business layer (its prices, services, hours and area). Conversations from other "
    "accounts teach the shared voice and pipeline, never their prices, services or places.\n"
    "- A person typing fast makes mistakes. A price below the business's rates, an address, door code or flat "
    "typed into the chat, a time confirmed before the business decided, a promise only the business can make, "
    "or anything against the boundaries is a mistake, not a pattern: never turn it into a rule or a sample, and "
    "list it in the operator notes.\n"
    "- Different people, or one person on different days, word things differently. Learn what they share; "
    "where they vary, keep the variety instead of choosing one line.\n"
    "- A real person never sends two clients the same text. In writing_samples, give at least three different "
    "wordings for each answer the business gives often (greeting, asking for a time, sending photos, the price, "
    "confirming, on the way, arrival). In the operator notes, add a section \"PHRASE GROUPS\": one line per "
    "group of short phrases from the typed replies that mean the same and can stand in for each other, written "
    "\"a | b | c\", for the reply variation settings. Never put a price, a time or an address in a group.\n"
    "- \"[non-text message]\" is a photo, voice note or sticker sent from the phone; \"[sent photo #N: ...]\" is "
    "a photo from the business's library. Lines \"— booking: ... —\" are what the booking system did, not "
    f"messages. \"{CUT}\" means the chat went on.\n"
    "- Personal data is already masked: [client], [number], [username], [email], and the business's own "
    "[address] and [way in]. Keep them masked."
)


def hand_origin(account: str, *, own: bool) -> str:
    """The FROM line's text: whose chat it is, and what it may teach."""
    if own:
        return f"{account} (THIS business: its prices, services, hours and area count)"
    return f"{account} (another account: learn its voice and pipeline only, never its prices, services or area)"


def render_hand_chat(lines: Sequence[dict[str, Any]], *, origin: str, masks: Sequence[tuple[str, str]] = (),
                     tz: Optional[ZoneInfo] = None, max_chars: int = MAX_HAND_CHAT_CHARS) -> str:
    """One chat for a run from hand-typed chats: where it comes from, then
    the chat, each business reply marked typed by hand or written by the
    assistant. Longer than `max_chars`, it is cut and its start kept."""
    out = [f"FROM: {origin}", ""]
    size = sum(len(part) + 1 for part in out)
    for _, rendered in rendered_lines(lines, masks, tz, lambda line: (
            "Client" if line["who"] == "client" else TYPED if line.get("typed") else ASSISTANT)):
        chunk = "\n".join(rendered)
        if size + len(chunk) + len(CUT) + 1 > max_chars:
            out.append(CUT)
            break
        out.append(chunk)
        size += len(chunk) + 1
    return "\n".join(out)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower()) or "account"


def plan_hand_run(chats: Sequence[dict[str, Any]], accounts: Sequence[tuple[int, str]], *,
                  max_chats: int = MAX_CHATS, max_chars: int = MAX_TRANSCRIPT_CHARS) -> list[dict[str, Any]]:
    """Name and pick the chats of a run from hand-typed chats.

    `chats` carry "tenant_id", "typed" (replies typed by hand), "last_at"
    and "chars" (the rendered length); `accounts` are (tenant id, name),
    the business the run is for first. Each account's chats are ranked, the
    most typed replies first, then the newest, and named after the account
    and the rank ("pam-03"). They are picked in turns, each account's next
    best after the other's, as many as the run's limits take, so one busy
    account does not crowd the others out. Returns copies in that order,
    with "name" and "picked" set."""
    slugs: dict[int, str] = {}
    for tenant_id, name in accounts:
        slug = _slug(name)
        slugs[tenant_id] = f"{slug}{tenant_id}" if slug in slugs.values() else slug
    turns: list[tuple[int, int, dict[str, Any]]] = []
    for turn_of_account, (tenant_id, _) in enumerate(accounts):
        own = [dict(c) for c in chats if c["tenant_id"] == tenant_id]
        own.sort(key=lambda c: str(c.get("last_at") or ""), reverse=True)
        own.sort(key=lambda c: c["typed"], reverse=True)
        for rank, chat in enumerate(own, start=1):
            chat["name"] = f"{slugs[tenant_id]}-{rank:02d}"
            turns.append((rank, turn_of_account, chat))
    turns.sort(key=lambda item: item[:2])
    planned, count, total = [], 0, 0
    for _, _, chat in turns:
        chat["picked"] = count < max_chats and total + chat["chars"] <= max_chars
        if chat["picked"]:
            count, total = count + 1, total + chat["chars"]
        planned.append(chat)
    return planned


def build_hand_messages(prompt: str, chats: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": f"{prompt}\n\n{TRANSCRIPT_NOTE}\n\n{HAND_NOTE}\n\n{_transcripts(chats)}"}]


def text_max_tokens() -> int:
    try:
        return max(1000, int(os.getenv("FINETUNE_TEXT_MAX_TOKENS") or DEFAULT_TEXT_MAX_TOKENS))
    except ValueError:
        return DEFAULT_TEXT_MAX_TOKENS


# ----------------------------------------------------------------- output


def split_blocks(text: str) -> dict[str, str]:
    """The model's answer by block: "business", "industry", "notes"."""
    found: dict[str, str] = {}
    headers = list(_HEADER.finditer(text or ""))
    for i, header in enumerate(headers):
        title = header.group(1).upper()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[header.end():end].strip()
        if title.startswith("BUSINESS LAYER"):
            found.setdefault("business", body)
        elif title.startswith("INDUSTRY STANDARD"):
            found.setdefault("industry", body)
        elif title.startswith("NOTES"):
            found.setdefault("notes", body)
    return found


def _json_object(body: str, what: str) -> dict[str, Any]:
    body = _FENCE.sub("", body.strip()).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise FinetuneError(f"The {what} block has no JSON object.")
    try:
        value = json.loads(body[start:end + 1])
    except ValueError as exc:
        raise FinetuneError(f"The {what} block is not valid JSON: {exc}.") from None
    if not isinstance(value, dict):
        raise FinetuneError(f"The {what} block is not a JSON object.")
    return value


def check_business_layer(layer: Any) -> dict[str, Any]:
    try:
        return prompt_layers.validate_client(layer)
    except prompt_layers.PromptError as exc:
        raise FinetuneError(f"Business layer: {exc}") from None


def check_industry_sections(sections: Any) -> dict[str, str]:
    try:
        clean = prompt_layers.validate_industry({"sections": sections})["sections"]
    except prompt_layers.PromptError as exc:
        raise FinetuneError(f"Industry standard: {exc}") from None
    if not clean.get("boundaries"):
        raise FinetuneError("Industry standard: it has no boundaries section. Applying it would replace "
                            "the industry's boundaries with nothing.")
    return clean


def parse_output(text: str) -> dict[str, Any]:
    """What the run proposes, plus every problem found. A block that parses
    but does not validate is still returned, so the admin can fix it by
    hand instead of paying for another run."""
    blocks = split_blocks(text)
    errors: list[str] = []
    business: Optional[dict[str, Any]] = None
    industry: Optional[dict[str, Any]] = None

    if "business" not in blocks:
        errors.append("The answer has no === BUSINESS LAYER === block.")
    else:
        try:
            business = _json_object(blocks["business"], "business layer")
            business = check_business_layer(business)
        except FinetuneError as exc:
            errors.append(str(exc))

    if "industry" not in blocks:
        errors.append("The answer has no === INDUSTRY STANDARD === block.")
    else:
        try:
            parsed = _json_object(blocks["industry"], "industry standard")
            industry = parsed.get("sections", parsed)
            industry = check_industry_sections(industry)
        except FinetuneError as exc:
            errors.append(str(exc))

    return {"business_layer": business, "industry_sections": industry,
            "notes": blocks.get("notes", ""), "errors": errors}


# ------------------------------------------------------------------ model


def max_tokens() -> int:
    try:
        return max(1000, int(os.getenv("FINETUNE_MAX_TOKENS") or DEFAULT_MAX_TOKENS))
    except ValueError:
        return DEFAULT_MAX_TOKENS


async def complete(
    prompt: str,
    images: Sequence[tuple[str, bytes]],
    *,
    api_url: str,
    api_key: str,
    model: str,
    usage_sink: Optional[UsageSink] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """The model's raw answer. Raises vision.VisionError with a safe message."""
    vision._check_endpoint(api_url, api_key, model)
    owns = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        text = await vision._complete(
            api_url=api_url, api_key=api_key, client=http, usage_sink=usage_sink,
            payload={"model": model, "messages": build_messages(prompt, images),
                     "max_tokens": max_tokens(), "temperature": 0.2},
        )
    finally:
        if owns:
            await http.aclose()
    if not text:
        raise vision.VisionError("The model returned an empty answer.")
    return text


async def complete_text(
    prompt: str,
    chats: Sequence[tuple[str, str]],
    *,
    api_key: str,
    model: str,
    flagged: bool = False,
    hand: bool = False,
    usage_sink: Optional[UsageSink] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """The text model's raw answer for transcribed chats (`flagged`: real
    chats the admin flagged, with the note on reading their judgement;
    `hand`: real chats a person typed replies in, with the note on reading
    those). DeepSeek speaks the same chat-completions format, so this
    reuses vision.py's call, which allows a long answer the time it takes
    (ai_responder's cap is for replies to customers). Raises
    vision.VisionError with a safe message."""
    if not api_key:
        raise vision.VisionError("DEEPSEEK_PLATFORM_KEY is not set, so transcripts cannot be read.")
    build = build_hand_messages if hand else build_example_messages if flagged else build_text_messages
    owns = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        text = await vision._complete(
            api_url=ai_responder.API_URL, api_key=api_key, client=http, usage_sink=usage_sink,
            service="DeepSeek API", key_name="DEEPSEEK_PLATFORM_KEY",
            payload={"model": model, "messages": build(prompt, chats),
                     "max_tokens": text_max_tokens(), "temperature": 0.2},
        )
    finally:
        if owns:
            await http.aclose()
    if not text:
        raise vision.VisionError("The model returned an empty answer.")
    return text
