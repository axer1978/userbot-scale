"""Builds the prompt from the persona config and calls the DeepSeek API."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.deepseek.com/chat/completions"

# Used before the persona has been filled in from the Settings tab, so the
# app still produces something sane instead of erroring out.
FALLBACK_SYSTEM_PROMPT = (
    "You are drafting a reply from a real person's Telegram account. "
    "Keep it short and natural."
)

PERSONA_HEADER = (
    "You are drafting a reply that will be sent from a real person's personal "
    "Telegram account. Write in the first person as that person. Never state or "
    "imply that you are an AI, a bot, or an assistant. Output only the message "
    "text to send — no quotation marks, no labels, no commentary, no analysis."
)

_PERSONA_SECTIONS = (
    ("purpose", "WHAT THIS ACCOUNT IS FOR"),
    ("tone", "TONE AND STYLE"),
    ("languages", "LANGUAGE"),
    ("boundaries", "HARD RULES — NEVER BREAK THESE"),
    ("signature_style", "SIGN-OFF"),
)

# The setting is a bare noun ("english"), which on its own reads as a label
# rather than an instruction — weak next to anything later in the prompt that
# sounds more specific. Spelled out as a rule, it holds.
_LANGUAGE_DIRECTIVE = (
    "Write every reply in this language and no other. This holds even when the "
    "other person writes to you in a different language — do not switch to "
    "match them, and do not translate or repeat yourself in their language. "
    "The only exception is if the instructions above explicitly say to mirror "
    "whatever language they use."
)

# ------------------------------------------------------------------ bursts
#
# People do not text in paragraphs. A reply with two thoughts in it goes out
# as two messages, one after the other; a long reply is broken up at its
# natural pauses. The model marks the breaks with the separator and
# split_burst() turns the result into the parts that main.send_burst sends
# as consecutive Telegram messages. The separator itself never reaches
# Telegram.
BURST_SEPARATOR = "|||"
# Every part is a real message that counts against the daily ceilings, so
# the count cannot be left to the model. Overflow is folded into the last part.
MAX_BURST_MESSAGES = 4
# A line break counts as a boundary too. In practice the model marks the
# break between two thoughts the way a person does — by pressing Enter —
# far more often than with the separator it was asked for, and a burst that
# arrives as one multi-line bubble is not a burst.
_BURST_SPLIT = re.compile(r"\s*\|{3,}\s*|\s*\n\s*")

BURST_OUTPUT_NOTE = (
    "SENDING AS SEVERAL MESSAGES: real people often send a reply as a few "
    "short messages in a row rather than one block. When your reply has more "
    "than one thought in it, or it is getting long, break it into consecutive "
    "messages: every line break in your output is sent as a separate message, "
    "so put each message on its own line, exactly where a person would hit "
    f"send. (Writing {BURST_SEPARATOR} on its own line between messages means "
    "the same thing.) A short casual reply with two separate points is "
    "typically two messages of one sentence each; a longer reply is split "
    "where a person would naturally hit send — never in the middle of a "
    "sentence. A reply that is a single short thought stays one line. Use at "
    f"most {MAX_BURST_MESSAGES} messages and keep each one short."
)

# The brief from a linked chat is prepended under this header. The model has
# to treat it as things it already knows, not as a document it was handed.
BACKGROUND_HEADER = (
    "WHAT YOU ALREADY KNOW ABOUT THIS PERSON (from an earlier conversation with "
    "them on another account): treat the following as your own memory. Use it "
    "naturally where it helps, but never mention the other chat, the other "
    "account, or that you were told any of this."
)

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 60.0


class AIResponderError(Exception):
    """Raised for any failure that should surface in the admin panel."""


def split_burst(text: str) -> list[str]:
    """The parts of a reply, in order: one per separator or line break.

    Padding around the separator and a hand that leaned on the key ("||||")
    are tolerated; empty parts are dropped; the result is capped at
    MAX_BURST_MESSAGES with the overflow riding on the last part. A reply
    that was nothing but separators comes back empty — the caller reports
    that rather than sending a blank message.
    """
    parts = [part.strip() for part in _BURST_SPLIT.split(text or "")]
    parts = [part for part in parts if part]
    if len(parts) > MAX_BURST_MESSAGES:
        head = parts[: MAX_BURST_MESSAGES - 1]
        head.append(" ".join(parts[MAX_BURST_MESSAGES - 1 :]))
        parts = head
    return parts


def language_is_pinned(persona: dict[str, Any]) -> bool:
    """True when the Settings tab fixes the reply language."""
    return bool((persona.get("languages") or "").strip())


def build_system_prompt(persona: dict[str, Any]) -> str:
    """Assemble the system message; falls back to a neutral one when blank."""
    sections = []
    for key, label in _PERSONA_SECTIONS:
        value = (persona.get(key) or "").strip()
        if not value:
            continue
        if key == "languages":
            value += "\n" + _LANGUAGE_DIRECTIVE
        sections.append(f"{label}:\n{value}")
    if not sections:
        return FALLBACK_SYSTEM_PROMPT
    return PERSONA_HEADER + "\n\n" + "\n\n".join(sections)


# ------------------------------------------------------- adaptive style

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤️]"
)


def describe_style(history: list[dict[str, str]], language_locked: bool = False) -> str:
    """A short brief on how the other person writes, so the reply can match it.

    Built only from their own messages — mirroring our own past replies would
    just entrench whatever the model did first. Needs at least two samples
    before guessing. With a language pinned in Settings the brief must not
    contradict it, so it asks to mirror everything except the language.
    """
    theirs = [
        (m.get("content") or "")
        for m in history
        if m.get("role") == "user" and (m.get("content") or "").strip()
    ]
    if len(theirs) < 2:
        return ""

    lengths = [len(t) for t in theirs]
    avg_len = sum(lengths) / len(lengths)
    avg_words = sum(len(t.split()) for t in theirs) / len(theirs)
    with_emoji = sum(1 for t in theirs if _EMOJI.search(t))
    # Do they start sentences with a capital, and end with punctuation?
    capitalised = sum(1 for t in theirs if t[:1].isupper())
    punctuated = sum(1 for t in theirs if t.rstrip()[-1:] in ".!?")

    notes = []
    if avg_len < 25:
        notes.append(f"very short messages (about {avg_len:.0f} characters, "
                     f"{avg_words:.0f} words) — often just a few words")
    elif avg_len < 80:
        notes.append(f"short messages (about {avg_len:.0f} characters)")
    elif avg_len < 200:
        notes.append(f"medium-length messages (about {avg_len:.0f} characters)")
    else:
        notes.append(f"long, detailed messages (about {avg_len:.0f} characters)")

    ratio = with_emoji / len(theirs)
    if ratio > 0.5:
        notes.append("emoji in most messages")
    elif ratio > 0.15:
        notes.append("the occasional emoji")
    else:
        notes.append("no emoji")

    if capitalised / len(theirs) < 0.4:
        notes.append("mostly lower-case, not much capitalisation")
    if punctuated / len(theirs) < 0.3:
        notes.append("often no full stop at the end")

    if language_locked:
        language = (
            "Mirror all of that but NOT their language — the LANGUAGE rule above "
            "stands no matter what language they write in."
        )
    else:
        language = "Reply in the language they are writing in."

    return (
        "HOW THIS PERSON WRITES: " + "; ".join(notes) + ".\n"
        "Match them. Write about the same length — if they send one line, send one "
        "line, never a paragraph. Mirror their level of formality, their emoji use "
        "and their punctuation habits. " + language
    )


_LENGTH_RULES = {
    "short": "MESSAGE LENGTH: keep it to a few words, one line at most.",
    "medium": "MESSAGE LENGTH: a couple of sentences, no more.",
    "long": "MESSAGE LENGTH: a fuller reply of a few sentences is fine here.",
}


def _contact_sections(contact: Optional[dict[str, Any]]) -> list[str]:
    """Per-contact overrides from the panel, as prompt sections."""
    contact = contact or {}
    sections = []
    extra = (contact.get("persona_extra") or "").strip()
    if extra:
        sections.append(f"EXTRA RULES FOR THIS PERSON:\n{extra}")
    notes = (contact.get("style_notes") or "").strip()
    if notes:
        sections.append(f"ABOUT THIS PERSON:\n{notes}")
    samples = (contact.get("chat_samples") or "").strip()
    if samples:
        sections.append(
            "HOW I HAVE WRITTEN TO THIS PERSON BEFORE (match this voice exactly):\n"
            + samples
        )
    rule = _LENGTH_RULES.get((contact.get("message_length") or "auto").lower())
    if rule:
        sections.append(rule)
    return sections


def _samples_section(general_samples: str) -> str:
    samples = (general_samples or "").strip()
    if not samples:
        return ""
    return "HOW I WRITE (real examples of my messages — match this voice):\n" + samples


def _background_section(background: str) -> str:
    brief = (background or "").strip()
    if not brief:
        return ""
    return BACKGROUND_HEADER + "\n" + brief


def _merge_consecutive_turns(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fold runs of messages from the same side into one turn.

    Three messages typed in a row are one thought, not three, and a chat API
    expects the sides to alternate anyway. The caller's list is left as it
    is: describe_style() still needs the per-message rows.
    """
    merged: list[dict[str, str]] = []
    for message in history:
        if merged and merged[-1].get("role") == message.get("role"):
            merged[-1]["content"] = (
                merged[-1].get("content") or ""
            ) + "\n" + (message.get("content") or "")
        else:
            merged.append(copy.copy(message))
    return merged


