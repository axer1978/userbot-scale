"""Operator corrections as rules (Step 2 of learning from real chats).

The bot cannot be weight-finetuned (DeepSeek is an API), so it gets better
through its prompt. A rule is one short sentence the bot must follow:

- proposed by the model (suggest) from a draft an operator edited before
  approving it (messages.draft_text) or from a correction on a flagged chat
  (finetune_examples.corrections), each read with a few masked lines of the
  chat before it; or typed by the admin (created active);
- accepted by the admin (Finetune → Rules), for every account of the
  industry (the usual scope: the escort accounts share one tuning) or for
  one account;
- while active, added to every reply's prompt (prompt_layers.render, via
  TenantStore.bundle), at most CAP per scope, so the prompt cannot grow
  without limit;
- folded into the prompt itself (fold): an ordinary finetune run (source
  'rules') that rewrites the industry standard and the account's business
  layer with the rules in them, reviewed and applied in Finetune → Runs.
  Applying it marks the rules merged (on_applied), and they stop being
  added separately.

The weekly round (tick) starts the folds by itself, see its docstring.

Rules never hold chat text: the model writes them from masked context (the
client's name and username, the business's address and way in, numbers and
e-mails), is told never to put names, numbers, addresses or door codes in
one, and a proposal that still has a masked token or a phone number in it
is dropped. The admin reads every one before it goes live.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import asyncpg
import httpx

import ai_responder
import audit
import config_assist
import finetune
import finetune_api
import llm_usage
import platform_api
import prompt_layers
import tenants
import vision

log = logging.getLogger("rules")

# Statuses (migration 0017).
PROPOSED = "proposed"
ACTIVE = "active"
MERGED = "merged"
REJECTED = "rejected"
RETIRED = "retired"
STATUSES = (PROPOSED, ACTIVE, MERGED, REJECTED, RETIRED)
# What the admin can set; merged is set only by applying a fold.
SETTABLE = (PROPOSED, ACTIVE, REJECTED, RETIRED)

DRAFT_EDIT = "draft_edit"
CORRECTION = "correction"
MANUAL = "manual"
# An instruction for every customer the booker wrote in her booking chat
# (booking_flow.BookingFlow.save_standing).
BOOKER = "booker"

INDUSTRY = "industry"
ACCOUNT = "account"
SCOPES = (INDUSTRY, ACCOUNT)

MIN_CHARS = 5
MAX_CHARS = 400
# Active rules per scope (one industry, or one account). Each is a line in
# every reply's prompt; past this, fold them into the prompt first.
CAP = 25
# created_by of a rule the model proposed.
SUGGESTED = "suggested"

# Audit events of this module (audit.py lists the older ones).
RULE_CREATED = "prompt_rule_created"
RULE_CHANGED = "prompt_rule_changed"
RULES_SUGGESTED = "prompt_rules_suggested"
RULES_MERGED = "prompt_rules_merged"


class RuleError(ValueError):
    """Something about a rule the admin asked for that cannot be done."""


class CapReached(RuleError):
    pass


class RuleConflict(RuleError):
    """The rule is in a state that does not allow this (it was merged)."""


# ------------------------------------------------------------------ store


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _rule(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"], "industry_id": row["industry_id"], "tenant_id": row["tenant_id"],
        "scope": ACCOUNT if row["tenant_id"] is not None else INDUSTRY,
        "section": row["section"], "text": row["text"], "source": row["source"],
        "source_ref": _json(row["source_ref"]) or {}, "status": row["status"],
        "merged_into": row["merged_into"], "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat(timespec="seconds"),
        "updated_at": row["updated_at"].isoformat(timespec="seconds"),
    }


def clean_text(text: Any) -> str:
    if not isinstance(text, str):
        raise RuleError("A rule is a sentence of text.")
    text = " ".join(text.split())
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise RuleError(f"A rule is {MIN_CHARS} to {MAX_CHARS} characters; this one is {len(text)}.")
    return text


def check_section(section: Any) -> str:
    if section not in prompt_layers.SECTION_KEYS:
        raise RuleError(f"Unknown section {section!r}; sections are {', '.join(prompt_layers.SECTION_KEYS)}.")
    return section


def _scope_name(tenant_id: Optional[int]) -> str:
    return "this account" if tenant_id is not None else "the industry"


async def get(pool: Any, rule_id: int) -> dict[str, Any]:
    row = await pool.fetchrow("SELECT * FROM prompt_rules WHERE id = $1", rule_id)
    if row is None:
        raise LookupError(f"no rule {rule_id}")
    return _rule(row)


async def list_for(pool: Any, tenant_id: int, industry_id: int) -> list[dict[str, Any]]:
    """The rules that reach this account, in every status: its industry's
    and its own, oldest first."""
    rows = await pool.fetch(
        "SELECT * FROM prompt_rules WHERE (tenant_id IS NULL AND industry_id = $2) OR tenant_id = $1 ORDER BY id",
        tenant_id, industry_id)
    return [_rule(r) for r in rows]


async def active_for(pool: Any, tenant_id: int, industry_id: int) -> list[str]:
    """The texts of the active rules for this account, as they go into its
    prompt: the industry's first, then its own, each oldest first, so the
    order (and the version tag's hash) only changes when the rules do."""
    rows = await pool.fetch(
        "SELECT text FROM prompt_rules WHERE status = 'active' "
        "AND ((tenant_id IS NULL AND industry_id = $2) OR tenant_id = $1) "
        "ORDER BY (tenant_id IS NOT NULL), id",
        tenant_id, industry_id)
    return [r["text"] for r in rows]


async def counts(pool: Any, tenant_id: int, industry_id: int) -> dict[str, dict[str, int]]:
    """Active and proposed rules per scope, for the cap."""
    rows = await pool.fetch(
        "SELECT (tenant_id IS NOT NULL) AS own, status, count(*) AS n FROM prompt_rules "
        "WHERE ((tenant_id IS NULL AND industry_id = $2) OR tenant_id = $1) AND status IN ('active', 'proposed') "
        "GROUP BY 1, 2", tenant_id, industry_id)
    out = {INDUSTRY: {ACTIVE: 0, PROPOSED: 0}, ACCOUNT: {ACTIVE: 0, PROPOSED: 0}}
    for r in rows:
        out[ACCOUNT if r["own"] else INDUSTRY][r["status"]] = r["n"]
    return out


async def _check_cap(con: asyncpg.Connection, industry_id: int, tenant_id: Optional[int]) -> None:
    """Refuse one more active rule in a scope that has CAP. The lock makes
    two activations at once count each other."""
    await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"prompt_rules:{industry_id}:{tenant_id or 0}")
    if tenant_id is None:
        active = await con.fetchval("SELECT count(*) FROM prompt_rules WHERE status = 'active' "
                                    "AND tenant_id IS NULL AND industry_id = $1", industry_id)
    else:
        active = await con.fetchval("SELECT count(*) FROM prompt_rules WHERE status = 'active' AND tenant_id = $1",
                                    tenant_id)
    if active >= CAP:
        raise CapReached(
            f"{_scope_name(tenant_id).capitalize()} already has {CAP} active rules, the most every reply's prompt "
            f"carries. Fold them into the prompt first (Fold now, then apply the run), or retire one.")


