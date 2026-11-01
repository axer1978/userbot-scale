"""Admin API for the context review (context_review.py): Finetune → Context
review.

Mounted by panel.py behind the admin login. Every route is under
/api/finetune/, which staff.ADMIN_ONLY_PREFIXES keeps from every manager
whatever the role; a client's login opens nothing here.

- GET /api/finetune/context: how a reply of the account is built, piece by
  piece, with what can stop it before the model and what happens to its
  text after. No model call and no chat text: the bundle, the config, and
  what the runtime works out when it binds (the style card, the media
  library), read the same way here.
- GET /api/finetune/review-chats: the chats a review would read, each as the
  model reads it (masked), and which of them fit.
- POST /api/finetune/reviews: a review, in the background: one DeepSeek call
  on the platform key, metered as platform spend (purpose
  'context_review'). The chats stay in memory for the call only; the row
  keeps which chats were read (never their text), the numbers counted from
  the database when it started, and the model's answer as checked.
- GET /api/finetune/reviews, /reviews/{id}; DELETE /reviews/{id}.

A review still running past the model's time limit plus five minutes was
cut off by a restart of the panel (its task is gone): it is marked failed
the next time any review is read or listed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import ai_responder
import audit
import config_store
import context_review
import controls
import examples
import finetune
import finetune_api
import llm_usage
import media
import platform_api
import rules
import stats
import stylecard
import tenants
import transport
import vision
import wa_store

log = logging.getLogger("context_review_api")

router = APIRouter()
ACTOR = audit.ADMIN

# Audit events of this module (audit.py lists the older ones). Ids and counts only.
CONTEXT_REVIEW_STARTED = "context_review_started"
CONTEXT_REVIEW_REMOVED = "context_review_removed"
# llm_usage.purpose: platform spend, like the finetune runs.
PURPOSE = "context_review"

REVIEW_PATH = "/api/finetune/reviews"
MAX_OTHER_ACCOUNTS = finetune_api.MAX_HAND_ACCOUNTS
DEFAULT_DAYS, MIN_DAYS, MAX_DAYS = 30, 7, 90
MAX_LIST = 100
# The whole call, retries included; a review running past this plus five
# minutes was cut off by a restart.
MODEL_TIMEOUT_SECONDS = finetune.REQUEST_TIMEOUT_SECONDS
STALE_SECONDS = MODEL_TIMEOUT_SECONDS + 5 * 60
TEMPERATURE = 0.2
# The bot's newest lines counted for repeats (context_review.repeated_phrases).
MAX_PHRASE_ROWS = 20_000

RUNNING, DONE, FAILED = "running", "done", "failed"
STALE_ERROR = "The panel restarted during this review; start it again."
UNREADABLE = "The model's answer could not be read as a report."
CUT_OFF = "The model's answer was cut off; try fewer days or fewer accounts."
UNCHECKED = "The model's answer could not be checked; see the panel log. What came back is kept below."
TIMED_OUT = f"DeepSeek did not answer within {int(MODEL_TIMEOUT_SECONDS // 60)} minutes; start the review again."

_get_pool: Callable[[], Any] = lambda: None  # noqa: E731
_get_bus: Callable[[], Any] = lambda: None  # noqa: E731
_get_data_dir: Callable[[], Any] = lambda: None  # noqa: E731
# Keeps background reviews referenced until they finish.
_tasks: set[asyncio.Task] = set()


def bind(*, get_pool: Callable[[], Any], get_bus: Callable[[], Any],
         get_data_dir: Callable[[], Any] = lambda: None) -> None:
    global _get_pool, _get_bus, _get_data_dir
    _get_pool, _get_bus, _get_data_dir = get_pool, get_bus, get_data_dir


# ------------------------------------------------------- how a reply is built


def _media(bundle: tenants.Bundle) -> dict[str, Any]:
    """The media note as SessionRuntime.media_prompt builds it, from the
    account's library on disk, with the greeting files and the threesome
    girl's. Nothing when media is off or the panel has no data folder."""
    config, tenant = bundle.config, bundle.tenant
    root = _get_data_dir()
    if root is None or not config["media"].get("enabled", True):
        return {}
    try:
        library = media.MediaLibrary(tenants.tenant_data_dir(Path(root), tenant["id"], tenant["session_id"]) / "media")
    except OSError as exc:
        log.warning("No media library for tenant %s: %s", tenant["id"], type(exc).__name__)
        return {}
    items = library.for_ai()
    note = media.prompt_section(items, ask_before_video=config["media"].get("ask_before_video", True))
    threesome = library.threesome_items()
    if threesome:
        note = "\n".join(filter(None, [note, media.THREESOME_RULE]))
    greeting = library.greeting_items()[:media.MAX_ATTACHMENTS_PER_REPLY] if note else []
    return {"media_note": note, "media_files": len(items), "greeting": tuple(greeting),
            "threesome": tuple(threesome)}