def _redact(text: str, secret: Optional[str]) -> str:
    """Belt-and-braces: never let the key reach a log line or the admin panel."""
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


def _clip(text: str, limit: int = 300) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _retry_after(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, min(60.0, float(header)))
        except ValueError:
            pass
    return BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))


async def generate_reply(
    *,
    api_key: str,
    history: list[dict[str, str]],
    persona: dict[str, Any],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
    adaptive_style: bool = True,
    general_samples: str = "",
    contact: Optional[dict[str, Any]] = None,
    background: str = "",
    booking_note: str = "",
    media_note: str = "",
) -> str:
    """Return the draft reply text, or raise AIResponderError with a safe message.

    The text may contain BURST_SEPARATOR; callers pass it through split_burst
    before sending. `booking_note` is the state of this chat's appointments
    (see bookings.context_for_reply) — it goes last so it is the freshest
    thing in the prompt. `media_note` lists the files the reply may attach
    (see media.prompt_section); callers take the tags back out with
    media.split_attachments.
    """
    if not api_key:
        raise AIResponderError("DEEPSEEK_API_KEY is not set.")

    sections = [build_system_prompt(persona)]
    samples = _samples_section(general_samples)
    if samples:
        sections.append(samples)
    sections.extend(_contact_sections(contact))
    if adaptive_style:
        style = describe_style(history, language_locked=language_is_pinned(persona))
        if style:
            sections.append(style)
    brief = _background_section(background)
    if brief:
        sections.append(brief)
    if (booking_note or "").strip():
        sections.append(booking_note.strip())
    if (media_note or "").strip():
        sections.append(media_note.strip())
    sections.append(BURST_OUTPUT_NOTE)

    messages = [{"role": "system", "content": "\n\n".join(sections)}]
    messages.extend(_merge_consecutive_turns(history))
    if len(messages) == 1:
        raise AIResponderError("No conversation history to reply to.")

    return await _complete(
        api_key=api_key, messages=messages, ai_config=ai_config, client=client
    )


