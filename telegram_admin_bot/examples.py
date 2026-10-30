"""Earlier chat moments like the one being answered (Step 3 of learning from
real chats; config retrieval).

The bot runs on DeepSeek's API, so it cannot be trained on the business's
chats; it gets better through what its prompt shows it. This module keeps
real pairs of a client's message and the business's reply, and for every
new message puts the closest few into the prompt as examples of how the
business handles a moment like it: what she answers, how short, in what
tone.

What is kept (migration 0018, reply_examples). A turn of a chat is the
client's messages in a row and the business's sent messages right after
them. A turn is kept when its reply was

- typed by a person, every message of it (source hand): on the phone or by
  hand in the panel, the same rule as stats._SENT;
- the bot's draft, changed by an operator before approving it (edited):
  messages.draft_text is set; the reply is what was sent;
- in a chat the admin flagged good (flagged), up to where it was flagged:
  there the bot's own replies count, since a person judged them.

Never from a chat flagged bad, and never a plain bot reply otherwise:
learning from itself would only entrench its own mistakes. Both sides are
masked when stored (finetune.mask: the client's name and handle, numbers,
e-mails, the business's address and way in). The business's side is always
what was really sent; it is never generated.

Fixed rules, so an example never teaches the wrong thing. A turn is left
out when a time the client asked for was waiting for the owner (the bot
says nothing then; a hand-typed "yes come at 23" is the owner deciding, and
the bot must never learn to confirm), when the reply has the address or the
way in in it (the system sends those), when it is only photos or only the
rates or extras message (a rates message inside a reply becomes "[rates
message]": it goes out word for word from the prompt), and when it is long.

Wordings. The client's side is asked for in several more ways by DeepSeek
(on the platform key, metered), so a new phrasing still finds it. Only the
masked client text is sent, never the business's reply.

Vectors. Every text is turned into a vector by the local embedder
(embeddings.py); a new message is compared with them in memory (a few
thousand per industry; no pgvector). When the embedder is down the pairs
are still stored and embedded on a later run.

The scheduler's round (tick) runs hourly in the background, and only for
accounts whose industry has at least one account with retrieval.enabled:
an industry nobody uses it in costs nothing. The admin can run it for one
account at any time (examples_api.py), switched on or not, and try a
message to see what it would find before switching it on.

Examples come from the account itself and, with retrieval.share_in_industry,
from the other accounts of its industry, but only those without a figure
in the reply: a price or a time belongs to its own account. Another
industry's never.
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import takewhile
from typing import Any, Iterable, Optional, Sequence

import asyncpg
import httpx

import ai_responder
import audit
import booking_states as bs
import embeddings
import finetune
import llm_usage
import tenant_config
import tenants
import vision
import wa_store

log = logging.getLogger("examples")

HAND = "hand"
EDITED = "edited"
FLAGGED = "flagged"
SOURCES = (HAND, EDITED, FLAGGED)

BEFORE_TIME = "before_time"
TIME_PENDING = "time_pending"
BOOKED = "booked"
AFTER = "after"
PHASES = (BEFORE_TIME, TIME_PENDING, BOOKED, AFTER)

# What a rates or extras message in a reply becomes: it goes out word for
# word from the prompt, so the words are never an example.
RATES = "[rates message]"
# A reply longer than this is a speech, not a way of answering.
MAX_REPLY_CHARS = 400
# The embedder reads about this much anyway (128 tokens).
MAX_CLIENT_CHARS = 500
MAX_CONTEXT_CHARS = 200

# A turn is taken once nothing has happened in its chat for this long: a
# person typing a reply in three messages is not cut after the first.
SETTLE = timedelta(minutes=30)
# A draft waiting for approval keeps its turn open (approving it changes the
# reply), unless it has waited this long: then it was left, not forgotten.
DRAFT_WAIT = timedelta(hours=24)
# A visit that started this long ago or less: the client is "after".
AFTER_WINDOW = timedelta(hours=12)

# Work per run, per account and in all. A run is hourly; the first runs of
# a busy account catch up over several.
CHATS_PER_RUN = 200
ROWS_PER_CHAT = 400
EMBED_BATCH = embeddings.MAX_TEXTS
# 64 long texts take 10-20 s on the embedder's one CPU thread.
EMBED_TIMEOUT = 60.0
EMBED_BUDGET = 512
RUN_EVERY = timedelta(hours=1)

# Wordings: one DeepSeek call per this many examples, this many per example.
WORDINGS_PER_CALL = 20
WORDINGS_EACH = 6
WORDING_CALLS_PER_RUN = 3
MAX_WORDINGS_KEPT = 8
MAX_WORDING_CHARS = 300
WORDINGS_MODEL = "deepseek-chat"
WORDINGS_TIMEOUT = 120.0
WORDINGS_PURPOSE = "reply_wordings"

# The reply's own look-up: it waits for nothing.
QUERY_TIMEOUT = 1.5
CACHE_SECONDS = 600
# ~1.6 KB a vector; scoring 5000 takes ~30 ms (math.sumprod, one core).
CACHE_MAX = 5000
# How many of the best matches the admin's "try" shows.
TRY_RESULTS = 10

# The copy guard: a reply line at least this long, nearly all of whose
# character trigrams (this share) are in one example reply, is a copy.
COPY_MIN_CHARS = 30
COPY_SHARE = 0.9

RUN_KEY = "reply_examples_run"
MACHINE_ACTORS = [audit.BOT, audit.SYSTEM]
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FIRST_CHAT = -(2 ** 63)
_LAST_CHAT = 2 ** 63 - 1


def watermark_key(tenant_id: int) -> str:
    return f"reply_examples:{tenant_id}"


# ------------------------------------------------------------------ pairing


@dataclass(frozen=True)
class Row:
    """One message of a chat, as pairing reads it."""
    id: int
    direction: str
    status: str
    text: str
    created_at: datetime
    attachments: tuple[int, ...] = ()
    llm_model: Optional[str] = None
    draft_text: Optional[str] = None
    prompt_version: Optional[str] = None
    # A message_sent audit row by the bot or the system says the runtime
    # sent it, not a person (a model reply, the address, a reminder).
    by_machine: bool = False

    @property
    def inbound(self) -> bool:
        return self.direction == "in" and self.status == "received"

    @property
    def sent(self) -> bool:
        return self.direction == "out" and self.status == "sent"

    @property
    def draft(self) -> bool:
        return self.direction == "out" and self.status == "pending_approval"

    @property
    def typed(self) -> bool:
        """Typed by a person (stats._SENT): not the model's, and neither the
        bot nor the system sent it."""
        return self.sent and self.llm_model is None and not self.by_machine

    @property
    def system_sent(self) -> bool:
        """Sent by the runtime without the model: the address, the way in,
        a reminder, a booking notice."""
        return self.sent and self.llm_model is None and self.by_machine


@dataclass
class Turn:
    client: list[Row]
    business: list[Row]
    # Our last sent message before the client's.
    context: Optional[Row]


def pair(rows: Sequence[Row], *, now: datetime) -> list[Turn]:
    """The chat's turns, oldest first: the client's messages in a row, then
    the business's sent messages in a row. Drafts never sent are not part
    of a turn, but one still waiting for approval leaves its turn open
    (approving it changes the reply); the turn is taken once settled."""
    turns: list[Turn] = []
    context: Optional[Row] = None
    i, n = 0, len(rows)

    def waiting(row: Row) -> bool:
        return row.draft and now - row.created_at < DRAFT_WAIT

    while i < n:
        if not rows[i].inbound:
            if rows[i].sent:
                context = rows[i]
            i += 1
            continue
        client: list[Row] = []
        business: list[Row] = []
        open_draft = False
        while i < n and not rows[i].sent:
            client += [rows[i]] if rows[i].inbound else []
            open_draft = open_draft or waiting(rows[i])
            i += 1
        while i < n and not rows[i].inbound:
            business += [rows[i]] if rows[i].sent else []
            open_draft = open_draft or waiting(rows[i])
            i += 1
        if business and not open_draft:
            turns.append(Turn(client, business, context))
        if business:
            context = business[-1]
    return turns


@dataclass(frozen=True)
class Flag:
    """The admin's flag on a chat (finetune_examples)."""
    verdict: str
    upto_message_id: int
    corrections: frozenset[int] = frozenset()
    outcome: str = ""
    client_kind: str = ""