async def _per_chat(pool: Any, session_id: Optional[str]) -> int:
    """Chats with prompt notes of their own (the Style dialog): read without
    seeding a config, unlike config_store.load."""
    if not session_id:
        return 0
    raw = await pool.fetchval("SELECT config FROM session_config WHERE session_id = $1", session_id)
    if raw is None:
        return 0
    contacts = config_store.normalize(tenants._json(raw))["contacts"]
    return sum(1 for contact in contacts.values()
               if any(contact.get(f) for f in config_store.CONTACT_TEXT_FIELDS)
               or contact.get("message_length", "auto") != "auto")


async def _notes(pool: Any, bundle: tenants.Bundle) -> context_review.Notes:
    tenant_id = bundle.tenant["id"]
    card = await stylecard.card(pool, tenant_id, bundle.config)
    counts = await rules.counts(pool, tenant_id, bundle.industry["id"])
    return context_review.Notes(
        style_note=card["note"], style_enabled=card["enabled"], style_replies=card["stats"]["messages"],
        rules_industry=counts[rules.INDUSTRY][rules.ACTIVE], rules_account=counts[rules.ACCOUNT][rules.ACTIVE],
        per_chat=await _per_chat(pool, bundle.tenant["session_id"]),
        off_reason=await controls.off_reason(pool, tenant_id), **_media(bundle))


@router.get("/api/finetune/context")
async def api_context(tenant_id: int) -> dict[str, Any]:
    """How a reply of this account is built (context_review.context_view)."""
    bundle = await platform_api.guarded(platform_api.store().bundle(tenant_id))
    return context_review.context_view(bundle, await _notes(_get_pool(), bundle))


# ------------------------------------------------------------ the chats