async def generate_opener(
    *,
    api_key: str,
    goal: str,
    recipient_name: str,
    persona: dict[str, Any],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
    general_samples: str = "",
    contact: Optional[dict[str, Any]] = None,
    background: str = "",
) -> str:
    """Draft the first message of a conversation, given what it should achieve.

    An opener is always a single message: a cold first contact arriving as
    three bubbles reads as a bot. The burst note is left out, and any
    separator the model produces anyway is folded away.
    """
    if not (goal or "").strip():
        raise AIResponderError("No goal given for the outreach message.")

    sections = [build_system_prompt(persona)]
    sections.append(
        f"You are writing the FIRST message to {recipient_name}, someone in this "
        "person's own contacts. Keep it short, personal and natural — the way you "
        "would message someone you know, not a marketing blast. Do not invent facts "
        "about them, and do not pretend a previous conversation happened. Send it "
        "as one single message."
    )
    samples = _samples_section(general_samples)
    if samples:
        sections.append(samples)
    sections.extend(_contact_sections(contact))
    brief = _background_section(background)
    if brief:
        sections.append(brief)

    messages = [
        {"role": "system", "content": "\n\n".join(sections)},
        {"role": "user", "content": f"Write that message. Its purpose: {goal}"},
    ]
    text = await _complete(
        api_key=api_key, messages=messages, ai_config=ai_config, client=client
    )
    return " ".join(split_burst(text))


SUMMARY_SYSTEM_PROMPT = (
    "You are condensing one private Telegram conversation into a short brief, "
    "so that the same person can be answered well in a different chat. Write "
    "compact plain-text bullet lines, no more than six, no more than about 120 "
    "words in total, no heading and no preamble. Record only what would still "
    "matter later: who this person is and how they are known, facts they "
    "stated about themselves or their situation, anything either side asked "
    "for, agreed to or promised, questions left unanswered, and the state the "
    "conversation was left in. Do not include small talk, do not guess at "
    "anything that was not said, and do not give advice. In the brief, call "
    "the two sides 'they' and 'I' — 'I' being the account owner. If the "
    "conversation holds nothing worth carrying over, reply with exactly: NONE"
)

# The model is told to answer NONE for a chat with nothing in it; an empty
# brief is better than a paragraph of invented significance about "hi".
_NO_SUMMARY = "NONE"


def render_transcript(history: list[dict[str, str]]) -> str:
    """History as a plain 'them:/me:' script, which summarises better than JSON."""
    lines = []
    for message in history:
        speaker = "them" if message.get("role") == "user" else "me"
        text = " ".join((message.get("content") or "").split())
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