def classify(turn: Turn, flag: Optional[Flag]) -> tuple[Optional[str], list[Row]]:
    """Which kind of example the turn is, and the messages that are its
    reply; (None, []) when it teaches nothing (the bot's own reply)."""
    rows = turn.business
    if all(row.typed for row in rows):
        return HAND, rows
    if rows[0].draft_text is not None:
        # The approved draft, then the rest of it (sent as the admin), up to
        # whatever the runtime sent by itself after it (the address, say).
        return EDITED, [rows[0], *takewhile(lambda row: not row.by_machine, rows[1:])]
    if flag is not None and flag.verdict == "good":
        # The bot's replies count here; what the system sends by itself does not.
        reply = [row for row in takewhile(lambda row: not row.system_sent, rows)
                 if row.id <= flag.upto_message_id]
        if reply and turn.client[0].id <= flag.upto_message_id:
            return FLAGGED, reply
    return None, []


_MEDIA = re.compile(
    r"^\[(?:non-text message|photo|video|voice(?: note)?|sticker|sent a file|sent (?:photo|video)\b[^\]]*)\]",
    re.IGNORECASE,
)
_BLOCK = re.compile(r"\[\s*block\s*\](.*?)(?:\[\s*/\s*block\s*\]|$)", re.IGNORECASE | re.DOTALL)
_NOT_A_WORD = re.compile(r"[\W_]+")
_DIGIT = re.compile(r"\d")


def is_media(text: str, attachments: Sequence[int] = ()) -> bool:
    """A photo, video, voice note or sticker (or the placeholder a file
    sent from the library leaves in the thread), not words."""
    return bool(attachments) or not (text or "").strip() or bool(_MEDIA.match((text or "").strip()))


def _keys(text: str) -> list[str]:
    """Lines as compared with a prompt's rates block: case, emojis,
    punctuation and spacing left out."""
    return [key for key in (_NOT_A_WORD.sub(" ", line.lower()).strip() for line in text.splitlines()) if key]


