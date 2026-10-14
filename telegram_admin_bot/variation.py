"""Small differences between the bot's replies (config `variation`).

The prompt spells out its greeting and its first question, so every new
client gets nearly the same words, the same emoji and the same bubbles.
WhatsApp flags an account that sends many people identical messages. This
module changes the bot's own lines a little, differently every time: an
emoji left out or swapped for one of its kind, a phrase swapped for one the
operator listed as meaning the same, a lower-case start, a dropped full stop,
two short messages sent as one.

The one-piece messages are never touched: anything between [block] markers
(the rates, the extras, the address the code fills in) and the prompt's own
blocks even when a reply forgets the markers. Those go out exactly as the
business wrote them. Nothing here touches a digit either: prices, times and
numbers stay as the model wrote them (a phrase group must keep the same
numbers in every phrase).

Pure functions, like humanlike.py: the random source can be passed in, so
tests can pin it. Nothing here asks the LLM anything.
"""

from __future__ import annotations

import random
import re
from typing import Any, Optional

import ai_responder

# One emoji: a flag (two regional letters), or a symbol with its optional
# presentation selector (U+FE0F) and skin tone, and any parts joined to it
# by a zero-width joiner (U+200D: the laptop in a technologist, the black in
# a black cat). Skin tones (U+1F3FB-1F3FF) only ever follow a symbol, and
# the dingbat digits (U+2776-2793) are numbers, not emoji. The patterns are
# raw strings: `re` reads the \u and \U escapes itself.
_TONE = r"[\U0001F3FB-\U0001F3FF]?"
_SYMBOL = r"[\U0001F300-\U0001F3FA\U0001F400-\U0001FAFF\u2600-\u2775\u2794-\u27BF\u2B50\u2B55]"
_PART = _SYMBOL + r"\uFE0F?" + _TONE
_JOINED = r"\u200D\S\uFE0F?" + _TONE
EMOJI = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}|" + _PART + "(?:" + _JOINED + ")*")
_SELECTOR = re.compile(r"\uFE0F")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?])")
_DIGITS = re.compile(r"\d+")
END_PUNCTUATION = ".!?"
PHRASE_SEPARATOR = "|"
# Two messages only go out as one while together they are still a short line.
MERGE_MAX_CHARS = 90


def _base(emoji: str) -> str:
    """An emoji as compared with a family: without the presentation selector
    (the heart with and without it is the same heart)."""
    return _SELECTOR.sub("", emoji)


def emoji_groups(lines: list[str]) -> list[list[str]]:
    """variation.emoji_families, one family a line."""
    groups = [EMOJI.findall(line) for line in lines]
    return [group for group in groups if len(group) > 1]


def phrase_groups(lines: list[str]) -> list[list[str]]:
    """variation.phrases, one group a line, the phrases apart by "|". A
    line phrase_group_problem() refuses is left out."""
    groups = []
    for line in lines:
        if not phrase_group_problem(line):
            groups.append([p.strip() for p in line.split(PHRASE_SEPARATOR) if p.strip()])
    return groups


def phrase_group_problem(line: str) -> str:
    """Why a variation.phrases line cannot be used, or ""."""
    group = [p.strip() for p in line.split(PHRASE_SEPARATOR) if p.strip()]
    if len(group) < 2:
        return "needs two or more phrases apart by |"
    if len({tuple(_DIGITS.findall(p)) for p in group}) > 1:
        return "every phrase must have the same numbers (a swap never changes a price or a time)"
    return ""


def emoji_family_problem(line: str) -> str:
    """Why a variation.emoji_families line cannot be used, or ""."""
    if len(EMOJI.findall(line)) < 2:
        return "needs two or more emojis"
    return ""


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """The phrase anywhere, any case, but not inside a longer word ("hi" is
    not the start of "his")."""
    start = r"(?<!\w)" if phrase[:1].isalnum() else ""
    end = r"(?!\w)" if phrase[-1:].isalnum() else ""
    return re.compile(start + re.escape(phrase) + end, re.IGNORECASE)


def _keeps_case(text: str) -> bool:
    """Starts with "I" ("I'm", "I booked") or a word in capitals ("OK"):
    those keep their capitals wherever they go."""
    word = text.split(" ", 1)[0]
    letters = [c for c in word if c.isalpha()]
    if not letters:
        return False
    if word[:1] == "I" and not word[1:2].isalpha():
        return True
    return len(letters) > 1 and all(c.isupper() for c in letters)


def _fit_case(new: str, old: str, starts_sentence: bool) -> str:
    """`new` in place of `old`: a small letter where `old` had one, a
    capital where `old` started a sentence with one, else as the group
    writes it ("Yes, I booked you" -> "Yes, it's booked")."""
    if _keeps_case(new):
        return new
    if old[:1].islower():
        return new[:1].lower() + new[1:]
    if old[:1].isupper() and starts_sentence:
        return new[:1].upper() + new[1:]
    return new


def _starts_sentence(text: str, at: int) -> bool:
    before = EMOJI.sub("", text[:at]).rstrip()
    return not before or before[-1] in END_PUNCTUATION