async def create(pool: Any, *, industry_id: int, tenant_id: Optional[int], section: str, text: str, source: str,
                 source_ref: dict[str, Any], status: str, created_by: str, actor: str,
                 con: Optional[asyncpg.Connection] = None) -> dict[str, Any]:
    """One new rule, audited. `con`: inside the caller's transaction."""
    text, section = clean_text(text), check_section(section)
    if status not in (PROPOSED, ACTIVE):
        raise RuleError("A new rule is proposed or active.")
    if con is None:
        async with pool.acquire() as con, con.transaction():
            return await create(pool, industry_id=industry_id, tenant_id=tenant_id, section=section, text=text,
                                source=source, source_ref=source_ref, status=status, created_by=created_by,
                                actor=actor, con=con)
    if status == ACTIVE:
        await _check_cap(con, industry_id, tenant_id)
    row = await con.fetchrow(
        "INSERT INTO prompt_rules (industry_id, tenant_id, section, text, source, source_ref, status, created_by) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) RETURNING *",
        industry_id, tenant_id, section, text, source, json.dumps(source_ref), status, created_by)
    rule = _rule(row)
    # The text too: a rule is a few generic words, and its history lives here.
    await audit.record(con, tenant_id=tenant_id, actor=actor, event=RULE_CREATED, payload={
        "rule_id": rule["id"], "industry_id": industry_id, "scope": rule["scope"], "section": section,
        "status": status, "source": source, "text": text})
    return rule


async def update(pool: Any, rule_id: int, *, actor: str, text: Optional[str] = None,
                 section: Optional[str] = None, scope: Optional[str] = None, status: Optional[str] = None,
                 tenant_id: Optional[int] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Change a rule's text, section, scope or status; (before, after).

    A merged rule is part of a prompt version now and cannot change. Making
    a rule active, or moving an active one to the other scope, counts
    against that scope's cap. Moving it to one account needs the account:
    `tenant_id`, or the one it came from."""
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow("SELECT * FROM prompt_rules WHERE id = $1 FOR UPDATE", rule_id)
        if row is None:
            raise LookupError(f"no rule {rule_id}")
        before = _rule(row)
        if before["status"] == MERGED:
            raise RuleConflict(f"This rule is already folded into the prompt ({before['merged_into']}). "
                               "Change the prompt itself instead.")
        changes: dict[str, Any] = {}
        if text is not None and (text := clean_text(text)) != before["text"]:
            changes["text"] = text
        if section is not None and check_section(section) != before["section"]:
            changes["section"] = section
        if status is not None and status != before["status"]:
            if status not in SETTABLE:
                raise RuleError(f"A rule can be set to {', '.join(SETTABLE)}; merged comes from applying a fold.")
            changes["status"] = status
        if scope is not None and scope != before["scope"]:
            if scope not in SCOPES:
                raise RuleError("The scope is industry or account.")
            if scope == INDUSTRY:
                changes["tenant_id"] = None
            else:
                owner = tenant_id if tenant_id is not None else before["source_ref"].get("tenant_id")
                if owner is None:
                    raise RuleError("Say which account the rule is for.")
                industry_of = await con.fetchval("SELECT industry_id FROM tenants WHERE id = $1", owner)
                if industry_of != before["industry_id"]:
                    raise RuleError("That account is not in this rule's industry.")
                changes["tenant_id"] = owner
        if not changes:
            return before, before
        new_status = changes.get("status", before["status"])
        new_tenant = changes["tenant_id"] if "tenant_id" in changes else before["tenant_id"]
        if new_status == ACTIVE and (before["status"] != ACTIVE or "tenant_id" in changes):
            await _check_cap(con, before["industry_id"], new_tenant)
        sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changes))
        row = await con.fetchrow(f"UPDATE prompt_rules SET {sets}, updated_at = now() WHERE id = $1 RETURNING *",
                                 rule_id, *changes.values())
        after = _rule(row)
        await audit.record(con, tenant_id=after["tenant_id"] or before["tenant_id"], actor=actor, event=RULE_CHANGED,
                           payload={"rule_id": rule_id, "from": {k: before[k] for k in changes},
                                    "to": changes})
    return before, after


def changes_prompt(before: Optional[dict[str, Any]], after: dict[str, Any]) -> bool:
    """Whether the change alters what goes into replies' prompts: an active
    rule appeared, went, or changed its text or scope."""
    if before is None:
        return after["status"] == ACTIVE
    if ACTIVE not in (before["status"], after["status"]):
        return False
    return any(before[k] != after[k] for k in ("status", "text", "tenant_id"))


# -------------------------------------------------------------- suggestion

MAX_BATCH = 20
# Chat lines read before the edited or corrected message, and kept of them.
_CONTEXT_ROWS = 40
CONTEXT_LINES = 6
MAX_LINE_CHARS = 300
MAX_TEXT_CHARS = 700
SUGGEST_MAX_TOKENS = 2_000
# The existing rules the model is shown, so it does not propose them again.
MAX_SHOWN_RULES = 60
DEFAULT_SECTION = "booking"

# Filled with str.format: {industry}, {sections}, {existing}, {items}.
SUGGEST_PROMPT = (
    "You help the operator of a chat assistant that answers clients for businesses in the {industry} industry. "
    "Below are moments where the operator corrected the assistant: a draft the operator changed before sending it, "
    "or a correction the operator wrote on a chat. For each item, write ONE general rule the assistant should "
    "follow from now on, or none.\n\n"
    "A rule is:\n"
    "- one short sentence, in the business's own terms, telling the assistant what to do (e.g. \"When he asks for "
    "photos before naming a time, send them and ask what time he comes.\")\n"
    "- general: about a kind of situation, never about one client, one time, one day or one price\n"
    "- never with names, numbers, prices, times, addresses or door codes, and never [client], [number] or other "
    "masked details\n"
    "- never weaker than the platform rules or the boundaries: never a rule that lets the assistant confirm a time "
    "itself (only the owner does, in the APPOINTMENTS notes), type an address or door code (the system sends "
    "[address] and the way in), change or reword the rates or extras messages (they go out word for word inside "
    "[block]...[/block]), or send the same text to many clients\n"
    "- none for a typo, a pure style change (spelling, capitals, an emoji, word order, a little shorter) or a "
    "one-off case: answer null for those\n\n"
    "Also say where it belongs in the prompt, \"section\": one of {sections}; and its \"scope\": \"industry\" when "
    "every business of the industry should follow it (how to talk, the booking steps, qualifying clients), "
    "\"account\" when it is about this business only (its own services, prices, hours or place).\n\n"
    "RULES ALREADY IN PLACE (never propose these again, or the same in other words):\n"
    "{existing}\n\n"
    "Answer with ONE JSON object and nothing else, one entry per item:\n"
    "{{\"rules\": [{{\"item\": 1, \"rule\": \"...\", \"section\": \"booking\", \"scope\": \"industry\"}}, "
    "{{\"item\": 2, \"rule\": null}}]}}\n\n"
    "{items}"
)


@dataclass
class Item:
    """One edit or correction for the model, rendered and masked."""
    source: str
    message_id: int
    example_id: Optional[int]
    text: str = ""


_PENDING_EDITS = """
SELECT m.id, m.chat_id, m.text, m.draft_text, m.llm_model FROM messages m
 WHERE m.tenant_id = $1 AND m.draft_text IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM prompt_rules r
                    WHERE r.source = 'draft_edit' AND r.source_ref->>'message_id' = m.id::text)
   AND NOT EXISTS (SELECT 1 FROM prompt_rule_reads x WHERE x.source = 'draft_edit' AND x.message_id = m.id)
 ORDER BY m.id DESC
