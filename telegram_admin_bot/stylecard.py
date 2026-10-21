"""How the business types, measured from the replies a person typed by hand,
and given to the model as a short note (config style_card).

The prompt's tone section and writing samples say how the business sounds;
this says how it types, in numbers: how long its messages are, how many it
sends in a row, capitals, end punctuation, emojis, and how it opens a reply.
The writing samples are words to imitate; these are habits, so the model
writes fresh lines that look typed by the same person, and never copies
one (the same text to many clients gets a number flagged).

Typed by hand is the rule of stats._SENT and the hand-typed finetune runs:
an outgoing message the model did not write (no llm_model) that neither the
bot nor the system sent (no message_sent audit row with that actor). From
every account of the industry (scope "industry": one shared voice, as the
escort accounts share one tuning) or this account only, over the last
`days`. Photos and other media, and long block-like messages (the rates
list), say nothing about typing and are left out.

Openers are the first one or two words of a reply turn, kept only when used
in at least three different chats, so a client's name never gets in.

The runtime asks for the note on every bind (session_runtime.bind_tenant),
so the query reads at most MAX_MESSAGES of the newest typed replies.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import audit
import variation

HEADER = ("HOW SHE TYPES (measured from {n} of her own replies typed by hand; match these habits, "
          "never copy whole lines):")
MAX_NOTE_CHARS = 600
# The newest typed replies measured: plenty for a median, and it keeps the
# query small enough to run on every reload.
MAX_MESSAGES = 3000
MIN_OPENER_CHATS = 3
MAX_OPENERS = 5
MAX_EMOJIS = 5
# A message longer than this, or with more lines, is a block (a rates list,
# a menu), not typing.
BLOCK_CHARS = 300
BLOCK_LINES = 3

_MACHINE_ACTORS = [audit.BOT, audit.SYSTEM]
# What the runtime stores for media: "[non-text message]" from the phone,
# "[sent photo #3: ...]" from the library, "[photo] ..." described, and a
# deleted message's placeholder.
_PLACEHOLDER = re.compile(r"^\s*\[(?:non-text message|sent\b|photo\]|deleted\b|voice|video|sticker|file)",
                          re.IGNORECASE)
_END_PUNCTUATION = ".!?…"
_TOKEN_EDGES = ".,!?;:…\"'«»“”„()[]*_~-–—"

# The typed replies, newest first, each with the message before it in its
# chat (client or business), which says whether it starts a reply turn.
_TYPED = f"""
WITH machine AS (
  SELECT DISTINCT payload->>'message_id' AS id FROM audit_log
   WHERE tenant_id = ANY($1::int[]) AND event = '{audit.MESSAGE_SENT}' AND actor = ANY($3::text[])
     AND created_at >= $2
), typed AS (
  SELECT m.tenant_id, m.chat_id, m.id, m.text
    FROM messages m LEFT JOIN machine ON machine.id = m.id::text
   WHERE m.tenant_id = ANY($1::int[]) AND m.created_at >= $2
     AND m.direction = 'out' AND m.status = 'sent' AND m.llm_model IS NULL AND machine.id IS NULL
   ORDER BY m.created_at DESC LIMIT $4
)
SELECT t.tenant_id, t.chat_id, t.id, t.text, prev.id AS prev_id
  FROM typed t
  LEFT JOIN LATERAL (
    SELECT p.id FROM messages p
     WHERE p.tenant_id = t.tenant_id AND p.chat_id = t.chat_id AND p.id < t.id
       AND ((p.direction = 'out' AND p.status = 'sent') OR (p.direction = 'in' AND p.status = 'received'))
     ORDER BY p.id DESC LIMIT 1
  ) prev ON true