def swap_phrases(text: str, groups: list[list[str]], rng: random.Random) -> str:
    """Each group found in the message is replaced by one of its phrases at
    random (possibly the same). Longest phrases are looked for first, so
    "hi my love" wins over "hi"; a stretch already swapped is left alone."""
    taken: list[tuple[int, int]] = []
    swaps: list[tuple[int, int, str]] = []
    for group in groups:
        for phrase in sorted(group, key=len, reverse=True):
            for match in _phrase_pattern(phrase).finditer(text):
                if any(match.start() < end and start < match.end() for start, end in taken):
                    continue
                taken.append((match.start(), match.end()))
                new = _fit_case(rng.choice(group), match.group(0), _starts_sentence(text, match.start()))
                swaps.append((match.start(), match.end(), new))
                break
            else:
                continue
            break
    for start, end, new in sorted(swaps, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def vary_emojis(text: str, families: list[list[str]], drop: float, swap: float, rng: random.Random) -> str:
    """Per message: with chance `drop` its emojis are left out, with chance
    `swap` each one is swapped for another of its family, else they stay.
    A message that is nothing but emoji is never emptied: where it would
    lose them, it gets others of their family instead."""
    if not EMOJI.search(text):
        return text
    roll = rng.random()
    if roll < drop and EMOJI.sub("", text).strip():
        return _tidy(EMOJI.sub("", text))
    if roll < drop + swap:
        family = {_base(e): group for group in families for e in group}

        def other(match: re.Match[str]) -> str:
            group = family.get(_base(match.group(0)))
            if not group:
                return match.group(0)
            choices = [e for e in group if _base(e) != _base(match.group(0))]
            return rng.choice(choices) if choices else match.group(0)

        return EMOJI.sub(other, text)
    return text


def _tidy(text: str) -> str:
    return _SPACE_BEFORE_PUNCT.sub(r"\1", _SPACES.sub(" ", text)).strip()


def lower_start(text: str) -> str:
    """"Hi love" -> "hi love". A first word that is not a plain word ("OK",
    "iPhone", a code like "TQn9Y2") keeps its letters as they are."""
    word = text.split(" ", 1)[0]
    letters = [c for c in word if c.isalpha()]
    if not letters or not letters[0].isupper() or any(c.isdigit() for c in word):
        return text
    if any(c.isupper() for c in letters[1:]):
        return text
    i = text.index(letters[0])
    return text[:i] + text[i].lower() + text[i + 1:]


def _words_end(text: str) -> int:
    """Where the words of a message end: before the emoji and spaces that
    close it."""
    end = len(text.rstrip())
    starts = {m.end(): m.start() for m in EMOJI.finditer(text)}
    while end in starts:
        end = len(text[:starts[end]].rstrip())
    return end


def drop_end_punctuation(text: str) -> str:
    """"what time you come? 😘" -> "what time you come 😘". "...", "?!"
    and a message that is only "?" stay: they say something."""
    end = _words_end(text)
    if end < 2 or text[end - 1] not in END_PUNCTUATION or text[end - 2] in END_PUNCTUATION:
        return text
    if not any(c.isalnum() for c in text[:end - 1]):
        return text
    return _tidy(text[:end - 1] + text[end:])


def _merge(first: str, second: str) -> str:
    joiner = ", " if first[-1:].isalnum() else " "
    return first + joiner + second


def vary_pieces(
    pieces: list[tuple[str, bool]],
    variation: dict[str, Any],
    rng: Optional[random.Random] = None,
) -> list[tuple[str, bool]]:
    """ai_responder.reply_pieces with the bot's own lines varied; the
    one-piece messages (True) come back exactly as they were."""
    rng = rng or random  # type: ignore[assignment]
    families = emoji_groups(variation.get("emoji_families") or [])
    phrases = phrase_groups(variation.get("phrases") or [])
    out: list[tuple[str, bool]] = []
    for text, is_block in pieces:
        if is_block:
            out.append((text, True))
            continue
        text = swap_phrases(text, phrases, rng)
        text = vary_emojis(text, families, variation["emoji_drop"], variation["emoji_swap"], rng)
        if rng.random() < variation["lowercase_start"]:
            text = lower_start(text)
        if rng.random() < variation["drop_end_punctuation"]:
            text = drop_end_punctuation(text)
        if not text.strip():
            continue
        if (out and not out[-1][1] and len(out[-1][0]) + len(text) + 2 <= MERGE_MAX_CHARS
                and rng.random() < variation["merge_messages"]):
            out[-1] = (_merge(out[-1][0], text), False)
            continue
        out.append((text, False))
    return out


def vary_reply(
    text: str,
    variation: dict[str, Any],
    fixed: tuple[str, ...] = (),
    rng: Optional[random.Random] = None,
) -> str:
    """The reply text with its own lines varied (variation.enabled), ready
    for split_burst as before. `fixed` are the prompt's blocks, one-line
    ones too (ai_responder.fixed_blocks(prompt, min_lines=1)), kept even
    where the reply left out the markers. Off, the text comes back
    unchanged."""
    if not variation.get("enabled") or not (text or "").strip():
        return text
    pieces = ai_responder.reply_pieces(text, fixed)
    return ai_responder.join_pieces(vary_pieces(pieces, variation, rng))