# The chats with a client message in the window: not a bot, not a group
# (a Telegram group's chat id is negative; a WhatsApp group is a wa_peers
# row with its group JID), the booker's chat is left out later.
_CHATS = f"""
SELECT c.tenant_id, c.session_id, c.chat_id, c.display_name, c.username, {wa_store.push_name_sql('c')} AS person_name
  FROM conversations c
 WHERE c.tenant_id = ANY($1::int[]) AND NOT c.is_bot AND c.chat_id > 0
   AND NOT EXISTS (SELECT 1 FROM wa_peers w WHERE w.session_id = c.session_id AND w.chat_id = c.chat_id
                     AND w.group_jid IS NOT NULL)
   AND EXISTS (SELECT 1 FROM messages m WHERE m.tenant_id = c.tenant_id AND m.chat_id = c.chat_id
                  AND m.direction = 'in' AND m.status = 'received' AND m.created_at >= $2 AND m.created_at <= $3)
 ORDER BY c.tenant_id, c.chat_id
"""
# Their messages in the window: the newest $8 of each chat (its end says how
# it stands), oldest first. And who sent each, from its message_sent audit
# row: machine is the rule of stats._SENT and finetune_api._HAND_MESSAGES
# (the bot or the system sent it); its reason and the row's id say which bot
# reply it belongs to (context_review.review_lines). A message's audit row is
# never older than the message.
_MESSAGES = """
WITH sent AS (
  SELECT payload->>'message_id' AS id, min(id) AS audit_id, bool_or(actor = ANY($5::text[])) AS machine,
         (array_agg(reason ORDER BY id))[1] AS reason
    FROM audit_log WHERE tenant_id = ANY($1::int[]) AND event = $4 AND created_at >= $2
   GROUP BY 1
), numbered AS (
  SELECT m.tenant_id, m.chat_id, m.id, m.direction, m.status, m.text, m.attachments, m.created_at, m.llm_model,
         m.draft_text, coalesce(sent.machine, false) AS machine, sent.reason AS sent_reason,
         sent.audit_id AS sent_audit,
         row_number() OVER (PARTITION BY m.tenant_id, m.chat_id ORDER BY m.id DESC) AS back
    FROM messages m LEFT JOIN sent ON sent.id = m.id::text
   WHERE (m.tenant_id, m.chat_id) IN (SELECT * FROM unnest($6::int[], $7::bigint[]))
     AND m.created_at >= $2 AND m.created_at <= $3
)
SELECT tenant_id, chat_id, id, direction, status, text, attachments, created_at, llm_model, draft_text, machine,
       sent_reason, sent_audit
  FROM numbered WHERE back <= $8 ORDER BY tenant_id, chat_id, id
"""
# The drafts a person changed before approving them (session_runtime's
# approve_draft writes draft_approved last, after every message it sent).
_EDITED_APPROVALS = """
SELECT id, tenant_id, (payload->>'chat_id')::bigint AS chat_id, (payload->>'message_id')::bigint AS message_id
  FROM audit_log WHERE tenant_id = ANY($1::int[]) AND event = $2 AND payload->>'edited' = 'true' AND created_at >= $3
"""
_BOT_LINES = """
SELECT tenant_id, chat_id, text FROM messages
 WHERE tenant_id = ANY($1::int[]) AND created_at >= $2 AND created_at <= $3
   AND direction = 'out' AND status = 'sent' AND llm_model IS NOT NULL
 ORDER BY id DESC LIMIT $4
"""
_PEOPLE = f"""
SELECT c.tenant_id, c.chat_id, c.display_name, c.username, {wa_store.push_name_sql('c')} AS person_name
  FROM conversations c WHERE (c.tenant_id, c.chat_id) IN (SELECT * FROM unnest($1::int[], $2::bigint[]))
"""


@dataclass
class _Gathered:
    bundles: dict[int, tenants.Bundle]
    since: datetime
    until: datetime
    days: int
    # Planned (context_review.plan), each rendered as the model reads it.
    chats: list[dict[str, Any]]

    @property
    def picked(self) -> list[dict[str, Any]]:
        return [c for c in self.chats if c["picked"]]


async def _bundles(tenant_id: int, also: Sequence[int]) -> dict[int, tenants.Bundle]:
    """The account under review first, then the others, each once."""
    s = platform_api.store()
    bundles = {tenant_id: await platform_api.guarded(s.bundle(tenant_id))}
    others = [t for t in dict.fromkeys(also) if t != tenant_id]
    if len(others) > MAX_OTHER_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_OTHER_ACCOUNTS} other accounts per review.")
    for other in others:
        try:
            bundles[other] = await platform_api.guarded(s.bundle(other))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise HTTPException(status_code=400, detail=f"Account #{other} was not found; reload the page and "
                                                        "pick the other accounts again.") from None
    return bundles


