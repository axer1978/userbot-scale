"""WhatsApp transport: the account's socket lives in the wa-gateway service
(wa_gateway/, Baileys); this side asks it over the bus (commands.py) and
takes the messages it hands over through Postgres (wa_inbox).

Order of things, which is the point of the design:

- The runtime takes the account's lease first (leasing.py); only then does
  start() ask the gateway to `open` the socket, with the lease epoch. The
  gateway checks that epoch against Postgres and fences every later
  command with it, so a worker that lost the lease can't drive a socket
  another worker now owns.
- `open` is repeated every KEEPALIVE_SECONDS. For a socket that is already
  open it only reports the state; after a gateway restart it opens the
  socket again, with no pairing needed.
- Stopping (or losing the lease) sends `close`. The gateway never opens a
  socket on its own.
- Inbound messages wait in wa_inbox until this runtime has stored them;
  only then is the row deleted. A message delivered twice is recognised by
  its WhatsApp id and stored once.
- A lost session (logged out, banned, replaced) is never reconnected: the
  runtime halts loudly (SessionRuntime.on_session_lost).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

import redis.asyncio as aioredis

import commands
import controls
from database import STATUS_ERROR
import wa_device_profiles
import wa_store
from transport import (
    PEER_FLOOD,
    SESSION_REJECTED,
    UNREACHABLE,
    WHATSAPP,
    Failure,
    Inbound,
    NeedsLogin,
    PeerInfo,
    Transport,
)

log = logging.getLogger("session_runtime")

GATEWAY = "@wa-gateway"
KEEPALIVE_SECONDS = 15.0
OPEN_TIMEOUT = 10.0
CLOSE_TIMEOUT = 5.0
# A send waits for WhatsApp's server ack; presence and read receipts are
# quick on the gateway's side.
SEND_TIMEOUT = 45.0
ACTION_TIMEOUT = 10.0
LOGOUT_TIMEOUT = 30.0
# How many of a chat's newest received messages a read receipt covers.
READ_BATCH = 10
# Baileys rc14's code for "account restricted / reach-out timelocked".
ACCOUNT_RESTRICTED = 463
DRAIN_BATCH = 50
EVENT_RETRY_SECONDS = 2.0
# Chats whose newest read-marked message id is remembered (the oldest
# forgotten beyond this; forgetting only costs one repeated read receipt).
READ_UPTO_MAX = 5_000

# What the gateway's session_lost reasons mean for the operator.
SESSION_LOST = {
    "loggedOut": "the linked device was logged out (from the phone's Linked devices, or by WhatsApp)",
    "forbidden": "WhatsApp refused the account — it may be banned",
    "badSession": "the stored WhatsApp session is corrupt",
    "connectionReplaced": "another WhatsApp Web session took over this login",
    "multideviceMismatch": "WhatsApp's multi-device state no longer matches this login",
}


def event_channel(session_id: str) -> str:
    return f"wa:ev:{session_id}"


class GatewayError(RuntimeError):
    """wa-gateway refused a command. `kind` is its error_kind: stale_epoch,
    not_connected, busy, bad_request, not_found, session_lost,
    rate_limited, not_on_whatsapp, blocked or other."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


def gateway_error(exc: commands.CommandError) -> GatewayError:
    """The gateway's error string is "<error_kind>: <detail>"."""
    if isinstance(exc, commands.CommandTimeout):
        return GatewayError("not_connected", "the WhatsApp gateway is not answering")
    kind, _, detail = str(exc).partition(":")
    if getattr(exc, "kind", None):
        return GatewayError(exc.kind, detail.strip() if kind.strip() == exc.kind else str(exc))
    kind = kind.strip()
    if not kind or " " in kind:
        return GatewayError("other", str(exc))
    return GatewayError(kind, detail.strip())