"""
_PENDING_CORRECTIONS = """
SELECT e.id AS example_id, e.chat_id, c.key::bigint AS message_id, c.value AS note
  FROM finetune_examples e, jsonb_each_text(e.corrections) c
 WHERE e.tenant_id = $1
   AND NOT EXISTS (SELECT 1 FROM prompt_rules r
                    WHERE r.source = 'correction' AND r.source_ref->>'message_id' = c.key)
   AND NOT EXISTS (SELECT 1 FROM prompt_rule_reads x
                    WHERE x.source = 'correction' AND x.message_id = c.key::bigint)
 ORDER BY e.updated_at DESC, c.key::bigint DESC
"""


async def waiting(pool: Any, tenant_id: int) -> dict[str, int]:
    """Edits and corrections of this account the suggestion has not read
    yet: none has a rule (in any status) or was sent to the model before."""
    edits = await pool.fetchval(f"SELECT count(*) FROM ({_PENDING_EDITS}) p", tenant_id)
    corrections = await pool.fetchval(f"SELECT count(*) FROM ({_PENDING_CORRECTIONS}) p", tenant_id)
    return {"draft_edits": edits, "corrections": corrections}


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


async def _context(con: Any, tenant_id: int, chat_id: int, before_id: int,
                   masks: Sequence[tuple[str, str]]) -> list[str]:
    """The last CONTEXT_LINES lines of the chat before message `before_id`,
    as crossed the wire (finetune.chat_lines), masked."""
    rows = await con.fetch(
        "SELECT id, direction, status, text, attachments, created_at FROM messages "
        "WHERE tenant_id = $1 AND chat_id = $2 AND id < $3 ORDER BY id DESC LIMIT $4",
        tenant_id, chat_id, before_id, _CONTEXT_ROWS)
    lines = finetune.chat_lines([dict(r) for r in reversed(rows)])[-CONTEXT_LINES:]
    out = []
    for line in lines:
        text = _clip(finetune.mask(line["text"], masks), MAX_LINE_CHARS)
        out.append(f"— booking: {text} —" if line["who"] == "event"
                   else f"{'Client' if line['who'] == 'client' else 'Business'}: {text}")
    return out


async def _sent_reply(con: Any, tenant_id: int, chat_id: int, draft: Any) -> str:
    """What went out for an edited draft: its row's text, then the other
    messages of the same reply (send_burst stores one row per message, all
    with the draft's model) right after it."""
    parts = [draft["text"]]
    rows = await con.fetch(
        "SELECT direction, status, text, llm_model FROM messages WHERE tenant_id = $1 AND chat_id = $2 AND id > $3 "
        "ORDER BY id LIMIT 10", tenant_id, chat_id, draft["id"])
    for r in rows:
        if r["direction"] != "out" or r["status"] != "sent" or r["llm_model"] != draft["llm_model"]:
            break
        parts.append(r["text"])
    return "\n".join(p for p in parts if p)


async def _items(pool: Any, bundle: tenants.Bundle, limit: int = MAX_BATCH) -> list[Item]:
    """The next batch for the model: corrections first (the operator said
    what was wrong), then edited drafts, newest first, each with the chat
    lines before it, masked with that chat's client and the business's
    address and way in."""
    tenant_id = bundle.tenant["id"]
    async with pool.acquire() as con:
        corrections = await con.fetch(_PENDING_CORRECTIONS + " LIMIT $2", tenant_id, limit)
        edits = await con.fetch(_PENDING_EDITS + " LIMIT $2", tenant_id, max(0, limit - len(corrections)))
        chats = {r["chat_id"] for r in (*corrections, *edits)}
        people = await con.fetch("SELECT chat_id, display_name, username FROM conversations "
                                 "WHERE tenant_id = $1 AND chat_id = ANY($2::bigint[])", tenant_id, list(chats))
        masks = {p["chat_id"]: finetune_api._masks(bundle, p) for p in people}
        no_masks = finetune.masks_for(address=(bundle.config.get("booking") or {}).get("address", ""),
                                      way_in=(bundle.config.get("booking") or {}).get("arrival_instructions", ""))
        items: list[Item] = []
        for c in corrections:
            chat_masks = masks.get(c["chat_id"], no_masks)
            message = await con.fetchrow("SELECT id, direction, text FROM messages WHERE tenant_id = $1 AND id = $2",
                                         tenant_id, c["message_id"])
            item = Item(CORRECTION, c["message_id"], c["example_id"])
            items.append(item)
            if message is None:
                continue  # gone since it was flagged: read, nothing to learn
            lines = await _context(con, tenant_id, c["chat_id"], c["message_id"], chat_masks)
            said = _clip(finetune.mask(message["text"], chat_masks), MAX_TEXT_CHARS)
            note = _clip(finetune.mask(c["note"], chat_masks), MAX_TEXT_CHARS)
            if message["direction"] == "in":
                body = [f"The client wrote: {said}", f"The operator's note on this client message: {note}"]
            else:
                body = [f"The business sent: {said}", f"The operator's correction: {note}"]
            item.text = _render_item("a correction on a flagged chat", lines, body)
        for d in edits:
            chat_masks = masks.get(d["chat_id"], no_masks)
            lines = await _context(con, tenant_id, d["chat_id"], d["id"], chat_masks)
            sent = await _sent_reply(con, tenant_id, d["chat_id"], d)
            body = [f"The assistant wrote: {_clip(finetune.mask(d['draft_text'], chat_masks), MAX_TEXT_CHARS)}",
                    f"The operator sent instead: {_clip(finetune.mask(sent, chat_masks), MAX_TEXT_CHARS)}"]
            items.append(Item(DRAFT_EDIT, d["id"], None, _render_item("a draft the operator edited", lines, body)))
    return items


def _render_item(kind: str, lines: list[str], body: list[str]) -> str:
    before = "\n".join(lines) if lines else "(the start of the chat)"
    return f"({kind})\nBefore it:\n{before}\n" + "\n".join(body)


def suggestion_prompt(industry_name: str, items: Sequence[Item], existing: Sequence[str]) -> str:
    shown = "\n".join(f"- {text}" for text in existing[:MAX_SHOWN_RULES]) or "(none yet)"
    rendered = "\n\n".join(f"### Item {n}\n{item.text}" for n, item in enumerate(items, start=1))
    return SUGGEST_PROMPT.format(industry=industry_name.strip() or "this",
                                 sections=", ".join(prompt_layers.SECTION_KEYS), existing=shown, items=rendered)


# Never in a rule: the masks' tokens (a rule about one client) and a phone
# number. [address] is allowed: "write [address] alone on its line" is a rule.
_PERSONAL = re.compile(r"\[(?:client|number|username|email|way in)\]", re.IGNORECASE)


def parse_suggestions(raw: str, count: int) -> dict[int, dict[str, str]]:
    """{item number: {"text", "section", "scope"}} for the items the model
    found a general rule in. Raises RuleError when the answer is not the
    JSON asked for (nothing is then saved, or marked read)."""
    try:
        data = config_assist.extract_json(raw)
    except ValueError as exc:
        raise RuleError(f"The model's answer was not the JSON asked for: {exc}") from None
    entries = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise RuleError('The model\'s answer has no "rules" list.')
    out: dict[int, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("rule"), str):
            continue
        try:
            number = int(entry.get("item"))
            text = clean_text(entry["rule"])
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= count or _PERSONAL.search(text) or finetune._PHONE.search(text):
            continue
        section = entry.get("section") if entry.get("section") in prompt_layers.SECTION_KEYS else DEFAULT_SECTION
        scope = entry.get("scope") if entry.get("scope") in SCOPES else INDUSTRY
        out.setdefault(number, {"text": text, "section": section, "scope": scope})
    return out


async def complete(prompt: str, *, api_key: str, model: str, max_tokens: int,
                   usage_sink: Optional[ai_responder.UsageSink] = None,
                   client: Optional[httpx.AsyncClient] = None) -> str:
    """The text model's raw answer (DeepSeek, on the platform key), through
    vision.py's call like finetune.complete_text: it allows the long answer
    of a fold the time it takes. Raises vision.VisionError with a safe
    message."""
    if not api_key:
        raise vision.VisionError("DEEPSEEK_PLATFORM_KEY is not set.")
    owns = client is None
    http = client or httpx.AsyncClient(timeout=finetune.REQUEST_TIMEOUT_SECONDS)
    try:
        text = await vision._complete(
            api_url=ai_responder.API_URL, api_key=api_key, client=http, usage_sink=usage_sink,
            service="DeepSeek API", key_name="DEEPSEEK_PLATFORM_KEY",
            payload={"model": model, "messages": [{"role": "user", "content": prompt}],
                     "max_tokens": max_tokens, "temperature": 0.2},
        )
    finally:
        if owns:
            await http.aclose()
    if not text:
        raise vision.VisionError("The model returned an empty answer.")
    return text


def _meter(pool: Any):
    async def meter(model: str, usage: dict[str, int]) -> None:
        # Platform spend, like the finetune runs: the admin asked for it.
        await llm_usage.record(pool, tenant_id=None, purpose="finetune", model=model, usage=usage)
    return meter


async def suggest(pool: Any, tenant_id: int, *, api_key: str, actor: str) -> dict[str, Any]:
    """One model call for the next batch of this account's edits and
    corrections (at most MAX_BATCH). Each general rule found is stored as
    proposed (created_by 'suggested'); every item sent is marked read, rule
    or not, so the next call reads new ones. {"rules": [...], "read": n}."""
    bundle = await tenants.TenantStore(pool).bundle(tenant_id)
    items = await _items(pool, bundle)
    readable = [item for item in items if item.text]
    proposals: dict[int, dict[str, str]] = {}
    if readable:
        known = [r["text"] for r in await list_for(pool, tenant_id, bundle.industry["id"])
                 if r["status"] in (ACTIVE, PROPOSED)]
        prompt = suggestion_prompt(bundle.industry["name"], readable, known)
        raw = await complete(prompt, api_key=api_key, model=finetune.text_model(), max_tokens=SUGGEST_MAX_TOKENS,
                             usage_sink=_meter(pool))
        proposals = parse_suggestions(raw, len(readable))
    created: list[dict[str, Any]] = []
    async with pool.acquire() as con, con.transaction():
        seen = {text.casefold() for text in await con.fetchval(
            "SELECT coalesce(array_agg(text), '{}') FROM prompt_rules WHERE industry_id = $1 "
            "AND (tenant_id IS NULL OR tenant_id = $2)", bundle.industry["id"], tenant_id)}
        for number, item in enumerate(readable, start=1):
            proposal = proposals.get(number)
            if proposal is None or proposal["text"].casefold() in seen:
                continue
            seen.add(proposal["text"].casefold())
            ref = {"tenant_id": tenant_id, "message_id": item.message_id}
            if item.example_id is not None:
                ref["example_id"] = item.example_id
            created.append(await create(
                pool, con=con, industry_id=bundle.industry["id"],
                tenant_id=tenant_id if proposal["scope"] == ACCOUNT else None, section=proposal["section"],
                text=proposal["text"], source=item.source, source_ref=ref, status=PROPOSED, created_by=SUGGESTED,
                actor=actor))
        await con.executemany(
            "INSERT INTO prompt_rule_reads (source, message_id, tenant_id) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING", [(item.source, item.message_id, tenant_id) for item in items])
        if items:
            await audit.record(con, tenant_id=tenant_id, actor=actor, event=RULES_SUGGESTED, payload={
                "read": len(items), "proposed": [r["id"] for r in created]})
    return {"rules": created, "read": len(items)}


# -------------------------------------------------------------------- fold

FOLDED_LINE = re.compile(r"FOLDED:\s*rule\s*#?(\d+)", re.IGNORECASE)
SKIPPED_LINE = re.compile(r"SKIPPED:\s*rule\s*#?(\d+)", re.IGNORECASE)
# How much a fold may grow one section: a few sentences per rule, never a rewrite.
GROWTH_CHARS = 1500

# Filled by fold_prompt(), placeholder by placeholder (the JSON in it is full of braces).
FOLD_PROMPT = (
    "You keep the prompt of a chat assistant that answers clients for businesses in the <INDUSTRY> industry. The "
    "operator corrected the assistant, and the corrections became short RULES. Until now each rule was added to the "
    "prompt on its own, after everything else. Your job: write the rules into the prompt itself, each where it "
    "belongs, so the list of rules can be emptied. Nothing else changes.\n\n"
    "BUSINESS: <BUSINESS>\n\n"
    "PLATFORM RULES (fixed, for reference only: never part of your answer, and nothing you write may contradict "
    "them):\n<BASE>\n\n"
    "CURRENT INDUSTRY STANDARD (every business of the industry shares it):\n<INDUSTRY_JSON>\n\n"
    "CURRENT BUSINESS LAYER of <BUSINESS> (its own changes on top of the standard: \"override\" replaces the "
    "standard's section for this business, \"append\" is added after it; \"addendum\" is a free note):\n"
    "<CLIENT_JSON>\n\n"
    "INDUSTRY-WIDE RULES, to fold into the INDUSTRY STANDARD (oldest first):\n<INDUSTRY_RULES>\n\n"
    "RULES FOR <BUSINESS> ONLY, to fold into the BUSINESS LAYER (oldest first):\n<ACCOUNT_RULES>\n\n"
    "HOW TO FOLD\n"
    "- Keep everything a rule does not touch word for word: every section, sentence, example, placeholder, "
    "[block]...[/block], emoji and line break. Do not rewrite, shorten, reorder, translate or improve anything else.\n"
    "- Put each rule where it belongs: in the section suggested with it, unless another section clearly fits "
    "better. Write it into that section's text, in its voice and terms, next to what it corrects, not as a list of "
    "rules at the end.\n"
    "- Where a rule says what the text already says, or what another rule says, keep it once. Where a rule and the "
    "text, or two rules, conflict, the newer rule (later in its list) wins: change the old text to match it.\n"
    "- Never weaken the boundaries, the platform rules, or the fixed rules the standard states (for example: only "
    "the owner confirms a time, through the APPOINTMENTS notes; the address and the way in go out only as the "
    "system's [address] and way in, never typed; the rates and extras messages go out word for word inside "
    "[block]...[/block]; never the same text to many clients). A rule that would weaken one is not folded: list it "
    "as SKIPPED.\n"
    "- In the business layer, \"boundaries\" can only be appended to (mode \"append\"), never overridden. Put a "
    "rule for this business into the override or append it already has for that section, or else add "
    "{\"mode\": \"append\", \"text\": \"...\"} for that section. Never turn an \"append\" into an \"override\", "
    "and never add an \"override\".\n"
    "- Never add names, phone numbers, addresses, door codes, prices that are not already there, or anything about "
    "one client.\n"
    "- Keep each section within its current length plus <GROWTH> characters, and under <MAX_CHARS> characters.\n\n"
    "ANSWER with exactly these three blocks, in this order, and nothing else:\n\n"
    "=== BUSINESS LAYER ===\n<BUSINESS_ANSWER>\n\n"
    "=== INDUSTRY STANDARD (<INDUSTRY>) ===\n<INDUSTRY_ANSWER>\n\n"
    "=== NOTES FOR OPERATOR ===\n"
    "One line per rule, in the order listed: \"FOLDED: rule <id> -> <section>\", or \"SKIPPED: rule <id>: <why>\". "
    "Then anything the operator should check."
)

_BUSINESS_ANSWER = ('The whole business layer with the rules for <BUSINESS> folded in, as one JSON object: '
                    '{"overrides": {...}, "addendum": "..."}, with every override it has now.')
_INDUSTRY_ANSWER = ('The whole industry standard with the industry-wide rules folded in, as one JSON object: '
                    '{"sections": {...}}, with every section it has now.')
_NOTHING = "{}   (there are no rules for this part: write exactly {} and nothing else)"


@dataclass
class FoldPlan:
    """What one fold run reads, taken when it starts: the account, its
    industry (with the template version, for the stale check on applying),
    the live standard and business layer, and the rules to fold in."""
    tenant: dict[str, Any]
    industry: dict[str, Any]
    base_rules: str
    sections: dict[str, str]
    client: dict[str, Any]
    industry_rules: list[dict[str, Any]]
    account_rules: list[dict[str, Any]]
    # When the newest of these rules last changed.
    last_change: Optional[datetime] = None

    @property
    def rules(self) -> list[dict[str, Any]]:
        return self.industry_rules + self.account_rules


async def plan_fold(pool: Any, tenant_id: int, *, industry_wide: bool = True) -> FoldPlan:
    """The fold for this account: its own active rules into its business
    layer and, with `industry_wide`, its industry's into the standard."""
    store = tenants.TenantStore(pool)
    tenant = await store.get(tenant_id)
    industry = await store.get_industry(tenant["industry_id"])
    live = await store.version(tenants.INDUSTRY, industry["id"], industry["template_version"])
    client: dict[str, Any] = {"overrides": {}, "addendum": ""}
    if tenant["prompt_version"]:
        stored = (await store.version(tenants.CLIENT, tenant_id, tenant["prompt_version"]))["content"]
        client = prompt_layers.validate_client(stored, strict=False)
    listed = [r for r in await list_for(pool, tenant_id, industry["id"]) if r["status"] == ACTIVE]
    industry_rules = [r for r in listed if r["scope"] == INDUSTRY] if industry_wide else []
    account_rules = [r for r in listed if r["scope"] == ACCOUNT]
    last_change = await pool.fetchval("SELECT max(updated_at) FROM prompt_rules WHERE id = ANY($1::bigint[])",
                                      [r["id"] for r in industry_rules + account_rules])
    return FoldPlan(
        tenant=tenant, industry=industry, base_rules=(await store.base())["content"]["rules"],
        sections=dict(live["content"].get("sections") or {}), client=client,
        industry_rules=industry_rules, account_rules=account_rules, last_change=last_change,
    )


def _listed(rules: Sequence[dict[str, Any]]) -> str:
    return "\n".join(f"- rule {r['id']} ({r['section']}): {r['text']}" for r in rules) or "(none)"


def fold_prompt(plan: FoldPlan) -> str:
    business = plan.tenant["name"].strip() or "this business"
    filled = {
        "BUSINESS": business,
        "INDUSTRY": plan.industry["name"].strip() or "this",
        "BASE": plan.base_rules,
        "INDUSTRY_JSON": json.dumps({"sections": plan.sections}, ensure_ascii=False, indent=2),
        "CLIENT_JSON": json.dumps(plan.client, ensure_ascii=False, indent=2),
        "INDUSTRY_RULES": _listed(plan.industry_rules),
        "ACCOUNT_RULES": _listed(plan.account_rules),
        "BUSINESS_ANSWER": _BUSINESS_ANSWER.replace("<BUSINESS>", business) if plan.account_rules else _NOTHING,
        "INDUSTRY_ANSWER": _INDUSTRY_ANSWER if plan.industry_rules else _NOTHING,
        "GROWTH": f"{GROWTH_CHARS:,}",
        "MAX_CHARS": f"{prompt_layers.MAX_SECTION_CHARS:,}",
    }
    # One pass, so nothing the layers or rules say is read as a placeholder.
    return re.sub(r"<([A-Z_]+)>", lambda m: filled.get(m.group(1), m.group(0)), FOLD_PROMPT)


def _check_growth(where: str, before: dict[str, str], after: dict[str, str], errors: list[str]) -> None:
    for key, text in after.items():
        grew = len(text) - len(before.get(key, ""))
        if grew > GROWTH_CHARS:
            errors.append(f"{where}: {key} grew by {grew:,} characters; a fold adds at most about "
                          f"{GROWTH_CHARS:,}. Check nothing else was rewritten.")


def _check_kept(where: str, before: str, after: str, errors: list[str]) -> None:
    """Boundaries only ever get stricter: every line they had is still there."""
    kept = {" ".join(line.split()) for line in after.splitlines()}
    for line in before.splitlines():
        line = " ".join(line.split())
        if line and line not in kept:
            errors.append(f"{where}: a line of the boundaries is gone or changed: {_clip(line, 120)}")


def _fold_industry(blocks: dict[str, str], plan: FoldPlan, errors: list[str]) -> Optional[dict[str, Any]]:
    if "industry" not in blocks:
        errors.append("The answer has no === INDUSTRY STANDARD === block.")
        return None
    try:
        parsed = finetune._json_object(blocks["industry"], "industry standard")
    except finetune.FinetuneError as exc:
        errors.append(str(exc))
        return None
    sections = parsed.get("sections", parsed)
    if not isinstance(sections, dict):
        errors.append("Industry standard: sections must be an object.")
        return None
    # A fold only adds: a section the answer left out is kept as it was.
    for key, text in plan.sections.items():
        if not str(sections.get(key) or "").strip():
            sections[key] = text
            errors.append(f"Industry standard: the answer left out {key}; it is kept as it was.")
    try:
        clean = finetune.check_industry_sections(sections)
    except finetune.FinetuneError as exc:
        errors.append(str(exc))
        return sections
    _check_growth("Industry standard", plan.sections, clean, errors)
    _check_kept("Industry standard", plan.sections.get("boundaries", ""), clean.get("boundaries", ""), errors)
    return clean


def _fold_business(blocks: dict[str, str], plan: FoldPlan, errors: list[str]) -> Optional[dict[str, Any]]:
    if "business" not in blocks:
        errors.append("The answer has no === BUSINESS LAYER === block.")
        return None
    try:
        layer = finetune._json_object(blocks["business"], "business layer")
    except finetune.FinetuneError as exc:
        errors.append(str(exc))
        return None
    overrides = layer.setdefault("overrides", {})
    if not isinstance(overrides, dict):
        errors.append("Business layer: overrides must be an object.")
        return layer
    # Likewise: an override or the addendum the answer left out stays.
    for key, override in plan.client["overrides"].items():
        if key not in overrides:
            overrides[key] = override
            errors.append(f"Business layer: the answer left out {key}; it is kept as it was.")
    if plan.client["addendum"] and not str(layer.get("addendum") or "").strip():
        layer["addendum"] = plan.client["addendum"]
        errors.append("Business layer: the answer left out the addendum; it is kept as it was.")
    try:
        clean = finetune.check_business_layer(layer)
    except finetune.FinetuneError as exc:
        errors.append(str(exc))
        return layer
    before = {k: v["text"] for k, v in plan.client["overrides"].items()}
    _check_growth("Business layer", before, {k: v["text"] for k, v in clean["overrides"].items()}, errors)
    if "boundaries" in plan.client["overrides"]:
        _check_kept("Business layer", before["boundaries"],
                    (clean["overrides"].get("boundaries") or {}).get("text", ""), errors)
    return clean


def parse_fold(raw: str, plan: FoldPlan) -> dict[str, Any]:
    """A fold run's answer as a finetune run result (finetune.parse_output's
    shape), reading only the parts it had rules for: the other part is None,
    so applying leaves it as it is. A left-out section or override is put
    back, and growth, lost boundary lines and rules missing from the notes
    are reported as errors for the admin to check.

    result["rules"]: the rule ids the run had per part, and the ones the
    notes list as FOLDED and SKIPPED. Applying merges only the FOLDED ones
    of the parts applied; any other stays active, to be folded next time."""
    errors: list[str] = []
    try:
        blocks = finetune.split_blocks(raw)
        business = _fold_business(blocks, plan, errors) if plan.account_rules else None
        industry = _fold_industry(blocks, plan, errors) if plan.industry_rules else None
    except Exception as exc:  # never leave the run "running" over a parse
        log.exception("Fold answer could not be read")
        blocks, business, industry = {}, None, None
        errors.append(f"The answer could not be read: {type(exc).__name__}.")
    notes = blocks.get("notes", "")
    planned = {r["id"] for r in plan.rules}
    folded = sorted({int(n) for n in FOLDED_LINE.findall(notes)} & planned)
    skipped = sorted(({int(n) for n in SKIPPED_LINE.findall(notes)} & planned) - set(folded))
    for rule in plan.rules:
        if rule["id"] not in folded and rule["id"] not in skipped:
            errors.append(f"Rule {rule['id']} is not in the notes as FOLDED: it stays active after applying. "
                          "Check whether it went in.")
    return {
        "business_layer": business, "industry_sections": industry, "notes": notes, "errors": errors,
        "rules": {"industry": [r["id"] for r in plan.industry_rules], "account": [r["id"] for r in plan.account_rules],
                  "folded": folded, "skipped": skipped},
    }


async def start_fold(pool: Any, plan: FoldPlan, *, api_key: str, actor: str) -> dict[str, Any]:
    """Start the fold as a finetune run (Finetune → Runs), named after its
    rules ("rule-12"); returned the way finetune_api returns runs."""
    if not plan.rules:
        raise RuleError("There are no active rules to fold for this account or its industry.")
    prompt = fold_prompt(plan)
    model = finetune.text_model()
    return await finetune_api._launch(
        plan.tenant["id"], plan.industry, model=model, source=finetune_api.RULES_SOURCE,
        files=[f"rule-{r['id']}" for r in plan.rules],
        call=lambda meter: complete(prompt, api_key=api_key, model=model, max_tokens=finetune.text_max_tokens(),
                                    usage_sink=meter),
        parse=lambda raw: parse_fold(raw, plan), pool=pool, actor=actor,
    )


async def running_fold(pool: Any, tenant_id: int) -> Optional[int]:
    """A fold of this account still working, if any (not one cut off by a
    restart: finetune_api counts those as failed)."""
    return await pool.fetchval(
        "SELECT id FROM finetune_runs WHERE tenant_id = $1 AND source = $2 AND status = 'running' "
        f"AND created_at >= now() - interval '{finetune_api.STALE_MINUTES} minutes' ORDER BY id DESC LIMIT 1",
        tenant_id, finetune_api.RULES_SOURCE)


async def on_applied(pool: Any, run: dict[str, Any], applied: dict[str, Any], *, actor: str) -> list[int]:
    """A fold run was applied (finetune_api.api_apply): its FOLDED rules of
    the parts applied are merged, with the version tag of the prompt that
    now holds them. A rule changed since the run started stays active: the
    run folded its old text."""
    folded = ((run.get("result") or {}).get("rules")) or {}
    done = set(folded.get("folded") or [])
    ids = []
    if "industry_version" in applied:
        ids += [i for i in folded.get("industry") or [] if i in done]
    if "client_version" in applied:
        ids += [i for i in folded.get("account") or [] if i in done]
    if not ids:
        return []
    store = tenants.TenantStore(pool)
    tenant = await store.get(run["tenant_id"])
    industry = await store.get_industry(run["industry_id"])
    tag = prompt_layers.layers_tag((await store.base_version(), industry["id"],
                                    applied.get("industry_version", industry["template_version"]),
                                    applied.get("client_version", tenant["prompt_version"])))
    async with pool.acquire() as con, con.transaction():
        merged = [r["id"] for r in await con.fetch(
            "UPDATE prompt_rules SET status = 'merged', merged_into = $2, updated_at = now() "
            "WHERE id = ANY($1::bigint[]) AND status = 'active' AND updated_at <= $3 RETURNING id",
            ids, tag, run["created_at"])]
        if merged:
            await audit.record(con, tenant_id=run["tenant_id"], actor=actor, event=RULES_MERGED,
                               payload={"run_id": run["id"], "rule_ids": sorted(merged), "merged_into": tag})
    return sorted(merged)


# ------------------------------------------------------------- weekly round

WEEK_KEY = "rules_fold_week"
FOLD_HOUR_UTC = 6
_warned_week: Optional[str] = None


def fold_week(now: datetime) -> str:
    """The fold week `now` falls in, named by its Monday: a week runs from
    Monday 06:00 UTC to the next Monday 06:00 UTC."""
    shifted = now.astimezone(timezone.utc) - timedelta(hours=FOLD_HOUR_UTC)
    return (shifted.date() - timedelta(days=shifted.weekday())).isoformat()


async def _weekly_plan(pool: Any) -> list[tuple[int, bool]]:
    """(tenant id, industry_wide) for each fold due: each industry with
    active industry-wide rules on its lowest-id account (with that
    account's own rules), then each other account with active rules of its
    own."""
    industries = await pool.fetch(
        "SELECT r.industry_id, min(t.id) AS tenant_id FROM prompt_rules r "
        "JOIN tenants t ON t.industry_id = r.industry_id "
        "WHERE r.status = 'active' AND r.tenant_id IS NULL GROUP BY r.industry_id ORDER BY r.industry_id")
    accounts = await pool.fetch(
        "SELECT DISTINCT tenant_id FROM prompt_rules WHERE status = 'active' AND tenant_id IS NOT NULL ORDER BY 1")
    planned = [(r["tenant_id"], True) for r in industries]
    covered = {tenant_id for tenant_id, _ in planned}
    return planned + [(r["tenant_id"], False) for r in accounts if r["tenant_id"] not in covered]


async def _unapplied_fold(pool: Any, tenant_id: int, since: datetime) -> Optional[int]:
    """A fold of this account not applied yet (working, or done and waiting
    for review), started after its rules last changed: it already covers
    them, so another would only duplicate it."""
    return await pool.fetchval(
        "SELECT id FROM finetune_runs WHERE tenant_id = $1 AND source = $2 AND created_at > $3 "
        "AND (status = 'done' OR (status = 'running' "
        f"AND created_at >= now() - interval '{finetune_api.STALE_MINUTES} minutes')) ORDER BY id DESC LIMIT 1",
        tenant_id, finetune_api.RULES_SOURCE, since)


async def tick(pool: Any, bus: Any, now: Optional[datetime] = None) -> list[int]:
    """The scheduler's round: the weekly fold of active rules into the prompt.

    At most once a fold week (from Monday 06:00 UTC; the week done is kept
    in platform_settings 'rules_fold_week', claimed before anything starts,
    so two rounds cannot both start folds), it starts one fold run:

    - per industry with active industry-wide rules, on its lowest-id account
      (the run needs an account; its own active rules go in too), since the
      escort accounts share one tuning and one run updates the standard for
      all of them;
    - per other account with active rules of its own.

    Skipped: an account with an unapplied fold (working, or done and
    waiting for the admin) started after its rules last changed, which
    already holds them; and everything while DEEPSEEK_PLATFORM_KEY is not
    set (logged once a week, and the week is not marked done, so the folds
    start once the key is set). The runs wait in Finetune → Runs: nothing
    changes until the admin applies one. Returns the runs started."""
    global _warned_week
    now = now or datetime.now(timezone.utc)
    week = fold_week(now)
    stored = await pool.fetchval("SELECT value->>'week' FROM platform_settings WHERE key = $1", WEEK_KEY)
    if stored == week:
        return []
    api_key = platform_api.platform_key()
    if not api_key:
        if _warned_week != week:
            _warned_week = week
            log.warning("DEEPSEEK_PLATFORM_KEY is not set: this week's fold of the operator rules waits for it.")
        return []
    claimed = await pool.fetchval(
        "INSERT INTO platform_settings (key, value, updated_by) VALUES ($1, $2::jsonb, 'scheduler') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now() "
        "WHERE platform_settings.value->>'week' IS DISTINCT FROM $3 RETURNING key",
        WEEK_KEY, json.dumps({"week": week}), week)
    if claimed is None:
        return []
    started: list[int] = []
    for tenant_id, industry_wide in await _weekly_plan(pool):
        try:
            plan = await plan_fold(pool, tenant_id, industry_wide=industry_wide)
            if not plan.rules:
                continue
            waiting_run = await _unapplied_fold(pool, tenant_id, plan.last_change)
            if waiting_run is not None:
                log.info("Tenant %s: fold run %s already holds its rules; not folding again.", tenant_id, waiting_run)
                continue
            run = await start_fold(pool, plan, api_key=api_key, actor=audit.SYSTEM)
            started.append(run["id"])
        except Exception:
            log.exception("Weekly fold of the operator rules for tenant %s failed", tenant_id)
    await pool.execute("UPDATE platform_settings SET value = $2::jsonb, updated_at = now() WHERE key = $1",
                       WEEK_KEY, json.dumps({"week": week, "runs": started}))
    if started:
        log.info("Started %s fold run(s) of the operator rules: %s.", len(started), started)
    return started