async def summarize_conversation(
    *,
    api_key: str,
    history: list[dict[str, str]],
    subject_name: str = "",
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Condense a chat into a brief another chat can be answered with.

    Returns "" when there is nothing worth carrying over, so callers can treat
    a thin conversation as no context rather than as context that says nothing.
    """
    transcript = render_transcript(history)
    if not transcript.strip():
        return ""

    who = f" The other person is {subject_name}." if (subject_name or "").strip() else ""
    prompt = (
        "Summarise this conversation as briefed above." + who
        + "\n\n" + transcript
    )
    # Deliberately colder and shorter than a reply: this is note-taking, and a
    # creative summary is a summary with things in it that were never said.
    summary_config = {
        **ai_config,
        "max_tokens": min(int(ai_config.get("max_tokens", 400) or 400), 400),
        "temperature": 0.2,
    }
    text = await _complete(
        api_key=api_key,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        ai_config=summary_config,
        client=client,
    )
    if text.strip().upper().strip(".") == _NO_SUMMARY:
        return ""
    return text.strip()


async def extract_booking(
    *,
    api_key: str,
    history: list[dict[str, str]],
    tz_name: str,
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict[str, Any]]:
    """Whether the client has settled on a specific appointment time.

    Returns the parsed JSON ({"booked", "start", ...}) when they have, else
    None. Like summarising, this is extraction, not writing: cold and short.
    """
    import bookings

    transcript = render_transcript(history)
    if not transcript.strip():
        return None
    extract_config = {
        **ai_config,
        "max_tokens": 200,
        "temperature": 0.0,
    }
    text = await _complete(
        api_key=api_key,
        messages=[
            {"role": "system", "content": bookings.EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": bookings.extraction_prompt(transcript, tz_name)},
        ],
        ai_config=extract_config,
        client=client,
    )
    return bookings.parse_extraction(text)


async def extract_arrival(
    *,
    api_key: str,
    history: list[dict[str, str]],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Whether the client's latest message says they are at the meeting place."""
    import bookings

    transcript = render_transcript(history[-6:])
    if not transcript.strip():
        return False
    text = await _complete(
        api_key=api_key,
        messages=[
            {"role": "system", "content": bookings.ARRIVAL_SYSTEM_PROMPT},
            {"role": "user", "content": "Conversation, oldest first:\n\n" + transcript},
        ],
        ai_config={**ai_config, "max_tokens": 30, "temperature": 0.0},
        client=client,
    )
    return bookings.parse_arrival(text)


async def _complete(
    *,
    api_key: str,
    messages: list[dict[str, str]],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """One DeepSeek chat completion, with retry/backoff and safe error text."""
    if not api_key:
        raise AIResponderError("DEEPSEEK_API_KEY is not set.")

    payload = {
        "model": ai_config.get("model") or "deepseek-chat",
        "messages": messages,
        "max_tokens": ai_config.get("max_tokens", 400),
        "temperature": ai_config.get("temperature", 1.0),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        last_error = "DeepSeek request failed."
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            try:
                response = await http.post(API_URL, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = "DeepSeek API timed out."
            except httpx.HTTPError as exc:
                last_error = _redact(
                    f"Could not reach the DeepSeek API: {type(exc).__name__}.", api_key
                )
            else:
                if response.status_code == 200:
                    return _parse_reply(response, api_key)

                detail = _clip(_redact(response.text, api_key))
                if response.status_code == 429:
                    last_error = f"DeepSeek rate limit (429). {detail}"
                    delay = _retry_after(response, attempt)
                elif response.status_code in (401, 403):
                    # Not retryable — a bad key will not fix itself.
                    raise AIResponderError(
                        f"DeepSeek rejected the API key (HTTP {response.status_code}). "
                        "Check that DEEPSEEK_API_KEY is correct."
                    )
                elif response.status_code >= 500:
                    last_error = f"DeepSeek server error (HTTP {response.status_code}). {detail}"
                    delay = _retry_after(response, attempt)
                else:
                    raise AIResponderError(
                        f"DeepSeek API error (HTTP {response.status_code}). {detail}"
                    )

            if attempt < MAX_ATTEMPTS:
                log.warning(
                    "DeepSeek attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise AIResponderError(f"{last_error} Gave up after {MAX_ATTEMPTS} attempts.")
    finally:
        if owns_client:
            await http.aclose()


def _parse_reply(response: httpx.Response, api_key: str) -> str:
    try:
        data = response.json()
    except ValueError:
        raise AIResponderError("DeepSeek returned a response that was not JSON.") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIResponderError(
            "DeepSeek response was missing choices[0].message.content."
        ) from None
    if not isinstance(content, str) or not content.strip():
        raise AIResponderError("DeepSeek returned an empty reply.")
    return content.strip()