def is_rates(text: str, fixed: Sequence[str] = ()) -> bool:
    """The rates or the extras message: one of the prompt's word-for-word
    blocks (ai_responder.fixed_blocks), or a list of prices typed by hand
    (three lines or more, most with a figure)."""
    keys = _keys(text)
    if not keys:
        return False
    if any(keys == _keys(block) for block in fixed):
        return True
    figures = sum(1 for line in keys if _DIGIT.search(line))
    return len(keys) >= 3 and figures >= max(2, (len(keys) + 1) // 2)


def _clean(text: str) -> str:
    lines = [line.strip() for line in (text or "").strip().splitlines()]
    return "\n".join(line for line in lines if line)


def _reply_part(text: str, fixed: Sequence[str]) -> str:
    """One business message as it is kept: a rates message (between [block]
    markers or not) as RATES."""
    if _BLOCK.search(text):
        text = _BLOCK.sub(f"\n{RATES}\n", text)
        return "\n".join(RATES if is_rates(line, fixed) else line for line in _clean(text).splitlines())
    return RATES if is_rates(text, fixed) else _clean(text)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# A client naming a time: a clock time ("22:30", "9.15"), an hour after a
# word like at / around / by ("at 22", "в 9", "к 11", "pl. 21", "ap 23"), an
# am/pm hour ("10pm"), or minutes or hours from now ("in 20 min", "через 20
# минут", "pēc 20 min"). Only the owner answers a time; a hand-typed "yes,
# come at 23" must never teach the bot to say yes itself, even on an
# account where no booking records the request (time_pending). A duration
# ("1h", "2 часа", "2 stundas") is a price question, not a time.
_HOUR = r"(?:[01]?\d|2[0-3])"
_NAMES_A_TIME = re.compile(
    rf"\b{_HOUR}[:.][0-5]\d\b"
    # ("about 2 hours" is a length, "в 2 часа" a time: only English and
    # Latvian hour words after the number are left out.)
    rf"|\b(?:at|around|about|by|till|until|from|pl|ap|uz|в|к|около|после|до)\.?\s*{_HOUR}\b(?![.,]\d)"
    r"(?!\s*(?:h|hrs?|hours?|stund\w*)\b)"
    rf"|\b{_HOUR}\s*(?:am|pm|o'?clock)\b"
    r"|\b(?:in|через|pēc|pec)\s*\d{1,3}\s*(?:min|mins|minutes|мин|минут\w*|minūt\w*|h|hours?|час\w*|stund\w*)\b",
    re.IGNORECASE,
)


def names_a_time(text: str) -> bool:
    """Whether a client message names a time (see _NAMES_A_TIME)."""
    return bool(_NAMES_A_TIME.search(text or ""))


def build(turn: Turn, reply: Sequence[Row], *, source: str, phase: str,
          masks: Sequence[tuple[str, str]] = (), fixed: Sequence[str] = ()) -> tuple[Optional[dict[str, Any]], str]:
    """The example a turn makes, masked, or (None, why not) when a fixed rule
    leaves it out."""
    if phase == TIME_PENDING:
        return None, "time_pending"
    client = _clean("\n".join(r.text for r in turn.client if not is_media(r.text, r.attachments)))
    client = _cut(finetune.mask(client, masks), MAX_CLIENT_CHARS)
    if not client:
        return None, "no_client_text"
    if names_a_time(client):
        return None, "time_named"
    parts: list[str] = []
    for row in reply:
        if is_media(row.text, row.attachments):
            continue
        for part in _reply_part(row.text, fixed).splitlines():
            if not (part == RATES and parts and parts[-1] == RATES):
                parts.append(part)
    if not parts:
        return None, "media_only"
    if all(part == RATES for part in parts):
        return None, "rates_only"
    reply_text = finetune.mask("\n".join(parts), masks)
    if "[address]" in reply_text or "[way in]" in reply_text:
        return None, "address"
    if len(reply_text) > MAX_REPLY_CHARS:
        return None, "too_long"
    context = ""
    if turn.context is not None and not is_media(turn.context.text, turn.context.attachments):
        context = _cut(finetune.mask(_reply_part(turn.context.text, fixed), masks), MAX_CONTEXT_CHARS)
    return {
        "in_first_id": turn.client[0].id, "reply_last_id": reply[-1].id,
        "context_text": context, "client_text": client, "reply_text": reply_text,
        "source": source, "phase": phase, "has_figures": bool(_DIGIT.search(reply_text)),
        "prompt_version": reply[0].prompt_version,
    }, ""


# -------------------------------------------------------------------- phase


@dataclass(frozen=True)
class Snapshot:
    """A booking as it stood at one moment."""
    state: str
    proposed_by: Optional[str]
    starts_at: datetime


def waiting_for_owner(snapshot: Snapshot) -> bool:
    """A time they asked for is with the owner, unanswered (the same test as
    booking_flow.waiting_for_owner)."""
    if snapshot.state in (bs.REQUESTED, bs.PENDING) and snapshot.proposed_by != bs.OWNER:
        return True
    return snapshot.state == bs.CONFIRMED and snapshot.proposed_by == bs.CUSTOMER


def phase_of(snapshots: Iterable[Snapshot], at: datetime) -> str:
    """Where a chat's bookings stand at `at`: a time waiting for the owner,
    a confirmed time still ahead, a visit just past, or none of these."""
    snapshots = list(snapshots)
    if any(waiting_for_owner(s) for s in snapshots):
        return TIME_PENDING
    if any(s.state == bs.CONFIRMED and s.starts_at > at for s in snapshots):
        return BOOKED
    if any(s.state in (bs.CONFIRMED, bs.COMPLETED, bs.NO_SHOW) and at - AFTER_WINDOW < s.starts_at <= at
           for s in snapshots):
        return AFTER
    return BEFORE_TIME


# Changes that leave a proposed time standing (booking_states: the others
# clear it).
_KEEPS_PROPOSAL = {"create", "import", "submitted", "confirm_attendance", "mark_completed", "mark_no_show"}


def _when(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def snapshot_at(booking: dict[str, Any], events: Sequence[dict[str, Any]], at: datetime) -> Optional[Snapshot]:
    """The booking at `at`, replayed from its events (booking_events, in
    order); None when it did not exist yet. A time moved later is not known
    from before the move: the booking's present time stands in for it."""
    state: Optional[str] = None
    proposer: Optional[str] = None
    starts: Optional[datetime] = None
    for event in events:
        if event["created_at"] > at:
            break
        state = event["to_state"]
        if event["action"] == "propose":
            proposer = bs.CUSTOMER if event["actor"] == bs.CUSTOMER else bs.OWNER
        elif event["action"] not in _KEEPS_PROPOSAL:
            proposer = None
        moved = (tenants._json(event.get("payload")) or {}).get("starts_at")
        starts = (_when(moved) if moved else None) or starts
    if state is None:
        if events or booking["created_at"] > at:
            return None
        # No events at all: as it is now, since it was made.
        return Snapshot(booking["state"], booking.get("proposed_by"), booking["starts_at"])
    return Snapshot(state, proposer, starts or booking["starts_at"])


async def phase_now(pool: asyncpg.Pool, tenant_id: int, chat_id: int, now: datetime) -> str:
    """The chat's phase from its bookings as they are."""
    rows = await pool.fetch(
        "SELECT state, proposed_by, starts_at FROM bookings WHERE tenant_id = $1 AND chat_id = $2 "
        "AND (state IN ('requested', 'pending', 'confirmed') OR starts_at > $3)",
        tenant_id, chat_id, now - AFTER_WINDOW)
    return phase_of((Snapshot(r["state"], r["proposed_by"], r["starts_at"]) for r in rows), now)


# --------------------------------------------------------------- accounts


async def _setting(pool: asyncpg.Pool, key: str) -> Optional[dict[str, Any]]:
    value = await pool.fetchval("SELECT value FROM platform_settings WHERE key = $1", key)
    value = tenants._json(value) if value is not None else None
    return value if isinstance(value, dict) else None


async def _save_setting(pool: asyncpg.Pool, key: str, value: dict[str, Any]) -> None:
    await pool.execute(
        "INSERT INTO platform_settings (key, value, updated_by, updated_at) VALUES ($1, $2::jsonb, 'system', now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
        key, json.dumps(value))


async def configs(pool: asyncpg.Pool) -> dict[int, tuple[int, Optional[dict[str, Any]]]]:
    """tenant id -> (industry id, effective config, or None when what is
    stored no longer validates)."""
    out: dict[int, tuple[int, Optional[dict[str, Any]]]] = {}
    for row in await pool.fetch("SELECT t.id, t.industry_id, t.config_json, i.default_config FROM tenants t "
                                "JOIN industries i ON i.id = t.industry_id ORDER BY t.id"):
        try:
            config = tenant_config.resolve(tenants._json(row["default_config"]),
                                           tenants._json(row["config_json"])).as_dict()
        except tenant_config.ConfigError:
            config = None
        out[row["id"]] = (row["industry_id"], config)
    return out


async def accounts(pool: asyncpg.Pool) -> list[int]:
    """The accounts the hourly round works on: every account of an industry
    in which at least one account has retrieval on. Shared examples come
    from the whole industry, so its other accounts count too."""
    found = await configs(pool)
    industries = {industry for industry, config in found.values() if config and config["retrieval"]["enabled"]}
    return [tenant_id for tenant_id, (industry, _) in found.items() if industry in industries]


# ------------------------------------------------------------------ ingest


def _owner_chat(provider: str) -> tuple[Optional[int], str]:
    """booking.provider as a chat id or a username: the owner's chat holds
    booking commands, not client turns."""
    value = (provider or "").strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value), ""
    return None, value.lstrip("@").lower()


_CHATS = f"""
SELECT c.chat_id, c.display_name, {wa_store.push_name_sql("c")} AS person_name, c.username, a.active_at,
       f.verdict, f.upto_message_id, f.corrections, f.outcome, f.client_kind
  FROM conversations c
  LEFT JOIN finetune_examples f ON f.tenant_id = c.tenant_id AND f.chat_id = c.chat_id
 CROSS JOIN LATERAL (SELECT GREATEST(c.last_message_at, f.updated_at) AS active_at) a
 WHERE c.tenant_id = $1 AND NOT c.is_bot
   AND a.active_at <= $4 AND (a.active_at, c.chat_id) > ($2::timestamptz, $3::bigint)
   AND NOT (c.chat_id = ANY($5::bigint[])) AND ($6 = '' OR lower(coalesce(c.username, '')) <> $6)
   AND (f.verdict IS NULL OR f.verdict <> 'bad')
   AND (f.verdict = 'good' OR EXISTS (
        SELECT 1 FROM messages m WHERE m.tenant_id = c.tenant_id AND m.chat_id = c.chat_id
           AND m.direction = 'out' AND m.status = 'sent' AND (m.llm_model IS NULL OR m.draft_text IS NOT NULL)))
 ORDER BY a.active_at, c.chat_id
 LIMIT $7
"""

_ROWS = """
SELECT chat_id, id, direction, status, text, attachments, llm_model, draft_text, prompt_version, created_at FROM (
  SELECT m.*, row_number() OVER (PARTITION BY m.chat_id ORDER BY m.id DESC) AS back
    FROM messages m
   WHERE m.tenant_id = $1 AND m.chat_id = ANY($2::bigint[])
     AND ((m.direction = 'in' AND m.status = 'received')
          OR (m.direction = 'out' AND m.status IN ('sent', 'pending_approval')))
) recent WHERE back <= $3 ORDER BY chat_id, id
"""

# Who sent each message: the same rule as stats._SENT. A message's audit row
# is never older than the message.
_MACHINE = """
SELECT DISTINCT payload->>'message_id' AS id FROM audit_log
 WHERE tenant_id = $1 AND event = $2 AND actor = ANY($3::text[]) AND created_at >= $4
   AND payload->>'message_id' = ANY($5::text[])
"""

# A chat flagged bad teaches nothing, and a flagged example only stands
# while its chat is flagged good up to it.
_DROP = """
DELETE FROM reply_examples e
 WHERE e.tenant_id = $1 AND EXISTS (
       SELECT 1 FROM finetune_examples f WHERE f.tenant_id = e.tenant_id AND f.chat_id = e.chat_id
          AND (f.verdict = 'bad' OR (e.source = 'flagged' AND (f.verdict <> 'good' OR e.reply_last_id > f.upto_message_id))))
    OR (e.tenant_id = $1 AND e.source = 'flagged' AND NOT EXISTS (
       SELECT 1 FROM finetune_examples f WHERE f.tenant_id = e.tenant_id AND f.chat_id = e.chat_id))
"""

# The flag's judgement, and how the chat ended: from its flag, or else from
# its bookings. Both can change after an example is stored.
_RESYNC = """
WITH flags AS (
  SELECT chat_id, verdict, client_kind, outcome FROM finetune_examples WHERE tenant_id = $1
), booked AS (
  SELECT chat_id, CASE WHEN bool_or(state = 'completed') THEN 'came'
                       WHEN bool_or(state = 'no_show') THEN 'no_show'
                       WHEN bool_or(state = 'cancelled' AND cancelled_by = 'customer') THEN 'cancelled'
                       ELSE '' END AS outcome
    FROM bookings WHERE tenant_id = $1 GROUP BY chat_id
), wanted AS (
  SELECT e.id, CASE WHEN f.verdict = 'plain' THEN 'plain' ELSE 'good' END AS verdict,
         COALESCE(f.client_kind, '') AS client_kind, COALESCE(NULLIF(f.outcome, ''), b.outcome, '') AS outcome
    FROM reply_examples e LEFT JOIN flags f ON f.chat_id = e.chat_id LEFT JOIN booked b ON b.chat_id = e.chat_id
   WHERE e.tenant_id = $1
)
UPDATE reply_examples e SET verdict = w.verdict, client_kind = w.client_kind, outcome = w.outcome, industry_id = $2
  FROM wanted w
 WHERE e.id = w.id
   AND (e.verdict, e.client_kind, e.outcome, e.industry_id) IS DISTINCT FROM (w.verdict, w.client_kind, w.outcome, $2)
"""

_INSERT = """
INSERT INTO reply_examples (tenant_id, industry_id, chat_id, in_first_id, reply_last_id, context_text, client_text,
                            reply_text, source, verdict, outcome, client_kind, phase, has_figures, prompt_version)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (tenant_id, chat_id, reply_last_id) DO NOTHING RETURNING id
"""


def _flag(row: Any) -> Optional[Flag]:
    if row["verdict"] is None:
        return None
    corrections = tenants._json(row["corrections"]) or {}
    return Flag(verdict=row["verdict"], upto_message_id=row["upto_message_id"],
                corrections=frozenset(int(k) for k in corrections if str(k).lstrip("-").isdigit()),
                outcome=row["outcome"] or "", client_kind=row["client_kind"] or "")


async def _chat_rows(con: Any, tenant_id: int, chat_ids: list[int]) -> dict[int, list[Row]]:
    rows = await con.fetch(_ROWS, tenant_id, chat_ids, ROWS_PER_CHAT)
    machine: set[str] = set()
    if rows:
        machine = {r["id"] for r in await con.fetch(
            _MACHINE, tenant_id, audit.MESSAGE_SENT, MACHINE_ACTORS, min(r["created_at"] for r in rows),
            [str(r["id"]) for r in rows if r["direction"] == "out"])}
    out: dict[int, list[Row]] = {}
    for r in rows:
        out.setdefault(r["chat_id"], []).append(Row(
            id=r["id"], direction=r["direction"], status=r["status"], text=r["text"] or "",
            created_at=r["created_at"], attachments=tuple(r["attachments"] or ()), llm_model=r["llm_model"],
            draft_text=r["draft_text"], prompt_version=r["prompt_version"], by_machine=str(r["id"]) in machine))
    return out


async def _chat_bookings(con: Any, tenant_id: int,
                         chat_ids: list[int]) -> dict[int, list[tuple[dict[str, Any], list[dict[str, Any]]]]]:
    bookings = [dict(r) for r in await con.fetch(
        "SELECT id, chat_id, state, proposed_by, starts_at, created_at FROM bookings "
        "WHERE tenant_id = $1 AND chat_id = ANY($2::bigint[])", tenant_id, chat_ids)]
    events: dict[int, list[dict[str, Any]]] = {}
    if bookings:
        for e in await con.fetch(
                "SELECT booking_id, to_state, action, actor, payload, created_at FROM booking_events "
                "WHERE tenant_id = $1 AND booking_id = ANY($2::bigint[]) ORDER BY id",
                tenant_id, [b["id"] for b in bookings]):
            events.setdefault(e["booking_id"], []).append(dict(e))
    out: dict[int, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
    for booking in bookings:
        out.setdefault(booking["chat_id"], []).append((booking, events.get(booking["id"], [])))
    return out


async def ingest_account(pool: asyncpg.Pool, tenant_id: int, *, now: Optional[datetime] = None,
                         max_chats: int = CHATS_PER_RUN, from_start: bool = False) -> dict[str, Any]:
    """New turns of this account's chats, stored as examples (never twice).

    Works through the chats in the order they were last active, from where
    the last run stopped (platform_settings 'reply_examples:<tenant id>'),
    at most `max_chats` at a time: `more` says whether there are more. A
    chat counts once nothing has happened in it for SETTLE; a chat active
    since (a new message, an approved draft, a new flag) is read again,
    and its turns already stored are skipped. `from_start` reads every
    chat again."""
    now = now or datetime.now(timezone.utc)
    bundle = await tenants.TenantStore(pool).bundle(tenant_id)
    config = bundle.config
    industry_id = bundle.tenant["industry_id"]
    fixed = ai_responder.fixed_blocks(bundle.prompt.text, 1)
    booking = config.get("booking") or {}
    owner_id, owner_name = _owner_chat(booking.get("provider", ""))
    mark = None if from_start else await _setting(pool, watermark_key(tenant_id))
    after_at = _when((mark or {}).get("through")) or _EPOCH
    after_chat = int((mark or {}).get("chat", _FIRST_CHAT))
    cutoff = now - SETTLE
    counts: dict[str, Any] = {"chats": 0, "turns": 0, "added": 0, "skipped": Counter(), "more": False}

    async with pool.acquire() as con:
        owners = [r["provider_chat_id"] for r in await con.fetch(
            "SELECT DISTINCT provider_chat_id FROM bookings WHERE tenant_id = $1 AND provider_chat_id IS NOT NULL",
            tenant_id)] + ([owner_id] if owner_id is not None else [])
        chats = await con.fetch(_CHATS, tenant_id, after_at, after_chat, cutoff, owners, owner_name, max_chats)
        counts["chats"] = len(chats)
        chat_ids = [c["chat_id"] for c in chats]
        rows = await _chat_rows(con, tenant_id, chat_ids) if chats else {}
        booked = await _chat_bookings(con, tenant_id, chat_ids) if chats else {}

        found: list[tuple[int, dict[str, Any], Optional[Flag]]] = []
        for chat in chats:
            flag = _flag(chat)
            masks = finetune.masks_for(display_name=chat["display_name"] or "", username=chat["username"] or "",
                                       person_name=chat["person_name"] or "", address=booking.get("address", ""),
                                       way_in=booking.get("arrival_instructions", ""))
            for turn in pair(rows.get(chat["chat_id"], []), now=now):
                counts["turns"] += 1
                source, reply = classify(turn, flag)
                if source is None:
                    counts["skipped"]["bot"] += 1
                    continue
                if flag is not None and any(row.id in flag.corrections for row in reply):
                    counts["skipped"]["corrected"] += 1  # the admin said what was wrong with it
                    continue
                at = reply[0].created_at
                phase = phase_of(filter(None, (snapshot_at(b, events, at)
                                               for b, events in booked.get(chat["chat_id"], []))), at)
                example, why = build(turn, reply, source=source, phase=phase, masks=masks, fixed=fixed)
                if example is None:
                    counts["skipped"][why] += 1
                    continue
                found.append((chat["chat_id"], example, flag))

        async with con.transaction():
            for chat_id, e, flag in found:
                added = await con.fetchval(
                    _INSERT, tenant_id, industry_id, chat_id, e["in_first_id"], e["reply_last_id"],
                    e["context_text"], e["client_text"], e["reply_text"], e["source"],
                    "plain" if flag is not None and flag.verdict == "plain" else "good",
                    flag.outcome if flag else "", flag.client_kind if flag else "",
                    e["phase"], e["has_figures"], e["prompt_version"])
                counts["added"] += added is not None
            counts["removed"] = int((await con.execute(_DROP, tenant_id)).split()[-1])
            await con.execute(_RESYNC, tenant_id, industry_id)

    if len(chats) == max_chats:
        counts["more"] = True
        through, last_chat = chats[-1]["active_at"], chats[-1]["chat_id"]
    else:
        through, last_chat = cutoff, _LAST_CHAT
    await _save_setting(pool, watermark_key(tenant_id), {
        "through": through.isoformat(), "chat": last_chat, "ingested_at": now.isoformat()})
    counts["skipped"] = dict(counts["skipped"])
    log.info("Reply examples for tenant %s: %s chats, %s turns, %s new, %s removed, skipped %s.",
             tenant_id, counts["chats"], counts["turns"], counts["added"], counts["removed"], counts["skipped"])
    return counts


# --------------------------------------------------------------- vectors


async def _store_queries(pool: asyncpg.Pool, items: Sequence[tuple[int, str]], got: embeddings.Embedded, *,
                         generated: bool) -> None:
    args = [(example_id, text, generated, vector, got.model) for (example_id, text), vector in zip(items, got.vectors)]
    sql = ("INSERT INTO example_queries (example_id, text, generated, embedding, embed_model) "
           "VALUES ($1, $2, $3, $4::real[], $5)")
    if not generated:
        sql += " ON CONFLICT (example_id) WHERE NOT generated DO NOTHING"
    async with pool.acquire() as con, con.transaction():
        # An example removed meanwhile (a flag changed): its texts go with it.
        alive = {r["id"] for r in await con.fetch("SELECT id FROM reply_examples WHERE id = ANY($1::bigint[])",
                                                  [a[0] for a in args])}
        if any(a[0] in alive for a in args):
            await con.executemany(sql, [a for a in args if a[0] in alive])


async def _embed(texts: list[str], client: Optional[httpx.AsyncClient]) -> Optional[embeddings.Embedded]:
    """In batches of EMBED_BATCH with the long timeout; None as soon as one
    fails (the embedder is down or overloaded: a later run goes on)."""
    model, vectors = "", []
    for start in range(0, len(texts), EMBED_BATCH):
        got = await embeddings.embed(texts[start:start + EMBED_BATCH], client=client, timeout=EMBED_TIMEOUT)
        if got is None:
            return None
        model = got.model
        vectors += got.vectors
    return embeddings.Embedded(model=model, vectors=vectors)


async def embed_pending(pool: asyncpg.Pool, tenant_ids: Sequence[int], *, client: Optional[httpx.AsyncClient],
                        budget: int = EMBED_BUDGET) -> dict[str, Any]:
    """The client text of examples not embedded yet, at most `budget`.
    With the embedder down they wait for the next run."""
    rows = await pool.fetch(
        "SELECT e.id, e.client_text FROM reply_examples e WHERE e.tenant_id = ANY($1::int[]) AND NOT EXISTS ("
        "SELECT 1 FROM example_queries q WHERE q.example_id = e.id AND NOT q.generated) ORDER BY e.id LIMIT $2",
        list(tenant_ids), budget)
    done, model = 0, None
    for start in range(0, len(rows), EMBED_BATCH):
        batch = [(r["id"], r["client_text"]) for r in rows[start:start + EMBED_BATCH]]
        got = await _embed([text for _, text in batch], client)
        if got is None:
            log.info("Embedder not answering: %s reply examples wait for the next run.", len(rows) - done)
            return {"embedded": done, "model": model, "up": False}
        await _store_queries(pool, batch, got, generated=False)
        done, model = done + len(batch), got.model
    return {"embedded": done, "model": model, "up": True}


async def reembed_stale(pool: asyncpg.Pool, tenant_ids: Sequence[int], *, model: str,
                        client: Optional[httpx.AsyncClient], budget: int = EMBED_BUDGET) -> int:
    """Texts embedded by another model than the embedder's now: vectors of
    two models never compare, so they are embedded again."""
    rows = await pool.fetch(
        "SELECT q.id, q.text FROM example_queries q JOIN reply_examples e ON e.id = q.example_id "
        "WHERE e.tenant_id = ANY($1::int[]) AND q.embed_model <> $2 ORDER BY q.id LIMIT $3",
        list(tenant_ids), model, budget)
    done = 0
    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start:start + EMBED_BATCH]
        got = await _embed([r["text"] for r in batch], client)
        if got is None:
            break
        await pool.executemany("UPDATE example_queries SET embedding = $2::real[], embed_model = $3 WHERE id = $1",
                               [(r["id"], v, got.model) for r, v in zip(batch, got.vectors)])
        done += len(batch)
    return done


# --------------------------------------------------------------- wordings

WORDINGS_PROMPT = """You help a business's chat assistant recognise what its clients mean. Below are messages clients sent to the business (an independent escort, chatting on Telegram or WhatsApp). For each message, write {each} other ways a client might write the same message: the same meaning and the same intent, as other clients would type it. You do not answer the messages.
- Write the way clients really type: casual, slang, abbreviations, typos, little punctuation, few capitals; some of them very short (two or three words).
- Write in the language of the original. {market}
- Keep placeholders such as [client], [number], [username], [email] and [rates message] as they are. Never add a name, phone number, price, time, date or place the original does not have.
Answer with JSON only, in this form: {{"items": [{{"id": <the message's id>, "wordings": ["...", "..."]}}]}}

Messages:
{messages}"""
MARKET_MIXED = ("If the original is in English, Latvian or Russian (clients of this business switch between the "
                "three), write two or three of the wordings in the other two of these languages.")
MARKET_OWN = "Every wording in the language of the original."


def market(config: Optional[dict[str, Any]]) -> str:
    """The language note: an account in Latvia, or one set to answer in
    Latvian or Russian, gets clients writing in all three."""
    config = config or {}
    policy = str(config.get("language_policy") or "")
    if config.get("timezone") == "Europe/Riga" or policy.startswith(("fixed:lv", "fixed:ru")):
        return MARKET_MIXED
    return MARKET_OWN


def wordings_prompt(items: Sequence[tuple[int, str]], market_note: str) -> str:
    """Only the masked client texts go out, never a reply."""
    messages = "\n".join(json.dumps({"id": example_id, "text": text}, ensure_ascii=False) for example_id, text in items)
    return WORDINGS_PROMPT.format(each=WORDINGS_EACH, market=market_note, messages=messages)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_wordings(raw: str, originals: dict[int, str]) -> dict[int, list[str]]:
    """The model's answer, checked: only ids that were asked about, only
    short distinct texts unlike the original, masked again, and never a
    figure the original did not have."""
    body = _FENCE.sub("", (raw or "").strip()).strip()
    start, end = body.find("{"), body.rfind("}")
    try:
        data = json.loads(body[start:end + 1]) if start >= 0 else None
    except ValueError:
        data = None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict[int, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            example_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        original = originals.get(example_id)
        if original is None or not isinstance(item.get("wordings"), list):
            continue
        seen = {" ".join(original.lower().split())}
        kept: list[str] = []
        for text in item["wordings"]:
            if not isinstance(text, str):
                continue
            text = finetune.mask(" ".join(text.split()), ())
            key = text.lower()
            if not text or len(text) > MAX_WORDING_CHARS or key in seen:
                continue
            if _DIGIT.search(text) and not _DIGIT.search(original):
                continue
            seen.add(key)
            kept.append(text)
        if kept:
            out[example_id] = kept[:MAX_WORDINGS_KEPT]
    return out


async def ask_wordings(items: Sequence[tuple[int, str]], *, market_note: str, api_key: str,
                       client: Optional[httpx.AsyncClient], usage_sink: Optional[ai_responder.UsageSink]) -> str:
    """One DeepSeek call (the platform key) for up to WORDINGS_PER_CALL
    client texts. Raises vision.VisionError with a safe message."""
    return await vision._complete(
        api_url=ai_responder.API_URL, api_key=api_key, client=client, usage_sink=usage_sink,
        service="DeepSeek API", key_name="DEEPSEEK_PLATFORM_KEY",
        payload={"model": WORDINGS_MODEL, "messages": [{"role": "user", "content": wordings_prompt(items, market_note)}],
                 "max_tokens": 4000, "temperature": 0.9, "response_format": {"type": "json_object"}},
    )


async def make_wordings(pool: asyncpg.Pool, tenant_ids: Sequence[int], *, api_key: str,
                        client: Optional[httpx.AsyncClient], limit: int = WORDINGS_PER_CALL * WORDING_CALLS_PER_RUN,
                        ) -> dict[str, Any]:
    """Other wordings of the client side, for examples that have none yet
    (embedded and switched on), at most `limit`: one DeepSeek call per
    WORDINGS_PER_CALL of one account. Each example is claimed before its
    call, so two runs never pay for it twice; a failed call or embedding
    hands it back for a later run."""
    result = {"examples": 0, "wordings": 0, "calls": 0, "error": ""}
    if not api_key:
        log.info("No DEEPSEEK_PLATFORM_KEY: reply examples get no other wordings.")
        result["error"] = "no_key"
        return result
    rows = await pool.fetch(
        "SELECT e.id, e.tenant_id, e.client_text FROM reply_examples e WHERE e.tenant_id = ANY($1::int[]) "
        "AND e.active AND e.worded_at IS NULL AND EXISTS (SELECT 1 FROM example_queries q "
        "WHERE q.example_id = e.id AND NOT q.generated) ORDER BY e.tenant_id, e.id LIMIT $2",
        list(tenant_ids), limit)
    found = await configs(pool) if rows else {}

    async def meter(model: str, usage: dict[str, int]) -> None:
        # Platform spend, like a finetune run: no client asked for it.
        await llm_usage.record(pool, tenant_id=None, purpose=WORDINGS_PURPOSE, model=model, usage=usage)

    batches: list[list[Any]] = []
    for row in rows:
        if batches and batches[-1][0]["tenant_id"] == row["tenant_id"] and len(batches[-1]) < WORDINGS_PER_CALL:
            batches[-1].append(row)
        else:
            batches.append([row])
    for batch in batches:
        claimed = {r["id"] for r in await pool.fetch(
            "UPDATE reply_examples SET worded_at = now() WHERE id = ANY($1::bigint[]) AND worded_at IS NULL "
            "RETURNING id", [r["id"] for r in batch])}
        originals = {r["id"]: r["client_text"] for r in batch if r["id"] in claimed}
        if not originals:
            continue

        async def give_back() -> None:
            await pool.execute("UPDATE reply_examples SET worded_at = NULL WHERE id = ANY($1::bigint[])",
                               list(originals))

        try:
            result["calls"] += 1
            raw = await ask_wordings(list(originals.items()), market_note=market(found.get(batch[0]["tenant_id"],
                                                                                          (0, None))[1]),
                                     api_key=api_key, client=client, usage_sink=meter)
        except vision.VisionError as exc:
            log.warning("Wordings for reply examples failed: %s", exc)
            await give_back()
            result["error"] = str(exc)
            break
        worded = parse_wordings(raw, originals)
        if not worded and originals:
            log.warning("Wordings for %s reply examples came back unusable; trying again later.", len(originals))
            await give_back()
            result["error"] = "unusable answer"
            break
        items = [(example_id, text) for example_id, texts in worded.items() for text in texts]
        got = await _embed([text for _, text in items], client) if items else embeddings.Embedded("", [])
        if got is None:
            log.info("Embedder not answering: wordings wait for the next run.")
            await give_back()
            result["error"] = "embedder"
            break
        if items:
            await _store_queries(pool, items, got, generated=True)
        result["examples"] += len(originals)
        result["wordings"] += len(items)
    return result


# --------------------------------------------------------------- the round

_task: Optional[asyncio.Task] = None


async def run_once(pool: asyncpg.Pool, tenant_ids: Optional[Sequence[int]] = None, *,
                   client: Optional[httpx.AsyncClient] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """One round: new turns of every account worked on, their vectors, and
    wordings for some. Each part bounded; whatever is left waits for the
    next round."""
    if tenant_ids is None:
        tenant_ids = await accounts(pool)
    if not tenant_ids:
        return {}
    owns = client is None
    http = client or httpx.AsyncClient(timeout=WORDINGS_TIMEOUT)
    summary: dict[str, Any] = {"accounts": len(tenant_ids), "added": 0}
    try:
        for tenant_id in tenant_ids:
            # One account failing (its stored config no longer validates,
            # say) does not hold up the others.
            try:
                summary["added"] += (await ingest_account(pool, tenant_id, now=now))["added"]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reply examples for tenant %s failed", tenant_id)
        embedded = await embed_pending(pool, tenant_ids, client=http)
        summary["embedded"] = embedded["embedded"]
        model = embedded["model"] or (await embeddings.health(client=http) if embedded["up"] else None)
        if model:
            summary["reembedded"] = await reembed_stale(pool, tenant_ids, model=model, client=http,
                                                        budget=max(0, EMBED_BUDGET - embedded["embedded"]))
            import platform_api  # the panel's module; read here only for the key
            summary["wordings"] = await make_wordings(pool, tenant_ids, api_key=platform_api.platform_key(),
                                                      client=http)
    finally:
        if owns:
            await http.aclose()
    log.info("Reply examples round: %s", summary)
    return summary


async def tick(pool: Any, bus: Any) -> None:
    """The scheduler's round: new pairs from the chats, and their wordings.
    At most hourly, and in the background: embedding and wording take
    minutes, and the scheduler's other rounds (reminders) must not wait."""
    global _task
    if _task is not None and not _task.done():
        return
    now = datetime.now(timezone.utc)
    last = _when((await _setting(pool, RUN_KEY) or {}).get("at"))
    if last is not None and now - last < RUN_EVERY:
        return
    tenant_ids = await accounts(pool)
    if not tenant_ids:
        return
    await _save_setting(pool, RUN_KEY, {"at": now.isoformat()})

    async def run() -> None:
        try:
            await run_once(pool, tenant_ids)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reply examples round failed")

    _task = asyncio.create_task(run(), name="reply-examples")


# --------------------------------------------------------------- retrieval


class Cache:
    """The vectors one account's replies are compared with: its own
    examples' texts and, when shared, the industry's examples without
    figures. Kept on the runtime and loaded again every CACHE_SECONDS;
    the texts themselves stay in the database until one is shown."""

    def __init__(self) -> None:
        self.key: Optional[tuple[Any, ...]] = None
        self.loaded_at = -math.inf
        self.query_ids: list[int] = []
        self.example_ids: list[int] = []
        self.tenants: list[int] = []
        self.chats: list[int] = []
        self.phases: list[str] = []
        self.models: list[str] = []
        self.vectors: list[array.array] = []
        self.lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self.vectors)

    def fresh(self, key: tuple[Any, ...]) -> bool:
        return self.key == key and time.monotonic() - self.loaded_at < CACHE_SECONDS

    async def load(self, pool: asyncpg.Pool, *, tenant_id: int, industry_id: Optional[int], share: bool,
                   limit: int = CACHE_MAX) -> "Cache":
        """The account's own examples first, then the industry's, newest
        first, at most `limit` vectors. Another industry's never: the
        account's industry is the one its owner has now."""
        rows = await pool.fetch(
            "SELECT q.id, q.example_id, q.embedding, q.embed_model, e.tenant_id, e.chat_id, e.phase "
            "FROM example_queries q JOIN reply_examples e ON e.id = q.example_id "
            "JOIN tenants t ON t.id = e.tenant_id "
            "WHERE e.active AND (e.tenant_id = $1 OR ($3 AND t.industry_id = $2 AND NOT e.has_figures)) "
            "ORDER BY (e.tenant_id = $1) DESC, e.id DESC, q.id LIMIT $4",
            tenant_id, industry_id if industry_id is not None else -1, share, limit)
        fields = ("query_ids", "example_ids", "tenants", "chats", "phases", "models")
        columns = ("id", "example_id", "tenant_id", "chat_id", "phase", "embed_model")
        for name, column in zip(fields, columns):
            setattr(self, name, [r[column] for r in rows])
        self.vectors = [array.array("f", r["embedding"]) for r in rows]
        self.key = (tenant_id, industry_id, share)
        self.loaded_at = time.monotonic()
        return self


@dataclass(frozen=True)
class Match:
    example_id: int
    similarity: float
    query_id: int


async def best(cache: Cache, vector: Sequence[float], model: str, *, phase: Optional[str], tenant_id: int,
               exclude_chat: Optional[int] = None) -> list[Match]:
    """Every example's best score over its texts, best first. Only vectors
    of the same model and, given `phase`, examples of that phase; never the
    chat being answered. Hands the event loop back every thousand vectors."""
    top: dict[int, tuple[float, int]] = {}
    query = list(vector)
    for i in range(len(cache.vectors)):
        if i % 1000 == 999:
            await asyncio.sleep(0)
        if cache.models[i] != model or (phase is not None and cache.phases[i] != phase):
            continue
        if exclude_chat is not None and cache.chats[i] == exclude_chat and cache.tenants[i] == tenant_id:
            continue
        score = math.sumprod(query, cache.vectors[i])
        held = top.get(cache.example_ids[i])
        if held is None or score > held[0]:
            top[cache.example_ids[i]] = (score, cache.query_ids[i])
    ranked = sorted(top.items(), key=lambda item: -item[1][0])
    return [Match(example_id, round(score, 4), query_id) for example_id, (score, query_id) in ranked]


async def example_rows(pool: asyncpg.Pool, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The examples to show, switched on, with their account's name."""
    rows = await pool.fetch(
        "SELECT e.*, t.name AS account FROM reply_examples e JOIN tenants t ON t.id = e.tenant_id "
        "WHERE e.id = ANY($1::bigint[]) AND e.active", list(ids))
    return {r["id"]: dict(r) for r in rows}


NOTE_HEADER = (
    "SIMILAR MOMENTS FROM EARLIER CHATS (other clients; names and numbers hidden). They show how the business "
    "handles this kind of message: what she answers, how short, in what tone. Never copy their words: write this "
    "reply fresh. They never override the sections above or the APPOINTMENTS notes; prices, times and the address "
    "only ever come from those."
)
SHOWN_CONTEXT_CHARS = 100
SHOWN_CLIENT_CHARS = 200


def _inline(text: str, limit: int) -> str:
    """One line: the messages of one side separated by " / "."""
    return _cut(" / ".join(line.strip() for line in (text or "").splitlines() if line.strip()), limit)


def render_one(example: dict[str, Any]) -> str:
    lines = []
    context = example.get("context_text") or ""
    if context == RATES:
        lines.append("(She had just sent her rates message.)")
    elif context:
        lines.append(f"(She had just said: {_inline(context, SHOWN_CONTEXT_CHARS)})")
    lines.append(f"Client: {_inline(example['client_text'], SHOWN_CLIENT_CHARS)}")
    lines.append(f"She: {_inline(example['reply_text'], MAX_REPLY_CHARS)}")
    return "- " + "\n  ".join(lines)


def render(examples: Sequence[dict[str, Any]], max_chars: int) -> tuple[str, list[dict[str, Any]]]:
    """The note, best first, within `max_chars` (the examples, not the
    header): one that does not fit is left out and a shorter one after it
    may still go in. ("", []) when none fits."""
    blocks, used, size = [], [], 0
    for example in examples:
        block = render_one(example)
        if size + len(block) + 1 > max_chars:
            continue
        blocks.append(block)
        used.append(example)
        size += len(block) + 1
    if not blocks:
        return "", []
    return NOTE_HEADER + "\n" + "\n".join(blocks), used


def pick(matches: Sequence[Match], rows: dict[int, dict[str, Any]], *, k: int, min_similarity: float,
         max_chars: int) -> tuple[str, list[dict[str, Any]]]:
    """What goes into the note: the best `k` at or above `min_similarity`."""
    chosen = [rows[m.example_id] | {"similarity": m.similarity} for m in matches
              if m.similarity >= min_similarity and m.example_id in rows][:k]
    return render(chosen, max_chars)


# ----------------------------------------------------------- the copy guard


def _shingles(text: str) -> set[str]:
    text = " ".join(_NOT_A_WORD.sub(" ", text.lower()).split())
    return {text[i:i + 3] for i in range(len(text) - 2)}


def _found_in(mine: set[str], theirs: set[str]) -> float:
    """The share of `mine` found in `theirs`: 1.0 when a line was copied
    whole from an example, however much more the example says. (Jaccard
    would let a line copied from a longer example through.)"""
    return len(mine & theirs) / len(mine) if mine else 0.0


def copies(reply: str, example_replies: Sequence[str]) -> bool:
    """Whether the reply nearly copies an example: a line of it, or the
    whole of it, of COPY_MIN_CHARS or more, COPY_SHARE of whose character
    trigrams are in one line of an example reply or in the whole of it
    (case, punctuation and emojis left out). The rates and other one-piece
    messages are left out: those are the same for everyone by design."""
    lines = [text for text, block in ai_responder.reply_pieces(reply or "") if not block]
    mine = [_shingles(text) for text in [*lines, " ".join(lines)] if len(text.strip()) >= COPY_MIN_CHARS]
    theirs: list[set[str]] = []
    for example in example_replies:
        parts = [part for part in example.splitlines() if part.strip() and part.strip() != RATES]
        theirs += [_shingles(text) for text in [*parts, " ".join(parts)] if text.strip()]
    return any(_found_in(line, other) >= COPY_SHARE for line in mine for other in theirs)


@dataclass(frozen=True)
class Shown:
    """The examples note for one reply, and what the copy guard checks the
    reply against."""
    text: str = ""
    ids: tuple[int, ...] = ()
    replies: tuple[str, ...] = ()

    def copied_in(self, reply: str) -> bool:
        return bool(self.replies) and copies(reply, self.replies)


NOTHING = Shown()


def query_text(history: Sequence[dict[str, str]], masks: Sequence[tuple[str, str]] = ()) -> str:
    """The client's messages since our last reply, masked like the stored
    examples, without photos."""
    trailing: list[str] = []
    for message in reversed(history):
        if message.get("role") != "user":
            break
        trailing.append(message.get("content") or "")
    texts = [text for text in reversed(trailing) if not is_media(text)]
    return _cut(finetune.mask(_clean("\n".join(texts)), masks), MAX_CLIENT_CHARS)


async def note_for(runtime: Any, chat_id: int, history: list[dict[str, str]]) -> Shown:
    """The examples for the reply the runtime is about to write in this
    chat, or NOTHING: retrieval off, a time waiting for the owner, nothing
    alike, or the embedder not answering in QUERY_TIMEOUT. Never a reason
    for the reply to fail."""
    settings = (runtime.config or {}).get("retrieval") or {}
    if not settings.get("enabled"):
        return NOTHING
    try:
        return await _note_for(runtime, chat_id, history, settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[%s] No examples for chat %s: %s: %s", runtime.session_id, chat_id, type(exc).__name__, exc)
        return NOTHING


async def _note_for(runtime: Any, chat_id: int, history: list[dict[str, str]], settings: dict[str, Any]) -> Shown:
    conversation = await runtime.db.get_conversation(chat_id) or {}
    peer = await wa_store.peer(runtime.pool, runtime.session_id, chat_id)
    booking = runtime.config.get("booking") or {}
    masks = finetune.masks_for(display_name=conversation.get("display_name") or "",
                               person_name=(peer or {}).get("push_name") or "",
                               username=conversation.get("username") or "", address=booking.get("address", ""),
                               way_in=booking.get("arrival_instructions", ""))
    query = query_text(history, masks)
    # A time they name is the owner's to answer: no example may suggest one.
    if not query or names_a_time(query):
        return NOTHING
    # While their time waits for the owner, only the owner answers: nothing
    # here may suggest an answer.
    phase = await phase_now(runtime.pool, runtime.tenant_id, chat_id, runtime.utcnow())
    if phase == TIME_PENDING:
        return NOTHING
    bundle = getattr(runtime, "bundle", None)
    industry_id = bundle.tenant["industry_id"] if bundle is not None else None
    key = (runtime.tenant_id, industry_id, bool(settings["share_in_industry"]))
    cache = getattr(runtime, "examples_cache", None)
    if cache is None:
        cache = runtime.examples_cache = Cache()
    if not cache.fresh(key):
        async with cache.lock:
            if not cache.fresh(key):
                await cache.load(runtime.pool, tenant_id=runtime.tenant_id, industry_id=industry_id,
                                 share=key[2])
    if not len(cache):
        return NOTHING
    embedded = await embeddings.embed([query], client=runtime.http_client, timeout=QUERY_TIMEOUT)
    if embedded is None:
        return NOTHING
    started = time.perf_counter()
    matches = await best(cache, embedded.vectors[0], embedded.model, phase=phase, tenant_id=runtime.tenant_id,
                         exclude_chat=chat_id)
    took = (time.perf_counter() - started) * 1000
    # A few spare: one switched off since the cache was loaded is skipped.
    wanted = [m for m in matches if m.similarity >= settings["min_similarity"]][:settings["k"] + 3]
    text, used = "", []
    if wanted:
        text, used = pick(wanted, await example_rows(runtime.pool, [m.example_id for m in wanted]),
                          k=settings["k"], min_similarity=settings["min_similarity"], max_chars=settings["max_chars"])
    if not used:
        log.info("[%s]   no examples alike for chat %s (%s vectors, %.0f ms).", runtime.session_id, chat_id,
                 len(cache), took)
        return NOTHING
    # Ids and scores only: never the texts.
    log.info("[%s]   examples for chat %s: %s (%s vectors, %.0f ms).", runtime.session_id, chat_id,
             ", ".join(f"#{e['id']} {e['similarity']:.2f}" for e in used), len(cache), took)
    return Shown(text=text, ids=tuple(e["id"] for e in used), replies=tuple(e["reply_text"] for e in used))


async def try_message(pool: asyncpg.Pool, *, tenant_id: int, industry_id: int, settings: dict[str, Any],
                      text: str, phase: Optional[str] = BEFORE_TIME,
                      client: Optional[httpx.AsyncClient] = None) -> Optional[dict[str, Any]]:
    """What a client message would find, switched on or not, for the admin to
    judge (examples_api): the best TRY_RESULTS, which of them the note
    would show, and the note. None when the embedder is not answering."""
    cache = await Cache().load(pool, tenant_id=tenant_id, industry_id=industry_id,
                               share=bool(settings["share_in_industry"]))
    embedded = await embeddings.embed([_cut(text.strip(), MAX_CLIENT_CHARS)], client=client, timeout=EMBED_TIMEOUT)
    if embedded is None:
        return None
    started = time.perf_counter()
    matches = (await best(cache, embedded.vectors[0], embedded.model, phase=phase, tenant_id=tenant_id))[:TRY_RESULTS]
    took = (time.perf_counter() - started) * 1000
    rows = await example_rows(pool, [m.example_id for m in matches])
    note, used = pick(matches, rows, k=settings["k"], min_similarity=settings["min_similarity"],
                      max_chars=settings["max_chars"])
    # As note_for: a message naming a time gets no examples (time_named says why).
    time_named = names_a_time(text)
    if time_named:
        note, used = "", []
    shown = {e["id"] for e in used}
    texts = {r["id"]: (r["text"], r["generated"]) for r in await pool.fetch(
        "SELECT id, text, generated FROM example_queries WHERE id = ANY($1::bigint[])", [m.query_id for m in matches])}
    results = []
    for m in matches:
        row = rows.get(m.example_id)
        if row is None:
            continue
        matched, generated = texts.get(m.query_id, ("", False))
        results.append({
            "id": row["id"], "similarity": m.similarity, "matched": matched, "generated": generated,
            "client_text": row["client_text"], "reply_text": row["reply_text"], "context_text": row["context_text"],
            "source": row["source"], "verdict": row["verdict"], "phase": row["phase"],
            "has_figures": row["has_figures"], "own": row["tenant_id"] == tenant_id, "tenant_id": row["tenant_id"],
            "account": row["account"], "chat_id": row["chat_id"], "chat": f"{row['account']}, chat {row['chat_id']}",
            "shown": row["id"] in shown,
        })
    return {"model": embedded.model, "vectors": len(cache), "ms": round(took, 1), "results": results, "note": note,
            "time_named": time_named}


# ------------------------------------------------------------------ status


async def counts(pool: asyncpg.Pool, tenant_id: int, industry_id: int) -> dict[str, Any]:
    """How many examples the account has, and of what kind."""
    row = await pool.fetchrow(
        """
        SELECT count(*) AS total, count(*) FILTER (WHERE e.active) AS active,
               count(*) FILTER (WHERE e.has_figures) AS with_figures,
               count(*) FILTER (WHERE q.example_id IS NOT NULL) AS embedded,
               count(*) FILTER (WHERE e.worded_at IS NOT NULL) AS worded,
               COALESCE(sum(w.n), 0) AS wordings
          FROM reply_examples e
          LEFT JOIN (SELECT DISTINCT example_id FROM example_queries WHERE NOT generated) q ON q.example_id = e.id
          LEFT JOIN (SELECT example_id, count(*) AS n FROM example_queries WHERE generated GROUP BY example_id) w
                 ON w.example_id = e.id
         WHERE e.tenant_id = $1
        """, tenant_id)
    by = {}
    for column in ("source", "phase"):
        by[column] = {r["k"]: r["n"] for r in await pool.fetch(
            f"SELECT {column} AS k, count(*) AS n FROM reply_examples WHERE tenant_id = $1 GROUP BY {column} "
            "ORDER BY 1", tenant_id)}
    shared = await pool.fetchval(
        "SELECT count(*) FROM reply_examples e JOIN tenants t ON t.id = e.tenant_id "
        "WHERE t.industry_id = $1 AND e.tenant_id <> $2 AND e.active AND NOT e.has_figures", industry_id, tenant_id)
    return {
        "total": row["total"], "active": row["active"], "with_figures": row["with_figures"],
        "embedded": row["embedded"], "not_embedded": row["total"] - row["embedded"],
        "worded": row["worded"], "wordings": int(row["wordings"]),
        "by_source": {s: by["source"].get(s, 0) for s in SOURCES},
        "by_phase": {p: by["phase"].get(p, 0) for p in PHASES if p != TIME_PENDING},
        "shared_in_industry": shared,
    }


async def last_ingest(pool: asyncpg.Pool, tenant_id: int) -> Optional[str]:
    return (await _setting(pool, watermark_key(tenant_id)) or {}).get("ingested_at")