def _owner_chats(bundle: tenants.Bundle, provider_chats: set[int]) -> Callable[[Any], bool]:
    """Whether a conversation is the booker's chat (booking commands, not a
    client): one that requests went to, or booking.provider itself."""
    owner_id, owner_name = examples.owner_chat(bundle.config["booking"].get("provider", ""))
    service = transport.TELEGRAM_SERVICE_ID if bundle.tenant.get("channel") != transport.WHATSAPP else None

    def is_owner(person: Any) -> bool:
        chat_id = person["chat_id"]
        return (chat_id in provider_chats or chat_id == owner_id or chat_id == service
                or (bool(owner_name) and (person["username"] or "").lower() == owner_name))
    return is_owner


async def _gather(tenant_id: int, also: Sequence[int], days: int, now: Optional[datetime] = None) -> _Gathered:
    """The chats a review of this account (and `also`) over the last `days`
    reads: each rendered as the model reads it, and planned."""
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    bundles = await _bundles(tenant_id, also)
    ids = list(bundles)
    async with _get_pool().acquire() as con:
        people = await con.fetch(_CHATS, ids, since, until)
        providers = await con.fetch("SELECT DISTINCT tenant_id, provider_chat_id FROM bookings "
                                    "WHERE tenant_id = ANY($1::int[]) AND provider_chat_id IS NOT NULL", ids)
        owners = {t: _owner_chats(b, {r["provider_chat_id"] for r in providers if r["tenant_id"] == t})
                  for t, b in bundles.items()}
        people = [p for p in people if not owners[p["tenant_id"]](p)]
        keys = [(p["tenant_id"], p["chat_id"]) for p in people]
        rows = await con.fetch(_MESSAGES, ids, since, until, audit.MESSAGE_SENT, finetune_api._MACHINE_ACTORS,
                               [k[0] for k in keys], [k[1] for k in keys], finetune_api._RAW_ROWS) if keys else []
        booked = await con.fetch("SELECT tenant_id, chat_id, state FROM bookings WHERE tenant_id = ANY($1::int[]) "
                                 "AND created_at >= $2 AND created_at <= $3", ids, since, until)
        taken = await con.fetch("SELECT DISTINCT tenant_id, payload->>'chat_id' AS chat_id FROM audit_log "
                                "WHERE tenant_id = ANY($1::int[]) AND event = $2 AND created_at >= $3 "
                                "AND created_at <= $4", ids, audit.HUMAN_TAKEOVER, since, until)
        edited = await con.fetch(_EDITED_APPROVALS, ids, audit.DRAFT_APPROVED, since) if keys else []
    approvals: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for a in edited:
        approvals.setdefault((a["tenant_id"], a["chat_id"]), []).append((a["id"], a["message_id"]))
    messages: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for r in rows:
        messages.setdefault((r["tenant_id"], r["chat_id"]), []).append(dict(r))
    states: dict[tuple[int, int], list[str]] = {}
    for b in booked:
        states.setdefault((b["tenant_id"], b["chat_id"]), []).append(b["state"])
    taken_over = {(t["tenant_id"], str(t["chat_id"])) for t in taken}

    chats = []
    for person in people:
        key = (person["tenant_id"], person["chat_id"])
        lines = context_review.review_lines(messages.get(key, []), approvals.get(key, ()))
        said = [line for line in lines if line["kind"] != context_review.EVENT]
        if not said:
            continue
        own = bundles[key[0]]
        masks, tz = finetune_api._masks(own, person), finetune_api._tz(own)
        result = context_review.outcome(states.get(key, ()), lines, until)
        header = partial(context_review.chat_header, account=own.tenant["name"], messages=len(said), outcome=result,
                         answer=context_review.first_answer(lines),
                         taken_over=(key[0], str(key[1])) in taken_over)
        render = partial(context_review.render_chat, lines, masks=masks, tz=tz)
        chats.append({
            "key": f"{key[0]}:{key[1]}", "tenant_id": key[0], "session_id": person["session_id"],
            "chat_id": key[1], "account": own.tenant["name"], "display_name": person["display_name"] or "",
            "outcome": result, "outcome_label": context_review.OUTCOME_LABELS[result], "messages": len(said),
            # Sized with the widest ref, so the real one never makes it longer.
            "chars": len(render(header=header(ref=f"C{finetune.MAX_CHATS}"))),
            "last_at": said[-1]["at"].isoformat() if isinstance(said[-1]["at"], datetime) else str(said[-1]["at"]),
            "lines": context_review.line_counts(lines), "_render": render, "_header": header, "_masks": masks,
        })
    planned = context_review.plan(chats, [(t, b.tenant["name"]) for t, b in bundles.items()])
    for chat in planned:
        render, header = chat.pop("_render"), chat.pop("_header")
        # Only what the model reads: a chat that does not fit has its size, not its text.
        chat["text"] = render(header=header(ref=chat["ref"])) if chat["picked"] else ""
        if chat["picked"]:
            chat["chars"] = len(chat["text"])
    return _Gathered(bundles=bundles, since=since, until=until, days=days, chats=planned)