"""


def is_typing(text: str) -> bool:
    """A message that shows how she types: text, not a media placeholder,
    not a block."""
    text = (text or "").strip()
    if not text or _PLACEHOLDER.match(text) or "[block]" in text.lower():
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return len(text) <= BLOCK_CHARS and len(lines) <= BLOCK_LINES


def _words(text: str) -> list[str]:
    """Words as she typed them: emojis out, a token with a letter or digit."""
    return [t for t in variation.EMOJI.sub(" ", text).split() if any(c.isalnum() for c in t)]


def _starts_lower(text: str) -> Optional[bool]:
    first = next((c for c in variation.EMOJI.sub("", text) if c.isalpha()), None)
    return None if first is None else first.islower()


def _ends_bare(text: str) -> Optional[bool]:
    """No . ! ? or … after the last word (emojis after it do not count)."""
    words = variation.EMOJI.sub("", text).rstrip()
    if not any(c.isalnum() for c in words):
        return None
    return words[-1] not in _END_PUNCTUATION


def _opener(text: str) -> list[str]:
    """The first word and the first two words, lower-case, without emojis
    and punctuation; none when the reply opens with a number or a handle."""
    tokens = [t.strip(_TOKEN_EDGES) for t in variation.EMOJI.sub(" ", text.lower()).split()]
    tokens = [t for t in tokens if t]
    if not tokens or any(c.isdigit() or c == "@" for c in tokens[0]):
        return []
    out = [tokens[0]]
    if len(tokens) > 1 and not any(c.isdigit() or c == "@" for c in tokens[1]):
        out.append(f"{tokens[0]} {tokens[1]}")
    return out


def _median(values: Sequence[float]) -> float:
    return round(float(statistics.median(values)), 1) if values else 0.0


def _share(flags: Sequence[Optional[bool]]) -> float:
    known = [f for f in flags if f is not None]
    return round(sum(known) / len(known), 2) if known else 0.0


def measure(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The numbers, from typed replies: each row has "chat" (any key of its
    chat), "id", "text" and "prev_id", the message before it in the chat.

    A reply turn is the typed messages sent in a row: one starts after a
    client message, a message the bot or the system sent, or the chat's
    start. Media and blocks are left out of every number but do not end a
    turn."""
    by_chat: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: r["id"]):
        by_chat[row["chat"]].append(row)
    # Turn (its first message's id) -> its messages that show typing.
    turns: dict[int, list[dict[str, Any]]] = defaultdict(list)
    turn_chat: dict[int, Any] = {}
    for chat, chat_rows in by_chat.items():
        current, last_id = 0, None
        for row in chat_rows:
            # Right after the previous typed message: the same turn.
            if last_id is None or row.get("prev_id") != last_id:
                current = row["id"]
                turn_chat[current] = chat
            last_id = row["id"]
            if is_typing(row["text"]):
                turns[current].append(row)
    kept = [row for turn in turns.values() for row in turn]
    texts = [row["text"].strip() for row in kept]

    emojis: Counter[str] = Counter()
    for text in texts:
        emojis.update(e.replace("️", "") for e in variation.EMOJI.findall(text))

    opener_chats: dict[str, set[Any]] = defaultdict(set)
    for turn_id, turn in turns.items():
        for opener in _opener(turn[0]["text"]):
            opener_chats[opener].add(turn_chat[turn_id])
    common = {o: len(chats) for o, chats in opener_chats.items() if len(chats) >= MIN_OPENER_CHATS}
    # A first word is worth naming on its own only when it opens more chats
    # than its two-word openers do ("hi" beside "hi love").
    openers = {o: n for o, n in common.items()
               if " " in o or n > max((m for p, m in common.items() if p.startswith(o + " ")), default=0)}

    return {
        "messages": len(kept),
        "turns": len(turns),
        "chats": len({row["chat"] for row in kept}),
        "median_words": _median([len(_words(t)) for t in texts]),
        "median_messages_per_turn": _median([len(turn) for turn in turns.values()]),
        "lower_start_share": _share([_starts_lower(t) for t in texts]),
        "no_end_punctuation_share": _share([_ends_bare(t) for t in texts]),
        "emoji_share": _share([bool(variation.EMOJI.search(t)) for t in texts]),
        "top_emojis": [{"emoji": e, "count": n} for e, n in emojis.most_common(MAX_EMOJIS)],
        "openers": [{"text": o, "chats": n}
                    for o, n in sorted(openers.items(), key=lambda item: (-item[1], item[0]))[:MAX_OPENERS]],
    }


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _percent(share: float) -> str:
    return f"{round(share * 100)}%"


