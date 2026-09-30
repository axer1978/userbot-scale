"""Postgres side of WhatsApp accounts (migration 0006): who a chat is with
(wa_peers), the inbound handoff from wa-gateway (wa_inbox) and the stored
login (wa_auth_state, written by the gateway).

Every per-account table keys chats by `chat_id BIGINT`, as for Telegram. A
WhatsApp chat gets its chat_id from wa_peers, which maps it to the JIDs
WhatsApp uses for the person: the phone JID (34600123456@s.whatsapp.net)
and/or the privacy-preserving LID (123456789@lid). Both point at one chat
once WhatsApp has told us they belong together.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

log = logging.getLogger("wa_store")

PHONE_SERVER = "@s.whatsapp.net"
LID_SERVER = "@lid"


def split_jid(jid: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(phone_jid, lid) for one JID; the other side is None."""
    if not jid:
        return None, None
    if jid.endswith(LID_SERVER):
        return None, jid
    if jid.endswith(PHONE_SERVER):
        return jid, None
    return None, None


def phone_of(phone_jid: Optional[str]) -> Optional[str]:
    """"+34600123456" for a phone JID (a device suffix like ":12" dropped)."""
    if not phone_jid or not phone_jid.endswith(PHONE_SERVER):
        return None
    user = phone_jid[: -len(PHONE_SERVER)].split(":", 1)[0]
    return f"+{user}" if user.isdigit() else None


def phone_jid_for(number: str) -> str:
    """The phone JID for a number typed any way ("+34 600 12 34 56")."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 6:
        raise ValueError(f"{number!r} is not a phone number")
    return f"{digits}{PHONE_SERVER}"


async def chat_for(
    pool: asyncpg.Pool,
    session_id: str,
    *,
    phone_jid: Optional[str] = None,
    lid: Optional[str] = None,
    push_name: Optional[str] = None,
) -> tuple[int, str]:
    """The chat_id (and the JID to send to) for a person, creating the
    mapping on first sight and completing it when WhatsApp reveals the other
    half of the phone/LID pair. `push_name` is the person's own display
    name; pass None for anything we sent (our own name is on it)."""
    if not phone_jid and not lid:
        raise ValueError("a WhatsApp chat needs a phone JID or a LID")
    async with pool.acquire() as con, con.transaction():
        rows = await con.fetch(
            """
            SELECT chat_id, jid, phone_jid, lid, push_name FROM wa_peers
             WHERE session_id = $1 AND (phone_jid = $2 OR lid = $3)
             ORDER BY chat_id
             FOR UPDATE
            """,
            session_id, phone_jid, lid,
        )
        if not rows:
            row = await con.fetchrow(
                """
                INSERT INTO wa_peers (session_id, jid, phone_jid, lid, push_name)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING chat_id, jid
                """,
                session_id, phone_jid or lid, phone_jid, lid, push_name or "",
            )
            return row["chat_id"], row["jid"]
        if len(rows) > 1:
            # The phone JID and the LID were first seen as two chats, and
            # now one message says they are the same person. The histories
            # stay where they are; replies go to the phone JID's chat.
            keep = next((r for r in rows if r["phone_jid"] == phone_jid), rows[0])
            log.warning("[%s] One WhatsApp contact is two chats here (%s); using chat %s.",
                        session_id, ", ".join(str(r["chat_id"]) for r in rows), keep["chat_id"])
            return keep["chat_id"], keep["jid"]
        row = rows[0]
        new_phone = row["phone_jid"] or phone_jid
        new_lid = row["lid"] or lid
        new_name = push_name if push_name else row["push_name"]
        new_jid = new_phone or new_lid
        if (new_phone, new_lid, new_name, new_jid) != (row["phone_jid"], row["lid"], row["push_name"], row["jid"]):
            await con.execute(
                """
                UPDATE wa_peers SET phone_jid = $3, lid = $4, push_name = $5, jid = $6, updated_at = now()
                 WHERE session_id = $1 AND chat_id = $2
                """,
                session_id, row["chat_id"], new_phone, new_lid, new_name, new_jid,
            )
        return row["chat_id"], new_jid


async def peer(pool: asyncpg.Pool, session_id: str, chat_id: int) -> Optional[dict[str, Any]]:
    row = await pool.fetchrow(
        "SELECT chat_id, jid, phone_jid, lid, push_name FROM wa_peers WHERE session_id = $1 AND chat_id = $2",
        session_id, chat_id,
    )
    return dict(row) if row else None


def display_name(row: Optional[dict[str, Any]]) -> str:
    """How a WhatsApp chat is labelled: the person's own name, else their
    number, else a neutral placeholder (a LID is not worth showing)."""
    if not row:
        return "WhatsApp chat"
    return row.get("push_name") or phone_of(row.get("phone_jid")) or f"WhatsApp chat {row['chat_id']}"


# ------------------------------------------------------------------- inbox


async def inbox_batch(pool: asyncpg.Pool, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """The oldest messages the gateway handed over and nobody has taken yet."""
    rows = await pool.fetch(
        "SELECT id, wa_message_id, payload FROM wa_inbox WHERE session_id = $1 ORDER BY id LIMIT $2",
        session_id, limit,
    )
    out = []
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append({"id": row["id"], "wa_message_id": row["wa_message_id"], "payload": payload})
    return out


async def inbox_ack(pool: asyncpg.Pool, session_id: str, row_id: int) -> None:
    """The message is stored in `messages` (or deliberately dropped): the
    handoff row can go."""
    await pool.execute("DELETE FROM wa_inbox WHERE session_id = $1 AND id = $2", session_id, row_id)


# ------------------------------------------------------------------- login


async def has_login(pool: asyncpg.Pool, session_id: str) -> bool:
    return bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM wa_auth_state WHERE session_id = $1 AND kind = 'creds')", session_id,
    ))


async def forget_login(pool: asyncpg.Pool, session_id: str) -> int:
    """Delete the stored WhatsApp login (it no longer works): the account
    needs pairing again. Returns how many rows went."""
    result = await pool.execute("DELETE FROM wa_auth_state WHERE session_id = $1", session_id)
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