_CHAT_FIELDS = ("key", "ref", "tenant_id", "session_id", "chat_id", "account", "display_name", "outcome",
                "outcome_label", "messages", "chars", "picked", "text")


@router.get("/api/finetune/review-chats")
async def api_review_chats(tenant_id: int, also: list[int] = Query(default=[]),
                           days: int = Query(default=DEFAULT_DAYS, ge=MIN_DAYS, le=MAX_DAYS)) -> dict[str, Any]:
    """The chats a review would read (picked) or could read, with what the
    model reads for each picked one ("" for the others), and a count per
    account."""
    gathered = await _gather(tenant_id, also, days)
    accounts = {t: {"tenant_id": t, "name": b.tenant["name"], "chats": 0, "picked": 0}
                for t, b in gathered.bundles.items()}
    for chat in gathered.chats:
        accounts[chat["tenant_id"]]["chats"] += 1
        accounts[chat["tenant_id"]]["picked"] += int(chat["picked"])
    return {"accounts": list(accounts.values()),
            "chats": [{k: chat[k] for k in _CHAT_FIELDS} for chat in gathered.chats],
            "limits": {"chats": finetune.MAX_CHATS, "chars": finetune.MAX_TRANSCRIPT_CHARS}, "days": days}


# --------------------------------------------------------- exact numbers


async def _phrases(con: Any, gathered: _Gathered) -> list[dict[str, Any]]:
    """The bot's repeated lines over every chat of the accounts in the window."""
    ids = list(gathered.bundles)
    rows = [dict(r) for r in await con.fetch(_BOT_LINES, ids, gathered.since, gathered.until, MAX_PHRASE_ROWS)]
    pairs = sorted({(r["tenant_id"], r["chat_id"]) for r in rows})
    people = await con.fetch(_PEOPLE, [p[0] for p in pairs], [p[1] for p in pairs]) if pairs else []
    chat_masks = {(p["tenant_id"], p["chat_id"]): finetune_api._masks(gathered.bundles[p["tenant_id"]], p)
                  for p in people}
    account_masks = {}
    for tenant_id, bundle in gathered.bundles.items():
        booking = bundle.config.get("booking") or {}
        account_masks[tenant_id] = finetune.masks_for(address=booking.get("address", ""),
                                                      way_in=booking.get("arrival_instructions", ""))
    return context_review.repeated_phrases(
        rows, prompts={t: b.prompt.text for t, b in gathered.bundles.items()}, account_masks=account_masks,
        chat_masks=chat_masks)