def render(stats: dict[str, Any]) -> str:
    """The note as the model reads it, under MAX_NOTE_CHARS: the openers and
    emojis it lists are cut first."""
    openers = [o["text"] for o in stats["openers"]]
    emojis = [e["emoji"] for e in stats["top_emojis"]]
    while True:
        lines = [
            HEADER.format(n=stats["messages"]),
            f"- about {_number(stats['median_words'])} words a message, "
            f"{_number(stats['median_messages_per_turn'])} message(s) in a row per reply",
            f"- starts with a small letter in {_percent(stats['lower_start_share'])} of messages; "
            f"no . ! or ? at the end in {_percent(stats['no_end_punctuation_share'])}",
        ]
        if stats["emoji_share"]:
            lines.append(f"- an emoji in {_percent(stats['emoji_share'])} of messages"
                         + (", most often " + " ".join(emojis) if emojis else ""))
        else:
            lines.append("- no emojis")
        if openers:
            lines.append("- opens replies in several chats with (vary them): "
                         + ", ".join(f'"{o}"' for o in openers))
        note = "\n".join(lines)
        if len(note) <= MAX_NOTE_CHARS or not (openers or emojis):
            return note[:MAX_NOTE_CHARS]
        if openers:
            openers.pop()
        else:
            emojis.pop()


async def _tenant_ids(pool: Any, tenant_id: int, scope: str) -> list[int]:
    if scope != "industry":
        return [tenant_id]
    rows = await pool.fetch(
        "SELECT id FROM tenants WHERE industry_id = (SELECT industry_id FROM tenants WHERE id = $1) ORDER BY id",
        tenant_id)
    return [r["id"] for r in rows] or [tenant_id]


async def stats_for(pool: Any, tenant_id: int, settings: dict[str, Any],
                    now: Optional[datetime] = None) -> dict[str, Any]:
    """The numbers for this account with its style_card settings."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=int(settings.get("days") or 90))
    ids = await _tenant_ids(pool, tenant_id, settings.get("scope") or "industry")
    rows = await pool.fetch(_TYPED, ids, since, _MACHINE_ACTORS, MAX_MESSAGES)
    return measure([{"chat": (r["tenant_id"], r["chat_id"]), "id": r["id"], "text": r["text"] or "",
                     "prev_id": r["prev_id"]} for r in rows])


async def card(pool: Any, tenant_id: int, config: dict[str, Any],
               now: Optional[datetime] = None) -> dict[str, Any]:
    """The style card for the admin (rules_api): the settings, the numbers,
    and the note as it would go into the prompt, measured even while the
    card is off so it can be previewed. The note is "" below min_replies."""
    settings = config.get("style_card") or {}
    stats = await stats_for(pool, tenant_id, settings, now)
    enough = stats["messages"] >= int(settings.get("min_replies") or 0)
    return {
        "enabled": bool(settings.get("enabled")), "scope": settings.get("scope") or "industry",
        "days": settings.get("days"), "min_replies": settings.get("min_replies"),
        "note": render(stats) if enough and stats["messages"] else "", "stats": stats,
    }


async def note_for(pool: Any, tenant_id: int, config: dict[str, Any]) -> str:
    """The style note for this account's replies, or "" (off, or too few
    replies typed by hand to measure)."""
    settings = config.get("style_card") or {}
    if not settings.get("enabled"):
        return ""
    stats = await stats_for(pool, tenant_id, settings)
    if not stats["messages"] or stats["messages"] < int(settings.get("min_replies") or 0):
        return ""
    return render(stats)