class WhatsAppTransport(Transport):
    channel = WHATSAPP
    network = "WhatsApp"
    id_field = "wa_message_id"
    hold_kind = controls.WHATSAPP
    can_send = True
    # A send WhatsApp refused is stored red in the thread, text and all.
    record_failed_sends = True
    # Only text goes out on WhatsApp for now: no media library in replies.
    can_send_files = False

    def __init__(self, rt: Any) -> None:
        super().__init__(rt)
        self.state: dict[str, Any] = {"connected": False, "error": None}
        self.me_info: dict[str, Any] = {}
        # Set once the gateway reports the session lost: nothing reopens it.
        self.lost = False
        self._events_task: Optional[asyncio.Task] = None
        self._keeper_task: Optional[asyncio.Task] = None
        self._drain_lock = asyncio.Lock()
        self._redis: Optional[aioredis.Redis] = None
        self._gateway_down_logged = False
        # chat_id -> the newest received message id already marked read.
        self._read_upto: dict[int, str] = {}

    # ----------------------------------------------------------- state

    @property
    def connected(self) -> bool:
        return self.state["connected"]

    @property
    def error(self) -> Optional[str]:
        return self.state["error"]

    @property
    def me(self) -> dict[str, Any]:
        return self.me_info

    def adapt_prompt(self, text: str) -> str:
        # The base rules were written for Telegram ("in a Telegram chat").
        return text.replace("Telegram", "WhatsApp")

    # ------------------------------------------------------- lifecycle

    async def prepare(self) -> None:
        rt = self.rt
        if not await wa_store.has_login(rt.pool, rt.session_id):
            raise NeedsLogin(
                f"session {rt.session_id!r} has no WhatsApp login yet — pair it from the panel "
                "(+ Add account → WhatsApp) before starting this runtime."
            )

    async def reload_login(self) -> str:
        if not await wa_store.has_login(self.rt.pool, self.rt.session_id):
            raise ValueError("This account has no WhatsApp login to reconnect with.")
        log.warning("[%s] Reconnecting to WhatsApp (through the gateway).", self.rt.session_id)
        return "through the WhatsApp gateway"

    def _open_args(self) -> dict[str, Any]:
        args: dict[str, Any] = {"session_id": self.rt.session_id, "epoch": self.rt.lease_epoch}
        # The device identity the number was paired with (stored by the
        # panel), else the same deterministic one the panel would pick.
        browser = (self.rt.account.get("identity") or {}).get("wa_browser")
        args["browser"] = list(browser) if browser else list(wa_device_profiles.derive(self.rt.session_id))
        return args

    async def start(self) -> None:
        self.lost = False
        self._events_task = asyncio.create_task(self._listen())
        self._keeper_task = asyncio.create_task(self._keep_open())

    async def halt_updates(self) -> None:
        for name in ("_keeper_task", "_events_task"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def disconnect(self) -> None:
        """Ask the gateway to close the socket. Best effort: if it can't be
        told, its lease watchdog closes the socket once the lease is gone."""
        rt = self.rt
        if rt.bus is not None and rt.lease_epoch:
            try:
                await rt.bus.dispatch(GATEWAY, "close", {"session_id": rt.session_id, "epoch": rt.lease_epoch},
                                      timeout=CLOSE_TIMEOUT)
            except Exception as exc:
                log.warning("[%s] Could not tell the WhatsApp gateway to close (%s); its lease watchdog will.",
                            rt.session_id, type(exc).__name__)
        if self._redis is not None:
            with suppress(Exception):
                await self._redis.aclose()
            self._redis = None
        self.state["connected"] = False
        self.state["error"] = None
        self.me_info.clear()

    async def _keep_open(self) -> None:
        rt = self.rt
        while True:
            # This loop is what brings the socket back after a gateway
            # restart and what drains the inbox when no event arrives: no
            # failure in one round (a database blip inside on_connected,
            # say) may end it.
            if not self.lost:
                try:
                    await self._open_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] The WhatsApp keepalive round failed; retrying in %.0fs.",
                                  rt.session_id, KEEPALIVE_SECONDS)
                try:
                    await self.drain()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] Taking WhatsApp messages from the inbox failed; retrying.", rt.session_id)
            await asyncio.sleep(KEEPALIVE_SECONDS)

    async def _open_once(self) -> None:
        rt = self.rt
        try:
            result = await rt.bus.dispatch(GATEWAY, "open", self._open_args(), timeout=OPEN_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except commands.CommandTimeout:
            await self._set_disconnected("The WhatsApp gateway is not answering.")
            if not self._gateway_down_logged:
                log.error("[%s] The WhatsApp gateway is not answering; retrying every %.0fs.",
                          rt.session_id, KEEPALIVE_SECONDS)
                self._gateway_down_logged = True
            return
        except commands.CommandError as exc:
            detail = str(exc)
            if getattr(exc, "kind", None) == "session_lost":
                # The gateway already lost this login (logged out, replaced,
                # banned) and its session_lost event did not reach us (the
                # bus dropped it, or we were restarting). Halt as if it had:
                # otherwise this loop would ask again every KEEPALIVE_SECONDS
                # and the panel would never show why.
                reason = gateway_error(exc).detail.split(";", 1)[0].strip() or "unknown"
                await self.session_lost(reason)
                return
            await self._set_disconnected(f"WhatsApp gateway: {detail}")
            log.error("[%s] The WhatsApp gateway refused to open the socket: %s", rt.session_id, detail)
            return
        self._gateway_down_logged = False
        state = (result or {}).get("state") if isinstance(result, dict) else None
        if state == "open" and not self.connected:
            await self._set_connected((result or {}).get("me") or {})
        elif state not in (None, "open", "opening", "connecting") and self.connected:
            await self._set_disconnected(f"WhatsApp socket is {state}.")

    async def _set_connected(self, me: dict[str, Any]) -> None:
        was = self.connected
        self.me_info = {
            "id": None,
            "name": me.get("name") or wa_store.phone_of(me.get("jid")) or "WhatsApp",
            "username": None,
            "jid": me.get("jid"),
            "lid": me.get("lid"),
        }
        self.state["connected"] = True
        self.state["error"] = None
        if not was:
            await self.rt.on_connected()
            await self.drain()

    async def _set_disconnected(self, error: str) -> None:
        was = self.connected
        self.state["connected"] = False
        if self.state["error"] != error or was:
            self.state["error"] = error
            await self.rt.on_connection_error(error)

    # ---------------------------------------------------------- events

    async def _listen(self) -> None:
        rt = self.rt
        channel = event_channel(rt.session_id)
        while True:
            try:
                if self._redis is None:
                    self._redis = aioredis.from_url(rt.redis_url, decode_responses=True,
                                                    socket_connect_timeout=commands.CONNECT_TIMEOUT_SECONDS)
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(channel)
                try:
                    while True:
                        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                        if message is not None:
                            await self._on_event(message["data"])
                finally:
                    with suppress(Exception):
                        await pubsub.unsubscribe(channel)
                        await pubsub.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[%s] WhatsApp event subscription dropped (%s); resubscribing.",
                            rt.session_id, type(exc).__name__)
                await asyncio.sleep(EVENT_RETRY_SECONDS)

    async def _on_event(self, data: Any) -> None:
        rt = self.rt
        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            log.warning("[%s] Malformed WhatsApp gateway event dropped.", rt.session_id)
            return
        kind = event.get("type")
        try:
            if kind == "connection":
                state = event.get("state")
                if state == "open":
                    await self._set_connected(event.get("me") or {})
                elif state in ("closed", "connecting"):
                    await self._set_disconnected(f"WhatsApp socket is {state}.")
            elif kind == "session_lost":
                await self.session_lost(str(event.get("reason") or "unknown"), event.get("code"))
            elif kind == "inbox":
                await self.drain()
            elif kind == "message_failed":
                await self.delivery_failed(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] Handling a WhatsApp gateway event (%s) failed", rt.session_id, kind)

    async def session_lost(self, reason: str, code: Any = None) -> None:
        if self.lost:
            return
        self.lost = True
        self.state["connected"] = False
        why = SESSION_LOST.get(reason, reason)
        self.state["error"] = f"WhatsApp session lost: {why}"
        await self.rt.on_session_lost(reason, why, code)

    async def delivery_failed(self, event: dict[str, Any]) -> None:
        """WhatsApp refused a message after it was sent (Baileys does not
        wait for the server, so this arrives as a later status update). The
        message turns red in the thread, and the failure is acted on like a
        refused send: a rate limit halts, a blocked recipient pauses."""
        rt = self.rt
        wa_message_id = str(event.get("wa_message_id") or "")
        kind = str(event.get("error_kind") or "other")
        code = event.get("code")
        if code == ACCOUNT_RESTRICTED:
            # WhatsApp has restricted the account (no new chats). Treated
            # like a rate limit: everything halts until a person looks.
            kind = "rate_limited"
        row = await rt.pool.fetchrow(
            "SELECT id, chat_id FROM messages WHERE session_id = $1 AND wa_message_id = $2",
            rt.session_id, wa_message_id,
        )
        chat_id = row["chat_id"] if row else None
        if chat_id is None and event.get("jid"):
            phone_jid, lid = wa_store.split_jid(event.get("jid"))
            if phone_jid or lid:
                chat_id, _ = await wa_store.chat_for(rt.pool, rt.session_id, phone_jid=phone_jid, lid=lid)
        log.warning("[%s] WhatsApp did not deliver message %s (%s, code %s).", rt.session_id, wa_message_id,
                    kind, code)
        if row is not None:
            updated = await rt.db.update_message(row["id"], status=STATUS_ERROR)
            if updated is not None:
                await rt.push_message(updated)
        failure = GatewayError(kind, f"delivery failed (code {code})")
        if not await rt.handle_send_failure(chat_id, failure):
            await rt.push_error(chat_id, f"WhatsApp did not deliver a message ({kind}, code {code}).")

    # --------------------------------------------------------- inbound

    async def drain(self) -> int:
        """Store every message waiting in wa_inbox for this account, oldest
        first, deleting each handoff row only once it is stored."""
        rt = self.rt
        taken = 0
        async with self._drain_lock:
            while True:
                batch = await wa_store.inbox_batch(rt.pool, rt.session_id, DRAIN_BATCH)
                if not batch:
                    return taken
                for row in batch:
                    await self._take(row["payload"], row["wa_message_id"])
                    await wa_store.inbox_ack(rt.pool, rt.session_id, row["id"])
                    taken += 1

    async def _take(self, payload: dict[str, Any], wa_message_id: str) -> None:
        rt = self.rt
        phone_jid = payload.get("phone_jid")
        lid = payload.get("lid")
        if not phone_jid and not lid:
            phone_jid, lid = wa_store.split_jid(payload.get("jid"))
        if not phone_jid and not lid:
            log.warning("[%s] WhatsApp message %s has no usable JID; dropped.", rt.session_id, wa_message_id)
            return
        from_me = bool(payload.get("from_me"))
        chat_id, _ = await wa_store.chat_for(
            rt.pool, rt.session_id, phone_jid=phone_jid, lid=lid,
            push_name=None if from_me else (payload.get("push_name") or None),
        )
        if await rt.db.find_by_wa_message_id(chat_id, wa_message_id) is not None:
            return  # delivered before: stored once, handled once

        async def load_peer() -> PeerInfo:
            return PeerInfo(wa_store.display_name(await wa_store.peer(rt.pool, rt.session_id, chat_id)))

        message = Inbound(
            chat_id=chat_id,
            text=(payload.get("text") or "").strip(),
            external_id=wa_message_id,
            load_peer=load_peer,
            from_me=from_me,
            has_photo=payload.get("type") == "image",
            reply_to=payload.get("quoted_id"),
            raw=payload,
        )
        if from_me:
            await rt.handle_own_echo(message)
        else:
            await rt.handle_inbound(message)

    # --------------------------------------------------------- sending

    async def resolve_peer(self, chat_id: int) -> Any:
        if not self.connected:
            raise RuntimeError("WhatsApp is not connected.")
        row = await wa_store.peer(self.rt.pool, self.rt.session_id, chat_id)
        if row is None:
            raise RuntimeError(f"Cannot resolve chat {chat_id}. Receive a message from them first.")
        return row["jid"]

    async def _call(self, action: str, args: dict[str, Any], timeout: float) -> Any:
        """A socket-bound gateway command, fenced by this runtime's lease
        epoch. Raises GatewayError."""
        rt = self.rt
        if rt.bus is None:
            raise GatewayError("not_connected", "the bus is not connected")
        try:
            return await rt.bus.dispatch(
                GATEWAY, action, {"session_id": rt.session_id, "epoch": rt.lease_epoch, **args}, timeout=timeout,
            )
        except commands.CommandError as exc:
            raise gateway_error(exc) from exc

    async def send_text(self, peer: Any, chat_id: int, text: str, typing_seconds: Optional[float]) -> Any:
        """Send one text. With `typing_seconds` the chat shows "typing…"
        (composing) that long first; the message goes out while it shows,
        then the indicator is cleared. A failed indicator never stops the
        message; a failed send is never retried here (it may have gone)."""
        if typing_seconds is not None:
            try:
                await self._call("presence", {"jid": peer, "state": "composing"}, ACTION_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[%s] Typing indicator unavailable (%s); sending anyway.", self.rt.session_id,
                            type(exc).__name__)
            await asyncio.sleep(typing_seconds)
        try:
            return await self._call("send_text", {"jid": peer, "text": text}, SEND_TIMEOUT)
        finally:
            if typing_seconds is not None:
                with suppress(Exception):
                    await self._call("presence", {"jid": peer, "state": "paused"}, ACTION_TIMEOUT)

    async def send_file(self, peer: Any, chat_id: int, path: Path, is_video: bool, show_upload: bool,
                        view_once: bool = False) -> Any:
        raise GatewayError("bad_request", "sending files on WhatsApp is not supported")

    # WhatsApp deletes one's own messages for everyone for about two days.
    can_delete = True

    async def delete_messages(self, peer: Any, chat_id: int, message_ids: list[Any]) -> None:
        row = await wa_store.peer(self.rt.pool, self.rt.session_id, chat_id)
        if row is None:
            raise GatewayError("not_found", "no WhatsApp chat for this conversation")
        await self._call("delete", {"jid": row["jid"], "message_ids": [str(i) for i in message_ids]},
                         ACTION_TIMEOUT)

    def message_id(self, sent: Any) -> Any:
        return sent.get("message_id") if isinstance(sent, dict) else None

    def classify(self, exc: BaseException) -> Optional[Failure]:
        if not isinstance(exc, GatewayError):
            return None
        if exc.kind == "rate_limited":
            # WhatsApp publishes no wait to sleep through: like Telegram's
            # PeerFlood, the account halts (safety.halt_on_peer_flood).
            return Failure(PEER_FLOOD, "a rate limit (rate-overlimit)")
        if exc.kind == "session_lost":
            return Failure(SESSION_REJECTED, "session lost")
        if exc.kind in ("not_on_whatsapp", "blocked"):
            return Failure(UNREACHABLE, "not on WhatsApp" if exc.kind == "not_on_whatsapp" else "blocked")
        return None

    # ---------------------------------------------------- chat actions

    async def mark_read(self, chat_id: int, message_id: Any = None) -> None:
        """Blue ticks for the chat's newest received messages not yet marked
        (or just `message_id`). Whether the sender sees them depends on the
        account's own read-receipt privacy setting."""
        rt = self.rt
        if message_id is not None:
            ids = [message_id]
        else:
            rows = await rt.pool.fetch(
                """
                SELECT wa_message_id FROM messages
                 WHERE session_id = $1 AND chat_id = $2 AND direction = 'in' AND wa_message_id IS NOT NULL
                 ORDER BY id DESC LIMIT $3
                """,
                rt.session_id, chat_id, READ_BATCH,
            )
            ids = []
            for row in rows:
                if row["wa_message_id"] == self._read_upto.get(chat_id):
                    break
                ids.append(row["wa_message_id"])
        if not ids:
            return
        row = await wa_store.peer(rt.pool, rt.session_id, chat_id)
        if row is None:
            return
        await self._call("read", {"jid": row["jid"], "message_ids": list(reversed(ids))}, ACTION_TIMEOUT)
        self._read_upto.pop(chat_id, None)
        self._read_upto[chat_id] = ids[0]
        while len(self._read_upto) > READ_UPTO_MAX:
            self._read_upto.pop(next(iter(self._read_upto)))

    async def set_presence(self, online: bool) -> None:
        await self._call("presence", {"state": "available" if online else "unavailable"}, ACTION_TIMEOUT)

    # --------------------------------------------------------- lookups

    async def list_contacts(self) -> list[tuple[int, PeerInfo]]:
        raise ValueError("Outreach is not available for WhatsApp accounts.")

    async def resolve_owner(self, value: str) -> tuple[int, PeerInfo]:
        """booking.provider for a WhatsApp account is the owner's phone number."""
        rt = self.rt
        chat_id, _ = await wa_store.chat_for(rt.pool, rt.session_id, phone_jid=wa_store.phone_jid_for(value))
        return chat_id, PeerInfo(wa_store.display_name(await wa_store.peer(rt.pool, rt.session_id, chat_id)))

    async def download_photo(self, message: Inbound, path: Optional[Path] = None) -> Optional[bytes]:
        return None  # photos are not fetched from WhatsApp yet

    async def log_out(self) -> bool:
        """Hard-off: unlink this device on WhatsApp's side."""
        result = await self._call("logout", {}, LOGOUT_TIMEOUT)
        return bool((result or {}).get("logged_out"))