async def _counts(gathered: _Gathered) -> dict[str, Any]:
    """The numbers beside the model's reading (ReviewCounts), all from the
    database: outcomes and lines over the chats read; drafts, takeovers and
    unanswered over every chat of each account (stats.quality: approved
    counts the edited ones too)."""
    picked = gathered.picked
    by_account = []
    quality = []
    pool = _get_pool()
    for tenant_id, bundle in gathered.bundles.items():
        name = bundle.tenant["name"]
        own = [c for c in gathered.chats if c["tenant_id"] == tenant_id]
        by_account.append({"tenant_id": tenant_id, "name": name, "in_window": len(own),
                           "read": sum(1 for c in own if c["picked"])})
        q = await stats.quality(pool, tenant_id, gathered.since, gathered.until)
        quality.append({"tenant_id": tenant_id, "name": name, "replies_written": q["replies_written"],
                        "drafts": q["drafts"], "takeovers": q["takeovers"],
                        "unanswered_by_reason": q["unanswered_by_reason"]})
    lines = {kind: sum(c["lines"][kind] for c in picked) for kind in context_review.LINE_KINDS}
    async with pool.acquire() as con:
        phrases = await _phrases(con, gathered)
    return {
        "window": {"since": gathered.since.isoformat(timespec="seconds"),
                   "until": gathered.until.isoformat(timespec="seconds"), "days": gathered.days},
        "chats": {"in_window": len(gathered.chats), "read": len(picked), "by_account": by_account},
        "outcomes": {o: sum(1 for c in picked if c["outcome"] == o) for o in context_review.OUTCOMES},
        "lines": lines, "quality": quality, "phrases": phrases,
    }


# --------------------------------------------------------------- reviews


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


SECTIONS = [{"key": key, "heading": context_review.BOX_HEADINGS[key]} for key in context_review.BOX_KEYS]


def _review(row: Any, *, full: bool = True) -> dict[str, Any]:
    chats = tenants._json(row["chats"]) or []
    review = {
        "id": row["id"], "tenant_id": row["tenant_id"], "also_tenant_ids": tenants._json(row["also_tenant_ids"]) or [],
        "days": row["days"], "status": row["status"], "model": row["model"], "chats_read": len(chats),
        "created_at": _iso(row["created_at"]), "finished_at": _iso(row["finished_at"]), "error": row["error"],
    }
    if full:
        report = tenants._json(row["report"])
        review.update({
            "chats": [{**c, "outcome_label": context_review.OUTCOME_LABELS.get(c["outcome"], c["outcome"])}
                      for c in chats],
            "counts": tenants._json(row["counts"]), "report": report,
            "errors": tenants._json(row["errors"]) or [],
            # What came back, only when it could not be read as a report.
            "raw_output": row["raw_output"] if report is None else "",
            "created_by": row["created_by"], "sections": SECTIONS,
        })
    return review


async def _settle_stale(pool: Any) -> None:
    """Reviews cut off by a restart of the panel: their task is gone."""
    await pool.execute(
        "UPDATE context_reviews SET status = $1, error = $2, finished_at = now() "
        "WHERE status = $3 AND created_at < now() - make_interval(secs => $4)",
        FAILED, STALE_ERROR, RUNNING, float(STALE_SECONDS))


