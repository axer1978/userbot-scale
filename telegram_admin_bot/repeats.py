"""The bot does not say the same thing over and over.

Two things, both decided in code:

- A client who sends the same message again and again (a time-waster in a
  loop, a scraper, someone copying the bot's own text back): once that
  message has been answered `replies.max_same_answers` times in the last
  day, it gets no reply any more. Anything new he writes is answered as
  usual. Never caught: a message that names a day or a time, says he is on
  his way or has arrived, or asks where she is; a photo; a one-word message
  ("yes", "ok", "hi"); and the account's acknowledgements.
- The bot's own lines (`variation.avoid_repeats`): a line it already sent in
  this chat, or sent to `variation.repeat_chats` other chats in the last
  `variation.repeat_days` days, is written once more in other words.
  WhatsApp flags an account that sends many people the same text, and the
  same question asked twice in the same words reads as a bot. The lines
  most used lately are also shown to the model before it writes, so most
  replies come out different the first time.

The one-piece messages never count: anything between [block] markers (the
rates, the extras, the address the code fills in) and the prompt's own
blocks go out word for word by design. Nor do lines of fewer than MIN_WORDS
words ("ok my love", "here love 😘"): a person says those all the time. A
line from another chat with a number in it is never shown to the model (a
price, a time, or the address or way in typed out), and a line holding one
of the operator's fallback phrases is never reworded (Unanswered finds them
by their words).

Lines are compared as keys: lower case, emoji and punctuation left out,
spaces collapsed; digits stay, so "at 22" and "at 23" are never the same.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

import asyncpg

import ai_limits
import ai_responder
import bookings
import examples
import media

# A client's message counts as answered again within this window. A regular
# who says "hi" once a week is never caught by it.
SAME_ASK_WINDOW = timedelta(hours=24)
# A client's message counts from this many words: "yes", "ok", "hi" answer
# whatever was asked and are never the same question again.
MIN_ASK_WORDS = 2
# Two keys this alike (difflib ratio), with the same digits, are the same
# line: "can i see more pics" and "can i see more pics pls". Only for keys
# this long; shorter ones must be equal.
SIMILAR_RATIO = 0.85
SIMILAR_MIN_CHARS = 12
# A bot line counts from this many words: shorter ones ("ok my love",
# "here love") are everyday words.
MIN_WORDS = 4
# The account's lines sent elsewhere, at most this many rows read.
SENT_ROWS = 3000
# At most this many overused lines go to the model before it writes.
SHOWN_LINES = 15
# A line shown to the model is cut to this many characters.
SHOWN_CHARS = 160

_APOSTROPHE = re.compile(r"['’]")
_NOT_A_WORD = re.compile(r"[\W_]+")
_DIGITS = re.compile(r"\d+")
_TAG = re.compile(r"\[(?:address|send \d+|way in)\]", re.IGNORECASE)
# A day or a part of the day, besides the clock times examples.names_a_time
# finds: "now", "tonight", "tomorrow evening", "after 22", "22h", "in an
# hour" (EN, RU, LV, ET).
_WHEN = re.compile(
    r"(?i)\b(?:now|right now|rn|asap|atm|at the moment|right away|straight away|immediately|today|2day|tonight|tonite"
    r"|2nite|tomorrow|tmrw|tmr|tomoro|2moro|2morrow|morning|afternoon|evening|night"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|later today"
    r"|in an hour|in half an hour|half an hour|an hour from now|in \d+|after \d+|before \d+|\d{1,2}\s?h(?:\d{2})?"
    r"|\d{1,2}\s?ish|(?:[01]\d|2[0-3])[0-5]\d"
    # Russian.
    r"|сейчас|щас|ща|сегодня|завтра|послезавтра|вечером|утром|днём|днем|ночью|через \w+"
    r"|понедельник\w*|вторник\w*|сред\w|четверг\w*|пятниц\w|суббот\w|воскресень\w+|seichas|sejchas|cherez \w+"
    # Latvian, also without diacritics.
    r"|šodien|sodien|šovakar|sovakar|rīt|rit|parīt|parit|tagad|tūlīt|tulit|šobrīd|sobrid|pašlaik|paslaik|vakarā|vakara"
    r"|pirmdien|otrdien|trešdien|tresdien|ceturtdien|piektdien|sestdien|svētdien|svetdien|pēc \w+|pec \w+"
    r"|pus\w+os|\d{1,2}os"
    # Estonian, also without diacritics.
    r"|täna|tana|homme|ülehomme|ulehomme|praegu|kohe|nüüd|nuud|õhtul|ohtul|hommikul|pärast|parast"
    r"|esmaspäev\w*|teisipäev\w*|kolmapäev\w*|neljapäev\w*|reede\w*|laupäev\w*|pühapäev\w*|kell \d+|pool \w+"
    # Spanish.
    r"|ahora|ahorita|hoy|mañana|manana|esta noche|esta tarde|a las \d+|en \d+|en una hora|en media hora"
    r"|lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\b"
    # The whole message is an hour: "8", "20?", "20.30".
    r"|^\W*\d{1,2}(?:[:. ]\d{2})?\W*$",
    re.MULTILINE,
)
# On his way or there: "I'm here", "outside", "omw", "leaving now".
_COMING = re.compile(
    r"(?i)\b(?:here|outside|downstairs|arrived|arriving|at the door|at the gate|in front|on my way|omw|otw|coming|leaving"
    r"|parked|parking|nearby|close by|around the corner|almost there|5 min away"
    r"|я тут|я здесь|на месте|подъехал|подъезжаю|подошёл|подошел|еду|иду|выехал|выезжаю|рядом|внизу|у подъезда"
    r"|esmu klāt|esmu klat|esmu šeit|esmu seit|esmu lejā|esmu leja|pie mājas|pie majas|pie durvīm|pie durvim"
    r"|braucu|nāku|naku|tuvumā|tuvuma|tūlīt būšu|tulit busu"
    r"|olen siin|olen kohal|olen all|ukse taga|tulen|tulemas|lähedal|lahedal|kohe jõuan|kohe jouan"
    r"|estoy aquí|estoy aqui|estoy abajo|en la puerta|llegando|estoy cerca|voy)\b"
)
# A pet name, left out when lines are grouped: "hi love, what time…" and
# "hi babe, what time…" are one line sent twice.
_PET = frozenset("love babe baby darling dear bby honey hun sweetie sweetheart".split())


def key(text: str) -> str:
    """A message or a line as compared here: lower case, without emoji,
    punctuation or extra spaces ("that's" is one word). Digits stay."""
    return " ".join(_NOT_A_WORD.sub(" ", _APOSTROPHE.sub("", (text or "").lower())).split())


def _group_key(k: str) -> str:
    """A key without its pet names ("my love" too)."""
    words = k.split()
    out = [w for i, w in enumerate(words)
           if w not in _PET and not (w == "my" and i + 1 < len(words) and words[i + 1] in _PET)]
    return " ".join(out) or k


def same(a: str, b: str) -> bool:
    """Whether two keys are the same line: equal, or (both long enough, with
    the same digits) all but equal."""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < SIMILAR_MIN_CHARS or _DIGITS.findall(a) != _DIGITS.findall(b):
        return False
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return matcher.quick_ratio() >= SIMILAR_RATIO and matcher.ratio() >= SIMILAR_RATIO


def _within(a: str, b: str) -> bool:
    """Bot lines only: the same line, or (the shorter of MIN_WORDS words or
    more) nearly all its words are in the other one, its numbers too: a line
    sent merged with another into one message, or the same question with
    the pet name moved ("what time do you want to come love" after "hi love
    what time do you want to come")."""
    if same(a, b):
        return True
    short, long_ = sorted((a.split(), b.split()), key=len)
    if len(short) < MIN_WORDS:
        return False
    words, other = Counter(short), Counter(long_)
    if not Counter(w for w in short if w.isdigit()) <= Counter(w for w in long_ if w.isdigit()):
        return False
    return sum((words & other).values()) / len(short) >= SIMILAR_RATIO


def _holds(k: str, skip_keys: Sequence[str]) -> bool:
    """The line is part of, or holds, one of `skip_keys`."""
    return any(_within(k, s) or f" {s} " in f" {k} " for s in skip_keys)


def _words(text: str) -> bool:
    """Something to compare: words, not a photo or a sticker."""
    return bool(key(text)) and not examples.is_media(text)


# ---------------------------------------------------------------- his side

_Turn = tuple[list[str], Optional[datetime]]


def _turns(history: Sequence[dict[str, Any]], sent_at: Sequence[Optional[datetime]]) -> list[_Turn]:
    """Their messages in runs (one turn = their messages between two of
    ours), each with when the run started; the last run is empty when we
    wrote last. Every run but the last was answered: a message of ours
    follows it."""
    times = list(sent_at) if len(sent_at) == len(history) else [None] * len(history)
    turns: list[_Turn] = []
    open_run = False
    for message, at in zip(history, times):
        if message.get("role") != "user":
            open_run = False
            continue
        if not open_run:
            turns.append(([], at))
            open_run = True
        turns[-1][0].append(message.get("content") or "")
    if history and history[-1].get("role") != "user":
        turns.append(([], None))
    return turns


def latest_from_them(history: Sequence[dict[str, Any]]) -> list[str]:
    """Their messages since our last one."""
    out: list[str] = []
    for message in reversed(history):
        if message.get("role") != "user":
            break
        out.append(message.get("content") or "")
    return out[::-1]


def names_when(text: str) -> bool:
    """The message names a time or a day ("at 22", "22:30", "tonight",
    "tomorrow evening", "now", "in 20 min")."""
    return examples.names_a_time(text) or bool(_WHEN.search(text or ""))


def names_when_now(history: Sequence[dict[str, Any]]) -> bool:
    """One of their messages since our last one names a time or a day: such
    a message always gets its answer."""
    return any(names_when(text) for text in latest_from_them(history))


def _never_a_repeat(text: str, acknowledgements: Sequence[str]) -> bool:
    """A message that is answered however often it comes: a photo, a time
    or a day, on his way or there, where she is, one word, an
    acknowledgement."""
    return (not _words(text) or names_when(text) or bool(_COMING.search(text)) or bookings.asks_where(text)
            or len(key(text).split()) < MIN_ASK_WORDS
            or (bool(acknowledgements) and ai_limits.is_acknowledgement(text, acknowledgements)))


def repeated_ask(history: Sequence[dict[str, Any]], sent_at: Sequence[Optional[datetime]],
                 now: datetime, max_answers: int, acknowledgements: Sequence[str] = ()) -> int:
    """How many times their latest message was already answered, when every
    message they sent since our last one is one they already sent in at
    least `max_answers` earlier turns of the last day (each of which got an
    answer); else 0. Any message of theirs since our last one that is never
    a repeat (_never_a_repeat: a photo, a time or a day, "I'm here", "where
    are you", one word) makes it 0. `sent_at` says when each history message
    was stored (when it doesn't match the history, the whole history
    counts)."""
    if max_answers <= 0:
        return 0
    turns = _turns(history, sent_at)
    if not turns or not turns[-1][0]:
        return 0
    latest = turns[-1][0]
    if any(_never_a_repeat(text, acknowledgements) for text in latest):
        return 0
    since = now - SAME_ASK_WINDOW
    earlier = [[key(text) for text in texts if _words(text)]
               for texts, at in turns[:-1] if at is None or at >= since]
    least = 0
    for line in (key(text) for text in latest):
        answered = sum(1 for others in earlier if any(same(line, other) for other in others))
        if answered < max_answers:
            return 0
        least = answered if not least else min(least, answered)
    return least


# ---------------------------------------------------------------- our side


def own_lines(text: str, fixed: tuple[str, ...] = ()) -> list[str]:
    """The reply's own lines, one per message they go out as: not the
    [block] messages or the prompt's `fixed` blocks, without media and
    address tags, and only lines of MIN_WORDS words or more."""
    text, _ = media.split_attachments(ai_responder.without_no_reply(text or ""))
    out: list[str] = []
    # Outside the blocks, every piece is one line: one message.
    for piece, is_block in ai_responder.reply_pieces(text, fixed):
        piece = _TAG.sub(" ", piece).strip()
        if not is_block and len(key(piece).split()) >= MIN_WORDS:
            out.append(piece)
    return out


def said_in_chat(history: Sequence[dict[str, Any]]) -> list[str]:
    """The keys of every line of ours in the history (one message may hold
    several lines), of MIN_WORDS words or more."""
    keys: list[str] = []
    for message in history:
        if message.get("role") != "assistant":
            continue
        for line in (message.get("content") or "").splitlines():
            if _words(line) and len(key(line).split()) >= MIN_WORDS:
                keys.append(key(line))
    return keys


def overused(rows: Iterable[tuple[int, str]], min_chats: int,
             skip: Sequence[str] = ()) -> list[tuple[str, int]]:
    """The lines sent to at least `min_chats` different chats, as (the way
    it was written most often, how many chats), most chats first. `rows`
    are (chat_id, message text). Lines that differ only in the pet name are
    one line. Left out: a message of several lines (a [block]: the rates, a
    menu), media, a line with a number in it (a price or a time is never the
    same twice anyway, and the address or way in typed out must never reach
    another chat's prompt), and any line that is part of, or holds, one of
    `skip` (the prompt's blocks, the address, the way in, fallback
    phrases)."""
    skip_keys = [k for k in (key(s) for s in skip) if k]
    groups: dict[str, dict[str, Any]] = {}
    for chat_id, text in rows:
        text = (text or "").strip()
        if not text or "\n" in text or not _words(text) or _DIGITS.search(text):
            continue
        k = key(text)
        if len(k.split()) < MIN_WORDS or _holds(k, skip_keys):
            continue
        group = groups.setdefault(_group_key(k), {"chats": set(), "texts": {}})
        group["chats"].add(chat_id)
        group["texts"][text] = group["texts"].get(text, 0) + 1
    found = [(max(g["texts"].items(), key=lambda item: item[1])[0], len(g["chats"]))
             for g in groups.values() if len(g["chats"]) >= max(1, min_chats)]
    return sorted(found, key=lambda item: (-item[1], item[0]))


def repeated_lines(text: str, *, chat_keys: Sequence[str], used_elsewhere: Sequence[str],
                   fixed: tuple[str, ...] = (), skip: Sequence[str] = ()) -> list[str]:
    """The reply's own lines that were already sent in this chat
    (`chat_keys`, from said_in_chat) or are overused with other clients
    (`used_elsewhere`, texts from overused()). A line holding one of `skip`
    (the operator's fallback phrases) is never one."""
    elsewhere = [key(t) for t in used_elsewhere]
    skip_keys = [k for k in (key(s) for s in skip) if k]
    out: list[str] = []
    for line in own_lines(text, fixed):
        k = key(line)
        if skip_keys and _holds(k, skip_keys):
            continue
        if any(_within(k, other) for other in chat_keys) or any(_within(k, other) for other in elsewhere):
            out.append(line)
    return out


def shown(lines: Sequence[str], limit: int = SHOWN_LINES) -> list[str]:
    """Lines for the model's note: de-duplicated, at most `limit`, each cut
    to SHOWN_CHARS."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        k = key(line)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(line if len(line) <= SHOWN_CHARS else line[:SHOWN_CHARS - 1] + "…")
        if len(out) >= limit:
            break
    return out


async def sent_elsewhere(pool: asyncpg.Pool, tenant_id: int, chat_id: int, days: int) -> list[tuple[int, str]]:
    """(chat_id, text) of the messages the bot wrote and sent in the
    account's other chats in the last `days` days, newest first. Messages
    typed by hand are hers to word as she likes and are not counted."""
    rows = await pool.fetch(
        "SELECT chat_id, text FROM messages WHERE tenant_id = $1 AND direction = 'out' AND status = 'sent' "
        "AND llm_model IS NOT NULL AND chat_id <> $2 AND created_at >= now() - make_interval(days => $3) "
        "ORDER BY id DESC LIMIT $4",
        tenant_id, chat_id, days, SENT_ROWS,
    )
    return [(row["chat_id"], row["text"] or "") for row in rows]
