"""Telegram AI support assistant — userbot + local admin panel.

Runs the Telethon client and the FastAPI admin server on one asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import webbrowser
from contextlib import suppress
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import InputPeerUser, User

import ai_responder
import bookings
import config_store
import context_link
import google_calendar
import instances
import media
from env_file import is_placeholder, parse_env_file, recover_wrapped, write_keys
from login_flow import LoginError, LoginFlow
from database import (
    DIR_IN,
    DIR_OUT,
    DIR_SYSTEM,
    OUT_CANCELLED,
    OUT_DRAFTED,
    OUT_FAILED,
    OUT_QUEUED,
    OUT_SENT,
    STATUS_ERROR,
    STATUS_NOTE,
    STATUS_PENDING,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    STATUS_SENT,
    Database,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# --instance NAME points DATA_DIR / ADMIN_PORT at instances/NAME before anything
# below reads them. Only when run as a script — importing main (tests) is not
# a launch and should not parse the interpreter's arguments.
INSTANCE = instances.resolve() if __name__ == "__main__" else os.getenv("INSTANCE", "")
# Where mutable state lives. Separate from the code so a container (or a named
# instance) can keep the database and config away from the code.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR)
DB_PATH = DATA_DIR / "assistant.db"
# Where the sign-in screen saves credentials. Next to main.py normally; on the
# data volume in a container so a rebuild does not log the account out.
ENV_PATH = DATA_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"

# Localhost only by default — the panel has no login, so it must not be
# reachable from the network. ADMIN_HOST exists for containers, where the
# process binds inside the container and Docker publishes it back to the
# host's loopback only. Anything other than loopback is shouted about at
# startup, because it means the panel is exposed with no authentication.
HOST = (os.getenv("ADMIN_HOST") or "127.0.0.1").strip()
PORT = int(os.getenv("ADMIN_PORT") or 8787)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

REQUIRED_ENV = ("API_ID", "API_HASH", "SESSION", "DEEPSEEK_API_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("assistant")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class Env:
    api_id: int
    api_hash: str
    session: str
    deepseek_key: str


def read_credentials() -> dict[str, str]:
    """Whatever credentials are on hand, from the environment and .env files.

    Placeholders copied from .env.example count as blank. Missing values are
    not an error here: the panel's sign-in screen fills them in.
    """
    # Only this instance's own .env (plus whatever the process environment
    # carries, which is how Docker and systemd pass credentials in). Never the
    # default instance's file: two instances sharing a session would both
    # answer the same chats.
    load_dotenv(ENV_PATH)

    # Read the file directly too, so a value that got wrapped across lines when
    # it was pasted in can be stitched back together instead of arriving cut off.
    file_values = parse_env_file(ENV_PATH)

    resolved = {}
    for name in REQUIRED_ENV:
        value = recover_wrapped(name, (os.getenv(name) or "").strip(), file_values)
        if not value:
            value = file_values.get(name, "")
        resolved[name] = "" if is_placeholder(value) else value
    # Session strings are base64: any whitespace in there came from copy-paste.
    resolved["SESSION"] = "".join(resolved["SESSION"].split())
    return resolved


def build_env(values: dict[str, str]) -> Env:
    """Turn raw credential strings into an Env, or raise ValueError saying why not."""
    missing = [name for name in REQUIRED_ENV if not values.get(name)]
    if missing:
        raise ValueError("Not set: " + ", ".join(missing))

    try:
        api_id = int(values["API_ID"])
    except ValueError:
        raise ValueError(
            f"API_ID must be the number from my.telegram.org (got {values['API_ID']!r})."
        )

    session = "".join(values["SESSION"].split())
    try:
        StringSession(session)
    except ValueError:
        raise ValueError(
            f"SESSION is not a usable Telethon session string ({len(session)} chars; "
            "a valid one is around 350). It was probably truncated when pasted in."
        )

    env = Env()
    env.api_id = api_id
    env.api_hash = values["API_HASH"]
    env.session = session
    env.deepseek_key = values["DEEPSEEK_API_KEY"]
    return env


def save_credentials(values: dict[str, str]) -> None:
    write_keys(ENV_PATH, values, template=ENV_EXAMPLE_PATH)
    for name, value in values.items():
        os.environ[name] = value
        credentials[name] = value


# ---------------------------------------------------------------------------
# Live state shared by the Telethon handlers and the HTTP handlers
# ---------------------------------------------------------------------------


class Hub:
    """Fan-out of live events to every open admin panel tab."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)


hub = Hub()
db = Database(DB_PATH)
# Raw credential strings as last read/saved; env is None until all four are usable.
credentials: dict[str, str] = {}
env: Optional[Env] = None
config: dict[str, Any] = config_store.load()
http_client: Optional[httpx.AsyncClient] = None
client: Optional[TelegramClient] = None
telegram_task: Optional[asyncio.Task] = None
login_flow = LoginFlow()
me_info: dict[str, Any] = {}
telegram_state: dict[str, Any] = {"connected": False, "error": None}

# One in-flight drafting task per chat. A newer message supersedes an older
# draft, so the reply always answers the latest state of the conversation.
draft_tasks: dict[int, asyncio.Task] = {}
# Single worker draining the outreach queue, so sends stay paced.
outreach_task: Optional[asyncio.Task] = None
# Texts we are sending right now, so the outgoing-message handler doesn't
# record a duplicate of a message our own send path already logged.
in_flight_sends: dict[int, list[str]] = {}
# Same for files: how many photos/videos are on their way to each chat. An
# outgoing media event carries no text to match on, so it is counted instead.
in_flight_media: dict[int, int] = {}

# Photos and videos the AI may attach when asked — the media/ folder.
media_library = media.MediaLibrary(DATA_DIR / "media")

# Online/offline presence, tracked separately from typing so "online" doesn't
# flip on at the exact instant a message goes out.
presence_online = False
offline_timer: Optional[asyncio.Task] = None
# Chats currently mid-exchange. Presence is one switch for the whole account,
# so with several conversations running at once it must not be flipped off by
# whichever one happens to finish first while the others are still typing.
active_chats: set[int] = set()

# Chats whose draft has already committed to sending. A newer incoming message
# must not cancel a send that is halfway through Telegram's wire.
sending_chats: set[int] = set()

# Appointments asked for in chat, waiting on (or answered by) the provider.
booking_store = bookings.BookingStore(DATA_DIR / "bookings.json")
# One in-flight "did they just agree a time?" check per chat, superseded by
# the next message the same way drafts are.
booking_scan_tasks: dict[int, asyncio.Task] = {}
# The provider setting resolved to a chat id — (value, chat_id, resolved_at).
# A failed lookup is remembered briefly so a typo does not cost a network
# round-trip on every incoming message.
_provider_resolved: tuple[str, Optional[int], float] = ("", None, 0.0)
PROVIDER_RETRY_SECONDS = 300
# The calendar client, rebuilt when its settings change.
_calendar: Optional[google_calendar.GoogleCalendar] = None
_calendar_key: tuple[str, str] = ("", "")
# Ticks once a minute to see whether a check-in is due for any confirmed booking.
reminder_task: Optional[asyncio.Task] = None
REMINDER_TICK_SECONDS = 60

# Caps how many DeepSeek calls run at once across all chats. Rebuilt when the
# setting changes; waiters already holding the old one drain normally.
_ai_gate: Optional[asyncio.Semaphore] = None
_ai_gate_size: int = 0


def ai_gate() -> asyncio.Semaphore:
    global _ai_gate, _ai_gate_size
    size = max(1, int(config["ai"].get("max_concurrent_requests", 4) or 4))
    if _ai_gate is None or size != _ai_gate_size:
        _ai_gate, _ai_gate_size = asyncio.Semaphore(size), size
    return _ai_gate


# ---------------------------------------------------------------------------
# Account safety
#
# Telegram does not explain why an account gets limited, and there is no way
# to ask. What is known is that the signals which matter are behavioural:
# outbound volume, how many *different* people are contacted, and how often
# recipients press "Report Spam". So the approach here is to actually send
# less when Telegram pushes back, rather than to try to look like something
# else while sending the same amount.
# ---------------------------------------------------------------------------


class SendBlocked(Exception):
    """A send was deliberately not attempted, with a reason worth showing."""