async def _get_review(review_id: int, pool: Any = None) -> dict[str, Any]:
    pool = pool or _get_pool()
    await _settle_stale(pool)
    row = await pool.fetchrow("SELECT * FROM context_reviews WHERE id = $1", review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown context review")
    return _review(row)


@router.get(REVIEW_PATH)
async def api_reviews(tenant_id: Optional[int] = None,
                      limit: int = Query(default=20, ge=1, le=MAX_LIST)) -> list[dict[str, Any]]:
    """The account's reviews, newest first, without their report."""
    pool = _get_pool()
    await _settle_stale(pool)
    if tenant_id is None:
        rows = await pool.fetch("SELECT * FROM context_reviews ORDER BY id DESC LIMIT $1", limit)
    else:
        rows = await pool.fetch("SELECT * FROM context_reviews WHERE tenant_id = $1 ORDER BY id DESC LIMIT $2",
                                tenant_id, limit)
    return [_review(r, full=False) for r in rows]


@router.get(REVIEW_PATH + "/{review_id}")
async def api_review(review_id: int) -> dict[str, Any]:
    return await _get_review(review_id)


class ReviewBody(BaseModel):
    tenant_id: int
    also_tenant_ids: list[int] = Field(default_factory=list, max_length=MAX_OTHER_ACCOUNTS)
    days: int = Field(DEFAULT_DAYS, ge=MIN_DAYS, le=MAX_DAYS)


async def _running(con: Any, tenant_id: int) -> Optional[int]:
    return await con.fetchval("SELECT id FROM context_reviews WHERE tenant_id = $1 AND status = $2", tenant_id, RUNNING)


@router.post(REVIEW_PATH)
async def api_start(body: ReviewBody) -> dict[str, Any]:
    """Start a review of the account's chats over the last `days`, with
    those of the other accounts if any. It runs in the background; the page
    polls it."""
    pool = _get_pool()
    await platform_api.guarded(platform_api.store().get(body.tenant_id))
    api_key = platform_api.platform_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_PLATFORM_KEY must be set in .env to read chats.")
    await _settle_stale(pool)
    if await _running(pool, body.tenant_id) is not None:
        raise HTTPException(status_code=409, detail="A review of this account is running; wait for it to finish.")
    gathered = await _gather(body.tenant_id, body.also_tenant_ids, body.days)
    picked = gathered.picked
    if not picked:
        where = " in these accounts" if len(gathered.bundles) > 1 else ""
        raise HTTPException(status_code=400, detail=f"No chats with a client message in the last {body.days} days"
                                                    f"{where}: there is nothing to review yet.")
    bundle = gathered.bundles[body.tenant_id]
    others = [t for t in gathered.bundles if t != body.tenant_id]
    messages = context_review.review_messages(
        account=bundle.tenant["name"], boxes=context_review.review_boxes(bundle), chats=[c["text"] for c in picked],
        others=[gathered.bundles[t].tenant["name"] for t in others])
    counts = await _counts(gathered)
    refs = [{k: c[k] for k in ("ref", "tenant_id", "session_id", "chat_id", "account", "outcome")} for c in picked]
    model = finetune.text_model()

    async with pool.acquire() as con, con.transaction():
        # Two clicks at once count each other.
        await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"context_review:{body.tenant_id}")
        if await _running(con, body.tenant_id) is not None:
            raise HTTPException(status_code=409, detail="A review of this account is running; wait for it to "
                                                        "finish.")
        review_id = await con.fetchval(
            "INSERT INTO context_reviews (tenant_id, also_tenant_ids, days, model, chats, counts, created_by) "
            "VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6::jsonb, $7) RETURNING id",
            body.tenant_id, json.dumps(others), body.days, model, json.dumps(refs), json.dumps(counts), ACTOR)
        await audit.record(con, tenant_id=body.tenant_id, actor=ACTOR, event=CONTEXT_REVIEW_STARTED, payload={
            "review_id": review_id, "also_tenant_ids": others, "days": body.days, "chats": len(picked)})
    # Whatever the model writes is stored through the masks of every chat it read.
    masks = sorted({m for c in picked for m in c["_masks"]}, key=lambda item: -len(item[0]))
    task = asyncio.create_task(_execute(review_id, messages, [c["ref"] for c in picked], api_key=api_key,
                                        model=model, pool=pool, masks=masks))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return await _get_review(review_id, pool)


@router.delete(REVIEW_PATH + "/{review_id}")
async def api_remove(review_id: int) -> dict[str, Any]:
    review = await _get_review(review_id)
    if review["status"] == RUNNING:
        raise HTTPException(status_code=409, detail="This review is still running; remove it once it has finished.")
    pool = _get_pool()
    async with pool.acquire() as con, con.transaction():
        await con.execute("DELETE FROM context_reviews WHERE id = $1", review_id)
        await audit.record(con, tenant_id=review["tenant_id"], actor=ACTOR, event=CONTEXT_REVIEW_REMOVED,
                           payload={"review_id": review_id})
    return {"removed": review_id}


