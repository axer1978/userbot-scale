"""Per-session runtime: the Telethon client lifecycle + business logic for
one Telegram account, ported from the original single-account `main.py` to
run against the fleet's shared Postgres pool.

One `SessionRuntime` instance == one Telegram account == one row in
`telegram_sessions`. A worker process (see `manager.py`, not built yet) is
expected to construct one `SessionRuntime` per leased session it owns and
call `start()` / `stop()` on each. Nothing here is module-level global
state — every module-level global in the old `main.py` became `self.<name>`
here, and every bare function became a method, specifically so N runtimes
can coexist in one process without stepping on each other's state.

Deliberately NOT included in this file (separate, still-open pieces of the
fleet rework):

- The FastAPI admin panel / HTTP routes (plan items 11-14). This module is
  the engine; a panel wraps N of these behind one API, routed by
  session_id, and calls the methods below instead of module functions.
- Turning a phone/code/2FA exchange into the dc_id/server_address/port/
  auth_key that `SessionRegistry.save_login()` needs (the fleet-mode
  rework of `login_flow.py`). `start()` here assumes the session already
  has usable auth in `telegram_sessions` — i.e. login already happened.
  Call `SessionRuntime.needs_login()` to check first.
- `manager.py` (the Master Process Manager) — leases sessions from
  Postgres, spawns worker processes, and is what will actually construct
  `SessionRuntime` objects in production. For a single manual test today,
  construct one directly (see `__main__` below) and it manages its own
  lease via a private `LeaseKeeper`.

Everything in `ai_responder.py`, `bookings.py`, `media.py`, `context_link.py`
and `google_calendar.py` is unchanged — they take `db`/paths as arguments
and were already storage-agnostic. `bookings.BookingStore` and
`media.MediaLibrary` are still per-session *files* (not yet migrated into
Postgres), so each runtime is given its own subdirectory under `DATA_DIR`
keyed by session_id, rather than the old single shared `DATA_DIR`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
from contextlib import suppress
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

import asyncpg
import httpx
from telethon import TelegramClient, errors, events
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import InputPeerUser, User

import ai_responder
import bookings
import commands
import config_store
import context_link
import device_profiles
import google_calendar
import leasing
import media
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
    SessionRegistry,
)

log = logging.getLogger("session_runtime")


class NeedsLogin(RuntimeError):
    """Raised by start() when the session has no usable auth_key yet."""


class SendBlocked(Exception):
    """A send was deliberately not attempted, with a reason worth showing."""


def _ov_int(overrides: dict[str, Any], key: str, fallback: int) -> int:
    value = overrides.get(key)
    return value if isinstance(value, int) else fallback


def _parse_time(value: Any, fallback: dtime) -> dtime:
    try:
        hour, minute = str(value).split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


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
    """A generic socks5://user:pass@host:port -> Telethon's proxy tuple.

    Provider-agnostic on purpose (plan decision: proxy provider not chosen
    yet). None (no proxy configured) means a direct connection, which is
    the expected state until Ops Item 2/3 supply real credentials.
    """
    if not proxy_url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    if parsed.scheme not in ("socks5", "socks5h"):
        raise ValueError(f"Unsupported proxy scheme {parsed.scheme!r}; expected socks5://")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Proxy URL missing host or port: {proxy_url!r}")
    return (
        "socks5",
        parsed.hostname,
        parsed.port,
        True,  # rdns — resolve hostnames through the proxy, not locally
        parsed.username,
        parsed.password,
    )


class Hub:
    """Every existing `await self.hub.broadcast(...)` call site in this file
    predates the panel/worker process split. Rather than touch each of the
    ~30 call sites, this keeps the same interface and republishes onto
    Redis (see commands.py) so any panel process with this session_id's
    websocket open — regardless of which worker process actually holds the
    live connection — receives it. There is no local websocket list here
    any more; that lived in the panel when panel and runtime were one
    process."""

    def __init__(self, runtime: "SessionRuntime") -> None:
        self._runtime = runtime

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if self._runtime.bus is not None:
            await self._runtime.bus.publish_event(self._runtime.session_id, payload)


class SessionRuntime:
    """Runs one Telegram account: Telethon client, drafting, sends, safety
    limits, outreach, bookings, media, presence. Construct one per leased
    session_id; call start(), then stop() on shutdown."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        session_id: str,
        *,
        data_dir: Path,
        redis_url: str,
        worker_id: Optional[str] = None,
    ) -> None:
        self.pool = pool
        self.session_id = session_id
        self.redis_url = redis_url
        self.registry = SessionRegistry(pool)
        self.db = Database(pool, session_id)
        self.worker_id = worker_id or f"{socket.gethostname()}:{id(self)}"

        # Per-session data directory — bookings.json and media/ are still
        # files, not Postgres rows, so each session gets its own subtree
        # rather than the old single shared DATA_DIR.
        self.data_dir = data_dir / session_id
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.media_library = media.MediaLibrary(self.data_dir / "media")
        self.booking_store = bookings.BookingStore(self.data_dir / "bookings.json")

        self.hub = Hub(self)
        self.bus: Optional[commands.CommandBus] = None
        self._command_stop_event = asyncio.Event()
        self._command_serve_task: Optional[asyncio.Task] = None
        self.config: dict[str, Any] = config_store.normalize({})
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        self.deepseek_key: Optional[str] = None

        self.http_client: Optional[httpx.AsyncClient] = None
        self.client: Optional[TelegramClient] = None
        self.telegram_task: Optional[asyncio.Task] = None
        self.reminder_task: Optional[asyncio.Task] = None
        self.me_info: dict[str, Any] = {}
        self.telegram_state: dict[str, Any] = {"connected": False, "error": None}

        self.draft_tasks: dict[int, asyncio.Task] = {}
        self.outreach_task: Optional[asyncio.Task] = None
        self.in_flight_sends: dict[int, list[str]] = {}
        self.in_flight_media: dict[int, int] = {}

        self.presence_online = False
        self.offline_timer: Optional[asyncio.Task] = None
        self.active_chats: set[int] = set()
        self.sending_chats: set[int] = set()

        self.booking_scan_tasks: dict[int, asyncio.Task] = {}
        self._provider_resolved: tuple[str, Optional[int], float] = ("", None, 0.0)
        self.PROVIDER_RETRY_SECONDS = 300
        self._calendar: Optional[google_calendar.GoogleCalendar] = None
        self._calendar_key: tuple[str, str] = ("", "")
        self.REMINDER_TICK_SECONDS = 60

        self._ai_gate: Optional[asyncio.Semaphore] = None
        self._ai_gate_size = 0

        self._lease_keeper: Optional[leasing.LeaseKeeper] = None
        self._lease_keeper_task: Optional[asyncio.Task] = None
        self._stopping = False

        self.BURST_GAP_MIN_SECONDS = 0.6
        self.BURST_GAP_MAX_SECONDS = 2.2

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def needs_login(self) -> bool:
        auth = await self.registry.load_auth(self.session_id)
        return auth is None or not auth.get("auth_key")

    async def start(self) -> None:
        """Acquire this session's lease, connect to Telegram, start background loops."""
        lease = await leasing.acquire(self.pool, self.session_id, self.worker_id)
        if lease is None:
            holder_id, expires_at = await leasing.holder(self.pool, self.session_id)
            raise leasing.LeaseLost(
                f"session {self.session_id!r} is already leased by {holder_id!r} "
                f"until {expires_at}; refusing to run it twice."
            )

        self._lease_keeper = leasing.LeaseKeeper(
            self.pool, self.worker_id, on_lost=self._on_lease_lost
        )
        self._lease_keeper.track(lease)
        self._lease_keeper_task = asyncio.create_task(self._lease_keeper.run())

        # Everything past the lease must clean up after itself on failure.
        # Callers (manager.py, panel.py) log-and-skip a session that raises
        # NeedsLogin, so without this the renewal task would go on renewing a
        # lease nobody is using and that session could never be claimed again.
        try:
            await self.db.connect()
            self.bus = await commands.CommandBus.connect(self.redis_url)
            self._command_serve_task = asyncio.create_task(
                self.bus.serve(self.session_id, self.handle_command, self._command_stop_event)
            )
            self.http_client = httpx.AsyncClient(timeout=ai_responder.REQUEST_TIMEOUT_SECONDS)
            self.config = await config_store.load(self.pool, self.session_id)

            auth = await self.registry.load_auth(self.session_id)
            if auth is None or not auth.get("auth_key"):
                raise NeedsLogin(
                    f"session {self.session_id!r} has no auth_key yet — run the login flow "
                    "(SessionRegistry.create + save_login) before starting this runtime."
                )
            self.api_id = auth["api_id"]
            self.api_hash = auth["api_hash"]
            self.deepseek_key = await self.registry.load_deepseek_key(self.session_id)
            if not self.deepseek_key:
                raise NeedsLogin(
                    f"session {self.session_id!r} has no DeepSeek key set "
                    "(SessionRegistry.set_deepseek_key)."
                )

            proxy_url = await self.registry.load_proxy(self.session_id)
            await self._start_telegram(auth, proxy_url)
        except BaseException:
            await self._release_partial_start()
            raise

    async def _release_partial_start(self) -> None:
        """Undo what start() managed to do before it failed, so a session that
        cannot run does not sit on a live lease."""
        if self._lease_keeper is not None:
            await self._lease_keeper.stop()
            self._lease_keeper = None
        if self._lease_keeper_task is not None:
            with suppress(asyncio.CancelledError):
                await self._lease_keeper_task
            self._lease_keeper_task = None
        with suppress(Exception):
            await leasing.release(self.pool, self.session_id, self.worker_id)
        if self.http_client is not None:
            with suppress(Exception):
                await self.http_client.aclose()
            self.http_client = None
        self._command_stop_event.set()
        if self._command_serve_task is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._command_serve_task, timeout=5)
            self._command_serve_task = None
        if self.bus is not None:
            with suppress(Exception):
                await self.bus.close()
            self.bus = None
        with suppress(Exception):
            await self.db.close()

    async def stop(self) -> None:
        self._stopping = True
        await self._stop_telegram()
        if self._lease_keeper is not None:
            await self._lease_keeper.stop()
        if self._lease_keeper_task is not None:
            with suppress(asyncio.CancelledError):
                await self._lease_keeper_task
        await leasing.release(self.pool, self.session_id, self.worker_id)
        if self.http_client is not None:
            await self.http_client.aclose()
        self._command_stop_event.set()
        if self._command_serve_task is not None:
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._command_serve_task, timeout=5)
        if self.bus is not None:
            await self.bus.close()
        await self.db.close()

    async def handle_command(self, action: str, args: dict[str, Any]) -> Any:
        """Executed when this session's owning worker receives a command over
        the Redis bus (commands.py) — the panel asking for something that
        needs the live Telethon client. Anything that only needs Postgres or
        local files, the panel does directly and this is never reached."""
        if action == "send":
            return await self.send_as_me(args["chat_id"], args["text"])

        if action == "send_media":
            return await self.send_media_as_me(args["chat_id"], args["media_id"])

        if action == "list_contacts":
            return await self.list_contacts()

        if action == "cancel_draft":
            self.cancel_draft(args["chat_id"])
            return {"ok": True}

        if action == "cancel_all_drafts":
            for chat_id in list(self.draft_tasks):
                self.cancel_draft(chat_id)
            return {"ok": True}

        if action == "reload_config":
            # The panel wrote the new config straight to Postgres (it holds
            # no runtime to go through); without this the worker's in-memory
            # copy would stay stale until its own next unrelated read.
            self.config = await config_store.load(self.pool, self.session_id)
            return {"ok": True}

        if action == "resend_unsent_bookings":
            await self.resend_unsent_bookings()
            return {"ok": True}

        if action == "ensure_outreach_worker":
            self.ensure_outreach_worker()
            return {"ok": True}

        if action == "cancel_outreach":
            task, self.outreach_task = self.outreach_task, None
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            return {"ok": True}

        if action == "approve_draft":
            draft_id = args["draft_id"]
            draft = await self.db.get_message(draft_id)
            if draft is None:
                raise ValueError("Unknown draft")
            if draft["status"] != STATUS_PENDING:
                raise ValueError(f"Draft is already {draft['status']}")
            text_override = args.get("text")
            text = (text_override if text_override is not None else draft["text"]).strip()
            attachments = [
                i for i in (draft.get("attachments") or []) if self.media_library.get(i) is not None
            ]
            parts = ai_responder.split_burst(text)
            if not parts and not attachments:
                raise ValueError("Message is empty")
            sent = await self.send_burst(draft["chat_id"], parts, draft_id=draft_id, attachments=attachments)
            await self.settle_outreach_draft(draft_id, OUT_SENT, text=text)
            return sent

        if action == "booking_scan":
            chat_id = args["chat_id"]
            if await self.db.get_conversation(chat_id) is None:
                raise ValueError("Unknown conversation")
            if not self.booking_settings().get("enabled"):
                raise ValueError("Bookings are turned off in Settings")
            if chat_id == await self.provider_chat_id():
                raise ValueError("That chat is the booking provider")
            before = len(self.booking_store.all())
            self.cancel_booking_scan(chat_id)
            await self.booking_scan_worker(chat_id)
            found = [b.to_dict() for b in self.booking_store.all()[before:]]
            return {"found": found, "bookings": [b.to_dict() for b in self.booking_store.for_chat(chat_id)]}

        if action == "booking_decide":
            booking_id, confirmed = args["booking_id"], args["confirmed"]
            booking = self.booking_store.get(booking_id)
            if booking is None:
                raise ValueError("Unknown booking")
            if booking.status != bookings.PENDING:
                raise ValueError(f"Booking is already {booking.status}")
            await self.decide_booking(booking, confirmed, by="panel")
            if booking.provider_chat_id is not None:
                try:
                    await self.send_as_me(
                        booking.provider_chat_id,
                        f"#{booking.id} was {'confirmed' if confirmed else 'declined'} from the panel.",
                    )
                except Exception as exc:
                    log.warning(
                        "[%s] Could not tell the provider about #%s: %s",
                        self.session_id, booking.id, type(exc).__name__,
                    )
            return booking.to_dict()

        raise ValueError(f"Unknown command action: {action!r}")

    async def _on_lease_lost(self, session_id: str) -> None:
        """The renewal loop confirmed (or fears) another worker now owns this
        session. Disconnect immediately — continuing would risk exactly the
        two-workers-mutating-one-session AUTH_KEY_UNREGISTERED failure the
        leasing module exists to prevent."""
        log.error(
            "Lease lost for session %s; disconnecting to avoid a double-run.", session_id
        )
        await self._stop_telegram()

    # ------------------------------------------------------------------
    # Config save helper (was module-global reassignment; now returns the
    # fresh value and stores it on self, same call-site shape as before)
    # ------------------------------------------------------------------

    async def save_config(
        self, payload: dict[str, Any], *, expected_revision: Optional[int] = None
    ) -> dict[str, Any]:
        self.config = await config_store.save(
            self.pool, self.session_id, payload, expected_revision=expected_revision
        )
        return self.config

    def ai_gate(self) -> asyncio.Semaphore:
        size = max(1, int(self.config["ai"].get("max_concurrent_requests", 4) or 4))
        if self._ai_gate is None or size != self._ai_gate_size:
            self._ai_gate, self._ai_gate_size = asyncio.Semaphore(size), size
        return self._ai_gate

    # ------------------------------------------------------------------
    # Account safety
    #
    # Telegram does not explain why an account gets limited, and there is no
    # way to ask. The signals that matter are behavioural: outbound volume,
    # how many *different* people are contacted, and how often recipients
    # press "Report Spam". So the approach here is to actually send less
    # when Telegram pushes back, rather than to try to look like something
    # else while sending the same amount.
    # ------------------------------------------------------------------

    async def halt_everything(self, reason: str) -> None:
        """Flip the global pause and tell every open panel tab why.

        Recovery is deliberately manual: if something is wrong, an operator
        should look at it before this account starts sending again.
        """
        log.error("[%s] HALTING ALL AUTOMATION: %s", self.session_id, reason)
        if not self.config["behavior"].get("global_pause"):
            await self.save_config(
                {**self.config, "behavior": {**self.config["behavior"], "global_pause": True}}
            )
        for chat_id in list(self.draft_tasks):
            self.cancel_draft(chat_id)
        await self.db.cancel_queued_outreach()
        await self.hub.broadcast({"type": "config", "config": self.config})
        await self.hub.broadcast({"type": "halted", "reason": reason})
        await self.push_error(None, f"Automation halted: {reason}")
        try:
            (self.data_dir / "last_halt.txt").write_text(
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {reason}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        await self.registry.set_state(self.session_id, "halted", reason)

    async def check_daily_quota(self) -> None:
        safety = self.config["safety"]
        since = self.start_of_day_utc()

        sent = await self.db.sent_since(since)
        limit = int(safety.get("daily_send_limit", 150))
        if sent >= limit:
            raise SendBlocked(
                f"Daily send limit reached ({sent}/{limit} messages today). "
                "Sending resumes tomorrow; raise the limit in Settings if this is wrong."
            )

        peers = await self.db.distinct_peers_since(since)
        peer_limit = int(safety.get("daily_peer_limit", 30))
        if peers >= peer_limit:
            raise SendBlocked(
                f"Daily limit on distinct people reached ({peers}/{peer_limit} today). "
                "Writing to many different people in one day is the strongest spam signal."
            )

    async def handle_send_failure(self, chat_id: Optional[int], exc: BaseException) -> bool:
        """Translate a Telegram error into the right defensive action.

        Returns True when the error was recognised and handled, so callers
        can avoid double-reporting it.
        """
        safety = self.config["safety"]

        if isinstance(exc, errors.PeerFloodError):
            if safety.get("halt_on_peer_flood", True):
                await self.halt_everything(
                    "Telegram returned PeerFloodError — it considers this account "
                    "to be sending unsolicited messages. Everything is paused. Do "
                    "not resume until you know why; sending through this is what "
                    "gets a number banned."
                )
            else:
                await self.push_error(chat_id, "PeerFloodError from Telegram (halt disabled).")
            return True

        if isinstance(exc, (errors.UserDeactivatedBanError, errors.AuthKeyUnregisteredError,
                            errors.SessionRevokedError)):
            await self.halt_everything(
                f"Telegram rejected the session ({type(exc).__name__}). The account "
                "may be banned or the session revoked. Automation is stopped."
            )
            return True

        if isinstance(exc, (errors.FloodWaitError, errors.SlowModeWaitError)):
            wait = int(getattr(exc, "seconds", 0) or 0)
            cap = int(safety.get("max_flood_wait_seconds", 300))
            log.warning("[%s] Telegram asked us to wait %ss before sending again.", self.session_id, wait)
            await self.push_error(
                chat_id, f"Telegram rate limit: it asked for a {wait}s pause. Backing off."
            )
            if wait > cap:
                await self.halt_everything(
                    f"Telegram demanded a {wait}s wait, beyond the {cap}s this is "
                    "willing to sleep through. Paused so nothing retries into it."
                )
            else:
                await asyncio.sleep(wait)
            return True

        if isinstance(exc, (errors.UserIsBlockedError, errors.UserPrivacyRestrictedError,
                            errors.InputUserDeactivatedError, errors.ChatWriteForbiddenError)):
            if chat_id is not None:
                await self.db.set_paused(chat_id, True)
                await self.hub.broadcast({"type": "conversation_paused", "chat_id": chat_id})
            await self.push_error(
                chat_id,
                f"Cannot message this person ({type(exc).__name__}); this "
                "conversation is now paused. They may have blocked the account.",
            )
            return True

        return False

    async def may_message(self, chat_id: int) -> None:
        """Refuse to open a conversation with someone who never opted in."""
        if not self.config["safety"].get("known_contacts_only", True):
            return
        conversation = await self.db.get_conversation(chat_id)
        if conversation is None:
            raise SendBlocked(
                f"Chat {chat_id} is unknown — not in contacts and has never sent a "
                "message. Refusing to open a conversation with a stranger."
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def within_active_hours(self, timing: dict[str, Any]) -> bool:
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
        return now >= start or now <= end

    async def resolve_peer(self, chat_id: int):
        if self.client is None or not self.telegram_state["connected"]:
            raise RuntimeError("Telegram is not connected.")
        try:
            return await self.client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            access_hash = await self.db.get_access_hash(chat_id)
            if access_hash is None:
                raise RuntimeError(
                    f"Cannot resolve chat {chat_id}. Receive a message from them first."
                )
            return InputPeerUser(chat_id, access_hash)

    async def push_message(self, row: dict[str, Any]) -> None:
        conversation = await self.db.get_conversation(row["chat_id"])
        await self.hub.broadcast({"type": "message", "message": row, "conversation": conversation})

    async def push_error(self, chat_id: Optional[int], text: str) -> None:
        log.error("[%s] %s", self.session_id, text)
        row = None
        if chat_id is not None:
            row = await self.db.record_message(
                chat_id, DIR_SYSTEM, STATUS_ERROR, text, bump_preview=False
            )
        await self.hub.broadcast({"type": "error", "chat_id": chat_id, "text": text, "message": row})

    def media_prompt(self) -> str:
        if not self.config["media"].get("enabled", True):
            return ""
        self.media_library.refresh()
        return media.prompt_section(
            self.media_library.all(), ask_before_video=self.config["media"].get("ask_before_video", True)
        )

    def contact_overrides(self, chat_id: Optional[int]) -> dict[str, Any]:
        if chat_id is None:
            return {}
        return self.config.get("contacts", {}).get(str(chat_id)) or {}

    async def borrowed_context(self, chat_id: int) -> str:
        settings = self.config["context_link"]
        if not settings.get("enabled", True):
            return ""
        async with self.ai_gate():
            return await context_link.build_background(
                self.db,
                chat_id,
                api_key=self.deepseek_key,
                ai_config=self.config["ai"],
                settings=settings,
                client=self.http_client,
            )

    async def detect_links(self, conversation: dict[str, Any]) -> None:
        try:
            created = await context_link.autolink(self.db, conversation, self.config["context_link"])
        except Exception:
            log.exception("[%s] Link detection failed for chat %s", self.session_id, conversation.get("chat_id"))
            return
        for link in created:
            await self.hub.broadcast({"type": "chat_link", "link": link})

    def typing_seconds(self, text: str, chat_id: Optional[int] = None) -> float:
        human = self.config["human"]
        overrides = self.contact_overrides(chat_id)
        cps = max(1, _ov_int(overrides, "typing_speed_cps", int(human.get("typing_speed_cps", 12))))
        cap = max(1, _ov_int(overrides, "typing_max_seconds", int(human.get("typing_max_seconds", 25))))
        base = max(0.1, min(cap, len(text) / cps))
        return base * random.uniform(0.85, 1.15)

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------

    async def set_presence(self, online: bool) -> None:
        if self.presence_online == online:
            return
        try:
            await self.client(UpdateStatusRequest(offline=not online))
            self.presence_online = online
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] Could not update presence: %s", self.session_id, type(exc).__name__)

    async def _go_offline_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self.active_chats:
                return
            await self.set_presence(False)
        except asyncio.CancelledError:
            raise

    def schedule_go_offline(self, chat_id: Optional[int]) -> None:
        if chat_id is not None:
            self.active_chats.discard(chat_id)
        presence = self.config["presence"]
        if not presence.get("enabled", True):
            return
        if self.active_chats:
            return
        overrides = self.contact_overrides(chat_id)
        lo = _ov_int(overrides, "offline_delay_min", int(presence.get("offline_delay_min", 15)))
        hi = _ov_int(overrides, "offline_delay_max", int(presence.get("offline_delay_max", 90)))
        if self.offline_timer is not None and not self.offline_timer.done():
            self.offline_timer.cancel()
        self.offline_timer = asyncio.create_task(
            self._go_offline_after(random.uniform(min(lo, hi), max(lo, hi)))
        )

    async def go_online_for(self, chat_id: Optional[int]) -> None:
        if chat_id is not None:
            self.active_chats.add(chat_id)
        presence = self.config["presence"]
        if not presence.get("enabled", True):
            return
        if self.offline_timer is not None and not self.offline_timer.done():
            self.offline_timer.cancel()
        if self.presence_online:
            return
        overrides = self.contact_overrides(chat_id)
        lo = _ov_int(overrides, "online_delay_min", int(presence.get("go_online_delay_min", 2)))
        hi = _ov_int(overrides, "online_delay_max", int(presence.get("go_online_delay_max", 8)))
        await asyncio.sleep(random.uniform(min(lo, hi), max(lo, hi)))
        await self.set_presence(True)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def deliver(self, peer: Any, chat_id: int, text: str, typing: bool) -> Any:
        if not (typing and self.config["human"].get("typing_indicator", True)):
            return await self.client.send_message(peer, text)

        seconds = self.typing_seconds(text, chat_id)
        log.info("[%s]   typing for %.1fs…", self.session_id, seconds)

        result = None
        attempted = False
        try:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(seconds)
                attempted = True
                result = await self.client.send_message(peer, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if result is not None:
                return result
            if attempted:
                raise
            log.warning("[%s] Typing indicator unavailable (%s); sending anyway.", self.session_id, type(exc).__name__)
        else:
            return result

        return await self.client.send_message(peer, text)

    async def mark_read(self, chat_id: int, message_id: Optional[int] = None) -> None:
        if not self.config["human"].get("mark_read", True):
            return
        try:
            await self.client.send_read_acknowledge(chat_id, max_id=message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] Could not mark chat %s read: %s", self.session_id, chat_id, type(exc).__name__)

    async def send_as_me(
        self,
        chat_id: int,
        text: str,
        draft_id: Optional[int] = None,
        typing: bool = False,
        guard: bool = True,
    ) -> dict[str, Any]:
        if guard:
            await self.check_daily_quota()
        peer = await self.resolve_peer(chat_id)
        self.in_flight_sends.setdefault(chat_id, []).append(text)
        try:
            sent = await self.deliver(peer, chat_id, text, typing)
            telegram_id = getattr(sent, "id", None)
            if draft_id is not None:
                row = await self.db.update_message(
                    draft_id, text=text, status=STATUS_SENT, telegram_id=telegram_id
                )
                await self.db.set_conversation_preview(chat_id, text)
            else:
                row = await self.db.record_message(
                    chat_id, DIR_OUT, STATUS_SENT, text, telegram_id=telegram_id
                )
                if row is None:
                    row = await self.db.find_by_telegram_id(chat_id, telegram_id)
        finally:
            pending = self.in_flight_sends.get(chat_id) or []
            if text in pending:
                pending.remove(text)
            if not pending:
                self.in_flight_sends.pop(chat_id, None)

        if row is not None:
            await self.push_message(row)
        return row or {}

    async def deliver_file(self, peer: Any, chat_id: int, item: dict[str, Any], path: Path) -> Any:
        is_video = item.get("kind") == media.VIDEO
        if not self.config["human"].get("typing_indicator", True):
            return await self.client.send_file(peer, str(path), supports_streaming=is_video)
        try:
            async with self.client.action(chat_id, "video" if is_video else "photo") as progress:
                return await self.client.send_file(
                    peer, str(path), supports_streaming=is_video, progress_callback=progress.progress,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, (errors.RPCError, OSError)):
                raise
            log.warning("[%s] Upload indicator unavailable (%s); sending anyway.", self.session_id, type(exc).__name__)
        return await self.client.send_file(peer, str(path), supports_streaming=is_video)

    async def send_media_as_me(
        self, chat_id: int, item_id: int, draft_id: Optional[int] = None, guard: bool = True,
    ) -> dict[str, Any]:
        item = self.media_library.get(item_id)
        path = self.media_library.path(item_id)
        if item is None or path is None:
            raise ValueError(f"Media #{item_id} is no longer in the library.")
        if guard:
            await self.check_daily_quota()
        peer = await self.resolve_peer(chat_id)
        text = media.sent_placeholder(item)
        self.in_flight_media[chat_id] = self.in_flight_media.get(chat_id, 0) + 1
        try:
            sent = await self.deliver_file(peer, chat_id, item, path)
            telegram_id = getattr(sent, "id", None)
            if draft_id is not None:
                row = await self.db.update_message(
                    draft_id, text=text, status=STATUS_SENT, telegram_id=telegram_id, attachments=[item_id],
                )
                await self.db.set_conversation_preview(chat_id, text)
            else:
                row = await self.db.record_message(
                    chat_id, DIR_OUT, STATUS_SENT, text, telegram_id=telegram_id, attachments=[item_id],
                )
                if row is None:
                    row = await self.db.find_by_telegram_id(chat_id, telegram_id)
                    if row is not None:
                        row = await self.db.update_message(row["id"], text=text, attachments=[item_id])
        finally:
            left = self.in_flight_media.get(chat_id, 1) - 1
            if left > 0:
                self.in_flight_media[chat_id] = left
            else:
                self.in_flight_media.pop(chat_id, None)

        if row is not None:
            await self.push_message(row)
        log.info("[%s]   sent %s to chat %s.", self.session_id, media.label(item), chat_id)
        return row or {}

    async def send_burst(
        self,
        chat_id: int,
        parts: list[str],
        draft_id: Optional[int] = None,
        typing: bool = False,
        guard: bool = True,
        attachments: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        files = [i for i in (attachments or []) if self.media_library.get(i) is not None]
        for index, part in enumerate(parts):
            if index:
                await asyncio.sleep(random.uniform(self.BURST_GAP_MIN_SECONDS, self.BURST_GAP_MAX_SECONDS))
            row = await self.send_as_me(
                chat_id, part, draft_id=draft_id if index == 0 else None, typing=typing, guard=guard,
            )
            if index == 0 and draft_id is not None and files:
                row = await self.db.update_message(draft_id, attachments=[]) or row
                await self.push_message(row)
        for index, item_id in enumerate(files):
            if parts or index:
                await asyncio.sleep(random.uniform(self.BURST_GAP_MIN_SECONDS, self.BURST_GAP_MAX_SECONDS))
            row = await self.send_media_as_me(
                chat_id, item_id, draft_id=draft_id if (not parts and index == 0) else None, guard=guard,
            )
        return row

    # ------------------------------------------------------------------
    # Drafting pipeline
    # ------------------------------------------------------------------

    def schedule_draft(self, chat_id: int) -> None:
        self.cancel_draft(chat_id)
        self.draft_tasks[chat_id] = asyncio.create_task(self.draft_worker(chat_id))

    def cancel_draft(self, chat_id: int) -> None:
        task = self.draft_tasks.pop(chat_id, None)
        if task is None or task.done():
            return
        if chat_id in self.sending_chats:
            return
        task.cancel()

    async def draft_worker(self, chat_id: int) -> None:
        try:
            timing = self.config["timing"]
            overrides = self.contact_overrides(chat_id)
            low = _ov_int(overrides, "min_delay_seconds", int(timing.get("min_delay_seconds", 20)))
            high = _ov_int(overrides, "max_delay_seconds", int(timing.get("max_delay_seconds", 90)))
            delay = random.uniform(min(low, high), max(low, high))

            await self.hub.broadcast({"type": "drafting", "chat_id": chat_id, "delay_seconds": round(delay, 1)})
            log.info("[%s]   drafting a reply for chat %s in %.0fs…", self.session_id, chat_id, delay)
            await asyncio.sleep(delay)

            if self.config["behavior"].get("global_pause"):
                return
            conversation = await self.db.get_conversation(chat_id)
            if conversation is None or conversation["automation_paused"]:
                return
            if not self.within_active_hours(self.config["timing"]):
                log.info("[%s] Outside active hours; skipping draft for chat %s.", self.session_id, chat_id)
                return

            history = await self.db.get_history_for_ai(chat_id, limit=30)
            if not history:
                log.info("[%s] No usable history for chat %s; skipping draft.", self.session_id, chat_id)
                return

            if self.config["behavior"].get("auto_send"):
                await self.check_daily_quota()

            await self.go_online_for(chat_id)
            await self.mark_read(chat_id)

            background = await self.borrowed_context(chat_id)
            if background:
                log.info("[%s]   drawing on a linked chat for context.", self.session_id)

            news, news_kind = self.booking_news(chat_id)
            booking_note = bookings.context_for_reply(
                self.booking_store.for_chat(chat_id), news, news_kind
            )
            if news is not None:
                log.info("[%s]   booking #%s: writing the %s into this reply.", self.session_id, news.id, news_kind)

            media_note = self.media_prompt()

            async with self.ai_gate():
                text = await ai_responder.generate_reply(
                    api_key=self.deepseek_key,
                    history=history,
                    persona=self.config["persona"],
                    ai_config=self.config["ai"],
                    client=self.http_client,
                    adaptive_style=self.config["human"].get("adaptive_style", True),
                    general_samples=self.config["finetune"].get("writing_samples", ""),
                    contact=overrides,
                    background=background,
                    booking_note=booking_note,
                    media_note=media_note,
                )

            text, attachments = media.split_attachments(text)
            attachments = [i for i in attachments if self.media_library.get(i) is not None]
            if not media_note:
                attachments = []
            parts = ai_responder.split_burst(text)
            if not parts and not attachments:
                raise ai_responder.AIResponderError("The reply came back empty.")
            if attachments:
                log.info("[%s]   attaching %s.", self.session_id, ", ".join(
                    media.label(self.media_library.get(i)) for i in attachments
                ))

            holds_video = any(
                (self.media_library.get(i) or {}).get("kind") == media.VIDEO for i in attachments
            )
            hold = holds_video and self.config["media"].get("videos_need_approval", True)

            if self.config["behavior"].get("auto_send") and not hold:
                self.sending_chats.add(chat_id)
                try:
                    await self.send_burst(chat_id, parts, typing=True, attachments=attachments)
                finally:
                    self.sending_chats.discard(chat_id)
                log.info(
                    "[%s] Auto-sent AI reply to chat %s%s.", self.session_id, chat_id,
                    f" as {len(parts)} messages" if len(parts) > 1 else "",
                )
            else:
                if hold and self.config["behavior"].get("auto_send"):
                    log.info("[%s]   reply carries a video — held for approval in the panel.", self.session_id)
                row = await self.db.record_message(
                    chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False, attachments=attachments,
                )
                if row is not None:
                    await self.push_message(row)
                log.info("[%s] Draft awaiting approval for chat %s.", self.session_id, chat_id)
            if news is not None:
                if news_kind == "reminder":
                    self.booking_store.update(news, reminder_sent=True)
                else:
                    self.booking_store.update(news, client_notified=True)
                await self.broadcast_booking(news)
            self.schedule_go_offline(chat_id)

        except asyncio.CancelledError:
            raise
        except SendBlocked as exc:
            log.info("[%s] Not replying in chat %s: %s", self.session_id, chat_id, exc)
            await self.push_error(chat_id, str(exc))
        except ai_responder.AIResponderError as exc:
            await self.push_error(chat_id, str(exc))
        except Exception as exc:
            if not await self.handle_send_failure(chat_id, exc):
                log.exception("[%s] Unexpected failure while drafting for chat %s", self.session_id, chat_id)
                await self.push_error(chat_id, f"Drafting failed: {type(exc).__name__}: {exc}")
        finally:
            if chat_id in self.active_chats:
                self.schedule_go_offline(chat_id)
            if self.draft_tasks.get(chat_id) is asyncio.current_task():
                self.draft_tasks.pop(chat_id, None)

    # ------------------------------------------------------------------
    # Outreach
    # ------------------------------------------------------------------

    @staticmethod
    def start_of_day_utc() -> str:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    def ensure_outreach_worker(self) -> None:
        if self.outreach_task is None or self.outreach_task.done():
            self.outreach_task = asyncio.create_task(self.outreach_worker())

    async def outreach_worker(self) -> None:
        try:
            while True:
                item = await self.db.next_queued_outreach()
                if item is None:
                    return

                settings = self.config["outreach"]
                if self.config["behavior"].get("global_pause"):
                    log.info("[%s] Outreach paused (global pause); leaving %s queued.", self.session_id, item["id"])
                    return

                sent_today = await self.db.outreach_sent_since(self.start_of_day_utc())
                limit = int(settings.get("daily_limit", 20))
                if sent_today >= limit:
                    log.info(
                        "[%s] Outreach daily limit reached (%s/%s); the rest stays queued for tomorrow.",
                        self.session_id, sent_today, limit,
                    )
                    await self.hub.broadcast({
                        "type": "outreach_paused",
                        "reason": f"Daily limit of {limit} reached. Remaining messages stay queued.",
                    })
                    return

                await self.process_outreach(item)

                if await self.db.next_queued_outreach() is not None:
                    low = int(settings.get("min_gap_seconds", 90))
                    high = int(settings.get("max_gap_seconds", 300))
                    gap = random.uniform(min(low, high), max(low, high))
                    log.info("[%s] Next outreach message in %.0fs.", self.session_id, gap)
                    await asyncio.sleep(gap)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] Outreach worker stopped unexpectedly", self.session_id)

    async def process_outreach(self, item: dict[str, Any]) -> None:
        outreach_id, chat_id = item["id"], item["chat_id"]

        try:
            await self.may_message(chat_id)
            await self.check_daily_quota()
        except SendBlocked as exc:
            log.info("[%s] Outreach %s not sent: %s", self.session_id, outreach_id, exc)
            await self.db.update_outreach(outreach_id, status=OUT_FAILED, error=str(exc))
            await self.push_error(chat_id, f"Outreach skipped: {exc}")
            await self.broadcast_outreach()
            return

        background = await self.borrowed_context(chat_id)

        try:
            async with self.ai_gate():
                text = await ai_responder.generate_opener(
                    api_key=self.deepseek_key,
                    goal=item["goal"],
                    recipient_name=item["display_name"] or "them",
                    persona=self.config["persona"],
                    ai_config=self.config["ai"],
                    client=self.http_client,
                    general_samples=self.config["finetune"].get("writing_samples", ""),
                    contact=self.contact_overrides(chat_id),
                    background=background,
                )
        except ai_responder.AIResponderError as exc:
            await self.db.update_outreach(outreach_id, status=OUT_FAILED, error=str(exc))
            await self.push_error(chat_id, f"Outreach draft failed: {exc}")
            await self.broadcast_outreach()
            return

        await self.go_online_for(chat_id)

        if not self.config["outreach"].get("auto_send"):
            row = await self.db.record_message(chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False)
            await self.db.update_outreach(
                outreach_id, status=OUT_DRAFTED, message=text, draft_id=row["id"] if row else None,
            )
            if row is not None:
                await self.push_message(row)
            log.info("[%s] Outreach draft for %s awaiting approval.", self.session_id, item["display_name"])
            self.schedule_go_offline(chat_id)
            await self.broadcast_outreach()
            return

        try:
            await self.send_as_me(chat_id, text, typing=True)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            await self.db.update_outreach(outreach_id, status=OUT_FAILED, error=detail)
            if not await self.handle_send_failure(chat_id, exc):
                await self.push_error(chat_id, f"Could not send outreach message: {detail}")
        else:
            await self.db.update_outreach(outreach_id, status=OUT_SENT, message=text, mark_sent=True)
            log.info("[%s] Outreach message sent to %s.", self.session_id, item["display_name"])
        self.schedule_go_offline(chat_id)
        await self.broadcast_outreach()

    async def settle_outreach_draft(self, draft_id: int, status: str, text: Optional[str] = None) -> None:
        item = await self.db.outreach_for_draft(draft_id)
        if item is None or item["status"] != OUT_DRAFTED:
            return
        await self.db.update_outreach(item["id"], status=status, message=text, mark_sent=(status == OUT_SENT))
        await self.broadcast_outreach()

    async def broadcast_outreach(self) -> None:
        await self.hub.broadcast({"type": "outreach", "items": await self.db.list_outreach()})

    async def list_contacts(self) -> list[dict[str, Any]]:
        result = await self.client(GetContactsRequest(hash=0))
        contacts = []
        for user in getattr(result, "users", []):
            if getattr(user, "deleted", False) or getattr(user, "is_self", False):
                continue
            name, username, is_bot, access_hash = describe_sender(user, user.id)
            await self.db.upsert_conversation(user.id, name, username, is_bot, access_hash)
            contacts.append({
                "chat_id": user.id, "display_name": name, "username": username, "is_bot": is_bot,
            })
        contacts.sort(key=lambda c: c["display_name"].lower())
        return contacts

    # ------------------------------------------------------------------
    # Bookings
    # ------------------------------------------------------------------

    def booking_settings(self) -> dict[str, Any]:
        return self.config.get("booking") or config_store.DEFAULTS["booking"]

    def booking_news(self, chat_id: int) -> tuple[Optional[bookings.Booking], str]:
        for booking in self.booking_store.for_chat(chat_id, (bookings.CONFIRMED, bookings.DECLINED)):
            if not booking.client_notified:
                return booking, "decision"
        for booking in self.booking_store.for_chat(chat_id, (bookings.CONFIRMED,)):
            if booking.reminder_requested_at and not booking.reminder_sent:
                return booking, "reminder"
        return None, ""

    def booking_now(self) -> datetime:
        return datetime.now(bookings.tzinfo_for(self.config["timing"].get("timezone") or "UTC"))

    async def check_reminders(self) -> None:
        settings = self.booking_settings()
        if not settings.get("enabled"):
            return
        minutes = int(settings.get("reminder_minutes_before", 0) or 0)
        for booking in self.booking_store.due_for_reminder(self.booking_now(), minutes):
            self.booking_store.update(booking, reminder_requested_at=bookings.utcnow())
            log.info("[%s] Booking #%s is %s; checking in with %s.", self.session_id, booking.id,
                     bookings.describe_until(booking, self.booking_now()), booking.client_name)
            await self.post_note(
                booking.chat_id,
                f"⏰ Booking #{booking.id} is {bookings.describe_until(booking, self.booking_now())} "
                "— asking the client whether they are still coming.",
            )
            await self.broadcast_booking(booking)
            if self.config["behavior"].get("global_pause"):
                continue
            conversation = await self.db.get_conversation(booking.chat_id)
            if conversation and conversation["automation_paused"]:
                continue
            self.schedule_draft(booking.chat_id)

    async def reminder_loop(self) -> None:
        try:
            while True:
                try:
                    await self.check_reminders()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] Reminder check failed", self.session_id)
                await asyncio.sleep(self.REMINDER_TICK_SECONDS)
        except asyncio.CancelledError:
            raise

    async def send_arrival_instructions(self, booking: bookings.Booking) -> None:
        text = (self.booking_settings().get("arrival_instructions") or "").strip()
        self.booking_store.update(booking, arrived_at=bookings.utcnow())
        if not text:
            await self.post_note(
                booking.chat_id,
                f"\U0001F6AA Booking #{booking.id}: the client has arrived, but no arrival "
                "instructions are set in Settings -> Bookings, so nothing was sent.",
            )
            await self.broadcast_booking(booking)
            return
        self.cancel_draft(booking.chat_id)
        self.sending_chats.add(booking.chat_id)
        try:
            await self.send_as_me(booking.chat_id, text, typing=True)
        except Exception as exc:
            if not await self.handle_send_failure(booking.chat_id, exc):
                await self.push_error(
                    booking.chat_id, f"Could not send the arrival instructions: {type(exc).__name__}: {exc}",
                )
            return
        finally:
            self.sending_chats.discard(booking.chat_id)
        self.booking_store.update(booking, instructions_sent_at=bookings.utcnow())
        await self.post_note(
            booking.chat_id, f"\U0001F6AA Booking #{booking.id}: the client has arrived — entry instructions sent.",
        )
        await self.broadcast_booking(booking)

    async def provider_chat_id(self) -> Optional[int]:
        value = self.booking_settings().get("provider") or ""
        if not value or self.client is None or not self.telegram_state["connected"]:
            return None
        cached_value, cached_id, resolved_at = self._provider_resolved
        now = asyncio.get_running_loop().time()
        if cached_value == value and (
            cached_id is not None or now - resolved_at < self.PROVIDER_RETRY_SECONDS
        ):
            return cached_id
        try:
            target: Any = int(value) if value.lstrip("-").isdigit() else value
            entity = await self.client.get_entity(target)
            chat_id = int(entity.id)
            name, username, is_bot, access_hash = describe_sender(entity, chat_id)
            await self.db.upsert_conversation(chat_id, name, username, is_bot, access_hash)
        except Exception as exc:
            log.warning("[%s] Cannot resolve booking provider %r: %s", self.session_id, value, type(exc).__name__)
            self._provider_resolved = (value, None, now)
            return None
        self._provider_resolved = (value, chat_id, now)
        return chat_id

    def calendar_client(self) -> Optional[google_calendar.GoogleCalendar]:
        import os

        calendar_id = self.booking_settings().get("google_calendar_id") or ""
        key_file = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
        if not key_file:
            default = self.data_dir / "google-service-account.json"
            key_file = str(default) if default.exists() else ""
        key = (calendar_id, key_file)
        if key == self._calendar_key:
            return self._calendar
        self._calendar_key, self._calendar = key, None
        if not calendar_id:
            return None
        if not key_file:
            log.warning("[%s] Google Calendar is set but GOOGLE_SERVICE_ACCOUNT_FILE is not; skipping.", self.session_id)
            return None
        try:
            self._calendar = google_calendar.GoogleCalendar(key_file, calendar_id, client=self.http_client)
        except google_calendar.CalendarError as exc:
            log.error("[%s] Google Calendar disabled: %s", self.session_id, exc)
        return self._calendar

    async def post_note(self, chat_id: int, text: str) -> None:
        row = await self.db.record_message(chat_id, DIR_SYSTEM, STATUS_NOTE, text, bump_preview=False)
        if row is not None:
            await self.push_message(row)

    async def broadcast_booking(self, booking: bookings.Booking) -> None:
        await self.hub.broadcast({"type": "booking", "booking": booking.to_dict()})

    def schedule_booking_scan(self, chat_id: int) -> None:
        self.cancel_booking_scan(chat_id)
        self.booking_scan_tasks[chat_id] = asyncio.create_task(self.booking_scan_worker(chat_id))

    def cancel_booking_scan(self, chat_id: int) -> None:
        task = self.booking_scan_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def booking_scan_worker(self, chat_id: int) -> None:
        try:
            settings = self.booking_settings()
            history = await self.db.get_history_for_ai(chat_id, limit=int(settings.get("scan_messages", 20)))
            if not history:
                return
            tz_name = self.config["timing"].get("timezone") or "UTC"

            arriving = self.booking_store.awaiting_arrival(
                chat_id, self.booking_now(), int(settings.get("reminder_minutes_before", 0) or 0)
            )
            if arriving is not None and history[-1].get("role") == "user":
                async with self.ai_gate():
                    arrived = await ai_responder.extract_arrival(
                        api_key=self.deepseek_key, history=history, ai_config=self.config["ai"], client=self.http_client,
                    )
                if arrived:
                    log.info("[%s] Booking #%s: %s says they have arrived.", self.session_id, arriving.id, arriving.client_name)
                    await self.send_arrival_instructions(arriving)
                    return
            async with self.ai_gate():
                found = await ai_responder.extract_booking(
                    api_key=self.deepseek_key, history=history, tz_name=tz_name, ai_config=self.config["ai"], client=self.http_client,
                )
            if not found:
                return
            fields = bookings.build_booking(
                found, tz_name=tz_name, default_duration=int(settings.get("default_duration_minutes", 60)),
            )
            if fields is None:
                log.info("[%s]   booking time in chat %s was unusable or in the past; ignoring.", self.session_id, chat_id)
                return
            start = datetime.fromisoformat(fields["start"])
            existing = self.booking_store.find_same_slot(chat_id, start)
            if existing is not None:
                if existing.status == bookings.PENDING and existing.provider_message_id is None:
                    await self.send_request_to_provider(existing)
                return
            await self.open_booking(chat_id, fields)
        except asyncio.CancelledError:
            raise
        except ai_responder.AIResponderError as exc:
            log.warning("[%s] Booking check failed for chat %s: %s", self.session_id, chat_id, exc)
        except Exception:
            log.exception("[%s] Booking check crashed for chat %s", self.session_id, chat_id)
        finally:
            if self.booking_scan_tasks.get(chat_id) is asyncio.current_task():
                self.booking_scan_tasks.pop(chat_id, None)

    async def open_booking(self, chat_id: int, fields: dict[str, Any]) -> bookings.Booking:
        conversation = await self.db.get_conversation(chat_id) or {}
        replaced = None
        for earlier in self.booking_store.for_chat(chat_id, (bookings.PENDING,)):
            self.booking_store.update(earlier, status=bookings.SUPERSEDED, decided_at=bookings.utcnow())
            await self.drop_calendar_event(earlier)
            await self.broadcast_booking(earlier)
            replaced = earlier
        booking = self.booking_store.add(
            chat_id=chat_id,
            client_name=conversation.get("display_name") or f"Chat {chat_id}",
            client_username=conversation.get("username"),
            replaces_id=replaced.id if replaced else None,
            **fields,
        )
        log.info("[%s] Booking #%s: %s asked for %s.", self.session_id, booking.id, booking.client_name,
                 bookings.describe_when(booking))

        await self.send_request_to_provider(booking)

        calendar = self.calendar_client()
        if calendar is not None:
            try:
                event_id = await calendar.create_event(
                    summary=f"[UNCONFIRMED] {booking.title or 'Appointment'} — {booking.client_name}",
                    description=self.calendar_description(booking),
                    start=booking.start_dt(), end=booking.end_dt(), tz_name=booking.timezone, tentative=True,
                )
                self.booking_store.update(booking, calendar_event_id=event_id)
            except google_calendar.CalendarError as exc:
                await self.push_error(chat_id, f"Google Calendar: {exc}")
            except Exception as exc:
                await self.push_error(chat_id, f"Google Calendar: {type(exc).__name__}: {exc}")

        await self.broadcast_booking(booking)
        return booking

    async def send_request_to_provider(self, booking: bookings.Booking) -> bool:
        provider = await self.provider_chat_id()
        if provider is None:
            await self.push_error(
                booking.chat_id,
                f"Booking #{booking.id} could not be sent: no provider is set in "
                "Settings -> Bookings, or the username cannot be found. It will be "
                "retried once the provider is set.",
            )
            return False
        try:
            row = await self.send_as_me(provider, bookings.format_request(booking))
        except Exception as exc:
            if not await self.handle_send_failure(provider, exc):
                await self.push_error(
                    booking.chat_id, f"Could not send booking #{booking.id} to the provider: {type(exc).__name__}: {exc}",
                )
            return False
        self.booking_store.update(booking, provider_chat_id=provider, provider_message_id=row.get("telegram_id"))
        await self.post_note(
            booking.chat_id,
            f"📅 Booking #{booking.id} requested for {bookings.describe_when(booking)} "
            "— waiting for the provider to confirm.",
        )
        await self.broadcast_booking(booking)
        return True

    async def resend_unsent_bookings(self) -> None:
        if not self.booking_settings().get("enabled"):
            return
        for booking in self.booking_store.pending():
            if booking.provider_message_id is None:
                log.info("[%s] Retrying booking #%s for the provider.", self.session_id, booking.id)
                await self.send_request_to_provider(booking)

    @staticmethod
    def calendar_description(booking: bookings.Booking) -> str:
        who = booking.client_name + (f" (@{booking.client_username})" if booking.client_username else "")
        lines = [f"Client: {who}", f"Booking #{booking.id} via Telegram"]
        if booking.notes:
            lines.append(f"Notes: {booking.notes}")
        return "\n".join(lines)

    async def drop_calendar_event(self, booking: bookings.Booking) -> None:
        calendar = self.calendar_client()
        if calendar is None or not booking.calendar_event_id:
            return
        try:
            await calendar.delete_event(booking.calendar_event_id)
        except Exception as exc:
            await self.push_error(booking.chat_id, f"Google Calendar: could not remove event: {exc}")
        else:
            self.booking_store.update(booking, calendar_event_id=None)

    async def handle_provider_reply(self, chat_id: int, text: str, reply_to: Optional[int]) -> bool:
        pending = self.booking_store.pending()
        if not pending:
            return False
        decision = bookings.parse_provider_reply(text, pending, reply_to)
        if decision is None:
            return False
        if decision.booking is None:
            try:
                await self.send_as_me(chat_id, bookings.format_help(pending))
            except Exception as exc:
                await self.handle_send_failure(chat_id, exc)
            return True
        await self.decide_booking(decision.booking, decision.confirmed, by="provider")
        try:
            await self.send_as_me(chat_id, bookings.format_acknowledgement(decision.booking, decision.confirmed))
        except Exception as exc:
            await self.handle_send_failure(chat_id, exc)
        return True

    async def decide_booking(self, booking: bookings.Booking, confirmed: bool, by: str) -> None:
        self.booking_store.update(
            booking, status=bookings.CONFIRMED if confirmed else bookings.DECLINED,
            decided_at=bookings.utcnow(), decided_by=by,
        )
        when = bookings.describe_when(booking)
        log.info("[%s] Booking #%s %s by %s.", self.session_id, booking.id, booking.status, by)

        calendar = self.calendar_client()
        if confirmed and calendar is not None and booking.calendar_event_id:
            try:
                await calendar.confirm_event(
                    booking.calendar_event_id, f"{booking.title or 'Appointment'} — {booking.client_name}",
                )
            except Exception as exc:
                await self.push_error(booking.chat_id, f"Google Calendar: could not confirm event: {exc}")
        elif not confirmed:
            await self.drop_calendar_event(booking)

        mark = "✅" if confirmed else "❌"
        await self.post_note(
            booking.chat_id,
            f"{mark} Booking #{booking.id} for {when} {'confirmed' if confirmed else 'declined'} by the {by}.",
        )
        await self.broadcast_booking(booking)

        if self.config["behavior"].get("global_pause"):
            log.info("[%s]   automation is paused; the client will be told when a draft next runs.", self.session_id)
            return
        conversation = await self.db.get_conversation(booking.chat_id)
        if conversation and conversation["automation_paused"]:
            log.info("[%s]   chat %s is paused; tell the client by hand.", self.session_id, booking.chat_id)
            return
        self.schedule_draft(booking.chat_id)

    # ------------------------------------------------------------------
    # Telethon handlers
    # ------------------------------------------------------------------

    async def on_incoming(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return

        chat_id = event.chat_id
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        name, username, is_bot, access_hash = describe_sender(sender, chat_id)
        conversation = await self.db.upsert_conversation(chat_id, name, username, is_bot, access_hash)
        await self.detect_links(conversation)

        text = (event.raw_text or "").strip()
        has_text = bool(text)
        stored_text = text if has_text else "[non-text message]"

        if self.config["behavior"].get("log_all_messages", True):
            row = await self.db.record_message(
                chat_id, DIR_IN, STATUS_RECEIVED, stored_text, telegram_id=event.message.id, mark_unread=True,
            )
        else:
            row = {
                "id": None, "chat_id": chat_id, "telegram_id": event.message.id, "direction": DIR_IN,
                "status": STATUS_RECEIVED, "text": stored_text,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }

        if row is not None:
            await self.push_message(row)

        log.info("[%s] DM from %s%s (chat %s): %s", self.session_id, name, " [bot]" if is_bot else "", chat_id,
                 f"{len(text)} chars" if has_text else "non-text message")

        is_provider = (
            has_text and self.booking_settings().get("enabled") and chat_id == await self.provider_chat_id()
        )
        if is_provider:
            if await self.handle_provider_reply(chat_id, text, event.message.reply_to_msg_id):
                log.info("[%s]   booking decision from the provider — handled.", self.session_id)
                return
            log.info("[%s]   message from the booking provider; replying as usual.", self.session_id)

        if not has_text:
            log.info("[%s]   no text to reply to — skipping.", self.session_id)
            return
        if self.config["behavior"].get("global_pause"):
            log.info("[%s]   automation is globally paused — skipping.", self.session_id)
            return

        if self.booking_settings().get("enabled") and not is_provider:
            self.schedule_booking_scan(chat_id)

        conversation = await self.db.get_conversation(chat_id)
        if conversation and conversation["automation_paused"]:
            log.info("[%s]   this conversation is paused — skipping.", self.session_id)
            return
        if not self.within_active_hours(self.config["timing"]):
            timing = self.config["timing"]
            log.info("[%s]   outside active hours (%s-%s %s) — skipping.", self.session_id,
                     timing.get("active_hours_start"), timing.get("active_hours_end"), timing.get("timezone"))
            return

        self.schedule_draft(chat_id)

    async def on_outgoing(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return

        text = (event.raw_text or "").strip()
        chat_id = event.chat_id
        if text and text in (self.in_flight_sends.get(chat_id) or []):
            return
        if not text and self.in_flight_media.get(chat_id):
            return
        if not self.config["behavior"].get("log_all_messages", True):
            return

        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        name, username, is_bot, access_hash = describe_sender(chat, chat_id)
        await self.db.upsert_conversation(chat_id, name, username, is_bot, access_hash)

        row = await self.db.record_message(
            chat_id, DIR_OUT, STATUS_SENT, text or "[non-text message]", telegram_id=event.message.id,
        )
        if row is not None:
            await self.push_message(row)

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    async def _start_telegram(self, auth: dict[str, Any], proxy_url: Optional[str]) -> None:
        flood_sleep_threshold = int(self.config["safety"].get("max_flood_wait_seconds", 300))
        identity = await self._resolve_identity()
        self.client = _client_from_auth(auth, proxy_url, flood_sleep_threshold, identity)
        self.client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        self.telegram_task = asyncio.create_task(self._run_telegram())
        self.reminder_task = asyncio.create_task(self.reminder_loop())

    async def _resolve_identity(self) -> dict[str, Any]:
        """This session's device identity, assigned once and then never
        changed. A blank device_model means it has not been assigned yet, so
        derive a deterministic one and persist it — from then on the stored
        value wins, even if device_profiles.py's list later changes."""
        identity = dict(self.config.get("identity") or {})
        if identity.get("device_model"):
            return identity
        identity = device_profiles.derive(self.session_id)
        await self.save_config({**self.config, "identity": identity})
        log.info(
            "[%s] Assigned device identity: %s / %s / %s",
            self.session_id, identity["device_model"],
            identity["system_version"], identity["app_version"],
        )
        return identity

    async def _stop_telegram(self) -> None:
        self.presence_online = False
        task, self.telegram_task = self.telegram_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        ticker, self.reminder_task = self.reminder_task, None
        if ticker is not None and not ticker.done():
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
        for chat_id in list(self.draft_tasks):
            self.cancel_draft(chat_id)
        for chat_id in list(self.booking_scan_tasks):
            self.cancel_booking_scan(chat_id)
        if self.offline_timer is not None and not self.offline_timer.done():
            self.offline_timer.cancel()
        old, self.client = self.client, None
        if old is not None and old.is_connected():
            with suppress(Exception):
                await old.disconnect()
        self.telegram_state["connected"] = False
        self.telegram_state["error"] = None
        self.me_info.clear()

    async def _run_telegram(self) -> None:
        """Keep the userbot connected; reconnect on failure without killing the process."""
        backoff = 5
        while True:
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    notice = (
                        "The saved Telegram session is no longer valid — it was "
                        "probably ended from Settings -> Devices. Sign in again."
                    )
                    log.error("[%s] %s", self.session_id, notice)
                    await self.registry.set_state(self.session_id, "needs_login", notice)
                    await self.registry.clear_auth(self.session_id)
                    return

                me = await self.client.get_me()
                self.me_info = {
                    "id": getattr(me, "id", None),
                    "name": describe_sender(me, getattr(me, "id", 0))[0],
                    "username": getattr(me, "username", None),
                }
                self.telegram_state["connected"] = True
                self.telegram_state["error"] = None
                backoff = 5
                if self.config["presence"].get("enabled", True):
                    await self.set_presence(False)
                log.info(
                    "[%s] Telegram connected as %s. Listening for private messages.",
                    self.session_id, self.me_info["name"],
                )
                if self.config["behavior"].get("global_pause"):
                    last = ""
                    with suppress(OSError):
                        last = (self.data_dir / "last_halt.txt").read_text(encoding="utf-8").strip()
                    log.warning(
                        "[%s] Automation is GLOBALLY PAUSED — incoming messages will NOT be "
                        "answered. Resume from the panel.%s",
                        self.session_id, f" Last automatic halt: {last}" if last else "",
                    )
                await self.hub.broadcast({"type": "status", "status": self.status()})
                await self.registry.set_state(self.session_id, "running", "")

                await self.client.run_until_disconnected()
                self.telegram_state["connected"] = False
                log.warning("[%s] Telegram disconnected; reconnecting…", self.session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.telegram_state["connected"] = False
                self.telegram_state["error"] = f"{type(exc).__name__}: {exc}"
                log.error("[%s] Telegram client error: %s", self.session_id, self.telegram_state["error"])
                await self.hub.broadcast({"type": "status", "status": self.status()})

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "telegram_connected": self.telegram_state["connected"],
            "telegram_error": self.telegram_state["error"],
            "me": self.me_info,
            "global_pause": self.config["behavior"].get("global_pause", False),
            "auto_send": self.config["behavior"].get("auto_send", False),
            "persona_configured": any(
                (self.config["persona"].get(k) or "").strip() for k in config_store.DEFAULTS["persona"]
            ),
        }


if __name__ == "__main__":
    """Manual single-session run for testing, standing in for manager.py
    until the Master Process Manager exists. Requires:
      DATABASE_URL   postgres DSN
      SESSION_ID     an existing, already-migrated telegram_sessions row
                     with auth_key + api credentials + deepseek key set
      DATA_DIR       (optional) base dir for per-session media/bookings
      REDIS_URL      (optional, default redis://localhost:6379/0) command
                     bus / event fan-out — see commands.py
    Does not touch Telegram unless the session already completed login —
    see NeedsLogin above.
    """
    import os
    import sys

    import pg

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    async def _main() -> None:
        dsn = os.environ["DATABASE_URL"]
        session_id = os.environ["SESSION_ID"]
        data_dir = Path(os.environ.get("DATA_DIR") or ".")
        redis_url = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"

        pool = await pg.create_pool(dsn)
        await pg.assert_version(pool, pg.latest_version())

        runtime = SessionRuntime(pool, session_id, data_dir=data_dir, redis_url=redis_url)
        if await runtime.needs_login():
            print(
                f"Session {session_id!r} has no usable auth yet. The login flow "
                "(phone/code/2FA -> SessionRegistry.save_login) is not part of "
                "session_runtime.py — run that first.",
                file=sys.stderr,
            )
            await pool.close()
            sys.exit(1)

        await runtime.start()
        try:
            await asyncio.Event().wait()  # run until Ctrl+C
        finally:
            await runtime.stop()
            await pool.close()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nShutting down.")
