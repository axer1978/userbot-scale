"""Telegram transport: every Telethon call one account makes, behind
transport.Transport. Moved here unchanged from session_runtime.py; the
business logic that decides when to call any of this stayed there.

Logs go to the "session_runtime" logger, as they did before the split, so
the manager's log reads the same.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

import asyncpg
from telethon import TelegramClient, errors, events, utils
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from telethon.tl.functions.account import GetAuthorizationsRequest, UpdateStatusRequest
from telethon.tl.functions.contacts import GetContactsRequest, ResolvePhoneRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInviteAlready,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    PeerChannel,
    PeerChat,
    User,
)

import anomaly
import config_store
import controls
import device_profiles
import proxies
from database import SessionRegistry
from transport import (
    PEER_FLOOD,
    RATE_LIMITED,
    SESSION_REJECTED,
    TELEGRAM,
    TELEGRAM_SERVICE_ID,
    UNREACHABLE,
    Failure,
    Inbound,
    NeedsLogin,
    PeerInfo,
    Transport,
)

log = logging.getLogger("session_runtime")

# ttl_seconds that Telegram reads as "view once" rather than a timer.
VIEW_ONCE_TTL = 0x7FFFFFFF

# booking.provider as a group: an invite link, a group's id (negative, as
# the panel's group list gives it) or "group: <its name>". A bare name is
# never looked up as a group: it could be someone else's @username.
INVITE_LINK = re.compile(r"(?:t|telegram)\.me/(?:\+|joinchat/)([\w-]+)", re.IGNORECASE)
PUBLIC_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z]\w{3,})/?", re.IGNORECASE)
GROUP_ID = re.compile(r"-\d+")
GROUP_NAME = re.compile(r"group\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
# Chats looked through for a group by name or id (the newest first).
GROUP_SCAN_DIALOGS = 3000

def describe_sender(sender: Any, fallback_id: int) -> tuple[str, Optional[str], bool, Optional[int]]:
    """(display_name, username, is_bot, access_hash) for a private-chat peer."""
    username = getattr(sender, "username", None)
    access_hash = getattr(sender, "access_hash", None)
    is_bot = bool(getattr(sender, "bot", False))

    if isinstance(sender, User) or hasattr(sender, "first_name"):
        parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
        name = " ".join(p for p in parts if p).strip()
    else:
        name = (getattr(sender, "title", None) or "").strip()

    if not name:
        name = username or f"Chat {fallback_id}"
    return name, username, is_bot, access_hash


def peer_info(sender: Any, fallback_id: int) -> PeerInfo:
    return PeerInfo(*describe_sender(sender, fallback_id))


def _is_group(entity: Any) -> bool:
    """A group the account can take part in: a basic group still in use, or
    a supergroup (not a broadcast channel)."""
    if isinstance(entity, Chat):
        return not (getattr(entity, "deactivated", False) or getattr(entity, "left", False)
                    or getattr(entity, "migrated_to", None))
    return isinstance(entity, Channel) and bool(entity.megagroup) and not getattr(entity, "left", False)


def _client_from_auth(
    auth: dict[str, Any],
    proxy: Optional[str],
    flood_sleep_threshold: int,
    identity: dict[str, Any],
) -> TelegramClient:
    """Build a Telethon client from SessionRegistry.load_auth()'s dict.

    Reconstructs the same (dc_id, server_address, port, auth_key) state a
    StringSession would decode from its base64 blob, except sourced from
    Postgres columns instead of a string — MemorySession is what
    StringSession itself subclasses, so this is exactly equivalent.
    A session with no auth_key yet gets an empty MemorySession, ready for
    a fresh login (handled by the not-yet-ported login flow, not here).
    """
    session = MemorySession()
    if auth.get("auth_key") and auth.get("dc_id"):
        session.set_dc(auth["dc_id"], auth["server_address"], auth["port"])
        session.auth_key = AuthKey(data=auth["auth_key"])
    # Without these Telethon reports the host's own platform and its own
    # version string, identically for every session in the process — see
    # device_profiles.py.
    return TelegramClient(
        session,
        auth["api_id"],
        auth["api_hash"],
        proxy=_parse_proxy(proxy),
        flood_sleep_threshold=flood_sleep_threshold,
        catch_up=True,
        device_model=identity["device_model"],
        system_version=identity["system_version"],
        app_version=identity["app_version"],
        lang_code=identity["lang_code"],
        system_lang_code=identity["system_lang_code"],
    )


def _parse_proxy(proxy_url: Optional[str]) -> Optional[tuple]:
    """The account's proxy URL (socks5://, socks5h:// or http://) ->
    Telethon's proxy tuple; None means a direct connection (proxies.py)."""
    return proxies.telethon_tuple(proxy_url)


