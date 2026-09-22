"""Postgres persistence for the fleet.

Two classes:

- `SessionRegistry` — fleet-wide rows in `telegram_sessions` (create a
  session, store its encrypted credentials, flip is_active, etc). One per
  process, not scoped to any particular session.
- `Database` — a session-scoped facade. `session_id` is bound once at
  construction (never passed per-method), so every call site that already
  does `db.get_conversation(chat_id)` etc. needs no change beyond
  constructing the right `Database` for that session. This is what lets
  `context_link.py` (which receives a bare `db` and never mentions a
  session id) work completely unchanged.

Every method below keeps the exact name, parameter list and return shape
(dict / list-of-dict, booleans as Python bool, timestamps as ISO-8601 UTC
strings with second precision, exactly like `datetime.now(timezone.utc)
.isoformat(timespec="seconds")` produced before) that the SQLite version
had. Only the SQL body and the storage engine changed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

import crypto

# Message.direction
DIR_IN = "in"
DIR_OUT = "out"
DIR_SYSTEM = "system"

# Message.status
STATUS_RECEIVED = "received"
STATUS_SENT = "sent"
STATUS_PENDING = "pending_approval"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"
STATUS_NOTE = "note"

# Outreach.status
OUT_QUEUED = "queued"
OUT_DRAFTED = "drafted"
OUT_SENT = "sent"
OUT_FAILED = "failed"
OUT_CANCELLED = "cancelled"

# ChatLink.origin
LINK_AUTO = "auto"
LINK_MANUAL = "manual"
LINK_BLOCKED = "blocked"

SESSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


class InvalidSessionId(ValueError):
    pass


def validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not SESSION_ID_RE.match(value):
        raise InvalidSessionId(
            f"session_id {value!r} must match {SESSION_ID_RE.pattern} "
            "(lowercase alnum, '_', '.', '-', max 40 chars)"
        )
    return value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts(value: Optional[str]) -> Optional[datetime]:
    """Inbound ISO-8601 string -> an aware UTC datetime asyncpg can bind to
    a TIMESTAMPTZ parameter. Naive strings are assumed UTC."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Outbound datetime -> ISO-8601 UTC string, same shape the SQLite
    version stored natively as TEXT."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _preview(text: str, limit: int = 90) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ----------------------------------------------------------------------
# Fleet-wide session registry
# ----------------------------------------------------------------------

_SESSION_COLUMNS = (
    "session_id, label, api_id, dc_id, server_address, port, user_id, username, "
    "phone_number, takeout_id, is_active, state, state_reason, "
    "lease_worker_id, lease_expires_at, lease_epoch, last_seen_at, created_at, updated_at"
)