async def halt_everything(reason: str) -> None:
    """Flip the global pause and tell every open panel why.

    Used for signals that mean Telegram is already unhappy with this account.
    Recovery is deliberately manual: if something is wrong, an operator should
    look at it before the account starts sending again.
    """
    global config
    log.error("HALTING ALL AUTOMATION: %s", reason)
    if not config["behavior"].get("global_pause"):
        config = config_store.save(
            {**config, "behavior": {**config["behavior"], "global_pause": True}}
        )
    for chat_id in list(draft_tasks):
        cancel_draft(chat_id)
    await db.cancel_queued_outreach()
    await hub.broadcast({"type": "config", "config": config})
    await hub.broadcast({"type": "halted", "reason": reason})
    await push_error(None, f"Automation halted: {reason}")
    # push_error(None, …) writes no database row, so keep the reason on disk:
    # after a restart the log below is otherwise the only place it exists.
    try:
        (DATA_DIR / "last_halt.txt").write_text(
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {reason}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


async def check_daily_quota() -> None:
    """Raise SendBlocked when today's self-imposed ceiling is already reached."""
    safety = config["safety"]
    since = start_of_day_utc()

    sent = await db.sent_since(since)
    limit = int(safety.get("daily_send_limit", 150))
    if sent >= limit:
        raise SendBlocked(
            f"Daily send limit reached ({sent}/{limit} messages today). "
            "Sending resumes tomorrow; raise the limit in Settings if this is wrong."
        )

    peers = await db.distinct_peers_since(since)
    peer_limit = int(safety.get("daily_peer_limit", 30))
    if peers >= peer_limit:
        raise SendBlocked(
            f"Daily limit on distinct people reached ({peers}/{peer_limit} today). "
            "Writing to many different people in one day is the strongest spam signal."
        )


async def handle_send_failure(chat_id: Optional[int], exc: BaseException) -> bool:
    """Translate a Telegram error into the right defensive action.

    Returns True when the error was recognised and handled, so callers can
    avoid double-reporting it. Anything unrecognised is left to the caller.
    """
    safety = config["safety"]

    # Telegram is telling us, in as many words, that this account looks like a
    # spammer. This is the last warning before a limit is applied.
    if isinstance(exc, errors.PeerFloodError):
        if safety.get("halt_on_peer_flood", True):
            await halt_everything(
                "Telegram returned PeerFloodError — it considers this account "
                "to be sending unsolicited messages. Everything is paused. Do "
                "not resume until you know why; sending through this is what "
                "gets a number banned."
            )
        else:
            await push_error(chat_id, "PeerFloodError from Telegram (halt disabled).")
        return True

    # The account is already restricted or gone. Nothing to do but stop.
    if isinstance(exc, (errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                        errors.SessionRevokedError)):
        await halt_everything(
            f"Telegram rejected the session ({type(exc).__name__}). The account "
            "may be banned or the session revoked. Automation is stopped."
        )
        return True

    # A rate limit on this specific action. Respect it exactly.
    if isinstance(exc, (errors.FloodWaitError, errors.SlowModeWaitError)):
        wait = int(getattr(exc, "seconds", 0) or 0)
        cap = int(safety.get("max_flood_wait_seconds", 300))
        log.warning("Telegram asked us to wait %ss before sending again.", wait)
        await push_error(
            chat_id,
            f"Telegram rate limit: it asked for a {wait}s pause. Backing off.",
        )
        if wait > cap:
            # Too long to hold a task open for. Pause the account rather than
            # sleeping, so nothing retries into the limit and deepens it.
            await halt_everything(
                f"Telegram demanded a {wait}s wait, beyond the {cap}s this is "
                "willing to sleep through. Paused so nothing retries into it."
            )
        else:
            await asyncio.sleep(wait)
        return True

    # This particular person cannot or should not be written to. Pause just
    # them — retrying would produce nothing but more failed requests.
    if isinstance(exc, (errors.UserIsBlockedError, errors.UserPrivacyRestrictedError,
                        errors.InputUserDeactivatedError, errors.ChatWriteForbiddenError)):
        if chat_id is not None:
            await db.set_paused(chat_id, True)
            await hub.broadcast({"type": "conversation_paused", "chat_id": chat_id})
        await push_error(
            chat_id,
            f"Cannot message this person ({type(exc).__name__}); this "
            "conversation is now paused. They may have blocked the account.",
        )
        return True

    return False


async def may_message(chat_id: int) -> None:
    """Refuse to open a conversation with someone who never opted in.

    Unsolicited messages to strangers are what recipients report, and reports
    are what actually get accounts banned. Someone who wrote to us first, or
    who is in the account's own contacts, has opted in; nobody else has.
    """
    if not config["safety"].get("known_contacts_only", True):
        return
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise SendBlocked(
            f"Chat {chat_id} is unknown — not in contacts and has never sent a "
            "message. Refusing to open a conversation with a stranger."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def within_active_hours(timing: dict[str, Any]) -> bool:
    if not timing.get("active_hours_enabled"):
        return True

    tz_name = timing.get("timezone") or "UTC"
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            log.warning("Unknown timezone %r in config; falling back to system time.", tz_name)
    now = datetime.now(tz).time() if tz else datetime.now().time()

    start = _parse_time(timing.get("active_hours_start"), dtime(0, 0))
    end = _parse_time(timing.get("active_hours_end"), dtime(23, 59))
    if start <= end:
        return start <= now <= end
    # Window wraps past midnight (e.g. 22:00 -> 06:00).
    return now >= start or now <= end


def _parse_time(value: Any, fallback: dtime) -> dtime:
    try:
        hour, minute = str(value).split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


async def resolve_peer(chat_id: int):
    """Get a sendable peer, rebuilding it from the stored access_hash if needed."""
    if client is None or not telegram_state["connected"]:
        raise RuntimeError("Telegram is not connected. Sign in from the panel first.")
    try:
        return await client.get_input_entity(chat_id)
    except (ValueError, TypeError):
        access_hash = await db.get_access_hash(chat_id)
        if access_hash is None:
            raise RuntimeError(
                f"Cannot resolve chat {chat_id}. Receive a message from them first."
            )
        return InputPeerUser(chat_id, access_hash)


async def push_message(row: dict[str, Any]) -> None:
    conversation = await db.get_conversation(row["chat_id"])
    await hub.broadcast({"type": "message", "message": row, "conversation": conversation})


async def push_error(chat_id: Optional[int], text: str) -> None:
    """Record and surface a failure without taking the process down."""
    log.error("%s", text)
    row = None
    if chat_id is not None:
        row = await db.record_message(
            chat_id, DIR_SYSTEM, STATUS_ERROR, text, bump_preview=False
        )
    await hub.broadcast(
        {"type": "error", "chat_id": chat_id, "text": text, "message": row}
    )


def media_prompt() -> str:
    """The files the AI may attach, as a prompt section — "" when off or empty."""
    if not config["media"].get("enabled", True):
        return ""
    media_library.refresh()
    return media.prompt_section(
        media_library.all(), ask_before_video=config["media"].get("ask_before_video", True)
    )


def contact_overrides(chat_id: Optional[int]) -> dict[str, Any]:
    """Per-contact style/timing overrides, or {} for a chat with none set."""
    if chat_id is None:
        return {}
    return config.get("contacts", {}).get(str(chat_id)) or {}


async def borrowed_context(chat_id: int) -> str:
    """What a linked chat already established about this person, if anything.

    Summarising costs an API call, so it runs under the same concurrency gate
    as the replies themselves. A failure here is swallowed inside
    context_link: a missing brief must never cost the reply.
    """
    settings = config["context_link"]
    if not settings.get("enabled", True):
        return ""
    async with ai_gate():
        return await context_link.build_background(
            db,
            chat_id,
            api_key=env.deepseek_key,
            ai_config=config["ai"],
            settings=settings,
            client=http_client,
        )


async def detect_links(conversation: dict[str, Any]) -> None:
    """Link a chat to another account of the same person, and say so in the panel."""
    try:
        created = await context_link.autolink(db, conversation, config["context_link"])
    except Exception:  # detection is a convenience; it must not drop a message
        log.exception("Link detection failed for chat %s", conversation.get("chat_id"))
        return
    for link in created:
        await hub.broadcast({"type": "chat_link", "link": link})


def _ov_int(overrides: dict[str, Any], key: str, fallback: int) -> int:
    value = overrides.get(key)
    return value if isinstance(value, int) else fallback


def typing_seconds(text: str, chat_id: Optional[int] = None) -> float:
    """How long a person would plausibly take to type this."""
    human = config["human"]
    overrides = contact_overrides(chat_id)
    cps = max(1, _ov_int(overrides, "typing_speed_cps", int(human.get("typing_speed_cps", 12))))
    cap = max(1, _ov_int(overrides, "typing_max_seconds", int(human.get("typing_max_seconds", 25))))
    base = max(0.1, min(cap, len(text) / cps))
    # A little jitter so it never reads as a fixed, computed duration.
    return base * random.uniform(0.85, 1.15)


# ---------------------------------------------------------------------------
# Presence — appearing online/offline independently of typing, so coming
# online never lines up exactly with a message going out.
# ---------------------------------------------------------------------------


async def set_presence(online: bool) -> None:
    global presence_online
    if presence_online == online:
        return
    try:
        await client(UpdateStatusRequest(offline=not online))
        presence_online = online
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Could not update presence: %s", type(exc).__name__)


async def _go_offline_after(delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        # Another chat may have become active while this timer was pending —
        # going offline then would contradict a conversation still in progress.
        if active_chats:
            return
        await set_presence(False)
    except asyncio.CancelledError:
        raise


def schedule_go_offline(chat_id: Optional[int]) -> None:
    """Queue going offline again, once no chat is still mid-exchange."""
    global offline_timer
    if chat_id is not None:
        active_chats.discard(chat_id)
    presence = config["presence"]
    if not presence.get("enabled", True):
        return
    if active_chats:
        return  # someone else is still being replied to; stay online for them
    overrides = contact_overrides(chat_id)
    lo = _ov_int(overrides, "offline_delay_min", int(presence.get("offline_delay_min", 15)))
    hi = _ov_int(overrides, "offline_delay_max", int(presence.get("offline_delay_max", 90)))
    if offline_timer is not None and not offline_timer.done():
        offline_timer.cancel()
    offline_timer = asyncio.create_task(_go_offline_after(random.uniform(min(lo, hi), max(lo, hi))))


async def go_online_for(chat_id: Optional[int]) -> None:
    """Simulate noticing the phone and unlocking it before opening the chat."""
    if chat_id is not None:
        active_chats.add(chat_id)
    presence = config["presence"]
    if not presence.get("enabled", True):
        return
    if offline_timer is not None and not offline_timer.done():
        offline_timer.cancel()
    # Already online for another conversation — a second "unlock the phone"
    # pause here would just delay this reply for no visible reason.
    if presence_online:
        return
    overrides = contact_overrides(chat_id)
    lo = _ov_int(overrides, "online_delay_min", int(presence.get("go_online_delay_min", 2)))
    hi = _ov_int(overrides, "online_delay_max", int(presence.get("go_online_delay_max", 8)))
    await asyncio.sleep(random.uniform(min(lo, hi), max(lo, hi)))
    await set_presence(True)


async def deliver(peer: Any, chat_id: int, text: str, typing: bool) -> Any:
    """Send the message, optionally typing first.

    The send happens inside the typing action so the indicator runs right up to
    the moment the message lands, rather than blinking off just before it.
    """
    if not (typing and config["human"].get("typing_indicator", True)):
        return await client.send_message(peer, text)

    seconds = typing_seconds(text, chat_id)
    log.info("  typing for %.1fs…", seconds)

    result = None
    attempted = False
    try:
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(seconds)
            attempted = True
            result = await client.send_message(peer, text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if result is not None:
            return result  # sent fine; only tearing the indicator down failed
        if attempted:
            raise  # a real send failure — the caller reports it
        # The indicator itself is unavailable. It is cosmetic; send regardless.
        log.warning("Typing indicator unavailable (%s); sending anyway.", type(exc).__name__)
    else:
        return result

    return await client.send_message(peer, text)


async def mark_read(chat_id: int, message_id: Optional[int] = None) -> None:
    """Mark their message read, so they see the second tick."""
    if not config["human"].get("mark_read", True):
        return
    try:
        await client.send_read_acknowledge(chat_id, max_id=message_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Could not mark chat %s read: %s", chat_id, type(exc).__name__)


async def send_as_me(
    chat_id: int,
    text: str,
    draft_id: Optional[int] = None,
    typing: bool = False,
    guard: bool = True,
) -> dict[str, Any]:
    """Send a message through the userbot and record it as outgoing/sent.

    `guard` applies the daily ceilings. It is on for every path, including
    messages typed by hand in the panel: the limits exist to protect the
    account, and Telegram does not care which of them sent the message.
    """
    if guard:
        await check_daily_quota()
    peer = await resolve_peer(chat_id)
    in_flight_sends.setdefault(chat_id, []).append(text)
    # Held until the row is written, not just until the send returns: Telethon
    # delivers the outgoing event as soon as the message lands, and if the
    # guard were already gone that handler would insert a second row for it —
    # or, on the approved-draft path, claim the telegram_id this update needs.
    try:
        sent = await deliver(peer, chat_id, text, typing)

        telegram_id = getattr(sent, "id", None)
        if draft_id is not None:
            row = await db.update_message(
                draft_id, text=text, status=STATUS_SENT, telegram_id=telegram_id
            )
            await db.set_conversation_preview(chat_id, text)
        else:
            row = await db.record_message(
                chat_id, DIR_OUT, STATUS_SENT, text, telegram_id=telegram_id
            )
            if row is None:  # the outgoing event beat us to it anyway
                row = await db.find_by_telegram_id(chat_id, telegram_id)
    finally:
        pending = in_flight_sends.get(chat_id) or []
        if text in pending:
            pending.remove(text)
        if not pending:
            in_flight_sends.pop(chat_id, None)

    if row is not None:
        await push_message(row)
    return row or {}


async def deliver_file(peer: Any, chat_id: int, item: dict[str, Any], path: Path) -> Any:
    """Upload and send one photo or video, showing the matching upload action."""
    is_video = item.get("kind") == media.VIDEO
    if not config["human"].get("typing_indicator", True):
        return await client.send_file(peer, str(path), supports_streaming=is_video)
    try:
        async with client.action(chat_id, "video" if is_video else "photo") as progress:
            return await client.send_file(
                peer,
                str(path),
                supports_streaming=is_video,
                progress_callback=progress.progress,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, (errors.RPCError, OSError)):
            raise  # a real send failure — the caller reports it
        # The indicator itself is cosmetic; send without it.
        log.warning("Upload indicator unavailable (%s); sending anyway.", type(exc).__name__)
    return await client.send_file(peer, str(path), supports_streaming=is_video)


async def send_media_as_me(
    chat_id: int,
    item_id: int,
    draft_id: Optional[int] = None,
    guard: bool = True,
) -> dict[str, Any]:
    """Send one file from the library and record it as an outgoing message.

    Recorded with a placeholder like "[sent photo #3: the beach]" so the
    thread — and the AI's own history — show what went out. Counts against
    the daily ceilings like any other message.
    """
    item = media_library.get(item_id)
    path = media_library.path(item_id)
    if item is None or path is None:
        raise ValueError(f"Media #{item_id} is no longer in the library.")
    if guard:
        await check_daily_quota()
    peer = await resolve_peer(chat_id)
    text = media.sent_placeholder(item)
    in_flight_media[chat_id] = in_flight_media.get(chat_id, 0) + 1
    try:
        sent = await deliver_file(peer, chat_id, item, path)
        telegram_id = getattr(sent, "id", None)
        if draft_id is not None:
            row = await db.update_message(
                draft_id, text=text, status=STATUS_SENT,
                telegram_id=telegram_id, attachments=[item_id],
            )
            await db.set_conversation_preview(chat_id, text)
        else:
            row = await db.record_message(
                chat_id, DIR_OUT, STATUS_SENT, text,
                telegram_id=telegram_id, attachments=[item_id],
            )
            if row is None:  # the outgoing event beat us to it
                row = await db.find_by_telegram_id(chat_id, telegram_id)
                if row is not None:
                    row = await db.update_message(row["id"], text=text, attachments=[item_id])
    finally:
        left = in_flight_media.get(chat_id, 1) - 1
        if left > 0:
            in_flight_media[chat_id] = left
        else:
            in_flight_media.pop(chat_id, None)

    if row is not None:
        await push_message(row)
    log.info("  sent %s to chat %s.", media.label(item), chat_id)
    return row or {}


# Two messages typed back to back still have a pause between them — finishing
# the thought, starting the next line. Without it the parts land in the same
# instant, which is the one thing a real burst never looks like.
BURST_GAP_MIN_SECONDS = 0.6
BURST_GAP_MAX_SECONDS = 2.2


async def send_burst(
    chat_id: int,
    parts: list[str],
    draft_id: Optional[int] = None,
    typing: bool = False,
    guard: bool = True,
    attachments: Optional[list[int]] = None,
) -> dict[str, Any]:
    """Send one reply as consecutive messages, the way a burst is texted.

    Every part goes out through send_as_me, so each is quota-checked, recorded
    and pushed to the panel exactly like an ordinary single reply — a burst of
    three is three messages by every measure that matters, including the daily
    ceilings. Files in `attachments` follow the text, each as a message of its
    own. The row returned is the last one, which is what callers report.
    """
    row: dict[str, Any] = {}
    files = [i for i in (attachments or []) if media_library.get(i) is not None]
    for index, part in enumerate(parts):
        if index:
            await asyncio.sleep(
                random.uniform(BURST_GAP_MIN_SECONDS, BURST_GAP_MAX_SECONDS)
            )
        # Only the first part can settle the draft already on screen; the rest
        # are new messages in their own right.
        row = await send_as_me(
            chat_id,
            part,
            draft_id=draft_id if index == 0 else None,
            typing=typing,
            guard=guard,
        )
        if index == 0 and draft_id is not None and files:
            # The draft row is now the first text part; the files it carried
            # become rows of their own below.
            row = await db.update_message(draft_id, attachments=[]) or row
            await push_message(row)
    for index, item_id in enumerate(files):
        if parts or index:
            await asyncio.sleep(
                random.uniform(BURST_GAP_MIN_SECONDS, BURST_GAP_MAX_SECONDS)
            )
        row = await send_media_as_me(
            chat_id,
            item_id,
            draft_id=draft_id if (not parts and index == 0) else None,
            guard=guard,
        )
    return row


# ---------------------------------------------------------------------------
# Drafting pipeline
# ---------------------------------------------------------------------------


def schedule_draft(chat_id: int) -> None:
    cancel_draft(chat_id)
    draft_tasks[chat_id] = asyncio.create_task(draft_worker(chat_id))


def cancel_draft(chat_id: int) -> None:
    task = draft_tasks.pop(chat_id, None)
    if task is None or task.done():
        return
    if chat_id in sending_chats:
        # Past the point of no return — the text is already going out. Tearing
        # the task down here would leave a message on Telegram with no row in
        # the database. Let it finish; the newer message gets its own draft.
        return
    task.cancel()


async def draft_worker(chat_id: int) -> None:
    try:
        timing = config["timing"]
        overrides = contact_overrides(chat_id)
        low = _ov_int(overrides, "min_delay_seconds", int(timing.get("min_delay_seconds", 20)))
        high = _ov_int(overrides, "max_delay_seconds", int(timing.get("max_delay_seconds", 90)))
        delay = random.uniform(min(low, high), max(low, high))

        await hub.broadcast(
            {"type": "drafting", "chat_id": chat_id, "delay_seconds": round(delay, 1)}
        )
        log.info("  drafting a reply for chat %s in %.0fs…", chat_id, delay)
        await asyncio.sleep(delay)

        # Re-read state after the delay — I may have paused the chat meanwhile.
        if config["behavior"].get("global_pause"):
            return
        conversation = await db.get_conversation(chat_id)
        if conversation is None or conversation["automation_paused"]:
            return
        if not within_active_hours(config["timing"]):
            log.info("Outside active hours; skipping draft for chat %s.", chat_id)
            return

        history = await db.get_history_for_ai(chat_id, limit=30)
        if not history:
            log.info("No usable history for chat %s; skipping draft.", chat_id)
            return

        # Before spending an API call on text we would not be allowed to send.
        if config["behavior"].get("auto_send"):
            await check_daily_quota()

        # Coming online is a separate beat from opening the chat, and opening
        # the chat is separate again from starting to type — so "online"
        # never lands at the exact moment a message goes out.
        await go_online_for(chat_id)

        # Read it before writing back, the way a person would: the delay above
        # is the time before opening the chat, this is opening it.
        await mark_read(chat_id)

        background = await borrowed_context(chat_id)
        if background:
            log.info("  drawing on a linked chat for context.")

        # Read at generation time, not when the task was scheduled: the
        # provider may have answered during the delay, and that answer must
        # be in this reply rather than the next.
        news, news_kind = booking_news(chat_id)
        booking_note = bookings.context_for_reply(
            booking_store.for_chat(chat_id), news, news_kind
        )
        if news is not None:
            log.info("  booking #%s: writing the %s into this reply.", news.id, news_kind)

        media_note = media_prompt()

        async with ai_gate():
            text = await ai_responder.generate_reply(
                api_key=env.deepseek_key,
                history=history,
                persona=config["persona"],
                ai_config=config["ai"],
                client=http_client,
                adaptive_style=config["human"].get("adaptive_style", True),
                general_samples=config["finetune"].get("writing_samples", ""),
                contact=overrides,
                background=background,
                booking_note=booking_note,
                media_note=media_note,
            )

        text, attachments = media.split_attachments(text)
        attachments = [i for i in attachments if media_library.get(i) is not None]
        if not media_note:
            attachments = []  # the model was offered no files; a tag is noise
        parts = ai_responder.split_burst(text)
        if not parts and not attachments:
            raise ai_responder.AIResponderError("The reply came back empty.")
        if attachments:
            log.info("  attaching %s.", ", ".join(
                media.label(media_library.get(i)) for i in attachments
            ))

        # A video goes out by itself only if Settings allow it; otherwise the
        # reply waits in the panel even with auto-send on.
        holds_video = any(
            (media_library.get(i) or {}).get("kind") == media.VIDEO for i in attachments
        )
        hold = holds_video and config["media"].get("videos_need_approval", True)

        if config["behavior"].get("auto_send") and not hold:
            # From here the message is going out; see cancel_draft. The whole
            # burst is covered, so a newer message cannot tear the task down
            # between two halves of one reply.
            sending_chats.add(chat_id)
            try:
                await send_burst(chat_id, parts, typing=True, attachments=attachments)
            finally:
                sending_chats.discard(chat_id)
            log.info(
                "Auto-sent AI reply to chat %s%s.",
                chat_id,
                f" as {len(parts)} messages" if len(parts) > 1 else "",
            )
        else:
            if hold and config["behavior"].get("auto_send"):
                log.info("  reply carries a video — held for approval in the panel.")
            row = await db.record_message(
                chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False,
                attachments=attachments,
            )
            if row is not None:
                await push_message(row)
            log.info("Draft awaiting approval for chat %s.", chat_id)
        if news is not None:
            # Written into the reply — sent, or on screen waiting for approval.
            if news_kind == "reminder":
                booking_store.update(news, reminder_sent=True)
            else:
                booking_store.update(news, client_notified=True)
            await broadcast_booking(news)
        schedule_go_offline(chat_id)

    except asyncio.CancelledError:
        raise
    except SendBlocked as exc:
        # A limit we set for ourselves, not a failure. Say so plainly.
        log.info("Not replying in chat %s: %s", chat_id, exc)
        await push_error(chat_id, str(exc))
    except ai_responder.AIResponderError as exc:
        await push_error(chat_id, str(exc))
    except Exception as exc:  # never let one chat take down the bot
        if not await handle_send_failure(chat_id, exc):
            log.exception("Unexpected failure while drafting for chat %s", chat_id)
            await push_error(chat_id, f"Drafting failed: {type(exc).__name__}: {exc}")
    finally:
        # A cancelled or failed draft still has to release its claim on
        # presence, or the account would sit "online" for a reply that never
        # came. Safe to repeat: schedule_go_offline discards an absent chat.
        if chat_id in active_chats:
            schedule_go_offline(chat_id)
        if draft_tasks.get(chat_id) is asyncio.current_task():
            draft_tasks.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Outreach — messages we start, to people already in the account's contacts
# ---------------------------------------------------------------------------


def start_of_day_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def ensure_outreach_worker() -> None:
    global outreach_task
    if outreach_task is None or outreach_task.done():
        outreach_task = asyncio.create_task(outreach_worker())


async def outreach_worker() -> None:
    """Drain the queue one at a time, spaced out and capped per day."""
    try:
        while True:
            item = await db.next_queued_outreach()
            if item is None:
                return

            settings = config["outreach"]
            if config["behavior"].get("global_pause"):
                log.info("Outreach paused (global pause); leaving %s queued.", item["id"])
                return

            sent_today = await db.outreach_sent_since(start_of_day_utc())
            limit = int(settings.get("daily_limit", 20))
            if sent_today >= limit:
                log.info(
                    "Outreach daily limit reached (%s/%s); the rest stays queued for tomorrow.",
                    sent_today, limit,
                )
                await hub.broadcast({
                    "type": "outreach_paused",
                    "reason": f"Daily limit of {limit} reached. Remaining messages stay queued.",
                })
                return

            await process_outreach(item)

            if await db.next_queued_outreach() is not None:
                low = int(settings.get("min_gap_seconds", 90))
                high = int(settings.get("max_gap_seconds", 300))
                gap = random.uniform(min(low, high), max(low, high))
                log.info("Next outreach message in %.0fs.", gap)
                await asyncio.sleep(gap)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Outreach worker stopped unexpectedly")


async def process_outreach(item: dict[str, Any]) -> None:
    """Draft one outreach message and either send it or queue it for approval."""
    outreach_id, chat_id = item["id"], item["chat_id"]

    # Checked before a token is spent drafting: outreach is the path that
    # actually contacts people who did not write first, so it is the one that
    # earns spam reports if it goes wrong.
    try:
        await may_message(chat_id)
        await check_daily_quota()
    except SendBlocked as exc:
        log.info("Outreach %s not sent: %s", outreach_id, exc)
        await db.update_outreach(outreach_id, status=OUT_FAILED, error=str(exc))
        await push_error(chat_id, f"Outreach skipped: {exc}")
        await broadcast_outreach()
        return

    background = await borrowed_context(chat_id)

    try:
        async with ai_gate():
            text = await ai_responder.generate_opener(
                api_key=env.deepseek_key,
                goal=item["goal"],
                recipient_name=item["display_name"] or "them",
                persona=config["persona"],
                ai_config=config["ai"],
                client=http_client,
                general_samples=config["finetune"].get("writing_samples", ""),
                contact=contact_overrides(chat_id),
                background=background,
            )
    except ai_responder.AIResponderError as exc:
        await db.update_outreach(outreach_id, status=OUT_FAILED, error=str(exc))
        await push_error(chat_id, f"Outreach draft failed: {exc}")
        await broadcast_outreach()
        return

    await go_online_for(chat_id)

    if not config["outreach"].get("auto_send"):
        row = await db.record_message(
            chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False
        )
        await db.update_outreach(
            outreach_id,
            status=OUT_DRAFTED,
            message=text,
            draft_id=row["id"] if row else None,
        )
        if row is not None:
            await push_message(row)
        log.info("Outreach draft for %s awaiting approval.", item["display_name"])
        schedule_go_offline(chat_id)
        await broadcast_outreach()
        return

    try:
        await send_as_me(chat_id, text, typing=True)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        await db.update_outreach(outreach_id, status=OUT_FAILED, error=detail)
        if not await handle_send_failure(chat_id, exc):
            await push_error(chat_id, f"Could not send outreach message: {detail}")
    else:
        await db.update_outreach(
            outreach_id, status=OUT_SENT, message=text, mark_sent=True
        )
        log.info("Outreach message sent to %s.", item["display_name"])
    schedule_go_offline(chat_id)
    await broadcast_outreach()


async def settle_outreach_draft(
    draft_id: int, status: str, text: Optional[str] = None
) -> None:
    """Close out the queue row behind an approved or rejected outreach draft."""
    item = await db.outreach_for_draft(draft_id)
    if item is None or item["status"] != OUT_DRAFTED:
        return
    await db.update_outreach(
        item["id"], status=status, message=text, mark_sent=(status == OUT_SENT)
    )
    await broadcast_outreach()


async def broadcast_outreach() -> None:
    await hub.broadcast({"type": "outreach", "items": await db.list_outreach()})


async def list_contacts() -> list[dict[str, Any]]:
    """The account's own Telegram contacts — the only people outreach can target."""
    result = await client(GetContactsRequest(hash=0))
    contacts = []
    for user in getattr(result, "users", []):
        if getattr(user, "deleted", False) or getattr(user, "is_self", False):
            continue
        name, username, is_bot, access_hash = describe_sender(user, user.id)
        await db.upsert_conversation(user.id, name, username, is_bot, access_hash)
        contacts.append({
            "chat_id": user.id,
            "display_name": name,
            "username": username,
            "is_bot": is_bot,
        })
    contacts.sort(key=lambda c: c["display_name"].lower())
    return contacts


# ---------------------------------------------------------------------------
# Bookings — a client agrees a time, the provider says yes or no
# ---------------------------------------------------------------------------


def booking_settings() -> dict[str, Any]:
    return config.get("booking") or config_store.DEFAULTS["booking"]


def booking_news(chat_id: int) -> tuple[Optional[bookings.Booking], str]:
    """What this chat has not yet been told: (booking, "decision" | "reminder").

    A decision comes before a reminder — the client cannot be asked whether
    they are coming to something they do not yet know is confirmed.
    """
    for booking in booking_store.for_chat(chat_id, (bookings.CONFIRMED, bookings.DECLINED)):
        if not booking.client_notified:
            return booking, "decision"
    for booking in booking_store.for_chat(chat_id, (bookings.CONFIRMED,)):
        if booking.reminder_requested_at and not booking.reminder_sent:
            return booking, "reminder"
    return None, ""


def booking_now() -> datetime:
    return datetime.now(bookings.tzinfo_for(config["timing"].get("timezone") or "UTC"))


async def check_reminders() -> None:
    """One tick: ask the next draft to check in with anyone whose slot is near."""
    settings = booking_settings()
    if not settings.get("enabled"):
        return
    minutes = int(settings.get("reminder_minutes_before", 0) or 0)
    for booking in booking_store.due_for_reminder(booking_now(), minutes):
        booking_store.update(booking, reminder_requested_at=bookings.utcnow())
        log.info("Booking #%s is %s; checking in with %s.", booking.id,
                 bookings.describe_until(booking, booking_now()), booking.client_name)
        await post_note(
            booking.chat_id,
            f"\u23F0 Booking #{booking.id} is {bookings.describe_until(booking, booking_now())} "
            "— asking the client whether they are still coming.",
        )
        await broadcast_booking(booking)
        if config["behavior"].get("global_pause"):
            continue
        conversation = await db.get_conversation(booking.chat_id)
        if conversation and conversation["automation_paused"]:
            continue
        schedule_draft(booking.chat_id)


async def reminder_loop() -> None:
    try:
        while True:
            try:
                await check_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder check failed")
            await asyncio.sleep(REMINDER_TICK_SECONDS)
    except asyncio.CancelledError:
        raise


async def send_arrival_instructions(booking: bookings.Booking) -> None:
    """They are at the door: the configured text goes out exactly as written.

    Sent directly rather than via a draft — an address and a door code are
    not something to have rephrased, and the client is standing outside.
    """
    text = (booking_settings().get("arrival_instructions") or "").strip()
    booking_store.update(booking, arrived_at=bookings.utcnow())
    if not text:
        await post_note(
            booking.chat_id,
            f"\U0001F6AA Booking #{booking.id}: the client has arrived, but no arrival "
            "instructions are set in Settings -> Bookings, so nothing was sent.",
        )
        await broadcast_booking(booking)
        return
    # A reply the AI was drafting to "I'm here" would only get in the way.
    cancel_draft(booking.chat_id)
    sending_chats.add(booking.chat_id)
    try:
        await send_as_me(booking.chat_id, text, typing=True)
    except Exception as exc:
        if not await handle_send_failure(booking.chat_id, exc):
            await push_error(
                booking.chat_id,
                f"Could not send the arrival instructions: {type(exc).__name__}: {exc}",
            )
        return
    finally:
        sending_chats.discard(booking.chat_id)
    booking_store.update(booking, instructions_sent_at=bookings.utcnow())
    await post_note(
        booking.chat_id,
        f"\U0001F6AA Booking #{booking.id}: the client has arrived — entry instructions sent.",
    )
    await broadcast_booking(booking)


async def provider_chat_id() -> Optional[int]:
    """The chat id behind the provider setting, or None if it cannot be found."""
    global _provider_resolved
    value = booking_settings().get("provider") or ""
    if not value or client is None or not telegram_state["connected"]:
        return None
    cached_value, cached_id, resolved_at = _provider_resolved
    now = asyncio.get_running_loop().time()
    if cached_value == value and (
        cached_id is not None or now - resolved_at < PROVIDER_RETRY_SECONDS
    ):
        return cached_id
    try:
        target: Any = int(value) if value.lstrip("-").isdigit() else value
        entity = await client.get_entity(target)
        chat_id = int(entity.id)
        name, username, is_bot, access_hash = describe_sender(entity, chat_id)
        await db.upsert_conversation(chat_id, name, username, is_bot, access_hash)
    except Exception as exc:
        log.warning("Cannot resolve booking provider %r: %s", value, type(exc).__name__)
        _provider_resolved = (value, None, now)
        return None
    _provider_resolved = (value, chat_id, now)
    return chat_id


def calendar_client() -> Optional[google_calendar.GoogleCalendar]:
    """The Google Calendar mirror, if one is configured; None otherwise.

    A broken setup is reported once per change of settings rather than on
    every booking, and never stops the Telegram side of the flow.
    """
    global _calendar, _calendar_key
    calendar_id = booking_settings().get("google_calendar_id") or ""
    key_file = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if not key_file:
        default = DATA_DIR / "google-service-account.json"
        key_file = str(default) if default.exists() else ""
    key = (calendar_id, key_file)
    if key == _calendar_key:
        return _calendar
    _calendar_key, _calendar = key, None
    if not calendar_id:
        return None
    if not key_file:
        log.warning("Google Calendar is set but GOOGLE_SERVICE_ACCOUNT_FILE is not; skipping.")
        return None
    try:
        _calendar = google_calendar.GoogleCalendar(key_file, calendar_id, client=http_client)
    except google_calendar.CalendarError as exc:
        log.error("Google Calendar disabled: %s", exc)
    return _calendar


async def post_note(chat_id: int, text: str) -> None:
    """A line in the thread that is neither side talking — stays out of AI history."""
    row = await db.record_message(chat_id, DIR_SYSTEM, STATUS_NOTE, text, bump_preview=False)
    if row is not None:
        await push_message(row)


async def broadcast_booking(booking: bookings.Booking) -> None:
    await hub.broadcast({"type": "booking", "booking": booking.to_dict()})


def schedule_booking_scan(chat_id: int) -> None:
    cancel_booking_scan(chat_id)
    booking_scan_tasks[chat_id] = asyncio.create_task(booking_scan_worker(chat_id))


def cancel_booking_scan(chat_id: int) -> None:
    task = booking_scan_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


async def booking_scan_worker(chat_id: int) -> None:
    """Ask the model whether the client has just settled on a time; act if so."""
    try:
        settings = booking_settings()
        history = await db.get_history_for_ai(
            chat_id, limit=int(settings.get("scan_messages", 20))
        )
        if not history:
            return
        tz_name = config["timing"].get("timezone") or "UTC"

        # Someone with an appointment about now: is this them saying they
        # are at the door? That takes precedence over booking anything new.
        arriving = booking_store.awaiting_arrival(
            chat_id, booking_now(), int(settings.get("reminder_minutes_before", 0) or 0)
        )
        if arriving is not None and history[-1].get("role") == "user":
            async with ai_gate():
                arrived = await ai_responder.extract_arrival(
                    api_key=env.deepseek_key, history=history,
                    ai_config=config["ai"], client=http_client,
                )
            if arrived:
                log.info("Booking #%s: %s says they have arrived.", arriving.id, arriving.client_name)
                await send_arrival_instructions(arriving)
                return
        async with ai_gate():
            found = await ai_responder.extract_booking(
                api_key=env.deepseek_key,
                history=history,
                tz_name=tz_name,
                ai_config=config["ai"],
                client=http_client,
            )
        if not found:
            return
        fields = bookings.build_booking(
            found,
            tz_name=tz_name,
            default_duration=int(settings.get("default_duration_minutes", 60)),
        )
        if fields is None:
            log.info("  booking time in chat %s was unusable or in the past; ignoring.", chat_id)
            return
        start = datetime.fromisoformat(fields["start"])
        existing = booking_store.find_same_slot(chat_id, start)
        if existing is not None:
            # Already known. If the provider never got it (they were not set
            # up yet, or the send failed), this is the moment to try again.
            if existing.status == bookings.PENDING and existing.provider_message_id is None:
                await send_request_to_provider(existing)
            return
        await open_booking(chat_id, fields)
    except asyncio.CancelledError:
        raise
    except ai_responder.AIResponderError as exc:
        log.warning("Booking check failed for chat %s: %s", chat_id, exc)
    except Exception:
        log.exception("Booking check crashed for chat %s", chat_id)
    finally:
        if booking_scan_tasks.get(chat_id) is asyncio.current_task():
            booking_scan_tasks.pop(chat_id, None)


async def open_booking(chat_id: int, fields: dict[str, Any]) -> bookings.Booking:
    """Record the request, put it to the provider, mirror it to the calendar."""
    conversation = await db.get_conversation(chat_id) or {}
    # A client who changes their mind before the provider answers gets one
    # open question, not two: the earlier request is withdrawn.
    replaced = None
    for earlier in booking_store.for_chat(chat_id, (bookings.PENDING,)):
        booking_store.update(earlier, status=bookings.SUPERSEDED, decided_at=bookings.utcnow())
        await drop_calendar_event(earlier)
        await broadcast_booking(earlier)
        replaced = earlier
    booking = booking_store.add(
        chat_id=chat_id,
        client_name=conversation.get("display_name") or f"Chat {chat_id}",
        client_username=conversation.get("username"),
        replaces_id=replaced.id if replaced else None,
        **fields,
    )
    log.info("Booking #%s: %s asked for %s.", booking.id, booking.client_name,
             bookings.describe_when(booking))

    await send_request_to_provider(booking)

    calendar = calendar_client()
    if calendar is not None:
        try:
            event_id = await calendar.create_event(
                summary=f"[UNCONFIRMED] {booking.title or 'Appointment'} — {booking.client_name}",
                description=calendar_description(booking),
                start=booking.start_dt(), end=booking.end_dt(),
                tz_name=booking.timezone, tentative=True,
            )
            booking_store.update(booking, calendar_event_id=event_id)
        except google_calendar.CalendarError as exc:
            await push_error(chat_id, f"Google Calendar: {exc}")
        except Exception as exc:
            await push_error(chat_id, f"Google Calendar: {type(exc).__name__}: {exc}")

    await broadcast_booking(booking)
    return booking


async def send_request_to_provider(booking: bookings.Booking) -> bool:
    """Put the request to the provider; True once it has actually gone out.

    A booking whose request could not be sent stays pending with no
    provider_message_id, and is retried from the next scan of that chat or
    as soon as the provider setting is saved.
    """
    provider = await provider_chat_id()
    if provider is None:
        await push_error(
            booking.chat_id,
            f"Booking #{booking.id} could not be sent: no provider is set in "
            "Settings -> Bookings, or the username cannot be found. It will be "
            "retried once the provider is set.",
        )
        return False
    try:
        row = await send_as_me(provider, bookings.format_request(booking))
    except Exception as exc:
        if not await handle_send_failure(provider, exc):
            await push_error(
                booking.chat_id, f"Could not send booking #{booking.id} to the provider: "
                f"{type(exc).__name__}: {exc}",
            )
        return False
    booking_store.update(
        booking, provider_chat_id=provider, provider_message_id=row.get("telegram_id"),
    )
    await post_note(
        booking.chat_id,
        f"📅 Booking #{booking.id} requested for {bookings.describe_when(booking)} "
        "— waiting for the provider to confirm.",
    )
    await broadcast_booking(booking)
    return True


async def resend_unsent_bookings() -> None:
    """After the provider setting changes: deliver whatever is still waiting."""
    if not booking_settings().get("enabled"):
        return
    for booking in booking_store.pending():
        if booking.provider_message_id is None:
            log.info("Retrying booking #%s for the provider.", booking.id)
            await send_request_to_provider(booking)


def calendar_description(booking: bookings.Booking) -> str:
    who = booking.client_name + (f" (@{booking.client_username})" if booking.client_username else "")
    lines = [f"Client: {who}", f"Booking #{booking.id} via Telegram"]
    if booking.notes:
        lines.append(f"Notes: {booking.notes}")
    return "\n".join(lines)


async def drop_calendar_event(booking: bookings.Booking) -> None:
    calendar = calendar_client()
    if calendar is None or not booking.calendar_event_id:
        return
    try:
        await calendar.delete_event(booking.calendar_event_id)
    except Exception as exc:
        await push_error(booking.chat_id, f"Google Calendar: could not remove event: {exc}")
    else:
        booking_store.update(booking, calendar_event_id=None)


async def handle_provider_reply(chat_id: int, text: str, reply_to: Optional[int]) -> bool:
    """The provider wrote to us. Returns True if it was a YES/NO about a booking.

    False means it was ordinary conversation, which the caller answers as
    usual. A bare "yes" with nothing waiting is conversation too — "yes" to
    a question the AI asked them, not to a booking.
    """
    pending = booking_store.pending()
    if not pending:
        return False
    decision = bookings.parse_provider_reply(text, pending, reply_to)
    if decision is None:
        return False
    if decision.booking is None:
        try:
            await send_as_me(chat_id, bookings.format_help(pending))
        except Exception as exc:
            await handle_send_failure(chat_id, exc)
        return True
    await decide_booking(decision.booking, decision.confirmed, by="provider")
    try:
        await send_as_me(chat_id, bookings.format_acknowledgement(decision.booking, decision.confirmed))
    except Exception as exc:
        await handle_send_failure(chat_id, exc)
    return True


async def decide_booking(booking: bookings.Booking, confirmed: bool, by: str) -> None:
    """Apply a yes or no: the record, the calendar, the panel, then the client."""
    booking_store.update(
        booking,
        status=bookings.CONFIRMED if confirmed else bookings.DECLINED,
        decided_at=bookings.utcnow(),
        decided_by=by,
    )
    when = bookings.describe_when(booking)
    log.info("Booking #%s %s by %s.", booking.id, booking.status, by)

    calendar = calendar_client()
    if confirmed and calendar is not None and booking.calendar_event_id:
        try:
            await calendar.confirm_event(
                booking.calendar_event_id,
                f"{booking.title or 'Appointment'} — {booking.client_name}",
            )
        except Exception as exc:
            await push_error(booking.chat_id, f"Google Calendar: could not confirm event: {exc}")
    elif not confirmed:
        await drop_calendar_event(booking)

    mark = "✅" if confirmed else "❌"
    await post_note(
        booking.chat_id,
        f"{mark} Booking #{booking.id} for {when} "
        f"{'confirmed' if confirmed else 'declined'} by the {by}.",
    )
    await broadcast_booking(booking)

    # The client hears about it the way they hear about everything else: an
    # AI reply in the account's own voice, auto-sent or held for approval
    # according to the usual setting. draft_worker picks the news up.
    if config["behavior"].get("global_pause"):
        log.info("  automation is paused; the client will be told when a draft next runs.")
        return
    conversation = await db.get_conversation(booking.chat_id)
    if conversation and conversation["automation_paused"]:
        log.info("  chat %s is paused; tell the client by hand.", booking.chat_id)
        return
    schedule_draft(booking.chat_id)


# ---------------------------------------------------------------------------
# Telethon handlers
# ---------------------------------------------------------------------------


async def on_incoming(event: events.NewMessage.Event) -> None:
    # Private chats only. This is true for bot accounts too — conversations
    # that run through a bot's interface are handled just like any other DM.
    if not event.is_private:
        return

    chat_id = event.chat_id
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None
    name, username, is_bot, access_hash = describe_sender(sender, chat_id)
    conversation = await db.upsert_conversation(chat_id, name, username, is_bot, access_hash)
    await detect_links(conversation)

    text = (event.raw_text or "").strip()
    has_text = bool(text)
    stored_text = text if has_text else "[non-text message]"

    if config["behavior"].get("log_all_messages", True):
        row = await db.record_message(
            chat_id,
            DIR_IN,
            STATUS_RECEIVED,
            stored_text,
            telegram_id=event.message.id,
            mark_unread=True,
        )
    else:
        row = {
            "id": None,
            "chat_id": chat_id,
            "telegram_id": event.message.id,
            "direction": DIR_IN,
            "status": STATUS_RECEIVED,
            "text": stored_text,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    if row is not None:
        await push_message(row)

    log.info("DM from %s%s (chat %s): %s", name, " [bot]" if is_bot else "", chat_id,
             f"{len(text)} chars" if has_text else "non-text message")

    # A YES/NO from the provider is a decision about a booking, not
    # conversation; anything else they write is answered like any other chat.
    is_provider = (
        has_text and booking_settings().get("enabled") and chat_id == await provider_chat_id()
    )
    if is_provider:
        if await handle_provider_reply(chat_id, text, event.message.reply_to_msg_id):
            log.info("  booking decision from the provider — handled.")
            return
        log.info("  message from the booking provider; replying as usual.")

    # Each early return is logged: the terminal should always explain why a
    # message did not get a reply.
    if not has_text:
        log.info("  no text to reply to — skipping.")
        return
    if config["behavior"].get("global_pause"):
        log.info("  automation is globally paused — skipping.")
        return

    # Independent of drafting: a time agreed while the chat is paused or after
    # hours is still a time the provider has to hear about. The provider's own
    # chat is not scanned — they cannot book an appointment with themselves.
    if booking_settings().get("enabled") and not is_provider:
        schedule_booking_scan(chat_id)

    conversation = await db.get_conversation(chat_id)
    if conversation and conversation["automation_paused"]:
        log.info("  this conversation is paused — skipping.")
        return
    if not within_active_hours(config["timing"]):
        timing = config["timing"]
        log.info("  outside active hours (%s-%s %s) — skipping.",
                 timing.get("active_hours_start"), timing.get("active_hours_end"),
                 timing.get("timezone"))
        return

    schedule_draft(chat_id)


async def on_outgoing(event: events.NewMessage.Event) -> None:
    """Mirror messages I send from my phone/desktop so the thread stays whole."""
    if not event.is_private:
        return

    text = (event.raw_text or "").strip()
    chat_id = event.chat_id
    if text and text in (in_flight_sends.get(chat_id) or []):
        return  # already recorded by send_as_me
    if not text and in_flight_media.get(chat_id):
        return  # a file on its way from send_media_as_me, which records it
    if not config["behavior"].get("log_all_messages", True):
        return

    try:
        chat = await event.get_chat()
    except Exception:
        chat = None
    name, username, is_bot, access_hash = describe_sender(chat, chat_id)
    await db.upsert_conversation(chat_id, name, username, is_bot, access_hash)

    row = await db.record_message(
        chat_id,
        DIR_OUT,
        STATUS_SENT,
        text or "[non-text message]",
        telegram_id=event.message.id,
    )
    if row is not None:
        await push_message(row)


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

app = FastAPI(title="Telegram AI Assistant — Admin")


class SendBody(BaseModel):
    text: str = Field(min_length=1)


class PauseBody(BaseModel):
    paused: bool


class GlobalPauseBody(BaseModel):
    global_pause: bool


class LinkBody(BaseModel):
    source_id: int


class ApproveBody(BaseModel):
    text: Optional[str] = None


class OutreachBody(BaseModel):
    chat_ids: list[int] = Field(default_factory=list)
    goal: str = ""


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "instance": INSTANCE,
        "telegram_connected": telegram_state["connected"],
        "telegram_error": telegram_state["error"],
        "me": me_info,
        "global_pause": config["behavior"].get("global_pause", False),
        "auto_send": config["behavior"].get("auto_send", False),
        "persona_configured": any(
            (config["persona"].get(k) or "").strip() for k in config_store.DEFAULTS["persona"]
        ),
    }


@app.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    return config


@app.put("/api/config")
async def api_put_config(payload: dict[str, Any]) -> dict[str, Any]:
    global config
    before = dict(booking_settings())
    try:
        config = config_store.save(payload)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not save config: {exc}") from exc
    await hub.broadcast({"type": "config", "config": config})
    log.info("Config updated from the admin panel.")
    after = booking_settings()
    if after.get("enabled") and (
        after.get("provider") != before.get("provider") or not before.get("enabled")
    ):
        asyncio.create_task(resend_unsent_bookings())
    return config


@app.get("/api/conversations")
async def api_conversations() -> list[dict[str, Any]]:
    return await db.list_conversations()


@app.get("/api/conversations/{chat_id}/messages")
async def api_messages(chat_id: int) -> dict[str, Any]:
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    return {
        "conversation": conversation,
        "messages": await db.get_messages(chat_id),
        "links": await db.get_links(chat_id),
    }


@app.post("/api/conversations/{chat_id}/read")
async def api_mark_read(chat_id: int) -> dict[str, Any]:
    conversation = await db.mark_read(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    await hub.broadcast({"type": "conversation", "conversation": conversation})
    return conversation


@app.get("/api/conversations/{chat_id}/links")
async def api_links(chat_id: int) -> dict[str, Any]:
    """Chats this one borrows context from, plus weaker matches worth offering.

    Suggestions are the pairs detection scored but would not act on by itself
    — same first name and nothing more — so linking those stays my decision.
    """
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    links = await db.get_links(chat_id)
    linked = {link["source_id"] for link in links}
    suggestions = [
        {
            "chat_id": other["chat_id"],
            "display_name": other["display_name"],
            "username": other["username"],
            "confidence": score,
            "reason": reason,
        }
        for other, score, reason in await context_link.find_candidates(
            db, conversation, min_score=context_link.SINGLE_NAME_SCORE
        )
        if other["chat_id"] not in linked
    ]
    return {"links": links, "suggestions": suggestions}


@app.post("/api/conversations/{chat_id}/links")
async def api_link(chat_id: int, body: LinkBody) -> dict[str, Any]:
    if body.source_id == chat_id:
        raise HTTPException(status_code=400, detail="A chat cannot be linked to itself.")
    for wanted in (chat_id, body.source_id):
        if await db.get_conversation(wanted) is None:
            raise HTTPException(status_code=404, detail="Unknown conversation")
    for link in await context_link.link_by_hand(db, chat_id, body.source_id):
        await hub.broadcast({"type": "chat_link", "link": link})
    log.info("Chat %s linked to chat %s by hand.", chat_id, body.source_id)
    return {"links": await db.get_links(chat_id)}


@app.delete("/api/conversations/{chat_id}/links/{source_id}")
async def api_unlink(chat_id: int, source_id: int) -> dict[str, Any]:
    """Cut a link. It stays cut — detection will not make it again."""
    if not await db.unlink_chats(chat_id, source_id):
        raise HTTPException(status_code=404, detail="These chats are not linked")
    await hub.broadcast(
        {"type": "chat_unlink", "chat_id": chat_id, "source_id": source_id}
    )
    log.info("Chat %s unlinked from chat %s.", chat_id, source_id)
    return {"links": await db.get_links(chat_id)}


@app.post("/api/conversations/{chat_id}/pause")
async def api_pause(chat_id: int, body: PauseBody) -> dict[str, Any]:
    conversation = await db.set_paused(chat_id, body.paused)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    if body.paused:
        cancel_draft(chat_id)
    await hub.broadcast({"type": "conversation", "conversation": conversation})
    return conversation


@app.post("/api/global-pause")
async def api_global_pause(body: GlobalPauseBody) -> dict[str, Any]:
    global config
    updated = {**config, "behavior": {**config["behavior"], "global_pause": body.global_pause}}
    config = config_store.save(updated)
    if body.global_pause:
        log.warning("Automation PAUSED from the admin panel — no replies until resumed.")
        for chat_id in list(draft_tasks):
            cancel_draft(chat_id)
    else:
        log.info("Automation resumed from the admin panel.")
    await hub.broadcast({"type": "config", "config": config})
    return {"global_pause": config["behavior"]["global_pause"]}


@app.post("/api/conversations/{chat_id}/send")
async def api_send(chat_id: int, body: SendBody) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    if await db.get_conversation(chat_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")

    # I answered by hand, so any AI draft in flight for this chat is stale.
    cancel_draft(chat_id)
    try:
        return await send_as_me(chat_id, text)
    except Exception as exc:
        detail = f"Could not send message: {type(exc).__name__}: {exc}"
        await push_error(chat_id, detail)
        raise HTTPException(status_code=502, detail=detail) from exc


@app.post("/api/drafts/{draft_id}/approve")
async def api_approve(draft_id: int, body: ApproveBody) -> dict[str, Any]:
    draft = await db.get_message(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Unknown draft")
    if draft["status"] != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"Draft is already {draft['status']}")

    text = (body.text if body.text is not None else draft["text"]).strip()
    attachments = [
        i for i in (draft.get("attachments") or []) if media_library.get(i) is not None
    ]
    parts = ai_responder.split_burst(text)
    if not parts and not attachments:
        raise HTTPException(status_code=400, detail="Message is empty")

    try:
        sent = await send_burst(
            draft["chat_id"], parts, draft_id=draft_id, attachments=attachments
        )
    except Exception as exc:
        detail = f"Could not send draft: {type(exc).__name__}: {exc}"
        await push_error(draft["chat_id"], detail)
        raise HTTPException(status_code=502, detail=detail) from exc

    await settle_outreach_draft(draft_id, OUT_SENT, text=text)
    return sent


@app.post("/api/drafts/{draft_id}/reject")
async def api_reject(draft_id: int) -> dict[str, Any]:
    draft = await db.get_message(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Unknown draft")
    row = await db.update_message(draft_id, status=STATUS_REJECTED)
    if row is not None:
        await push_message(row)
    await settle_outreach_draft(draft_id, OUT_CANCELLED)
    return row or {}


@app.get("/api/bookings")
async def api_bookings() -> list[dict[str, Any]]:
    return [b.to_dict() for b in booking_store.all()]


@app.post("/api/conversations/{chat_id}/booking-scan")
async def api_booking_scan(chat_id: int) -> dict[str, Any]:
    """Look at this chat for an agreed time right now, without waiting for
    the client's next message — after changing the provider, say."""
    if await db.get_conversation(chat_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    if not booking_settings().get("enabled"):
        raise HTTPException(status_code=409, detail="Bookings are turned off in Settings")
    if chat_id == await provider_chat_id():
        raise HTTPException(status_code=409, detail="That chat is the booking provider")
    before = len(booking_store.all())
    cancel_booking_scan(chat_id)
    await booking_scan_worker(chat_id)
    found = [b.to_dict() for b in booking_store.all()[before:]]
    return {"found": found, "bookings": [b.to_dict() for b in booking_store.for_chat(chat_id)]}


@app.post("/api/bookings/{booking_id}/confirm")
async def api_booking_confirm(booking_id: int) -> dict[str, Any]:
    return await _decide_from_panel(booking_id, confirmed=True)


@app.post("/api/bookings/{booking_id}/decline")
async def api_booking_decline(booking_id: int) -> dict[str, Any]:
    return await _decide_from_panel(booking_id, confirmed=False)


async def _decide_from_panel(booking_id: int, confirmed: bool) -> dict[str, Any]:
    """The panel operator answers instead of the provider, or for them."""
    booking = booking_store.get(booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Unknown booking")
    if booking.status != bookings.PENDING:
        raise HTTPException(status_code=409, detail=f"Booking is already {booking.status}")
    await decide_booking(booking, confirmed, by="panel")
    if booking.provider_chat_id is not None:
        try:
            await send_as_me(
                booking.provider_chat_id,
                f"#{booking.id} was {'confirmed' if confirmed else 'declined'} from the panel.",
            )
        except Exception as exc:
            log.warning("Could not tell the provider about #%s: %s", booking.id, type(exc).__name__)
    return booking.to_dict()


@app.get("/api/contacts")
async def api_contacts() -> list[dict[str, Any]]:
    if not telegram_state["connected"]:
        raise HTTPException(status_code=503, detail="Telegram is not connected yet")
    try:
        return await list_contacts()
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not read contacts: {type(exc).__name__}: {exc}"
        ) from exc


@app.get("/api/outreach")
async def api_outreach_list() -> list[dict[str, Any]]:
    return await db.list_outreach()


@app.post("/api/outreach")
async def api_outreach_queue(body: OutreachBody) -> dict[str, Any]:
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Say what the message should achieve")
    if not body.chat_ids:
        raise HTTPException(status_code=400, detail="Pick at least one contact")

    # Only people already in the account's contacts may be targeted.
    try:
        allowed = {c["chat_id"]: c["display_name"] for c in await list_contacts()}
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not verify contacts: {type(exc).__name__}"
        ) from exc

    unknown = [cid for cid in body.chat_ids if cid not in allowed]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(unknown)} of those are not in your Telegram contacts. "
                "Outreach only goes to people you already have as contacts."
            ),
        )

    recipients = [(cid, allowed[cid]) for cid in body.chat_ids]
    queued = await db.queue_outreach(recipients, goal)
    ensure_outreach_worker()
    await broadcast_outreach()
    return {"queued": len(queued), "skipped": len(recipients) - len(queued)}


@app.post("/api/outreach/cancel")
async def api_outreach_cancel() -> dict[str, Any]:
    cancelled = await db.cancel_queued_outreach()
    global outreach_task
    task, outreach_task = outreach_task, None
    if task is not None and not task.done():
        task.cancel()
        # Wait for it to actually stop. Otherwise queueing again straight after
        # sees a task that is cancelling-but-not-done and starts no replacement,
        # leaving the new items sitting in the queue forever.
        with suppress(asyncio.CancelledError):
            await task
    await broadcast_outreach()
    return {"cancelled": cancelled}


# ---------------------------------------------------------------------------
# Sign-in — the panel's login screen
# ---------------------------------------------------------------------------


class AuthStartBody(BaseModel):
    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    deepseek_api_key: str = ""


class AuthCodeBody(BaseModel):
    code: str = ""


class AuthPasswordBody(BaseModel):
    password: str = ""


# The DeepSeek key entered on the login form, held until Telegram sign-in
# succeeds so nothing is written to .env for a login that never completed.
pending_deepseek_key: str = ""


def auth_state() -> dict[str, Any]:
    """What the login screen needs: are we in, and if not, which step is next."""
    return {
        "logged_in": env is not None,
        # Never send the API hash back out; the ID alone is not sensitive.
        "prefill": {
            "api_id": credentials.get("API_ID", ""),
            "api_hash_saved": bool(credentials.get("API_HASH")),
            "deepseek_saved": bool(credentials.get("DEEPSEEK_API_KEY")),
        },
        **login_flow.state(),
    }


async def broadcast_auth() -> None:
    await hub.broadcast({"type": "auth", "auth": auth_state()})
    await hub.broadcast({"type": "status", "status": await api_status()})


async def finish_login(session: str, me: Any) -> None:
    """Persist what the login screen collected and bring the bot up on it."""
    global pending_deepseek_key
    values = {
        "API_ID": str(login_flow.api_id),
        "API_HASH": login_flow.api_hash or "",
        "SESSION": session,
    }
    if pending_deepseek_key:
        values["DEEPSEEK_API_KEY"] = pending_deepseek_key
    pending_deepseek_key = ""
    save_credentials(values)

    name = describe_sender(me, getattr(me, "id", 0))[0]
    log.info("Signed in as %s; credentials saved to %s.", name, ENV_PATH.name)
    await start_telegram(build_env(credentials))
    await broadcast_auth()


@app.get("/api/auth")
async def api_auth() -> dict[str, Any]:
    return auth_state()


@app.post("/api/auth/start")
async def api_auth_start(body: AuthStartBody) -> dict[str, Any]:
    global pending_deepseek_key
    if env is not None:
        raise HTTPException(status_code=409, detail="Already signed in. Log out first.")

    api_id_raw = body.api_id.strip()
    api_hash = body.api_hash.strip() or credentials.get("API_HASH", "")
    phone = body.phone.strip()
    deepseek_key = body.deepseek_api_key.strip()

    if not api_id_raw.isdigit():
        raise HTTPException(
            status_code=400,
            detail="API ID must be the number from my.telegram.org (7-8 digits).",
        )
    if not api_hash:
        raise HTTPException(status_code=400, detail="API hash is required.")
    if is_placeholder(api_hash) or is_placeholder(api_id_raw):
        raise HTTPException(
            status_code=400, detail="Those are the example values — use your own from my.telegram.org."
        )
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number is required.")
    if not deepseek_key and not credentials.get("DEEPSEEK_API_KEY"):
        raise HTTPException(
            status_code=400,
            detail="DeepSeek API key is required (from platform.deepseek.com).",
        )
    if deepseek_key and is_placeholder(deepseek_key):
        raise HTTPException(status_code=400, detail="That is the example DeepSeek key, not a real one.")

    try:
        await login_flow.start(int(api_id_raw), api_hash, phone)
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pending_deepseek_key = deepseek_key
    await broadcast_auth()
    return auth_state()


@app.post("/api/auth/code")
async def api_auth_code(body: AuthCodeBody) -> dict[str, Any]:
    try:
        result = await login_flow.submit_code(body.code)
    except LoginError as exc:
        await broadcast_auth()  # the step may have gone back to the start
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is not None:
        await finish_login(*result)
    else:
        await broadcast_auth()
    return auth_state()


@app.post("/api/auth/password")
async def api_auth_password(body: AuthPasswordBody) -> dict[str, Any]:
    try:
        result = await login_flow.submit_password(body.password)
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await finish_login(*result)
    return auth_state()


@app.post("/api/auth/resend")
async def api_auth_resend() -> dict[str, Any]:
    try:
        await login_flow.resend()
    except LoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await broadcast_auth()
    return auth_state()


@app.post("/api/auth/cancel")
async def api_auth_cancel() -> dict[str, Any]:
    global pending_deepseek_key
    pending_deepseek_key = ""
    await login_flow.reset()
    await broadcast_auth()
    return auth_state()


@app.post("/api/auth/logout")
async def api_auth_logout() -> dict[str, Any]:
    """Sign the account out of this bot: end the session on Telegram's side too."""
    if client is not None and client.is_connected():
        try:
            await client.log_out()
        except Exception as exc:
            log.warning("Telegram log_out failed (%s); dropping the session locally.", type(exc).__name__)
    await drop_session(notice=None)
    log.info("Logged out from the panel.")
    return auth_state()


async def drop_session(notice: Optional[str]) -> None:
    """Forget the saved session and go back to the sign-in screen."""
    global env
    await stop_telegram()
    env = None
    save_credentials({"SESSION": ""})
    await login_flow.reset(notice=notice)
    await broadcast_auth()


# ---------------------------------------------------------------------------
# Media library
# ---------------------------------------------------------------------------


class MediaDescribeBody(BaseModel):
    description: str = ""


class SendMediaBody(BaseModel):
    media_id: int


async def broadcast_media() -> None:
    await hub.broadcast({"type": "media", "media": media_library.all()})


@app.get("/api/media")
async def api_media_list() -> list[dict[str, Any]]:
    media_library.refresh()
    return media_library.all()


@app.put("/api/media/upload")
async def api_media_upload(request: Request, name: str, description: str = "") -> dict[str, Any]:
    """The file's bytes are the request body — streamed to disk, so a long
    video never has to fit in memory."""
    if media.kind_for(name) is None:
        raise HTTPException(
            status_code=400,
            detail="Only photos (jpg, png, webp, gif) and videos (mp4, mov, mkv, webm) are accepted.",
        )
    filename = media_library.unique_name(name)
    target = media_library.dir / filename
    media_library.dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + filename + ".part")
    written = 0
    try:
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                written += len(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="The file is empty.")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    item = media_library.add_file(filename, description)
    log.info("Media added: %s (%s bytes).", media.label(item), written)
    await broadcast_media()
    return item


@app.patch("/api/media/{item_id}")
async def api_media_describe(item_id: int, body: MediaDescribeBody) -> dict[str, Any]:
    item = media_library.describe(item_id, body.description)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    await broadcast_media()
    return item


@app.delete("/api/media/{item_id}")
async def api_media_delete(item_id: int) -> dict[str, Any]:
    if not media_library.remove(item_id):
        raise HTTPException(status_code=404, detail="Unknown media item")
    await broadcast_media()
    return {"ok": True}


@app.get("/api/media/{item_id}/file")
async def api_media_file(item_id: int) -> FileResponse:
    path = media_library.path(item_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    return FileResponse(str(path))


@app.post("/api/conversations/{chat_id}/send-media")
async def api_send_media(chat_id: int, body: SendMediaBody) -> dict[str, Any]:
    """A file sent by hand from the panel, into the open conversation."""
    if await db.get_conversation(chat_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    if media_library.get(body.media_id) is None:
        raise HTTPException(status_code=404, detail="Unknown media item")
    cancel_draft(chat_id)
    try:
        return await send_media_as_me(chat_id, body.media_id)
    except Exception as exc:
        detail = f"Could not send file: {type(exc).__name__}: {exc}"
        await push_error(chat_id, detail)
        raise HTTPException(status_code=502, detail=detail) from exc


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        await ws.send_json(
            {
                "type": "hello",
                "conversations": await db.list_conversations(),
                "config": config,
                "status": await api_status(),
                "auth": auth_state(),
                "bookings": [b.to_dict() for b in booking_store.all()],
                "media": media_library.all(),
            }
        )
        while True:
            await ws.receive_text()  # client sends keepalives; nothing to parse
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.disconnect(ws)


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error in admin API")
    return JSONResponse(
        status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


async def start_telegram(new_env: Env) -> None:
    """Bring the userbot up on these credentials (replacing any running one)."""
    global env, client, telegram_task
    await stop_telegram()
    env = new_env
    client = TelegramClient(
        StringSession(env.session),
        env.api_id,
        env.api_hash,
        # Short rate limits are slept through inside Telethon; anything longer
        # is raised so handle_send_failure can decide whether to back off or
        # stop the account entirely, rather than silently blocking a task.
        flood_sleep_threshold=int(config["safety"].get("max_flood_wait_seconds", 300)),
        # After a network drop (laptop sleep, Wi-Fi blip) ask Telegram for the
        # updates that happened meanwhile, instead of hoping it re-sends them.
        catch_up=True,
    )
    client.add_event_handler(on_incoming, events.NewMessage(incoming=True))
    client.add_event_handler(on_outgoing, events.NewMessage(outgoing=True))
    telegram_task = asyncio.create_task(run_telegram(client))
    global reminder_task
    reminder_task = asyncio.create_task(reminder_loop())


async def stop_telegram() -> None:
    global client, telegram_task, offline_timer, presence_online, reminder_task
    presence_online = False
    task, telegram_task = telegram_task, None
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    ticker, reminder_task = reminder_task, None
    if ticker is not None and not ticker.done():
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker
    for chat_id in list(draft_tasks):
        cancel_draft(chat_id)
    for chat_id in list(booking_scan_tasks):
        cancel_booking_scan(chat_id)
    if offline_timer is not None and not offline_timer.done():
        offline_timer.cancel()
    old, client = client, None
    if old is not None and old.is_connected():
        with suppress(Exception):
            await old.disconnect()
    telegram_state["connected"] = False
    telegram_state["error"] = None
    me_info.clear()


async def run_telegram(tg: TelegramClient) -> None:
    """Keep the userbot connected; reconnect on failure without killing the API."""
    global me_info
    backoff = 5
    while True:
        try:
            await tg.connect()
            if not await tg.is_user_authorized():
                # The saved session was revoked (Settings -> Devices) or has
                # expired. Send the person back to the sign-in screen rather
                # than sitting here with a dead session.
                notice = (
                    "The saved Telegram session is no longer valid — it was "
                    "probably ended from Settings -> Devices. Sign in again."
                )
                log.error("%s", notice)
                asyncio.create_task(drop_session(notice))
                return

            me = await tg.get_me()
            me_info = {
                "id": getattr(me, "id", None),
                "name": describe_sender(me, getattr(me, "id", 0))[0],
                "username": getattr(me, "username", None),
            }
            telegram_state["connected"] = True
            telegram_state["error"] = None
            backoff = 5
            if config["presence"].get("enabled", True):
                await set_presence(False)
            log.info("Telegram connected as %s. Listening for private messages "
                     "(send a DM from another account to test).", me_info["name"])
            if config["behavior"].get("global_pause"):
                last = ""
                with suppress(OSError):
                    last = (DATA_DIR / "last_halt.txt").read_text(encoding="utf-8").strip()
                log.warning(
                    "Automation is GLOBALLY PAUSED — incoming messages will NOT be "
                    "answered. Resume from the panel.%s",
                    f" Last automatic halt: {last}" if last else "",
                )
            await hub.broadcast({"type": "status", "status": await api_status()})

            await tg.run_until_disconnected()
            telegram_state["connected"] = False
            log.warning("Telegram disconnected; reconnecting…")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            telegram_state["connected"] = False
            telegram_state["error"] = f"{type(exc).__name__}: {exc}"
            log.error("Telegram client error: %s", telegram_state["error"])
            await hub.broadcast({"type": "status", "status": await api_status()})

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def run_web() -> None:
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False)
    )
    if HOST not in LOOPBACK:
        log.warning(
            "Admin panel bound to %s, not loopback. It has NO LOGIN — anyone who "
            "can reach this port controls the account. Only do this inside a "
            "container whose port is published to 127.0.0.1, or behind a firewall.",
            HOST,
        )
    url = f"http://{'127.0.0.1' if HOST in LOOPBACK else HOST}:{PORT}"
    log.info("Admin panel%s: %s", f" for instance '{INSTANCE}'" if INSTANCE else "", url)

    if HOST in LOOPBACK and not os.getenv("NO_BROWSER"):
        asyncio.create_task(open_browser(server, url))
    await server.serve()


async def open_browser(server: uvicorn.Server, url: str) -> None:
    """Open the panel once the server is listening. Set NO_BROWSER=1 to skip."""
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)
    else:
        return
    try:
        await asyncio.to_thread(webbrowser.open, url)
    except Exception as exc:
        log.info("Could not open a browser (%s). Open %s yourself.", type(exc).__name__, url)


async def main() -> None:
    global credentials, http_client

    await db.connect()
    http_client = httpx.AsyncClient(timeout=ai_responder.REQUEST_TIMEOUT_SECONDS)

    credentials = read_credentials()
    try:
        saved_env = build_env(credentials)
    except ValueError as exc:
        saved_env = None
        log.info("No usable saved login (%s). Sign in from the panel.", exc)
        if credentials.get("SESSION"):
            # Something is there but broken — don't keep tripping over it.
            credentials["SESSION"] = ""

    if saved_env is not None:
        await start_telegram(saved_env)

    try:
        await run_web()
    finally:
        await stop_telegram()
        await login_flow.reset()
        if http_client is not None:
            await http_client.aclose()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