class TelegramTransport(Transport):
    channel = TELEGRAM
    network = "Telegram"
    hold_kind = controls.TELEGRAM

    def __init__(self, rt: Any) -> None:
        super().__init__(rt)
        self.client: Optional[TelegramClient] = None
        self.task: Optional[asyncio.Task] = None
        self.state: dict[str, Any] = {"connected": False, "error": None}
        self.me_info: dict[str, Any] = {}
        self._auth: Optional[dict[str, Any]] = None
        # A proxy URL reload_login() already read, used by the next start().
        self._proxy_loaded = False
        self._proxy_url: Optional[str] = None

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

    @property
    def ready(self) -> bool:
        """A client exists and is connected: calls may be made."""
        return self.client is not None and self.state["connected"]

    def is_service_chat(self, chat_id: int) -> bool:
        return chat_id == TELEGRAM_SERVICE_ID

    # ------------------------------------------------------- lifecycle

    async def prepare(self) -> None:
        rt = self.rt
        auth = await rt.registry.load_auth(rt.session_id)
        if auth is None or not auth.get("auth_key"):
            raise NeedsLogin(
                f"session {rt.session_id!r} has no auth_key yet — run the login flow "
                "(SessionRegistry.create + save_login) before starting this runtime."
            )
        self._auth = auth
        rt.api_id = auth["api_id"]
        rt.api_hash = auth["api_hash"]

    async def reload_login(self) -> str:
        rt = self.rt
        auth = await rt.registry.load_auth(rt.session_id)
        if auth is None or not auth.get("auth_key"):
            raise ValueError("This account has no login to reconnect with.")
        proxy_url = await rt.registry.load_proxy(rt.session_id)
        log.warning("[%s] Reconnecting to Telegram (%s).", rt.session_id,
                    "through a proxy" if proxy_url else "directly")
        self._auth, self._proxy_url, self._proxy_loaded = auth, proxy_url, True
        return proxies.describe(proxy_url)

    async def start(self) -> None:
        rt = self.rt
        if self._proxy_loaded:
            proxy_url, self._proxy_loaded = self._proxy_url, False
        else:
            proxy_url = await rt.registry.load_proxy(rt.session_id)
        flood_sleep_threshold = int(rt.config["safety"].get("max_flood_wait_seconds", 300))
        identity = await self._resolve_identity()
        self.client = _client_from_auth(self._auth, proxy_url, flood_sleep_threshold, identity)
        # Both handlers log and swallow their own failures (Telethon runs
        # each update in a task of its own, so one bad message never holds
        # up the next).
        self.client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        self.task = asyncio.create_task(self._run())

    async def _resolve_identity(self) -> dict[str, Any]:
        """This session's device identity, assigned once and then never
        changed. A blank device_model means it has not been assigned yet, so
        derive a deterministic one and persist it — from then on the stored
        value wins, even if device_profiles.py's list later changes."""
        rt = self.rt
        identity = dict(rt.account.get("identity") or {})
        if identity.get("device_model"):
            return identity
        identity = device_profiles.derive(rt.session_id)
        await rt.save_account({**rt.account, "identity": identity})
        log.info(
            "[%s] Assigned device identity: %s / %s / %s",
            rt.session_id, identity["device_model"],
            identity["system_version"], identity["app_version"],
        )
        return identity

    async def halt_updates(self) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def disconnect(self) -> None:
        old, self.client = self.client, None
        if old is not None and old.is_connected():
            with suppress(Exception):
                await old.disconnect()
        self.state["connected"] = False
        self.state["error"] = None
        self.me_info.clear()

    async def _run(self) -> None:
        """Keep the userbot connected; reconnect on failure without killing the process."""
        rt = self.rt
        backoff = 5
        while True:
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    notice = (
                        "The saved Telegram session is no longer valid — it was "
                        "probably ended from Settings -> Devices. Sign in again."
                    )
                    log.error("[%s] %s", rt.session_id, notice)
                    await rt.registry.clear_auth(rt.session_id)
                    await rt.on_logged_out(notice)
                    return

                me = await self.client.get_me()
                self.me_info = {
                    "id": getattr(me, "id", None),
                    "name": describe_sender(me, getattr(me, "id", 0))[0],
                    "username": getattr(me, "username", None),
                }
                self.state["connected"] = True
                self.state["error"] = None
                backoff = 5
                await rt.on_connected()

                await self.client.run_until_disconnected()
                self.state["connected"] = False
                log.warning("[%s] Telegram disconnected; reconnecting…", rt.session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["connected"] = False
                self.state["error"] = f"{type(exc).__name__}: {exc}"
                log.error("[%s] Telegram client error: %s", rt.session_id, self.state["error"])
                await rt.on_connection_error(self.state["error"])

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    # --------------------------------------------------------- inbound

    def _inbound(self, event: Any, *, from_me: bool, group: bool = False, sender: str = "") -> Inbound:
        chat_id = event.chat_id
        message = event.message

        async def load_peer() -> PeerInfo:
            try:
                who = await (event.get_chat() if from_me or group else event.get_sender())
            except Exception:
                who = None
            return peer_info(who, chat_id)

        return Inbound(
            chat_id=chat_id,
            text=(event.raw_text or "").strip(),
            external_id=message.id,
            load_peer=load_peer,
            from_me=from_me,
            has_photo=getattr(message, "photo", None) is not None,
            reply_to=getattr(message, "reply_to_msg_id", None),
            is_service=chat_id == TELEGRAM_SERVICE_ID,
            raw=event,
            group=group,
            sender=sender,
        )

    async def _booker_group(self, event: Any) -> bool:
        """A message outside a private chat counts only in the group set as
        the booker (booking.provider); every other group, and channels, are
        never read."""
        return event.chat_id == await self.rt.provider_chat_id()

    async def on_incoming(self, event: Any) -> None:
        """Telethon's handler for a new message. Whatever goes wrong with
        one message is logged here, with the account and chat, and never
        reaches Telethon, so the next message is handled as usual."""
        try:
            if event.is_private:
                await self.rt.handle_inbound(self._inbound(event, from_me=False))
            elif await self._booker_group(event):
                try:
                    who = await event.get_sender()
                except Exception:
                    who = None
                sender = peer_info(who, event.sender_id or 0).name
                await self.rt.handle_inbound(self._inbound(event, from_me=False, group=True, sender=sender))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] Could not handle a message in chat %s", self.rt.session_id,
                          getattr(event, "chat_id", None))

    async def on_outgoing(self, event: Any) -> None:
        try:
            if not event.is_private and not await self._booker_group(event):
                return
            await self.rt.handle_own_echo(self._inbound(event, from_me=True, group=not event.is_private))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] Could not handle an outgoing message in chat %s", self.rt.session_id,
                          getattr(event, "chat_id", None))

    # --------------------------------------------------------- sending

    async def resolve_peer(self, chat_id: int) -> Any:
        if not self.ready:
            raise RuntimeError("Telegram is not connected.")
        try:
            return await self.client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            # The client's memory is empty after a restart: rebuilt from the
            # access hash stored with the conversation (a basic group needs none).
            real_id, kind = utils.resolve_id(chat_id)
            if kind is PeerChat:
                return InputPeerChat(real_id)
            access_hash = await self.rt.db.get_access_hash(chat_id)
            if access_hash is None:
                raise RuntimeError(
                    f"Cannot resolve chat {chat_id}. Receive a message from them first."
                )
            if kind is PeerChannel:
                return InputPeerChannel(real_id, access_hash)
            return InputPeerUser(chat_id, access_hash)

    async def send_text(self, peer: Any, chat_id: int, text: str, typing_seconds: Optional[float]) -> Any:
        if typing_seconds is None:
            return await self.client.send_message(peer, text)

        result = None
        attempted = False
        try:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(typing_seconds)
                attempted = True
                result = await self.client.send_message(peer, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if result is not None:
                return result
            if attempted:
                raise
            log.warning("[%s] Typing indicator unavailable (%s); sending anyway.", self.rt.session_id,
                        type(exc).__name__)
        else:
            return result

        return await self.client.send_message(peer, text)

    can_delete = True

    async def send_file(self, peer: Any, chat_id: int, path: Path, is_video: bool, show_upload: bool,
                        view_once: bool = False) -> Any:
        # Telegram's "view once": the self-destruct timer set to its maximum.
        # Photos and videos only, as files (not albums or documents).
        ttl = VIEW_ONCE_TTL if view_once else None
        if not show_upload:
            return await self.client.send_file(peer, str(path), supports_streaming=is_video, ttl=ttl)
        try:
            async with self.client.action(chat_id, "video" if is_video else "photo") as progress:
                return await self.client.send_file(
                    peer, str(path), supports_streaming=is_video, progress_callback=progress.progress, ttl=ttl,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, (errors.RPCError, OSError)):
                raise
            log.warning("[%s] Upload indicator unavailable (%s); sending anyway.", self.rt.session_id,
                        type(exc).__name__)
        return await self.client.send_file(peer, str(path), supports_streaming=is_video, ttl=ttl)

    async def delete_messages(self, peer: Any, chat_id: int, message_ids: list[Any]) -> None:
        # revoke=True: gone for the customer too. In a private chat Telegram
        # allows that for one's own messages at any age.
        await self.client.delete_messages(peer, [int(i) for i in message_ids], revoke=True)

    def message_id(self, sent: Any) -> Any:
        return getattr(sent, "id", None)

    def classify(self, exc: BaseException) -> Optional[Failure]:
        name = type(exc).__name__
        if isinstance(exc, errors.PeerFloodError):
            return Failure(PEER_FLOOD, name)
        if isinstance(exc, (errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                            errors.SessionRevokedError)):
            return Failure(SESSION_REJECTED, name)
        if isinstance(exc, (errors.FloodWaitError, errors.SlowModeWaitError)):
            return Failure(RATE_LIMITED, name, int(getattr(exc, "seconds", 0) or 0))
        if isinstance(exc, (errors.UserIsBlockedError, errors.UserPrivacyRestrictedError,
                            errors.InputUserDeactivatedError, errors.ChatWriteForbiddenError)):
            return Failure(UNREACHABLE, name)
        return None

    # ---------------------------------------------------- chat actions

    async def mark_read(self, chat_id: int, message_id: Any = None) -> None:
        await self.client.send_read_acknowledge(chat_id, max_id=message_id)

    async def set_presence(self, online: bool) -> None:
        await self.client(UpdateStatusRequest(offline=not online))

    # --------------------------------------------------------- lookups

    async def list_contacts(self) -> list[tuple[int, PeerInfo]]:
        result = await self.client(GetContactsRequest(hash=0))
        return [
            (user.id, peer_info(user, user.id))
            for user in getattr(result, "users", [])
            if not (getattr(user, "deleted", False) or getattr(user, "is_self", False))
        ]

    async def resolve_owner(self, value: str) -> tuple[int, PeerInfo]:
        entity = await self._owner_entity(value.strip())
        if isinstance(entity, Channel) and not entity.megagroup:
            raise ValueError("a channel can't answer booking requests: use a group")
        # A group's chat id is negative (-100… for a supergroup), as in its events.
        chat_id = utils.get_peer_id(entity)
        return chat_id, peer_info(entity, chat_id)

    async def _owner_entity(self, value: str) -> Any:
        """booking.provider: @username, numeric id or phone number of a
        person; or a group this account is in, by its invite link, its id
        (negative) or "group: <name>". A public group's @username or t.me
        link works like a person's.
        Telethon's get_entity finds a phone only among this account's
        contacts, so a phone is looked up with contacts.ResolvePhone (works
        when the owner lets people find them by number)."""
        client = self.client
        if match := INVITE_LINK.search(value):
            return await self._invited_group(match.group(1))
        if match := GROUP_NAME.fullmatch(value):
            name = match.group(1).strip()
            return await self._group_where(lambda d: (d.name or "").strip().casefold() == name.casefold(),
                                           f"named {name!r}")
        if GROUP_ID.fullmatch(value):
            return await self._group_by_id(int(value))
        if match := PUBLIC_LINK.fullmatch(value):
            value = "@" + match.group(1)
        digits = re.sub(r"[+()\s-]", "", value)
        if not digits.isdigit() or len(digits) < 7:
            return await client.get_entity(int(value) if value.lstrip("-").isdigit() else value)
        if not value.startswith("+") and value.lstrip("-").isdigit():
            # Bare digits: a user id, or a phone typed without the +.
            try:
                return await client.get_entity(int(value))
            except (ValueError, TypeError):
                pass
        try:
            return await client.get_entity("+" + digits)
        except (ValueError, TypeError):
            pass
        resolved = await client(ResolvePhoneRequest(phone=digits))
        user = next((u for u in resolved.users if u.id == getattr(resolved.peer, "user_id", None)), None)
        if user is None:
            raise ValueError(f"no Telegram user for phone {value}")
        return user

    async def _invited_group(self, invite_hash: str) -> Any:
        """The group an invite link is for. Joins nothing: the account must
        be in it already (a stranger's request would go nowhere)."""
        result = await self.client(CheckChatInviteRequest(invite_hash))
        if not isinstance(result, ChatInviteAlready):
            raise ValueError("this Telegram account is not in that group yet: join it first")
        return result.chat

    async def _group_by_id(self, peer_id: int) -> Any:
        """A group by its (negative) id. Telethon remembers nothing after a
        restart, so the stored access hash is tried before the chat list."""
        try:
            return await self.client.get_entity(peer_id)
        except (ValueError, TypeError):
            pass
        real_id, kind = utils.resolve_id(peer_id)
        access_hash = await self.rt.db.get_access_hash(peer_id)
        if kind is PeerChannel and access_hash is not None:
            with suppress(ValueError, TypeError, errors.RPCError):
                return await self.client.get_entity(InputPeerChannel(real_id, access_hash))
        return await self._group_where(lambda d: d.id == peer_id, f"with id {peer_id}")

    async def _group_where(self, wanted: Any, described: str) -> Any:
        """The one group among this account's chats that `wanted` picks."""
        found = [d.entity async for d in self.client.iter_dialogs(limit=GROUP_SCAN_DIALOGS)
                 if _is_group(d.entity) and wanted(d)]
        if not found:
            raise ValueError(f"this Telegram account is in no group {described}")
        if len(found) > 1:
            raise ValueError(f"this Telegram account is in {len(found)} groups {described}: "
                             "pick the group in the panel instead")
        return found[0]

    async def list_groups(self) -> list[dict[str, Any]]:
        if not self.ready:
            raise ValueError("Telegram is not connected.")
        groups = [
            {"id": str(d.id), "name": d.name or "", "members": getattr(d.entity, "participants_count", None),
             "admins_only": False}
            async for d in self.client.iter_dialogs(limit=GROUP_SCAN_DIALOGS)
            if _is_group(d.entity)
        ]
        return sorted(groups, key=lambda g: g["name"].casefold())

    async def download_photo(self, message: Inbound, path: Optional[Path] = None) -> Optional[bytes]:
        if path is None:
            return await self.client.download_media(message.raw.message, file=bytes)
        await self.client.download_media(message.raw.message, file=str(path))
        return None

    async def list_logins(self) -> Optional[list[dict[str, Any]]]:
        if not self.ready:
            return None
        try:
            result = await self.client(GetAuthorizationsRequest())
        except Exception as exc:
            log.warning("[%s] Could not list the account's logins: %s", self.rt.session_id, type(exc).__name__)
            return None
        return [anomaly.login_record(a) for a in getattr(result, "authorizations", [])]

    async def log_out(self) -> bool:
        if self.client is None:
            return False
        return bool(await self.client.log_out())


async def log_out_stored(pool: asyncpg.Pool, session_id: str) -> bool:
    """Connect with the account's stored key and log it out (hard-off for an
    account no worker runs; the caller holds its lease). True when Telegram
    confirmed."""
    client = None
    try:
        registry = SessionRegistry(pool)
        auth = await registry.load_auth(session_id)
        if not auth or not auth.get("auth_key"):
            return False
        account = await config_store.load(pool, session_id)
        identity = dict(account.get("identity") or {}) if account.get("identity", {}).get("device_model") \
            else device_profiles.derive(session_id)
        client = _client_from_auth(auth, await registry.load_proxy(session_id), 60, identity)
        await client.connect()
        if not await client.is_user_authorized():
            return False
        return bool(await client.log_out())
    finally:
        if client is not None:
            with suppress(Exception):
                await client.disconnect()