def _session_row(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "label": row["label"],
        "api_id": row["api_id"],
        "dc_id": row["dc_id"],
        "server_address": row["server_address"],
        "port": row["port"],
        "user_id": row["user_id"],
        "username": row["username"],
        "phone_number": row["phone_number"],
        "takeout_id": row["takeout_id"],
        "is_active": row["is_active"],
        "state": row["state"],
        "state_reason": row["state_reason"],
        "lease_worker_id": row["lease_worker_id"],
        "lease_expires_at": _iso(row["lease_expires_at"]),
        "lease_epoch": row["lease_epoch"],
        "last_seen_at": _iso(row["last_seen_at"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


class SessionRegistry:
    """Fleet-wide rows in `telegram_sessions`. Never selects a `*_enc`
    column except inside the load_*/save_* methods whose whole job is to
    encrypt/decrypt one field, so a JSON response built from `list()`/`get()`
    cannot physically contain ciphertext or plaintext key material."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = f"SELECT {_SESSION_COLUMNS} FROM telegram_sessions"
        if active_only:
            sql += " WHERE is_active"
        sql += " ORDER BY session_id"
        async with self._pool.acquire() as con:
            rows = await con.fetch(sql)
        return [_session_row(r) for r in rows]

    async def get(self, session_id: str) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                f"SELECT {_SESSION_COLUMNS} FROM telegram_sessions WHERE session_id = $1",
                session_id,
            )
        return _session_row(row) if row else None

    async def create(
        self,
        session_id: str,
        *,
        label: str = "",
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        session_id = validate_session_id(session_id)
        api_hash_enc = (
            crypto.encrypt_text(api_hash, aad=crypto.aad_for(session_id, "api_hash"))
            if api_hash
            else None
        )
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO telegram_sessions (session_id, label, api_id, api_hash_enc)
                VALUES ($1, $2, $3, $4)
                """,
                session_id,
                label,
                api_id,
                api_hash_enc,
            )
        return await self.get(session_id)  # type: ignore[return-value]

    async def save_login(
        self,
        session_id: str,
        *,
        dc_id: int,
        server_address: str,
        port: int,
        auth_key: bytes,
        user_id: Optional[int],
        username: Optional[str],
        phone_number: Optional[str],
    ) -> dict[str, Any]:
        auth_key_enc = crypto.encrypt(auth_key, aad=crypto.aad_for(session_id, "auth_key"))
        async with self._pool.acquire() as con:
            await con.execute(
                """
                UPDATE telegram_sessions
                   SET dc_id = $2, server_address = $3, port = $4, auth_key_enc = $5,
                       user_id = $6, username = $7, phone_number = $8, updated_at = now()
                 WHERE session_id = $1
                """,
                session_id,
                dc_id,
                server_address,
                port,
                auth_key_enc,
                user_id,
                username,
                phone_number,
            )
        return await self.get(session_id)  # type: ignore[return-value]

    async def load_auth(self, session_id: str) -> Optional[dict[str, Any]]:
        """Decrypts and returns everything needed to construct a live
        TelegramClient. Never log this return value."""
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT api_id, api_hash_enc, dc_id, server_address, port, auth_key_enc,
                       user_id, username, phone_number
                  FROM telegram_sessions WHERE session_id = $1
                """,
                session_id,
            )
        if row is None:
            return None
        api_hash = (
            crypto.decrypt_text(row["api_hash_enc"], aad=crypto.aad_for(session_id, "api_hash"))
            if row["api_hash_enc"] is not None
            else None
        )
        auth_key = (
            crypto.decrypt(row["auth_key_enc"], aad=crypto.aad_for(session_id, "auth_key"))
            if row["auth_key_enc"] is not None
            else None
        )
        return {
            "api_id": row["api_id"],
            "api_hash": api_hash,
            "dc_id": row["dc_id"],
            "server_address": row["server_address"],
            "port": row["port"],
            "auth_key": auth_key,
            "user_id": row["user_id"],
            "username": row["username"],
            "phone_number": row["phone_number"],
        }

    async def set_proxy(self, session_id: str, proxy_url: Optional[str]) -> None:
        enc = (
            crypto.encrypt_text(proxy_url, aad=crypto.aad_for(session_id, "proxy_url"))
            if proxy_url
            else None
        )
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE telegram_sessions SET proxy_url_enc = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                enc,
            )

    async def load_proxy(self, session_id: str) -> Optional[str]:
        async with self._pool.acquire() as con:
            enc = await con.fetchval(
                "SELECT proxy_url_enc FROM telegram_sessions WHERE session_id = $1", session_id
            )
        if enc is None:
            return None
        return crypto.decrypt_text(enc, aad=crypto.aad_for(session_id, "proxy_url"))

    async def set_deepseek_key(self, session_id: str, key: Optional[str]) -> None:
        enc = (
            crypto.encrypt_text(key, aad=crypto.aad_for(session_id, "deepseek"))
            if key
            else None
        )
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE telegram_sessions SET deepseek_key_enc = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                enc,
            )

    async def load_deepseek_key(self, session_id: str) -> Optional[str]:
        async with self._pool.acquire() as con:
            enc = await con.fetchval(
                "SELECT deepseek_key_enc FROM telegram_sessions WHERE session_id = $1", session_id
            )
        if enc is None:
            return None
        return crypto.decrypt_text(enc, aad=crypto.aad_for(session_id, "deepseek"))

    async def set_active(self, session_id: str, active: bool) -> dict[str, Any]:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE telegram_sessions SET is_active = $2, updated_at = now() WHERE session_id = $1",
                session_id,
                active,
            )
        return await self.get(session_id)  # type: ignore[return-value]

    async def set_state(self, session_id: str, state: str, reason: str = "") -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE telegram_sessions SET state = $2, state_reason = $3, updated_at = now() WHERE session_id = $1",
                session_id,
                state,
                reason,
            )

    async def clear_auth(self, session_id: str) -> None:
        """Logout: drop the auth key and DC info so the session needs a
        fresh login flow; leaves the row (and its history) in place."""
        async with self._pool.acquire() as con:
            await con.execute(
                """
                UPDATE telegram_sessions
                   SET auth_key_enc = NULL, dc_id = NULL, server_address = NULL, port = NULL,
                       state = 'new', state_reason = '', updated_at = now()
                 WHERE session_id = $1
                """,
                session_id,
            )

    async def delete(self, session_id: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute("DELETE FROM telegram_sessions WHERE session_id = $1", session_id)

    async def claimable(self) -> list[str]:
        """Active sessions with no live lease — what the manager assigns to
        workers at startup."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT session_id FROM telegram_sessions
                 WHERE is_active AND (lease_expires_at IS NULL OR lease_expires_at < now())
                 ORDER BY session_id
                """
            )
        return [r["session_id"] for r in rows]


# ----------------------------------------------------------------------
# Session-scoped facade
# ----------------------------------------------------------------------


class Database:
    """Every method here is scoped to the `session_id` bound at
    construction. No method takes a session parameter."""

    def __init__(self, pool: asyncpg.Pool, session_id: str) -> None:
        self._pool = pool
        self._session_id = validate_session_id(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    async def connect(self) -> None:
        """Kept for call-site compatibility with the old per-file Database;
        the pool is owned by the process, so this only seeds the per-session
        id counters used by bookings/media."""
        async with self._pool.acquire() as con:
            await con.executemany(
                "INSERT INTO session_counters (session_id, name) VALUES ($1, $2) "
                "ON CONFLICT (session_id, name) DO NOTHING",
                [(self._session_id, "booking"), (self._session_id, "media")],
            )

    async def close(self) -> None:
        """No-op: the pool outlives any single Database facade."""
        return None

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def upsert_conversation(
        self,
        chat_id: int,
        display_name: str,
        username: Optional[str],
        is_bot: bool,
        access_hash: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO conversations (session_id, chat_id, display_name, username,
                                           is_bot, access_hash, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (session_id, chat_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    username     = excluded.username,
                    is_bot       = excluded.is_bot,
                    access_hash  = COALESCE(excluded.access_hash, conversations.access_hash)
                """,
                self._session_id,
                chat_id,
                display_name,
                username,
                bool(is_bot),
                access_hash,
            )
        return await self.get_conversation(chat_id)  # type: ignore[return-value]

    async def get_conversation(self, chat_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM conversations WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
            )
        return _conversation(row) if row else None

    async def list_conversations(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM conversations
                 WHERE session_id = $1
                 ORDER BY COALESCE(last_message_at, created_at) DESC
                """,
                self._session_id,
            )
        return [_conversation(r) for r in rows]

    async def set_paused(self, chat_id: int, paused: bool) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE conversations SET automation_paused = $3 WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
                bool(paused),
            )
        return await self.get_conversation(chat_id)

    async def get_access_hash(self, chat_id: int) -> Optional[int]:
        async with self._pool.acquire() as con:
            return await con.fetchval(
                "SELECT access_hash FROM conversations WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
            )

    async def mark_read(self, chat_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE conversations SET unread = 0 WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
            )
        return await self.get_conversation(chat_id)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def record_message(
        self,
        chat_id: int,
        direction: str,
        status: str,
        text: str,
        telegram_id: Optional[int] = None,
        bump_preview: bool = True,
        mark_unread: bool = False,
        attachments: Optional[list[int]] = None,
    ) -> Optional[dict[str, Any]]:
        """Insert a message and refresh the conversation's preview.

        Returns None when the message is a duplicate of one already stored
        under the same Telegram message id (we send via Telethon *and*
        watch outgoing events, so the same message can arrive twice).
        """
        async with self._pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                """
                INSERT INTO messages (session_id, chat_id, telegram_id, direction, status,
                                      text, attachments)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (session_id, chat_id, telegram_id) WHERE telegram_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                self._session_id,
                chat_id,
                telegram_id,
                direction,
                status,
                text,
                list(attachments or []),
            )
            if row is None:
                return None
            message_id = row["id"]

            if bump_preview:
                await con.execute(
                    """
                    UPDATE conversations
                       SET last_message_at      = now(),
                           last_message_preview = $3,
                           unread               = CASE WHEN $4 THEN unread + 1 ELSE unread END
                     WHERE session_id = $1 AND chat_id = $2
                    """,
                    self._session_id,
                    chat_id,
                    _preview(text),
                    bool(mark_unread),
                )

        return await self.get_message(message_id)

    async def get_message(self, message_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM messages WHERE session_id = $1 AND id = $2",
                self._session_id,
                message_id,
            )
        return _message(row) if row else None

    async def find_by_telegram_id(
        self, chat_id: int, telegram_id: Optional[int]
    ) -> Optional[dict[str, Any]]:
        if telegram_id is None:
            return None
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM messages WHERE session_id = $1 AND chat_id = $2 AND telegram_id = $3",
                self._session_id,
                chat_id,
                telegram_id,
            )
        return _message(row) if row else None

    async def get_messages(self, chat_id: int, limit: int = 300) -> list[dict[str, Any]]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE session_id = $1 AND chat_id = $2
                     ORDER BY id DESC LIMIT $3
                ) AS recent ORDER BY id ASC
                """,
                self._session_id,
                chat_id,
                limit,
            )
        return [_message(r) for r in rows]

    async def get_history_for_ai(self, chat_id: int, limit: int = 30) -> list[dict[str, str]]:
        """Recent exchange as OpenAI-style role/content pairs.

        Drafts that were never approved, rejections and error rows are left
        out — only what actually crossed the wire is context for the model.
        """
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM (
                    SELECT direction, text, id FROM messages
                     WHERE session_id = $1
                       AND chat_id = $2
                       AND status = ANY($3)
                       AND direction = ANY($4)
                       AND btrim(text) <> ''
                     ORDER BY id DESC LIMIT $5
                ) AS recent ORDER BY id ASC
                """,
                self._session_id,
                chat_id,
                [STATUS_RECEIVED, STATUS_SENT],
                [DIR_IN, DIR_OUT],
                limit,
            )
        return [
            {"role": "user" if r["direction"] == DIR_IN else "assistant", "content": r["text"]}
            for r in rows
        ]

    async def update_message(
        self,
        message_id: int,
        *,
        text: Optional[str] = None,
        status: Optional[str] = None,
        telegram_id: Optional[int] = None,
        attachments: Optional[list[int]] = None,
    ) -> Optional[dict[str, Any]]:
        sets: list[str] = []
        params: list[Any] = [self._session_id, message_id]
        if text is not None:
            params.append(text)
            sets.append(f"text = ${len(params)}")
        if attachments is not None:
            params.append(list(attachments))
            sets.append(f"attachments = ${len(params)}")
        if status is not None:
            params.append(status)
            sets.append(f"status = ${len(params)}")
        if telegram_id is not None:
            params.append(telegram_id)
            sets.append(f"telegram_id = ${len(params)}")
        if not sets:
            return await self.get_message(message_id)
        sql = f"UPDATE messages SET {', '.join(sets)} WHERE session_id = $1 AND id = $2"
        async with self._pool.acquire() as con:
            await con.execute(sql, *params)
        return await self.get_message(message_id)

    async def pending_drafts(self, chat_id: Optional[int] = None) -> list[dict[str, Any]]:
        if chat_id is not None:
            sql = "SELECT * FROM messages WHERE session_id = $1 AND status = $2 AND chat_id = $3 ORDER BY id ASC"
            params = [self._session_id, STATUS_PENDING, chat_id]
        else:
            sql = "SELECT * FROM messages WHERE session_id = $1 AND status = $2 ORDER BY id ASC"
            params = [self._session_id, STATUS_PENDING]
        async with self._pool.acquire() as con:
            rows = await con.fetch(sql, *params)
        return [_message(r) for r in rows]

    async def reject_pending(self, chat_id: int) -> list[int]:
        """Drop still-pending drafts for a chat; returns the ids affected."""
        drafts = await self.pending_drafts(chat_id)
        for d in drafts:
            await self.update_message(d["id"], status=STATUS_REJECTED)
        return [d["id"] for d in drafts]

    async def set_conversation_preview(self, chat_id: int, text: str) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "UPDATE conversations SET last_message_at = now(), last_message_preview = $3 "
                "WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
                _preview(text),
            )

    # ------------------------------------------------------------------
    # Outreach — messages we start, rather than reply to
    # ------------------------------------------------------------------

    async def queue_outreach(
        self, recipients: Iterable[tuple[int, str]], goal: str
    ) -> list[dict[str, Any]]:
        """Queue one message per (chat_id, display_name).

        Skips anyone who already has a message waiting — queued, or drafted
        and sitting unapproved — so a person never ends up with two unsent
        openers.
        """
        created: list[int] = []
        async with self._pool.acquire() as con, con.transaction():
            for chat_id, name in recipients:
                exists = await con.fetchval(
                    "SELECT 1 FROM outreach WHERE session_id = $1 AND chat_id = $2 AND status = ANY($3)",
                    self._session_id,
                    chat_id,
                    [OUT_QUEUED, OUT_DRAFTED],
                )
                if exists:
                    continue
                row = await con.fetchrow(
                    """
                    INSERT INTO outreach (session_id, chat_id, display_name, goal, status, created_at)
                    VALUES ($1, $2, $3, $4, $5, now())
                    RETURNING id
                    """,
                    self._session_id,
                    chat_id,
                    name,
                    goal,
                    OUT_QUEUED,
                )
                created.append(row["id"])
        return [row for row in [await self.get_outreach(i) for i in created] if row]

    async def get_outreach(self, outreach_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM outreach WHERE session_id = $1 AND id = $2",
                self._session_id,
                outreach_id,
            )
        return _outreach(row) if row else None

    async def list_outreach(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM (
                    SELECT * FROM outreach WHERE session_id = $1 ORDER BY id DESC LIMIT $2
                ) AS recent ORDER BY id ASC
                """,
                self._session_id,
                limit,
            )
        return [_outreach(r) for r in rows]

    async def next_queued_outreach(self) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM outreach WHERE session_id = $1 AND status = $2 ORDER BY id ASC LIMIT 1",
                self._session_id,
                OUT_QUEUED,
            )
        return _outreach(row) if row else None

    async def update_outreach(
        self,
        outreach_id: int,
        *,
        status: Optional[str] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
        draft_id: Optional[int] = None,
        mark_sent: bool = False,
    ) -> Optional[dict[str, Any]]:
        sets: list[str] = []
        params: list[Any] = [self._session_id, outreach_id]
        for column, value in (
            ("status", status),
            ("message", message),
            ("error", error),
            ("draft_id", draft_id),
        ):
            if value is not None:
                params.append(value)
                sets.append(f"{column} = ${len(params)}")
        if mark_sent:
            sets.append("sent_at = now()")
        if not sets:
            return await self.get_outreach(outreach_id)
        sql = f"UPDATE outreach SET {', '.join(sets)} WHERE session_id = $1 AND id = $2"
        async with self._pool.acquire() as con:
            await con.execute(sql, *params)
        return await self.get_outreach(outreach_id)

    async def outreach_for_draft(self, draft_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM outreach WHERE session_id = $1 AND draft_id = $2",
                self._session_id,
                draft_id,
            )
        return _outreach(row) if row else None

    async def cancel_queued_outreach(self) -> int:
        """Stop everything not yet acted on. Returns how many were cancelled."""
        async with self._pool.acquire() as con:
            result = await con.execute(
                "UPDATE outreach SET status = $2 WHERE session_id = $1 AND status = $3",
                self._session_id,
                OUT_CANCELLED,
                OUT_QUEUED,
            )
        return _affected(result)

    async def outreach_sent_since(self, iso_timestamp: str) -> int:
        async with self._pool.acquire() as con:
            n = await con.fetchval(
                "SELECT COUNT(*) FROM outreach WHERE session_id = $1 AND status = $2 AND sent_at >= $3",
                self._session_id,
                OUT_SENT,
                _ts(iso_timestamp),
            )
        return n or 0

    async def sent_since(self, iso_timestamp: str) -> int:
        """Every message this account has sent since a moment — replies
        included. Telegram's spam heuristics count total outbound volume,
        not just conversations we started, so the daily ceiling has to see
        all of it."""
        async with self._pool.acquire() as con:
            n = await con.fetchval(
                "SELECT COUNT(*) FROM messages "
                " WHERE session_id = $1 AND direction = $2 AND status = $3 AND created_at >= $4",
                self._session_id,
                DIR_OUT,
                STATUS_SENT,
                _ts(iso_timestamp),
            )
        return n or 0

    async def distinct_peers_since(self, iso_timestamp: str) -> int:
        """How many different people we have written to since a moment.

        Messaging many *different* people is a far stronger spam signal
        than sending many messages inside one ongoing conversation.
        """
        async with self._pool.acquire() as con:
            n = await con.fetchval(
                "SELECT COUNT(DISTINCT chat_id) FROM messages "
                " WHERE session_id = $1 AND direction = $2 AND status = $3 AND created_at >= $4",
                self._session_id,
                DIR_OUT,
                STATUS_SENT,
                _ts(iso_timestamp),
            )
        return n or 0

    # ------------------------------------------------------------------
    # Chat links — carrying one conversation's context into another
    # ------------------------------------------------------------------

    async def link_chats(
        self,
        chat_id: int,
        source_id: int,
        origin: str = LINK_MANUAL,
        reason: str = "",
        confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Tie two chats together in both directions. Returns both link rows.

        Re-linking an existing pair updates it rather than failing, so a
        link I made by hand replaces the one that was guessed, keeping my
        reason.
        """
        if chat_id == source_id:
            raise ValueError("A chat cannot be linked to itself.")
        async with self._pool.acquire() as con, con.transaction():
            for a, b in ((chat_id, source_id), (source_id, chat_id)):
                await con.execute(
                    """
                    INSERT INTO chat_links (session_id, chat_id, source_id, origin, reason,
                                            confidence, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now())
                    ON CONFLICT (session_id, chat_id, source_id) DO UPDATE SET
                        origin     = excluded.origin,
                        reason     = excluded.reason,
                        confidence = excluded.confidence
                    """,
                    self._session_id,
                    a,
                    b,
                    origin,
                    reason,
                    float(confidence),
                )
        return [
            row
            for row in (
                await self.get_link(chat_id, source_id),
                await self.get_link(source_id, chat_id),
            )
            if row is not None
        ]

    async def unlink_chats(self, chat_id: int, source_id: int, block: bool = True) -> int:
        """Cut the link both ways. Returns how many rows went.

        `block` leaves a marker behind so detection does not re-link the
        pair on their next message — an unlink I did by hand has to stick.
        """
        async with self._pool.acquire() as con, con.transaction():
            result = await con.execute(
                """
                DELETE FROM chat_links
                 WHERE session_id = $1
                   AND ((chat_id = $2 AND source_id = $3) OR (chat_id = $3 AND source_id = $2))
                """,
                self._session_id,
                chat_id,
                source_id,
            )
            if block:
                for a, b in ((chat_id, source_id), (source_id, chat_id)):
                    await con.execute(
                        """
                        INSERT INTO chat_links (session_id, chat_id, source_id, origin, reason,
                                                confidence, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, now())
                        ON CONFLICT (session_id, chat_id, source_id) DO UPDATE SET
                            origin     = excluded.origin,
                            reason     = excluded.reason,
                            confidence = excluded.confidence,
                            created_at = excluded.created_at
                        """,
                        self._session_id,
                        a,
                        b,
                        LINK_BLOCKED,
                        "unlinked by hand",
                        0.0,
                    )
        return _affected(result)

    async def blocked_sources(self, chat_id: int) -> set[int]:
        """Chats this one was deliberately unlinked from."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                "SELECT source_id FROM chat_links WHERE session_id = $1 AND chat_id = $2 AND origin = $3",
                self._session_id,
                chat_id,
                LINK_BLOCKED,
            )
        return {r["source_id"] for r in rows}

    async def get_link(self, chat_id: int, source_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                SELECT l.*, c.display_name AS source_name, c.username AS source_username
                  FROM chat_links l
                  LEFT JOIN conversations c
                    ON c.session_id = l.session_id AND c.chat_id = l.source_id
                 WHERE l.session_id = $1 AND l.chat_id = $2 AND l.source_id = $3
                """,
                self._session_id,
                chat_id,
                source_id,
            )
        return _link(row) if row else None

    async def get_links(self, chat_id: int) -> list[dict[str, Any]]:
        """Every chat this one borrows context from, newest link first."""
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT l.*, c.display_name AS source_name, c.username AS source_username
                  FROM chat_links l
                  LEFT JOIN conversations c
                    ON c.session_id = l.session_id AND c.chat_id = l.source_id
                 WHERE l.session_id = $1 AND l.chat_id = $2 AND l.origin <> 'blocked'
                 ORDER BY l.confidence DESC, l.created_at DESC
                """,
                self._session_id,
                chat_id,
            )
        return [_link(r) for r in rows]

    async def all_links(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT l.*, c.display_name AS source_name, c.username AS source_username
                  FROM chat_links l
                  LEFT JOIN conversations c
                    ON c.session_id = l.session_id AND c.chat_id = l.source_id
                 WHERE l.session_id = $1 AND l.origin <> 'blocked'
                 ORDER BY l.created_at DESC
                """,
                self._session_id,
            )
        return [_link(r) for r in rows]

    # ------------------------------------------------------------------
    # Chat summaries — the condensed form a linked chat is carried in
    # ------------------------------------------------------------------

    async def last_message_id(self, chat_id: int) -> int:
        """Highest stored messages.id for a chat; 0 when it has none."""
        async with self._pool.acquire() as con:
            last = await con.fetchval(
                "SELECT MAX(id) FROM messages"
                " WHERE session_id = $1 AND chat_id = $2 AND status = ANY($3) AND direction = ANY($4)",
                self._session_id,
                chat_id,
                [STATUS_RECEIVED, STATUS_SENT],
                [DIR_IN, DIR_OUT],
            )
        return last or 0

    async def get_summary(self, chat_id: int) -> Optional[dict[str, Any]]:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM chat_summaries WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
            )
        if row is None:
            return None
        return {
            "chat_id": row["chat_id"],
            "summary": row["summary"],
            "last_message_id": row["last_message_id"],
            "updated_at": _iso(row["updated_at"]),
        }

    async def save_summary(self, chat_id: int, summary: str, last_message_id: int) -> dict[str, Any]:
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO chat_summaries (session_id, chat_id, summary, last_message_id, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (session_id, chat_id) DO UPDATE SET
                    summary         = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    updated_at      = excluded.updated_at
                """,
                self._session_id,
                chat_id,
                summary,
                last_message_id,
            )
        return await self.get_summary(chat_id)  # type: ignore[return-value]

    async def clear_summary(self, chat_id: int) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                "DELETE FROM chat_summaries WHERE session_id = $1 AND chat_id = $2",
                self._session_id,
                chat_id,
            )


def _affected(result: str) -> int:
    """asyncpg's `execute()` returns a command tag string like 'UPDATE 3' or
    'DELETE 1'; pull the row count out of it."""
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0


def _link(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "chat_id": row["chat_id"],
        "source_id": row["source_id"],
        "source_name": row["source_name"] or str(row["source_id"]),
        "source_username": row["source_username"],
        "origin": row["origin"],
        "reason": row["reason"],
        "confidence": row["confidence"],
        "created_at": _iso(row["created_at"]),
    }


def _conversation(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "chat_id": row["chat_id"],
        "display_name": row["display_name"],
        "username": row["username"],
        "is_bot": bool(row["is_bot"]),
        "automation_paused": bool(row["automation_paused"]),
        "unread": row["unread"],
        "last_message_at": _iso(row["last_message_at"]),
        "last_message_preview": row["last_message_preview"],
    }


def _outreach(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "display_name": row["display_name"],
        "goal": row["goal"],
        "status": row["status"],
        "message": row["message"],
        "error": row["error"],
        "draft_id": row["draft_id"],
        "created_at": _iso(row["created_at"]),
        "sent_at": _iso(row["sent_at"]),
    }


def _message(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "telegram_id": row["telegram_id"],
        "direction": row["direction"],
        "status": row["status"],
        "text": row["text"],
        "created_at": _iso(row["created_at"]),
        "attachments": _attachments(row),
    }


def _attachments(row: asyncpg.Record) -> list[int]:
    raw = row["attachments"]
    if not raw:
        return []
    return [int(x) for x in raw]
