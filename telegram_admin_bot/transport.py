"""The seam between one account's business logic (session_runtime.py) and
the messaging network it runs on.

A transport is only the plumbing for one account: connect, send a text or
a file, show "typing…", mark a chat read, set presence, and hand every
message it sees to the runtime in the neutral shape below. Everything that
decides *whether* and *when* to do any of that (delays, quiet hours, caps,
the kill switches, approval, halts) stays in SessionRuntime, so it is one
code path whatever the channel.

telegram_transport.py wraps Telethon. A WhatsApp transport talks to the
wa-gateway service over the bus instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

TELEGRAM = "telegram"
WHATSAPP = "whatsapp"
CHANNELS = (TELEGRAM, WHATSAPP)

# Telegram's own service account: login codes and "new login" notices come
# from it. Never answered; a message from it triggers a login check.
TELEGRAM_SERVICE_ID = 777000

# Failure kinds (Transport.classify): what a network error means for the
# account, whatever the network called it.
PEER_FLOOD = "peer_flood"            # the network thinks we are spamming
SESSION_REJECTED = "session_rejected"  # banned, revoked, logged out
RATE_LIMITED = "rate_limited"        # asked to wait `seconds` before sending
UNREACHABLE = "unreachable"          # this one person can't be messaged


class NeedsLogin(RuntimeError):
    """Raised by start() when the account has no usable login yet."""


@dataclass(frozen=True)
class PeerInfo:
    """Who a private chat is with, as the conversation row stores it."""

    name: str
    username: Optional[str] = None
    is_bot: bool = False
    access_hash: Optional[int] = None


@dataclass
class Inbound:
    """One private message the account received, or sent from elsewhere
    (from_me: typed on the phone, say).

    `peer` is loaded on demand: for an echo of the runtime's own send the
    runtime returns before anything needs to know who the chat is with, so
    looking it up (a network round trip on Telegram) is skipped."""

    chat_id: int
    text: str
    external_id: Any
    load_peer: Callable[[], Awaitable[PeerInfo]]
    from_me: bool = False
    has_photo: bool = False
    reply_to: Any = None
    is_service: bool = False
    # The transport's own handle on the message (a Telethon event), for
    # fetching its media later. Opaque to the runtime.
    raw: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class Failure:
    """A send error, translated. `label` is the network's own name for it
    (shown to the operator); `seconds` only for RATE_LIMITED."""

    kind: str
    label: str
    seconds: int = 0


class Transport:
    """The operations a runtime needs from its network. One instance per
    account, owned by its SessionRuntime (`self.rt`)."""

    channel: str = ""
    # How the network is named in messages to the operator.
    network: str = ""

    def __init__(self, rt: Any) -> None:
        self.rt = rt

    # ----------------------------------------------------------- state

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    @property
    def error(self) -> Optional[str]:
        raise NotImplementedError

    @property
    def me(self) -> dict[str, Any]:
        """The account itself: {"id", "name", "username"} while connected."""
        raise NotImplementedError

    @property
    def ready(self) -> bool:
        """Calls to the network may be made now."""
        return self.connected

    def is_service_chat(self, chat_id: int) -> bool:
        """The network's own service account (never a customer)."""
        return False

    # ------------------------------------------------------- lifecycle

    async def prepare(self) -> None:
        """Load the stored login; NeedsLogin when there is none."""
        raise NotImplementedError

    async def start(self) -> None:
        """Connect and keep connected (in the background), delivering
        messages to rt.handle_inbound / rt.handle_own_echo."""
        raise NotImplementedError

    async def halt_updates(self) -> None:
        """First half of stopping: no more messages are delivered."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Second half of stopping: close the connection, forget the state."""
        raise NotImplementedError

    async def reload_login(self) -> str:
        """Re-read the stored login (and proxy) before a reconnect; returns a
        description of how it will connect."""
        raise NotImplementedError

    # --------------------------------------------------------- sending

    async def resolve_peer(self, chat_id: int) -> Any:
        """The network's handle for a chat, to send to."""
        raise NotImplementedError

    async def send_text(self, peer: Any, chat_id: int, text: str, typing_seconds: Optional[float]) -> Any:
        """Send one message, showing "typing…" for `typing_seconds` first
        when it is not None. Returns what message_id() reads."""
        raise NotImplementedError

    async def send_file(self, peer: Any, chat_id: int, path: Path, is_video: bool, show_upload: bool) -> Any:
        raise NotImplementedError

    def message_id(self, sent: Any) -> Any:
        """The network's id for a message send_text / send_file returned."""
        raise NotImplementedError

    def classify(self, exc: BaseException) -> Optional[Failure]:
        """What a send error means, or None for one this transport does not
        recognise."""
        return None

    # ---------------------------------------------------- chat actions

    async def mark_read(self, chat_id: int, message_id: Any = None) -> None:
        raise NotImplementedError

    async def set_presence(self, online: bool) -> None:
        raise NotImplementedError

    # --------------------------------------------------------- lookups

    async def list_contacts(self) -> list[tuple[int, PeerInfo]]:
        raise NotImplementedError

    async def resolve_owner(self, value: str) -> tuple[int, PeerInfo]:
        """The chat for booking.provider (a username, phone number or id)."""
        raise NotImplementedError

    async def download_photo(self, message: Inbound, path: Optional[Path] = None) -> Optional[bytes]:
        """A received photo's bytes, or saved to `path` (then None)."""
        raise NotImplementedError

    async def list_logins(self) -> Optional[list[dict[str, Any]]]:
        """The account's logins (anomaly.login_record shape), or None when
        the network can't tell or isn't connected."""
        return None

    async def log_out(self) -> bool:
        """End this login on the network's side. True when it confirmed."""
        raise NotImplementedError


def make_transport(channel: str, rt: Any) -> Transport:
    """The transport for an account's channel."""
    if channel == TELEGRAM:
        from telegram_transport import TelegramTransport

        return TelegramTransport(rt)
    raise ValueError(f"No transport for channel {channel!r}")