# -------------------------------------------------------------- the model


async def complete(messages: list[dict[str, str]], *, api_key: str, model: str,
                   usage_sink: Optional[ai_responder.UsageSink] = None,
                   client: Optional[httpx.AsyncClient] = None) -> str:
    """The model's raw answer, asked for as one JSON object. DeepSeek speaks
    the chat-completions format vision.py's call uses, which allows a long
    answer the time it takes, as finetune.complete_text does. Raises
    vision.VisionError with a safe message."""
    if not api_key:
        raise vision.VisionError("DEEPSEEK_PLATFORM_KEY is not set, so chats cannot be read.")
    owns = client is None
    http = client or httpx.AsyncClient(timeout=finetune.REQUEST_TIMEOUT_SECONDS)
    try:
        text = await vision._complete(
            api_url=ai_responder.API_URL, api_key=api_key, client=http, usage_sink=usage_sink,
            service="DeepSeek API", key_name="DEEPSEEK_PLATFORM_KEY",
            # DeepSeek's chat model writes at most 8K tokens (finetune.text_max_tokens).
            payload={"model": model, "messages": messages, "max_tokens": finetune.text_max_tokens(),
                     "temperature": TEMPERATURE, "response_format": {"type": "json_object"}},
        )
    finally:
        if owns:
            await http.aclose()
    if not text:
        raise vision.VisionError("The model returned an empty answer.")
    return text


async def _execute(review_id: int, messages: list[dict[str, str]], refs: list[str], *, api_key: str, model: str,
                   pool: Any, masks: Sequence[tuple[str, str]] = ()) -> None:
    async def meter(used_model: str, usage: dict[str, int]) -> None:
        # Platform spend, like a finetune run: the admin started it.
        await llm_usage.record(pool, tenant_id=None, purpose=PURPOSE, model=used_model, usage=usage)

    try:
        raw = await asyncio.wait_for(complete(messages, api_key=api_key, model=model, usage_sink=meter),
                                     MODEL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await _finish(review_id, status=FAILED, error=TIMED_OUT, pool=pool)
        return
    except vision.VisionError as exc:
        await _finish(review_id, status=FAILED, error=str(exc), pool=pool)
        return
    except Exception as exc:
        # Its type only: an exception's text could quote what it was reading.
        log.error("Context review %s failed: %s", review_id, type(exc).__name__)
        await _finish(review_id, status=FAILED, error="Unexpected error; see the panel log.", pool=pool)
        return
    raw = context_review.mask_report(raw, masks)
    try:
        report, errors = context_review.parse_report(raw, refs)
        if report is None:
            error = CUT_OFF if errors == [context_review.CUT_OFF] else UNREADABLE
            await _finish(review_id, status=FAILED, raw=raw, errors=errors, error=error, pool=pool)
        else:
            await _finish(review_id, status=DONE, report=context_review.mask_report(report, masks), errors=errors,
                          pool=pool)
    except Exception as exc:
        # Never left running: the answer is kept for the admin to read.
        log.error("Context review %s: the answer could not be checked: %s", review_id, type(exc).__name__)
        await _finish(review_id, status=FAILED, raw=raw, error=UNCHECKED, pool=pool)


async def _finish(review_id: int, *, status: str, pool: Any, report: Optional[dict[str, Any]] = None,
                  raw: str = "", errors: Sequence[str] = (), error: str = "") -> None:
    """The review's end. The model's raw answer is kept only when it could
    not be read: a report holds what it says, checked."""
    await pool.execute(
        "UPDATE context_reviews SET status = $2, report = $3::jsonb, raw_output = $4, errors = $5::jsonb, "
        "error = $6, finished_at = now() WHERE id = $1 AND status = 'running'",
        review_id, status, json.dumps(report) if report is not None else None, raw, json.dumps(list(errors)), error)
