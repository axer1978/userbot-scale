"""Per-session runtime: the business logic for one account, ported from the
original single-account `main.py` to run against the fleet's shared
Postgres pool. Everything that talks to the network itself (connecting,
sending, typing, read receipts, presence) sits behind `self.transport`
(transport.py; telegram_transport.py for Telegram).

One `SessionRuntime` instance == one Telegram account == one row in
`telegram_sessions`, owned by exactly one tenant. `manager.py`'s workers
construct one per leased session and call `start()` / `stop()` on each; for
a single manual test, construct one directly (see `__main__` below) and it
manages its own lease. Nothing here is module-level global state, so N
runtimes can coexist in one process.

`start()` assumes the account has already signed in (login_flow.py); call
`needs_login()` to check first. It then binds to the tenant
(`bind_tenant()`): the tenant's effective config (tenant_config.py) and
rendered prompt (prompt_layers.py) decide how replies are timed and
written, policy.py checks every AI-written reply before an automatic send,
every send writes an audit row (audit.py) and every LLM call is metered
(llm_usage.py). The account's own state (pause switch, device identity,
per-contact overrides) stays in session_config via config_store.py.

Bookings live in Postgres (booking_store.py); booking_flow.py runs them
for this account: the customer's requests, the owner's typed answers,
reminders, the waitlist and arrival. The scheduler (scheduler.py) sends
`scheduler_tick` about once a minute; replies that quiet hours hold back
are written to deferred_replies and picked up by that tick, so a restart
overnight loses nothing. AI usage and reply limits (ai_limits.py) are
checked before every reply.

Safety (phase 3): the kill switches live in controls.py. While the
tenant is soft-off (`paused()`), nothing is sent on its own; every send by
the bot is checked against the switches and the send-volume anomaly right
before it goes out, so a switch thrown elsewhere takes effect at once. A
customer message with an escalation keyword pauses that chat and pings the
owner; a message written by hand in a chat (on the phone, or from the
panel) makes the bot keep quiet there for `takeover_hours`. The account
reports its health (health.py) and checks its Telegram logins for new ones
(anomaly.py).

Phase 4: every customer message the bot ends up not answering (a reply
limit, a failed model call, soft-off, a paused chat, an escalation, a
policy hold, staging) or answering with one of the tenant's fallback
phrases goes into the unanswered queue (unanswered.py), decided here in
code. Staging mode (`staging.*`) answers only the listed test chats.

`media.MediaLibrary` is still files, kept per tenant under
DATA_DIR/tenants/<tenant id> (tenants.tenant_data_dir).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import socket
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg
import httpx

import ai_limits
import ai_responder
import alerts
import anomaly
import audit
import booking_flow
import booking_store
import bookings
import commands
import config_store
import context_link
import controls
import health
import humanlike
import leasing
import llm_usage
import media
import pg
import policy
import scheduler
import tenant_config
import tenants
import unanswered
import vision
from transport import (
    PEER_FLOOD,
    RATE_LIMITED,
    SESSION_REJECTED,
    TELEGRAM,
    TELEGRAM_SERVICE_ID,  # noqa: F401  (re-exported; tests use session_runtime.TELEGRAM_SERVICE_ID)
    UNREACHABLE,
    Inbound,
    NeedsLogin,
    Transport,
    make_transport,
)
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

# reply_skip_reason's answer for a bare "ok" / "thanks". It is skipped like
# the others but not queued as unanswered: it needed no answer.
ACK_SKIP = "the message is only an acknowledgement"
# In staging, a chat that is not a test chat gets this note at most once an
# hour, so a busy chat's history isn't all notes.
STAGING_NOTE = "Staging: not answered — not a test chat."
STAGING_NOTE_SECONDS = 3600
# The database dropping out for a moment (a restart, a failover):
# storing an incoming message is tried this many times, waiting
# STORE_RETRY_SECONDS × the attempt number in between, so the message is
# not lost...
STORE_ATTEMPTS = 4
STORE_RETRY_SECONDS = 2.0
# ...and a reply whose drafting hit it (before anything was sent) is started
# again up to this many times, DRAFT_RETRY_SECONDS × the attempt later.
DRAFT_DB_RETRIES = 3
DRAFT_RETRY_SECONDS = 10.0


class SendBlocked(Exception):
    """A send was deliberately not attempted, with a reason worth showing."""


def _ov_int(overrides: dict[str, Any], key: str, fallback: int) -> int:
    value = overrides.get(key)
    return value if isinstance(value, int) else fallback


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
    """Runs one account: drafting, sends, safety limits, outreach, bookings,
    media, presence, over its transport. Construct one per leased
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

        # The tenant's files (media library) live under
        # DATA_DIR/tenants/<tenant id> (tenants.tenant_data_dir). Until
        # bind_tenant() knows the tenant, data_dir, media_library and
        # booking_store are not set.
        self.data_root = data_dir

        self.hub = Hub(self)
        self.bus: Optional[commands.CommandBus] = None
        self._command_stop_event = asyncio.Event()
        self._command_serve_task: Optional[asyncio.Task] = None
        self.tenants = tenants.TenantStore(pool)
        self.tenant_id: Optional[int] = None
        self.bundle: Optional[tenants.Bundle] = None
        # Two kinds of settings. `config` is the tenant's effective config
        # (tenant_config.py: platform <- industry <- client), everything
        # about how the bot behaves. `account` is this Telegram account's own
        # state from session_config (config_store.py): the pause switch,
        # the device identity and per-contact style overrides.
        self.config: dict[str, Any] = tenant_config.TenantConfig().model_dump(mode="json")
        self.account: dict[str, Any] = config_store.normalize({})
        self._bound_at = 0.0
        self.REBIND_SECONDS = 300
        self.api_id: Optional[int] = None
        self.api_hash: Optional[str] = None
        self.deepseek_key: Optional[str] = None

        self.http_client: Optional[httpx.AsyncClient] = None
        # The network this account runs on (transport.py): every call that
        # talks to it goes through here.
        self.transport: Transport = make_transport(TELEGRAM, self)
        self.reminder_task: Optional[asyncio.Task] = None

        self.draft_tasks: dict[int, asyncio.Task] = {}
        self.outreach_task: Optional[asyncio.Task] = None
        self.in_flight_sends: dict[int, list[str]] = {}
        self.in_flight_media: dict[int, int] = {}
        # Counts every attempt to hand a message to Telegram. A draft that
        # failed before this moved may be retried; one after it never is
        # (the message may have gone out).
        self.delivery_attempts = 0
        # Background tasks nobody else holds on to (a delayed stop).
        self._background: set[asyncio.Task] = set()

        self.presence_online = False
        self.offline_timer: Optional[asyncio.Task] = None
        self.active_chats: set[int] = set()
        self.sending_chats: set[int] = set()

        self.flow = booking_flow.BookingFlow(self)
        self.REMINDER_TICK_SECONDS = 60
        # The last AI limit reported, so the panel hears about it once.
        self._limit_reported = ""
        # Why this tenant is soft-off (controls.py), "" while it may send.
        self.off_reason = ""
        # Set once this runtime is done for good (stopped, fenced, logged
        # out, hard-off); the worker then drops it (manager.py).
        self.finished = False
        self.LOGINS_CHECK_SECONDS = 300
        self._logins_checked_at = 0.0
        # chat id -> when (monotonic) the staging note was last posted there.
        self._staging_noted: dict[int, float] = {}

        self._ai_gate: Optional[asyncio.Semaphore] = None
        self._ai_gate_size = 0

        self._lease_keeper: Optional[leasing.LeaseKeeper] = None
        self._lease_keeper_task: Optional[asyncio.Task] = None
        # The epoch of the lease this runtime holds (leasing.Lease.epoch);
        # 0 until start() has taken it.
        self.lease_epoch = 0
        self._stopping = False

    # The Telegram transport's state under the names it had before the
    # transport split (tests and older call sites use them).

    @property
    def client(self) -> Any:
        return getattr(self.transport, "client", None)

    @client.setter
    def client(self, value: Any) -> None:
        self.transport.client = value

    @property
    def telegram_state(self) -> dict[str, Any]:
        return self.transport.state

    @property
    def me_info(self) -> dict[str, Any]:
        return self.transport.me

    @me_info.setter
    def me_info(self, value: dict[str, Any]) -> None:
        self.transport.me_info = value

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
        self.lease_epoch = lease.epoch
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
            await self.bind_tenant()

            await self.transport.prepare()
            self.deepseek_key = await self.registry.load_deepseek_key(self.session_id)
            if not self.deepseek_key:
                raise NeedsLogin(
                    f"session {self.session_id!r} has no DeepSeek key set "
                    "(SessionRegistry.set_deepseek_key)."
                )

            await self._start_transport()
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
        if self._stopping:
            return
        self._stopping = True
        self.finished = True
        # Every step runs even when an earlier one fails (Postgres or Redis
        # down while stopping): a half-stopped runtime whose command server
        # still answered would act for an account it no longer runs.
        failed: list[str] = []

        async def step(name: str, coro) -> None:
            try:
                await coro
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
            except Exception:
                failed.append(name)
                log.exception("[%s] Stopping: %s failed", self.session_id, name)

        await step("telegram", self._stop_transport())
        if self._lease_keeper is not None:
            await step("lease keeper", self._lease_keeper.stop())
        if self._lease_keeper_task is not None:
            await step("lease keeper task", asyncio.wait_for(self._lease_keeper_task, timeout=5))
        # Not released (Postgres down): it simply expires within LEASE_SECONDS.
        await step("lease release", leasing.release(self.pool, self.session_id, self.worker_id))
        if self.http_client is not None:
            await step("http client", self.http_client.aclose())
        self._command_stop_event.set()
        if self._command_serve_task is not None:
            await step("command server", asyncio.wait_for(self._command_serve_task, timeout=5))
            if not self._command_serve_task.done():
                self._command_serve_task.cancel()
        if self.bus is not None:
            await step("command bus", self.bus.close())
        await step("database", self.db.close())
        if failed:
            log.warning("[%s] Stopped, but these steps failed: %s", self.session_id, ", ".join(failed))

    async def handle_command(self, action: str, args: dict[str, Any]) -> Any:
        """Executed when this session's owning worker receives a command over
        the Redis bus (commands.py) — the panel asking for something that
        needs the live Telethon client. Anything that only needs Postgres or
        local files, the panel does directly and this is never reached."""
        if action == "send":
            row = await self.send_as_me(
                args["chat_id"], args["text"], actor=audit.ADMIN, reason="sent by hand from the panel",
            )
            await self.start_takeover(args["chat_id"], how="A message was sent by hand from the panel",
                                      actor=audit.ADMIN)
            return row

        if action == "send_media":
            row = await self.send_media_as_me(
                args["chat_id"], args["media_id"], actor=audit.ADMIN, reason="sent by hand from the panel",
            )
            await self.start_takeover(args["chat_id"], how="A file was sent by hand from the panel",
                                      actor=audit.ADMIN)
            return row

        if action == "list_contacts":
            return await self.list_contacts()

        if action == "cancel_draft":
            self.cancel_draft(args["chat_id"])
            await self.clear_deferred(args["chat_id"])
            return {"ok": True}

        if action == "cancel_all_drafts":
            for chat_id in list(self.draft_tasks):
                self.cancel_draft(chat_id)
            await scheduler.clear_all_deferred(self.pool, self.tenant_id)
            return {"ok": True}

        if action == "scheduler_tick":
            return await self.scheduler_tick()

        if action == "reload_controls":
            return {"off": await self.refresh_controls()}

        if action == "owner_notice":
            # A platform notice to the owner (billing.py), from this account.
            row = await self.flow.send_owner(str(args["text"]), reason=str(args.get("reason") or "notice to the owner"))
            return {"sent": row is not None, "error": "" if row is not None else (
                f"sending is off ({self.off_reason})" if self.off_reason else
                "the owner could not be reached (booking.provider)")}

        if action == "reconnect":
            # The proxy (or the stored login) changed in the panel: connect
            # again with what is stored now.
            return await self.reconnect()

        if action == "hard_off":
            return await self.hard_off(str(args.get("reason") or ""))

        if action == "reload_config":
            # The panel wrote the new config straight to Postgres (it holds
            # no runtime to go through); without this the worker's in-memory
            # copy would stay stale until the next periodic rebind.
            await self.bind_tenant()
            return {"ok": True}

        if action == "resend_unsent_bookings":
            for booking in await self.booking_store.unsent():
                await self.flow.submit(booking)
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
            parts = ai_responder.split_burst(text, self.config["burst"]["max_messages"])
            if not parts and not attachments:
                raise ValueError("Message is empty")
            sent = await self.send_burst(
                draft["chat_id"], parts, draft_id=draft_id, attachments=attachments,
                actor=audit.ADMIN, reason="draft approved in the panel",
            )
            await self.settle_outreach_draft(draft_id, OUT_SENT, text=text)
            return sent

        if action == "booking_scan":
            chat_id = args["chat_id"]
            if await self.db.get_conversation(chat_id) is None:
                raise ValueError("Unknown conversation")
            if not self.flow.enabled():
                raise ValueError("Bookings are turned off (booking.enabled)")
            if chat_id == await self.flow.provider_chat_id():
                raise ValueError("That chat is the booking owner")
            self.flow.cancel_scan(chat_id)
            await self.flow.scan(chat_id)
            return {"bookings": [booking_store.public(b) for b in await self.booking_store.for_chat(
                chat_id, booking_flow.bs.STATES, include_past=True)]}

        if action == "booking_action":
            return await self.flow.admin_action(int(args["booking_id"]), str(args["action"]), args)

        if action == "booking_customer_action":
            # From the customer's booking page (public_app.py).
            booking = await self.booking_store.get(int(args["booking_id"]))
            action_name = str(args["action"])
            if action_name not in ("cancel", "confirm_attendance"):
                raise ValueError("Unknown customer action")
            updated = await self.flow.customer_action(booking, action_name, via=booking_flow.VIA_PAGE)
            return booking_store.public(updated) if updated else {}

        raise ValueError(f"Unknown command action: {action!r}")

    async def scheduler_tick(self) -> dict[str, Any]:
        """The timed work (see scheduler.py). Each part is guarded on its
        own: one that fails (a database blip, one bad booking) is logged and
        retried next minute, and does not keep the others from running.
        Sending still re-checks the switches at the last step, so a failed
        re-read of them here never lets anything out that shouldn't go."""
        errors: list[str] = []
        steps: list[tuple[str, Any]] = [
            ("controls", self.refresh_controls),
            ("spend cap", self.release_spend_cap),
            ("volume", self.check_volume),
        ]
        if time.monotonic() - self._logins_checked_at >= self.LOGINS_CHECK_SECONDS:
            steps.append(("logins", self.check_logins))
        steps += [("deferred replies", self.run_deferred), ("bookings", self.flow.tick)]
        for name, step in steps:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("[%s] Timed work (%s) failed; retried next tick", self.session_id, name)
                errors.append(f"{name}: {type(exc).__name__}")
        return {"ok": not errors, "off": self.off_reason, **({"errors": errors} if errors else {})}

    async def _on_lease_lost(self, session_id: str) -> None:
        """The renewal loop confirmed (or fears) another worker now owns this
        session, or the account was deactivated. Disconnect immediately —
        continuing would risk exactly the two-workers-mutating-one-session
        AUTH_KEY_UNREGISTERED failure the leasing module exists to prevent."""
        log.error(
            "Lease lost for session %s; disconnecting to avoid a double-run.", session_id
        )
        self.finished = True
        await self._stop_transport()

    # ------------------------------------------------------------------
    # Config save helper (was module-global reassignment; now returns the
    # fresh value and stores it on self, same call-site shape as before)
    # ------------------------------------------------------------------

    async def save_account(
        self, payload: dict[str, Any], *, expected_revision: Optional[int] = None
    ) -> dict[str, Any]:
        """Write this account's own state (session_config): the pause
        switch, device identity, per-contact overrides."""
        self.account = await config_store.save(
            self.pool, self.session_id, payload, expected_revision=expected_revision
        )
        return self.account

    async def bind_tenant(self) -> None:
        """Load, or reload, everything tenant-specific: the effective config,
        the rendered prompt and this account's own state. The first call also
        settles where the tenant's files live.

        A stored config that no longer validates (someone edited the
        database by hand) keeps the last good one rather than stopping the
        account, and says so."""
        try:
            bundle = await self.tenants.bundle_for_session(self.session_id)
        except (tenant_config.ConfigError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # (Hand-edited JSON of the wrong shape surfaces as the latter.)
            if self.bundle is None:
                raise
            log.error("[%s] Tenant config no longer valid; keeping the last good one: %s", self.session_id, exc)
            await self.push_error(None, f"Tenant config is invalid, still using the previous one: {exc}")
            return
        first = self.tenant_id is None
        self.bundle, self.tenant_id = bundle, bundle.tenant["id"]
        self.config = bundle.config
        self.account = await config_store.load(self.pool, self.session_id)
        if first:
            self.data_dir = tenants.tenant_data_dir(self.data_root, self.tenant_id, self.session_id)
            self.media_library = media.MediaLibrary(self.data_dir / "media")
            self.booking_store = booking_store.BookingStore(self.pool, self.tenant_id, self.session_id)
            imported = await self.booking_store.import_legacy_file(self.data_dir / "bookings.json")
            if imported:
                log.info("[%s] Imported %s bookings from bookings.json.", self.session_id, imported)
            self.off_reason = await controls.off_reason(self.pool, self.tenant_id)
        self._bound_at = time.monotonic()

    def utcnow(self) -> datetime:
        """The clock everything booking- and schedule-related reads (tests
        replace it)."""
        return datetime.now(timezone.utc)

    async def ai_limit_reason(self) -> str:
        """Why this client may not use the AI right now, or "". Reported to
        the panel and the audit log once each time a limit is first hit."""
        # The real clock, not utcnow(): usage rows carry the database's time.
        now_local = datetime.now(bookings.tzinfo_for(self.config["timezone"]))
        reason = await ai_limits.limit_reached(self.pool, self.tenant_id, self.config, now_local)
        if reason and reason != self._limit_reported:
            await self.push_error(None, f"Sending is off for this client: {reason}. Messages are still received.")
            await self.write_audit(audit.AI_LIMIT_REACHED, actor=audit.SYSTEM, reason=reason)
        self._limit_reported = reason
        if reason and await controls.add_hold(self.pool, self.tenant_id, controls.SPEND_CAP, reason,
                                              actor=audit.SYSTEM):
            # Soft-off until the period rolls over or the limit is raised
            # (release_spend_cap, on the scheduler's tick).
            await alerts.raise_alert(
                self.pool, tenant_id=self.tenant_id, kind="spend_cap", severity=alerts.WARNING,
                message=f"{reason}. The client is soft-off until the period rolls over or the limit is raised.",
            )
            await self.refresh_controls()
        return reason

    async def release_spend_cap(self) -> None:
        """Lift the spend_cap hold once no AI limit is reached any more."""
        if "AI limit" not in self.off_reason:
            return
        now_local = datetime.now(bookings.tzinfo_for(self.config["timezone"]))
        if await ai_limits.limit_reached(self.pool, self.tenant_id, self.config, now_local):
            return
        if await controls.remove_hold(self.pool, self.tenant_id, controls.SPEND_CAP, actor=audit.SYSTEM,
                                      reason="the AI limit is no longer reached"):
            self._limit_reported = ""
            await alerts.resolve(self.pool, tenant_id=self.tenant_id, kind="spend_cap", by="system: limit cleared")
            await self.refresh_controls()

    # ------------------------------------------------------------------
    # Kill switches (controls.py) and anomalies (anomaly.py)
    # ------------------------------------------------------------------

    async def refresh_controls(self) -> str:
        """Re-read the switches. Entering soft-off drops what was waiting to
        go out, so resuming replays nothing."""
        reason = await controls.off_reason(self.pool, self.tenant_id)
        was, self.off_reason = self.off_reason, reason
        if reason and not was:
            log.warning("[%s] Soft-off: %s", self.session_id, reason)
            # Not the task asking (a draft that just tripped an anomaly
            # still has to store itself as held).
            current = asyncio.current_task()
            dropped: set[int] = set()
            for chat_id, task in list(self.draft_tasks.items()):
                if task is not current:
                    if not task.done() and chat_id not in self.sending_chats:
                        dropped.add(chat_id)
                    self.cancel_draft(chat_id)
            dropped.update(await scheduler.clear_all_deferred(self.pool, self.tenant_id))
            # Resuming replays nothing, so these customers are not answered.
            for chat_id in sorted(dropped):
                await self.queue_unanswered(chat_id, unanswered.SOFT_OFF, reason)
        elif was and not reason:
            log.info("[%s] Resumed (was: %s).", self.session_id, was)
            if self.config["outreach"]["enabled"]:
                self.ensure_outreach_worker()
        if reason != was:
            await self.hub.broadcast({"type": "controls", "off_reason": reason})
            await self.hub.broadcast({"type": "status", "status": self.status()})
        return reason

    async def ensure_may_send(self, actor: str) -> None:
        """Right before anything goes out: the kill switches and the
        send-volume anomaly. A person sending by hand from the panel is
        not stopped by them."""
        if actor == audit.ADMIN:
            return
        reason = await self.refresh_controls()
        if reason:
            raise SendBlocked(f"Sending is off for this client ({reason}).")
        spike = await self.check_volume()
        if spike:
            raise SendBlocked(f"Sending is off for this client: {spike}.")

    async def suspend_for_anomaly(self, trigger: str, detail: str, payload: Optional[dict[str, Any]] = None) -> None:
        """Soft-off with an alert. Every trigger is audited, even when the
        client is already off for an earlier one."""
        await self.write_audit(audit.ANOMALY_DETECTED, actor=audit.SYSTEM, reason=detail,
                               payload={"trigger": trigger, **(payload or {})})
        added = await controls.add_hold(self.pool, self.tenant_id, controls.ANOMALY, detail, actor=audit.SYSTEM)
        await alerts.raise_alert(
            self.pool, tenant_id=self.tenant_id, kind=f"anomaly:{trigger}", severity=alerts.CRITICAL,
            message=f"{detail}. " + ("The client is soft-off until you resume it." if added else
                                     "The client was already soft-off for an anomaly."),
            payload={"trigger": trigger, **(payload or {})},
        )
        await self.push_error(None, f"Automatic soft-off: {detail}.")
        await self.refresh_controls()

    async def check_volume(self) -> str:
        spike = await anomaly.volume_spike(self.pool, self.tenant_id, self.config["anomaly"])
        if spike and "anomaly" not in self.off_reason:
            await self.suspend_for_anomaly(anomaly.VOLUME, spike)
        return spike

    async def check_logins(self) -> list[dict[str, Any]]:
        """Compare the account's Telegram logins with the last check. A new
        one is an anomaly (anomaly.new_login_suspend)."""
        self._logins_checked_at = time.monotonic()
        current = await self.transport.list_logins()
        if current is None:
            return []
        known =await health.known_logins(self.pool, self.tenant_id)
        fresh = anomaly.new_logins(known, current)
        await health.save_logins(self.pool, self.tenant_id, self.session_id, current)
        if fresh and self.config["anomaly"]["new_login_suspend"]:
            described = "; ".join(anomaly.describe_login(f) for f in fresh)
            await self.suspend_for_anomaly(
                anomaly.NEW_LOGIN, f"A new Telegram login appeared on the account ({described})",
                {"logins": fresh},
            )
        elif fresh:
            await alerts.raise_alert(
                self.pool, tenant_id=self.tenant_id, kind="anomaly:new_login", severity=alerts.WARNING,
                message="A new Telegram login appeared on the account: "
                        + "; ".join(anomaly.describe_login(f) for f in fresh),
                payload={"logins": fresh},
            )
        return fresh

    async def hard_off(self, reason: str) -> dict[str, Any]:
        """Log this account's session out of Telegram and stop. The caller
        (controls.hard_off) deletes the key and deactivates the account."""
        logged_out = False
        try:
            logged_out = await self.transport.log_out()
        except Exception as exc:
            log.error("[%s] Log out failed: %s", self.session_id, type(exc).__name__)
        log.error("[%s] HARD-OFF (%s); logged out: %s", self.session_id, reason, logged_out)
        self.finished = True
        # Stop after this command has answered; stopping closes the bus.
        asyncio.get_running_loop().call_later(1.0, lambda: self.spawn(self.stop(), "stop after hard-off"))
        return {"logged_out": logged_out}

    def spawn(self, coro: Any, what: str) -> asyncio.Task:
        """A background task that is referenced until it ends and whose
        failure is logged, not lost."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)

        def done(t: asyncio.Task) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("[%s] %s failed: %r", self.session_id, what, t.exception())

        task.add_done_callback(done)
        return task

    # ------------------------------------------------------------------
    # Per chat: escalation and human takeover
    # ------------------------------------------------------------------

    def takeover_until(self, conversation: Optional[dict[str, Any]]) -> Optional[datetime]:
        raw = (conversation or {}).get("human_takeover_until")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until if until > self.utcnow() else None

    def silenced(self, conversation: Optional[dict[str, Any]]) -> str:
        """Why the bot must not act in this chat, or ""."""
        if conversation is None:
            return ""
        if conversation.get("automation_paused"):
            return conversation.get("paused_reason") or "paused in the panel"
        until = self.takeover_until(conversation)
        if until is not None:
            local = until.astimezone(bookings.tzinfo_for(self.config["timezone"]))
            return f"a person is handling this chat until {local:%d.%m %H:%M}"
        return ""

    async def start_takeover(self, chat_id: int, *, how: str, actor: str) -> None:
        """Someone wrote in this chat by hand: the bot keeps quiet here for
        takeover_hours, then carries on by itself."""
        hours = float(self.config.get("takeover_hours") or 0)
        if hours <= 0 or self.transport.is_service_chat(chat_id) or chat_id == self.me_info.get("id"):
            return
        conversation = await self.db.get_conversation(chat_id)
        if conversation is None:
            return
        already = self.takeover_until(conversation) is not None
        until = self.utcnow() + timedelta(hours=hours)
        conversation = await self.db.set_takeover(chat_id, until)
        self.cancel_draft(chat_id)
        self.flow.cancel_scan(chat_id)
        await self.clear_deferred(chat_id)
        local = until.astimezone(bookings.tzinfo_for(self.config["timezone"]))
        if not already:
            await self.post_note(chat_id, f"✋ {how}, so the bot stays quiet in this chat until {local:%d.%m %H:%M}.")
            await self.write_audit(audit.HUMAN_TAKEOVER, actor=actor, reason=how,
                                   payload={"chat_id": chat_id, "until": until.isoformat(timespec="seconds")})
        await self.hub.broadcast({"type": "conversation", "conversation": conversation})

    async def escalate(self, chat_id: int, name: str, text: str, keyword: str) -> None:
        """A customer wrote an escalation keyword: pause the chat, tell the
        owner and the panel."""
        conversation = await self.db.set_paused(chat_id, True, reason=f"escalation: the customer wrote “{keyword}”")
        self.cancel_draft(chat_id)
        self.flow.cancel_scan(chat_id)
        await self.clear_deferred(chat_id)
        excerpt = text if len(text) <= 300 else text[:299] + "…"
        row = await self.flow.send_owner(
            f"⚠️ {name} needs a person: their message contains “{keyword}”. The bot has stopped answering "
            f"them until the chat is switched back on in the panel.\n\n“{excerpt}”",
            reason="escalation to the owner",
        )
        told = row is not None
        await self.post_note(chat_id, f"⚠️ Escalated (“{keyword}”): the bot stopped answering in this chat. "
                             + ("The owner was told." if told else
                                "The owner could NOT be told (check booking.provider)."))
        await self.write_audit(audit.ESCALATED, actor=audit.SYSTEM, reason=f"keyword “{keyword}”",
                               payload={"chat_id": chat_id, "keyword": keyword, "owner_told": told})
        if not told:
            await alerts.raise_alert(
                self.pool, tenant_id=self.tenant_id, kind="escalation_unrouted", severity=alerts.WARNING,
                message=f"{name} needs a person (“{keyword}”) but the owner could not be told. The chat is paused.",
                payload={"chat_id": chat_id},
            )
        await self.hub.broadcast({"type": "conversation", "conversation": conversation})
        await self.hub.broadcast({"type": "escalation", "chat_id": chat_id, "keyword": keyword, "name": name})

    async def clear_deferred(self, chat_id: int) -> None:
        await scheduler.clear_deferred(self.pool, self.tenant_id, chat_id)

    async def run_deferred(self) -> None:
        """Replies that quiet hours held back and whose time has come. While
        soft-off they are taken and dropped: resuming replays nothing."""
        for chat_id in await scheduler.take_due_deferred(self.pool, self.tenant_id, self.utcnow()):
            if not self.paused():
                self.schedule_draft(chat_id)

    # ------------------------------------------------------------------
    # Unanswered queue (unanswered.py) and staging
    # ------------------------------------------------------------------

    async def queue_unanswered(self, chat_id: int, reason: str, detail: str = "",
                               message_id: Optional[int] = None) -> None:
        """Put the customer's message (by default their newest one in this
        chat) into the unanswered queue. One entry per message: the first
        reason recorded wins. Never raises: the queue is a report, and a
        failure writing it must not cost the customer a reply."""
        try:
            if self.transport.is_service_chat(chat_id):
                return
            # The owner talking to their own bot is not a customer.
            if chat_id == await self.flow.provider_chat_id():
                return
            if message_id is None:
                message_id = await unanswered.last_customer_message(self.pool, self.tenant_id, chat_id)
            if message_id is None:
                return
            item = await unanswered.record(
                self.pool, tenant_id=self.tenant_id, session_id=self.session_id, chat_id=chat_id,
                message_id=message_id, reason=reason, detail=detail,
            )
            if item is not None:
                log.info("[%s]   chat %s: queued as unanswered (%s).", self.session_id, chat_id, reason)
                await self.hub.broadcast({"type": "unanswered", "chat_id": chat_id, "reason": reason})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] Could not queue chat %s as unanswered (%s)", self.session_id, chat_id, reason)

    def fallback_phrase(self, text: str) -> str:
        """The first of unanswered.fallback_phrases in this reply (any case),
        or "". A reply with one still goes out; it is only queued."""
        low = (text or "").lower()
        for phrase in self.config["unanswered"]["fallback_phrases"]:
            if phrase.strip() and phrase.strip().lower() in low:
                return phrase.strip()
        return ""

    def staging_on(self) -> bool:
        return bool(self.config["staging"]["enabled"])

    def is_test_chat(self, chat_id: int, username: Optional[str]) -> bool:
        """A staging test chat: its username (no @, any case) or its numeric
        chat id is listed in staging.test_chats."""
        listed = self.config["staging"]["test_chats"]
        name = (username or "").strip().lstrip("@").lower()
        return (bool(name) and name in listed) or str(chat_id) in listed

    async def staged_out(self, chat_id: int, username: Optional[str]) -> bool:
        """Staging is on and this is neither a test chat nor the owner's own
        chat (booking.provider): stored, never answered."""
        if not self.staging_on() or self.is_test_chat(chat_id, username):
            return False
        return chat_id != await self.flow.provider_chat_id()

    async def staging_skip(self, chat_id: int, message_id: Optional[int]) -> None:
        """A message staging keeps the bot out of: a note in the chat (at
        most once an hour per chat) and a queue entry."""
        now = time.monotonic()
        last = self._staging_noted.get(chat_id)
        if last is None or now - last >= STAGING_NOTE_SECONDS:
            self._staging_noted[chat_id] = now
            await self.post_note(chat_id, STAGING_NOTE)
        await self.queue_unanswered(chat_id, unanswered.STAGING, "staging: not a test chat", message_id)

    def paused(self) -> bool:
        """Soft-off: a hold on the tenant or the global stop (controls.py).
        "Pause all" in the panel is the manual hold."""
        return bool(self.off_reason)

    def usage_sink(self, purpose: str) -> ai_responder.UsageSink:
        """Meters every LLM call against this tenant (llm_usage.py)."""
        return functools.partial(self._record_usage, purpose)

    async def _record_usage(self, purpose: str, model: str, usage: dict[str, int]) -> None:
        await llm_usage.record(self.pool, tenant_id=self.tenant_id, purpose=purpose, model=model, usage=usage)

    async def write_audit(self, event: str, *, actor: str = audit.BOT, reason: str = "",
                    payload: Optional[dict[str, Any]] = None) -> None:
        await audit.record(self.pool, tenant_id=self.tenant_id, actor=actor, event=event,
                           reason=reason, payload=payload)

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
        await controls.add_hold(self.pool, self.tenant_id, controls.TELEGRAM, reason, actor=audit.SYSTEM)
        await self.write_audit(audit.ACCOUNT_HALTED, actor=audit.SYSTEM, reason=reason)
        await self.refresh_controls()
        for chat_id in list(self.draft_tasks):
            self.cancel_draft(chat_id)
        await self.db.cancel_queued_outreach()
        await self.hub.broadcast({"type": "halted", "reason": reason})
        await self.push_error(None, f"Automation halted: {reason}")
        await health.error(self.pool, self.tenant_id, self.session_id, reason)
        await alerts.raise_alert(self.pool, tenant_id=self.tenant_id, kind="telegram", severity=alerts.CRITICAL,
                                 message=f"Stopped: {reason}")

    async def check_daily_quota(self) -> None:
        safety = self.config["safety"]
        since = self.start_of_day_utc()

        sent = await self.db.sent_since(since)
        limit = int(self.config["daily_message_cap"])
        if sent >= limit:
            await self.cap_reached(f"daily send limit reached ({sent}/{limit} messages today)")
            raise SendBlocked(
                f"Daily send limit reached ({sent}/{limit} messages today). "
                "Sending resumes tomorrow; raise daily_message_cap in the tenant config if this is wrong."
            )

        hourly = int(self.config.get("hourly_message_cap") or 0)
        if hourly:
            hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
            sent_hour = await self.db.sent_since(hour_ago)
            if sent_hour >= hourly:
                await self.cap_reached(f"hourly send limit reached ({sent_hour}/{hourly} in the last hour)")
                raise SendBlocked(
                    f"Hourly send limit reached ({sent_hour}/{hourly} in the last hour). "
                    "Sending resumes as the hour moves on; see hourly_message_cap."
                )

        peers = await self.db.distinct_peers_since(since)
        peer_limit = int(safety["daily_peer_cap"])
        if peers >= peer_limit:
            await self.cap_reached(f"daily limit on distinct people reached ({peers}/{peer_limit} today)")
            raise SendBlocked(
                f"Daily limit on distinct people reached ({peers}/{peer_limit} today). "
                "Writing to many different people in one day is the strongest spam signal."
            )

    async def cap_reached(self, what: str) -> None:
        """A send cap stopped a message. The operator hears once, until they
        acknowledge the alert."""
        await alerts.raise_alert(self.pool, tenant_id=self.tenant_id, kind="send_cap", severity=alerts.WARNING,
                                 message=f"Messages are being held back: {what}.")

    async def handle_send_failure(self, chat_id: Optional[int], exc: BaseException) -> bool:
        """Translate a network error (classified by the transport) into the
        right defensive action.

        Returns True when the error was recognised and handled, so callers
        can avoid double-reporting it.
        """
        failure = self.transport.classify(exc)
        if failure is None:
            return False
        safety = self.config["safety"]
        net = self.transport.network

        if failure.kind == PEER_FLOOD:
            if safety.get("halt_on_peer_flood", True):
                await self.halt_everything(
                    f"{net} returned {failure.label} — it considers this account "
                    "to be sending unsolicited messages. Everything is paused. Do "
                    "not resume until you know why; sending through this is what "
                    "gets a number banned."
                )
            else:
                await self.push_error(chat_id, f"{failure.label} from {net} (halt disabled).")
            return True

        if failure.kind == SESSION_REJECTED:
            await self.registry.set_state(self.session_id, "needs_login", failure.label)
            await self.halt_everything(
                f"{net} rejected the session ({failure.label}). The account "
                "may be banned or the session revoked. Automation is stopped."
            )
            return True

        if failure.kind == RATE_LIMITED:
            wait = failure.seconds
            cap = int(safety.get("max_flood_wait_seconds", 300))
            log.warning("[%s] %s asked us to wait %ss before sending again.", self.session_id, net, wait)
            await health.rate_limited(self.pool, self.tenant_id, self.session_id, wait)
            await self.push_error(
                chat_id, f"{net} rate limit: it asked for a {wait}s pause. Backing off."
            )
            if wait > cap:
                await self.halt_everything(
                    f"{net} demanded a {wait}s wait, beyond the {cap}s this is "
                    "willing to sleep through. Paused so nothing retries into it."
                )
            else:
                await asyncio.sleep(wait)
            return True

        if failure.kind == UNREACHABLE:
            if chat_id is not None:
                await self.db.set_paused(chat_id, True)
                await self.hub.broadcast({"type": "conversation_paused", "chat_id": chat_id})
            await self.push_error(
                chat_id,
                f"Cannot message this person ({failure.label}); this "
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

    def local_now(self) -> datetime:
        """Now, in the tenant's timezone."""
        return self.utcnow().astimezone(bookings.tzinfo_for(self.config["timezone"]))

    def quiet_seconds_left(self, at: Optional[datetime] = None) -> float:
        """How long until quiet hours end (0 outside them) at `at`, now by default."""
        return humanlike.seconds_until_quiet_ends(at or self.local_now(), self.config["quiet_hours"])

    async def resolve_peer(self, chat_id: int):
        return await self.transport.resolve_peer(chat_id)

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
        return self.account.get("contacts", {}).get(str(chat_id)) or {}

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
                usage_sink=self.usage_sink("context_summary"),
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
            await self.transport.set_presence(online)
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
        """Hand one message to the network. With `typing` (and the typing
        indicator on) the chat shows "typing…" for as long as writing it
        would plausibly take, and the message goes out while it still shows."""
        seconds = None
        if typing and self.config["human"].get("typing_indicator", True):
            seconds = self.typing_seconds(text, chat_id)
            log.info("[%s]   typing for %.1fs…", self.session_id, seconds)
        return await self.transport.send_text(peer, chat_id, text, seconds)

    async def mark_read(self, chat_id: int, message_id: Optional[int] = None) -> None:
        if not self.config["human"].get("mark_read", True):
            return
        try:
            await self.transport.mark_read(chat_id, message_id)
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
        *,
        actor: str = audit.BOT,
        reason: str = "",
        llm_model: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send one text message as this account. Every send writes an
        audit row saying who caused it (`actor`) and why (`reason`);
        `llm_model` / `prompt_version` mark an AI-written message."""
        await self.ensure_may_send(actor)
        if guard:
            await self.check_daily_quota()
        peer = await self.resolve_peer(chat_id)
        self.in_flight_sends.setdefault(chat_id, []).append(text)
        self.delivery_attempts += 1
        try:
            sent = await self.deliver(peer, chat_id, text, typing)
            telegram_id = self.transport.message_id(sent)
            if draft_id is not None:
                row = await self.db.update_message(
                    draft_id, text=text, status=STATUS_SENT, telegram_id=telegram_id
                )
                await self.db.set_conversation_preview(chat_id, text)
            else:
                row = await self.db.record_message(
                    chat_id, DIR_OUT, STATUS_SENT, text, telegram_id=telegram_id,
                    llm_model=llm_model, prompt_version=prompt_version,
                )
                if row is None:
                    row = await self.db.find_by_telegram_id(chat_id, telegram_id)
        finally:
            pending = self.in_flight_sends.get(chat_id) or []
            if text in pending:
                pending.remove(text)
            if not pending:
                self.in_flight_sends.pop(chat_id, None)

        await self.write_audit(audit.MESSAGE_SENT, actor=actor, reason=reason,
                         payload={"message_id": (row or {}).get("id"), "kind": "text"})
        if row is not None:
            await self.push_message(row)
        return row or {}

    async def deliver_file(self, peer: Any, chat_id: int, item: dict[str, Any], path: Path) -> Any:
        return await self.transport.send_file(
            peer, chat_id, path, item.get("kind") == media.VIDEO,
            self.config["human"].get("typing_indicator", True),
        )

    async def send_media_as_me(
        self, chat_id: int, item_id: int, draft_id: Optional[int] = None, guard: bool = True,
        *, actor: str = audit.BOT, reason: str = "",
    ) -> dict[str, Any]:
        item = self.media_library.get(item_id)
        path = self.media_library.path(item_id)
        if item is None or path is None:
            raise ValueError(f"Media #{item_id} is no longer in the library.")
        await self.ensure_may_send(actor)
        if guard:
            await self.check_daily_quota()
        peer = await self.resolve_peer(chat_id)
        text = media.sent_placeholder(item)
        self.in_flight_media[chat_id] = self.in_flight_media.get(chat_id, 0) + 1
        self.delivery_attempts += 1
        try:
            sent = await self.deliver_file(peer, chat_id, item, path)
            telegram_id = self.transport.message_id(sent)
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

        await self.write_audit(audit.MESSAGE_SENT, actor=actor, reason=reason,
                         payload={"message_id": (row or {}).get("id"), "kind": item.get("kind"), "media_id": item_id})
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
        *,
        actor: str = audit.BOT,
        reason: str = "",
        llm_model: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {}
        files = [i for i in (attachments or []) if self.media_library.get(i) is not None]
        for index, part in enumerate(parts):
            if index:
                await asyncio.sleep(humanlike.burst_gap_seconds(self.config["burst"]))
            row = await self.send_as_me(
                chat_id, part, draft_id=draft_id if index == 0 else None, typing=typing, guard=guard,
                actor=actor, reason=reason, llm_model=llm_model, prompt_version=prompt_version,
            )
            if index == 0 and draft_id is not None and files:
                row = await self.db.update_message(draft_id, attachments=[]) or row
                await self.push_message(row)
        for index, item_id in enumerate(files):
            if parts or index:
                await asyncio.sleep(humanlike.burst_gap_seconds(self.config["burst"]))
            row = await self.send_media_as_me(
                chat_id, item_id, draft_id=draft_id if (not parts and index == 0) else None, guard=guard,
                actor=actor, reason=reason,
            )
        return row

    # ------------------------------------------------------------------
    # Drafting pipeline
    # ------------------------------------------------------------------

    def schedule_draft(self, chat_id: int) -> None:
        self.cancel_draft(chat_id)
        self.draft_tasks[chat_id] = asyncio.create_task(self.draft_worker(chat_id))

    def retry_draft_later(self, chat_id: int, attempt: int) -> None:
        """Start this chat's reply again after a pause (the database was
        unreachable while it was being written). A newer message, or any
        cancel, replaces or stops it like a normal draft."""
        async def later() -> None:
            await asyncio.sleep(DRAFT_RETRY_SECONDS * attempt)
            await self.draft_worker(chat_id, attempt)

        self.draft_tasks[chat_id] = asyncio.create_task(later())

    async def _best_effort(self, coro: Any, what: str) -> None:
        """Report something (an error row, a note) without letting a failure
        to report it (the database is down) turn into a second failure."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] Could not %s: %s: %s", self.session_id, what, type(exc).__name__, exc)

    def cancel_draft(self, chat_id: int) -> None:
        task = self.draft_tasks.pop(chat_id, None)
        if task is None or task.done():
            return
        if chat_id in self.sending_chats:
            return
        task.cancel()

    def reply_delay(self, chat_id: int) -> float:
        """Seconds before the reply to this chat is written: a per-contact
        override from the Style sheet if one is set, else the tenant's
        reply_delay. Quiet hours come on top (draft_worker)."""
        overrides = self.contact_overrides(chat_id)
        if overrides.get("min_delay_seconds") is not None or overrides.get("max_delay_seconds") is not None:
            delay_cfg = self.config["reply_delay"]
            low = _ov_int(overrides, "min_delay_seconds", int(delay_cfg["min_s"]))
            high = _ov_int(overrides, "max_delay_seconds", int(delay_cfg["max_s"]))
            return random.uniform(min(low, high), max(low, high))
        return humanlike.sample_reply_delay(self.config["reply_delay"])

    async def draft_worker(self, chat_id: int, attempt: int = 0) -> None:
        """Write (and send, or keep for approval) the reply to this chat.
        `attempt` counts restarts after the database was unreachable."""
        attempts_before = self.delivery_attempts
        try:
            overrides = self.contact_overrides(chat_id)
            delay = self.reply_delay(chat_id)
            # A reply that would land in quiet hours waits for them to end,
            # plus a fresh delay so a night's messages don't all get
            # answered in the same second at opening time.
            quiet = self.quiet_seconds_left(humanlike.later(self.local_now(), delay))
            if quiet:
                await self.defer_reply(chat_id, delay + quiet + humanlike.sample_reply_delay(self.config["reply_delay"]))
                return

            await self.hub.broadcast({"type": "drafting", "chat_id": chat_id, "delay_seconds": round(delay, 1)})
            log.info("[%s]   drafting a reply for chat %s in %.0fs…", self.session_id, chat_id, delay)
            await asyncio.sleep(delay)
            # Quiet hours may have been switched on or moved while waiting.
            if (left := self.quiet_seconds_left()) > 0:
                await self.defer_reply(chat_id, left + humanlike.sample_reply_delay(self.config["reply_delay"]))
                return

            # The switches may have been thrown while waiting.
            if off := await self.refresh_controls():
                await self.queue_unanswered(chat_id, unanswered.SOFT_OFF, off)
                return
            conversation = await self.db.get_conversation(chat_id)
            if conversation is None:
                return
            if quiet := self.silenced(conversation):
                await self.queue_unanswered(chat_id, unanswered.PAUSED, quiet)
                return
            # Staging may have been switched on after this reply was
            # scheduled (a reply held back by quiet hours, say).
            if await self.staged_out(chat_id, conversation.get("username")):
                await self.staging_skip(chat_id, None)
                return

            history = await self.db.get_history_for_ai(chat_id, limit=30)
            if not history:
                log.info("[%s] No usable history for chat %s; skipping draft.", self.session_id, chat_id)
                return

            # The booking check for this message decides what the reply
            # has to say (taken, confirmed, offered times).
            await self.flow.wait_scan(chat_id)
            note = await self.flow.reply_note(chat_id)
            if limit := await self.ai_limit_reason():
                # A limit puts the client soft-off (a spend_cap hold).
                await self.queue_unanswered(chat_id, unanswered.SOFT_OFF, limit)
                return
            if not note.has_news:
                skip = await self.reply_skip_reason(chat_id, history)
                if skip:
                    await self.skip_reply(chat_id, skip)
                    return

            if self.config["auto_send"]:
                await self.check_daily_quota()

            await self.go_online_for(chat_id)
            await self.mark_read(chat_id)

            background = await self.borrowed_context(chat_id)
            if background:
                log.info("[%s]   drawing on a linked chat for context.", self.session_id)

            if note.has_news:
                log.info("[%s]   this reply carries booking news.", self.session_id)
            # The tenant's "when not to answer" instruction is only offered
            # when there is nothing the reply must pass on.
            no_reply = "" if note.has_news else self.config["replies"]["no_reply_instruction"]

            media_note = self.media_prompt()
            prompt = self.bundle.prompt if self.bundle is not None else None

            async with self.ai_gate():
                text = await ai_responder.generate_reply(
                    api_key=self.deepseek_key,
                    history=history,
                    persona={},
                    ai_config=self.config["ai"],
                    client=self.http_client,
                    adaptive_style=self.config["human"]["adaptive_style"],
                    contact=overrides,
                    background=background,
                    booking_note=note.text,
                    media_note=media_note,
                    system_prompt=prompt.text if prompt else None,
                    language_locked=self.config["language_policy"] != "mirror",
                    burst_max=self.config["burst"]["max_messages"],
                    usage_sink=self.usage_sink("reply"),
                    no_reply_instruction=no_reply,
                )

            if no_reply and ai_responder.is_no_reply(text):
                await self.skip_reply(chat_id, "the no-reply instruction applies to this message")
                return

            text, attachments = media.split_attachments(text)
            attachments = [i for i in attachments if self.media_library.get(i) is not None]
            if not media_note:
                attachments = []
            parts = ai_responder.split_burst(text, self.config["burst"]["max_messages"])
            if not parts and not attachments:
                raise ai_responder.AIResponderError("The reply came back empty.")
            if attachments:
                log.info("[%s]   attaching %s.", self.session_id, ", ".join(
                    media.label(self.media_library.get(i)) for i in attachments
                ))

            holds_video = any(
                (self.media_library.get(i) or {}).get("kind") == media.VIDEO for i in attachments
            )
            hold = holds_video and self.config["media"]["videos_need_approval"]
            verdict = await self.check_policy(chat_id, text, prompt)
            model = self.config["ai"]["model"]
            version = prompt.version_tag if prompt else None
            # Found before sending, queued after: the reply still goes out
            # (or is drafted) as normal.
            fallback = self.fallback_phrase(text)

            if self.config["auto_send"] and not hold and verdict.ok:
                self.sending_chats.add(chat_id)
                try:
                    await self.send_burst(
                        chat_id, parts, typing=True, attachments=attachments,
                        actor=audit.BOT, reason="automatic reply", llm_model=model, prompt_version=version,
                    )
                finally:
                    self.sending_chats.discard(chat_id)
                log.info(
                    "[%s] Auto-sent AI reply to chat %s%s.", self.session_id, chat_id,
                    f" as {len(parts)} messages" if len(parts) > 1 else "",
                )
            else:
                if hold and self.config["auto_send"]:
                    log.info("[%s]   reply carries a video — held for approval in the panel.", self.session_id)
                row = await self.db.record_message(
                    chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False, attachments=attachments,
                    llm_model=model, prompt_version=version,
                )
                if row is not None:
                    await self.push_message(row)
                log.info("[%s] Draft awaiting approval for chat %s.", self.session_id, chat_id)
                # Waiting for approval because auto_send is off or for a
                # video is a normal draft; held by policy is unanswered.
                if not verdict.ok:
                    await self.queue_unanswered(chat_id, unanswered.POLICY_HOLD, "; ".join(verdict.reasons))
            if fallback:
                await self.queue_unanswered(chat_id, unanswered.FALLBACK, f"the reply contains “{fallback}”")
            await self.flow.delivered(chat_id, note)
            await scheduler.clear_deferred(self.pool, self.tenant_id, chat_id)
            self.schedule_go_offline(chat_id)

        except asyncio.CancelledError:
            raise
        except SendBlocked as exc:
            log.info("[%s] Not replying in chat %s: %s", self.session_id, chat_id, exc)
            await self._best_effort(self.push_error(chat_id, str(exc)), "report a blocked send")
            # Stopped at the last step: the switches, or a send cap.
            await self.queue_unanswered(chat_id, unanswered.SOFT_OFF if self.paused() else unanswered.SKIPPED,
                                        str(exc))
        except ai_responder.AIResponderError as exc:
            await self._best_effort(self.push_error(chat_id, str(exc)), "report an AI error")
            await self.queue_unanswered(chat_id, unanswered.AI_ERROR, str(exc))
        except Exception as exc:
            if (pg.is_transient(exc) and self.delivery_attempts == attempts_before
                    and attempt < DRAFT_DB_RETRIES and not self._stopping):
                # The database (or the connection to Telegram) dropped out
                # before anything was handed to Telegram: nothing went out,
                # so writing the reply again later is safe.
                log.warning("[%s] Reply to chat %s interrupted (%s: %s); trying again in %.0fs (%d/%d).",
                            self.session_id, chat_id, type(exc).__name__, exc,
                            DRAFT_RETRY_SECONDS * (attempt + 1), attempt + 1, DRAFT_DB_RETRIES)
                self.retry_draft_later(chat_id, attempt + 1)
                return
            try:
                handled = await self.handle_send_failure(chat_id, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] Handling a failed send in chat %s failed too", self.session_id, chat_id)
                handled = False
            if not handled:
                log.exception("[%s] Unexpected failure while drafting for chat %s", self.session_id, chat_id)
                await self._best_effort(self.push_error(chat_id, f"Drafting failed: {type(exc).__name__}: {exc}"),
                                        "report a drafting failure")
                await self.queue_unanswered(chat_id, unanswered.AI_ERROR, f"{type(exc).__name__}: {exc}")
            else:
                # A network error that was dealt with (a halt, a blocked
                # chat, a rate limit): the reply did not go out.
                await self.queue_unanswered(chat_id, unanswered.SOFT_OFF if self.paused() else unanswered.SKIPPED,
                                            f"{self.transport.network}: {type(exc).__name__}")
        finally:
            if chat_id in self.active_chats:
                self.schedule_go_offline(chat_id)
            if self.draft_tasks.get(chat_id) is asyncio.current_task():
                self.draft_tasks.pop(chat_id, None)

    async def defer_reply(self, chat_id: int, seconds: float) -> None:
        """Quiet hours: write the reply down for later instead of sleeping on
        it, so a restart doesn't lose it."""
        due = self.utcnow() + timedelta(seconds=seconds)
        await scheduler.defer_reply(self.pool, self.tenant_id, self.session_id, chat_id, due)
        log.info("[%s]   quiet hours: the reply to chat %s waits %.0f min.", self.session_id, chat_id, seconds / 60)
        await self.hub.broadcast({"type": "drafting", "chat_id": chat_id, "delay_seconds": round(seconds, 1)})

    async def reply_skip_reason(self, chat_id: int, history: list[dict[str, str]]) -> str:
        """Why this message should get no reply (replies.* in the config), or ""."""
        replies = self.config["replies"]
        last = history[-1] if history else {}
        if (replies["skip_acknowledgements"] and last.get("role") == "user"
                and ai_limits.is_acknowledgement(last.get("content", ""), replies["acknowledgements"])):
            return ACK_SKIP
        return await ai_limits.reply_limit(self.pool, self.tenant_id, chat_id, replies)

    async def skip_reply(self, chat_id: int, reason: str) -> None:
        log.info("[%s]   not replying in chat %s: %s", self.session_id, chat_id, reason)
        await self.post_note(chat_id, f"No reply: {reason}.")
        await self.write_audit(audit.REPLY_SKIPPED, reason=reason, payload={"chat_id": chat_id})
        # A reply limit or the tenant's no-reply instruction left a
        # customer without an answer: queued. A bare acknowledgement
        # ("ok", "thanks") needed none, so it is not.
        if reason != ACK_SKIP:
            await self.queue_unanswered(chat_id, unanswered.SKIPPED, reason)

    async def check_policy(
        self, chat_id: int, text: str, prompt: Optional[Any]
    ) -> policy.Verdict:
        """policy.py on an AI-written message. A failure is shown in the
        chat and audited; the caller then keeps the message as a draft
        instead of sending it."""
        verdict = policy.check_outbound(text, self.config, prompt.business_text if prompt else "")
        if not verdict.ok:
            log.warning("[%s] Reply to chat %s held by policy: %s", self.session_id, chat_id, verdict.reasons)
            await self.post_note(chat_id, "Held for approval: the reply " + "; ".join(verdict.reasons) + ".")
            await self.write_audit(audit.POLICY_HOLD, reason="; ".join(verdict.reasons),
                             payload={"reasons": verdict.reasons})
        if verdict.tripwire and self.config["anomaly"]["tripwire_suspend"]:
            await self.suspend_for_anomaly(
                anomaly.TRIPWIRE, "A reply the bot wrote tripped the outbound trip-wire: " + "; ".join(verdict.tripwire),
                {"chat_id": chat_id, "reasons": verdict.tripwire},
            )
        return verdict

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
                if self.paused():
                    log.info("[%s] Outreach paused (global pause); leaving %s queued.", self.session_id, item["id"])
                    return
                if not settings["enabled"]:
                    log.info("[%s] Outreach is off in the tenant config; leaving %s queued.", self.session_id, item["id"])
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
                    persona={},
                    ai_config=self.config["ai"],
                    client=self.http_client,
                    contact=self.contact_overrides(chat_id),
                    background=background,
                    system_prompt=self.bundle.prompt.text if self.bundle is not None else None,
                    usage_sink=self.usage_sink("outreach"),
                )
        except ai_responder.AIResponderError as exc:
            await self.db.update_outreach(outreach_id, status=OUT_FAILED, error=str(exc))
            await self.push_error(chat_id, f"Outreach draft failed: {exc}")
            await self.broadcast_outreach()
            return

        await self.go_online_for(chat_id)
        prompt = self.bundle.prompt if self.bundle is not None else None
        verdict = await self.check_policy(chat_id, text, prompt)

        if not self.config["outreach"]["auto_send"] or not verdict.ok:
            row = await self.db.record_message(
                chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False,
                llm_model=self.config["ai"]["model"], prompt_version=prompt.version_tag if prompt else None,
            )
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
            await self.send_as_me(
                chat_id, text, typing=True, actor=audit.BOT, reason="outreach",
                llm_model=self.config["ai"]["model"], prompt_version=prompt.version_tag if prompt else None,
            )
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
        contacts = []
        for chat_id, peer in await self.transport.list_contacts():
            await self.db.upsert_conversation(chat_id, peer.name, peer.username, peer.is_bot, peer.access_hash)
            contacts.append({
                "chat_id": chat_id, "display_name": peer.name, "username": peer.username, "is_bot": peer.is_bot,
            })
        contacts.sort(key=lambda c: c["display_name"].lower())
        return contacts

    # ------------------------------------------------------------------
    # Bookings
    # ------------------------------------------------------------------

    async def reminder_loop(self) -> None:
        """Backstop for a missed reload_config: pick up config and prompt
        changes (an industry template edit, say) every few minutes, and tell
        the health watchdog the account is connected (health.seen) once a
        minute. Booking work runs on the scheduler's tick."""
        try:
            while True:
                # Each part on its own: the database being away for a
                # minute must not stop the account reporting itself later.
                self.ensure_command_server()
                try:
                    if time.monotonic() - self._bound_at > self.REBIND_SECONDS:
                        await self.bind_tenant()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] Rebind failed", self.session_id)
                try:
                    if self.transport.connected:
                        await health.seen(self.pool, self.tenant_id, self.session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("[%s] Could not report health (%s: %s); trying again in a minute.",
                                self.session_id, type(exc).__name__, exc)
                self.prune_memory()
                await asyncio.sleep(self.REMINDER_TICK_SECONDS)
        except asyncio.CancelledError:
            raise

    def ensure_command_server(self) -> None:
        """The command server (commands.CommandBus.serve) reconnects by
        itself; should it still end while this runtime runs (a bug, an
        unexpected error), start it again, so the scheduler and the panel
        can reach this account without a restart."""
        task = self._command_serve_task
        if self.bus is None or self._stopping or self._command_stop_event.is_set():
            return
        if task is not None and not task.done():
            return
        if task is not None and not task.cancelled() and task.exception() is not None:
            log.error("[%s] Command server stopped (%r); starting it again.", self.session_id, task.exception())
        elif task is not None:
            log.error("[%s] Command server stopped; starting it again.", self.session_id)
        self._command_serve_task = asyncio.create_task(
            self.bus.serve(self.session_id, self.handle_command, self._command_stop_event)
        )

    def prune_memory(self) -> None:
        """Per-chat bookkeeping that is only a throttle or a hint is dropped
        once it no longer matters, so a long-running account's memory
        doesn't grow with every chat it ever saw."""
        now = time.monotonic()
        for chat_id, at in list(self._staging_noted.items()):
            if now - at >= STAGING_NOTE_SECONDS:
                self._staging_noted.pop(chat_id, None)
        self.flow.prune_memory()

    async def post_note(self, chat_id: int, text: str) -> None:
        row = await self.db.record_message(chat_id, DIR_SYSTEM, STATUS_NOTE, text, bump_preview=False)
        if row is not None:
            await self.push_message(row)

    async def provider_chat_id(self) -> Optional[int]:
        return await self.flow.provider_chat_id()

    # ------------------------------------------------------------------
    # Photos
    # ------------------------------------------------------------------

    async def read_photo(self, chat_id: int, message: Inbound) -> Optional[str]:
        """What a customer's photo shows, as text for the chat history:
        an arrival check when they may be arriving, else a short description
        (vision.* in the config). None when photos are not looked at."""
        cfg = self.config["vision"]
        url, key = vision.endpoint_from_env()
        if not (cfg["enabled"] and cfg["model"] and url and key) or await self.ai_limit_reason():
            return None
        try:
            image = await self.transport.download_photo(message)
        except Exception as exc:
            log.warning("[%s] Could not download a photo in chat %s: %s", self.session_id, chat_id, type(exc).__name__)
            return None
        if not image:
            return None
        try:
            arrival = await self.flow.arrival_photo(chat_id, image)
            if arrival is not None:
                return arrival
            if not cfg["describe_photos"]:
                return None
            async with self.ai_gate():
                description = await vision.describe_photo(
                    image, api_url=url, api_key=key, model=cfg["model"], client=self.http_client,
                    usage_sink=self.usage_sink("photo_describe"),
                )
        except vision.VisionError as exc:
            await self.push_error(chat_id, f"Photo not read: {exc}")
            return None
        return f"[photo] {description}"

    async def save_owner_photo(self, message: Inbound, caption: str) -> bool:
        """The owner sends a photo captioned "door" / "entrance" (or durvis,
        ieeja, дверь, вход): it becomes an entrance reference for the arrival
        photo check."""
        words = {w.strip(".,!:").lower() for w in caption.split()}
        if not words & {"door", "entrance", "durvis", "ieeja", "дверь", "вход"}:
            return False
        name = self.media_library.unique_name(f"entrance-{message.external_id}.jpg")
        try:
            await self.transport.download_photo(message, self.media_library.dir / name)
            item = self.media_library.add_file(name, caption[:200], role=media.ARRIVAL_REFERENCE)
        except Exception as exc:
            log.warning("[%s] Could not save the owner's entrance photo: %s", self.session_id, exc)
            return False
        with suppress(Exception):
            await self.send_as_me(message.chat_id, f"Saved as entrance reference photo #{item['id']}.",
                                  reason="entrance photo saved for the owner")
        return True

    # ------------------------------------------------------------------
    # Inbound messages
    # ------------------------------------------------------------------

    async def _store_with_retry(self, what: str, make: Any) -> Any:
        """`make()` (a database write), tried again while the database is
        unreachable: STORE_ATTEMPTS in all, with growing pauses. Anything
        else, or the last failure, is raised."""
        for attempt in range(1, STORE_ATTEMPTS + 1):
            try:
                return await make()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= STORE_ATTEMPTS or not pg.is_transient(exc):
                    raise
                log.warning("[%s] Database unreachable while %s (%s: %s); trying again (%d/%d).",
                            self.session_id, what, type(exc).__name__, exc, attempt + 1, STORE_ATTEMPTS)
                await asyncio.sleep(STORE_RETRY_SECONDS * attempt)

    async def on_incoming(self, event: Any) -> None:
        """A new message event from the transport's own library (Telethon);
        the transport normalises it and calls handle_inbound. Failures are
        logged there and never reach the library."""
        await self.transport.on_incoming(event)

    async def on_outgoing(self, event: Any) -> None:
        await self.transport.on_outgoing(event)

    async def handle_inbound(self, message: Inbound) -> None:
        """A private message the account received, from any transport."""
        chat_id = message.chat_id
        peer = await message.load_peer()
        name, username, is_bot = peer.name, peer.username, peer.is_bot
        conversation = await self._store_with_retry(
            "storing a conversation",
            lambda: self.db.upsert_conversation(chat_id, name, username, is_bot, peer.access_hash),
        )
        await self.detect_links(conversation)

        text = message.text
        has_text = bool(text)
        has_photo = message.has_photo
        stored_text = text if has_text else ("[photo]" if has_photo else "[non-text message]")

        # Always stored: the platform keeps a complete record of every
        # conversation (the pre-platform log_all_messages switch is gone).
        # Tried again while the database is briefly away; a repeat is
        # harmless (the network's message id makes it a duplicate).
        row = await self._store_with_retry("storing a message", lambda: self.db.record_message(
            chat_id, DIR_IN, STATUS_RECEIVED, stored_text, telegram_id=message.external_id, mark_unread=True,
        ))

        if row is not None:
            await self.push_message(row)

        log.info("[%s] DM from %s%s (chat %s): %s", self.session_id, name, " [bot]" if is_bot else "", chat_id,
                 f"{len(text)} chars" if has_text else "non-text message")

        if message.is_service:
            # Login codes and "new login" notices: never answered, and a
            # reason to look at the account's logins now.
            await self.check_logins()
            return

        is_provider = self.flow.enabled() and chat_id == await self.flow.provider_chat_id()
        quiet = self.silenced(conversation)
        # Staging: only the test chats get the normal flow; the owner's own
        # chat is never held back. Decided up front so a photo from anyone
        # else costs no vision call either.
        staged = await self.staged_out(chat_id, username)
        message_id = row["id"] if row is not None else None
        if is_provider:
            if has_photo and await self.save_owner_photo(message, text):
                return
            if has_text and await self.flow.on_owner_message(chat_id, text, message.reply_to):
                log.info("[%s]   booking command from the owner — handled.", self.session_id)
                return
            log.info("[%s]   message from the booking owner; replying as usual.", self.session_id)

        if has_photo and not is_provider and not self.paused() and not quiet and not staged:
            seen = await self.read_photo(chat_id, message)
            if seen is not None and row is not None:
                label = f"{text}\n{seen}" if has_text else seen
                updated = await self.db.update_message(row["id"], text=label)
                if updated is not None:
                    await self.push_message(updated)
                has_text = True
                text = label

        if not has_text:
            log.info("[%s]   no text to reply to — skipping.", self.session_id)
            return
        # Checked even while soft-off, so the chat stays paused afterwards.
        keyword = "" if is_provider or quiet else policy.escalation_match(text, self.config["escalation_keywords"])
        if keyword:
            log.info("[%s]   escalation keyword — the chat is paused and the owner pinged.", self.session_id)
            await self.escalate(chat_id, name, text, keyword)
            await self.queue_unanswered(chat_id, unanswered.ESCALATED, f"keyword “{keyword}”", message_id)
            return
        # Off in memory: make sure it still is (a resume may not have
        # reached this runtime yet).
        if self.paused() and await self.refresh_controls():
            log.info("[%s]   soft-off (%s) — not answering.", self.session_id, self.off_reason)
            await self.queue_unanswered(chat_id, unanswered.SOFT_OFF, self.off_reason, message_id)
            return
        # Staging keeps escalation (above) — a customer asking for a person
        # still pauses the chat and reaches the owner — but nothing else.
        if staged:
            log.info("[%s]   staging: chat %s is not a test chat — not answering.", self.session_id, chat_id)
            await self.staging_skip(chat_id, message_id)
            return
        if quiet:
            log.info("[%s]   not answering in this chat: %s.", self.session_id, quiet)
            await self.queue_unanswered(chat_id, unanswered.PAUSED, quiet, message_id)
            return

        if self.flow.enabled() and not is_provider:
            await self.flow.on_customer_message(chat_id, text)

        conversation = await self.db.get_conversation(chat_id)
        if quiet := self.silenced(conversation):
            log.info("[%s]   this conversation is paused — skipping.", self.session_id)
            await self.queue_unanswered(chat_id, unanswered.PAUSED, quiet, message_id)
            return
        # Quiet hours do not skip the reply; draft_worker holds it until
        # they end.
        self.schedule_draft(chat_id)

    async def handle_own_echo(self, message: Inbound) -> None:
        """A message the account itself sent, as the network reports it back:
        one of this runtime's own sends (ignored), or one written by hand
        elsewhere (the phone, a desktop app), which is stored and takes the
        chat over."""
        text = message.text
        chat_id = message.chat_id
        if text and text in (self.in_flight_sends.get(chat_id) or []):
            return
        if not text and self.in_flight_media.get(chat_id):
            return

        peer = await message.load_peer()
        await self._store_with_retry(
            "storing a conversation",
            lambda: self.db.upsert_conversation(chat_id, peer.name, peer.username, peer.is_bot, peer.access_hash),
        )

        row = await self._store_with_retry("storing a sent message", lambda: self.db.record_message(
            chat_id, DIR_OUT, STATUS_SENT, text or "[non-text message]", telegram_id=message.external_id,
        ))
        if row is None:
            return  # already stored: one of this runtime's own sends
        await self.push_message(row)
        # Written by hand on the account's own phone or app: a person has
        # taken this chat over. Not the owner's booking chat or Saved Messages.
        if chat_id != await self.flow.provider_chat_id():
            await self.start_takeover(chat_id, how=f"Someone wrote here by hand in {self.transport.network}",
                                      actor=audit.OWNER)

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    async def reconnect(self) -> dict[str, Any]:
        """Drop the connection and open it again with the login and proxy
        stored now. Drafts in progress are cancelled like on a stop."""
        proxy = await self.transport.reload_login()
        await self._stop_transport()
        await self._start_transport()
        return {"ok": True, "proxy": proxy}

    async def _start_transport(self) -> None:
        await self.transport.start()
        self.reminder_task = asyncio.create_task(self.reminder_loop())

    async def _stop_transport(self) -> None:
        self.presence_online = False
        await self.transport.halt_updates()
        ticker, self.reminder_task = self.reminder_task, None
        if ticker is not None and not ticker.done():
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
        for chat_id in list(self.draft_tasks):
            self.cancel_draft(chat_id)
        for chat_id in list(self.flow.scan_tasks):
            self.flow.cancel_scan(chat_id)
        if self.offline_timer is not None and not self.offline_timer.done():
            self.offline_timer.cancel()
        await self.transport.disconnect()

    # What the transport reports about its connection. The transport keeps
    # itself connected (and reconnects); these are the account's reactions.

    async def on_connected(self) -> None:
        if self.config["presence"].get("enabled", True):
            await self.set_presence(False)
        log.info(
            "[%s] %s connected as %s. Listening for private messages.",
            self.session_id, self.transport.network, self.me_info["name"],
        )
        if self.paused():
            log.warning(
                "[%s] Soft-off (%s) — incoming messages will NOT be answered. Resume from the panel.",
                self.session_id, self.off_reason,
            )
        await self.hub.broadcast({"type": "status", "status": self.status()})
        await self.registry.set_state(self.session_id, "running", "")
        await health.seen(self.pool, self.tenant_id, self.session_id)

    async def on_connection_error(self, error: str) -> None:
        await self.hub.broadcast({"type": "status", "status": self.status()})
        with suppress(Exception):
            await health.error(self.pool, self.tenant_id, self.session_id, error)

    async def on_logged_out(self, notice: str) -> None:
        """The stored login no longer works (the transport has already
        forgotten it): needs a new sign-in, loudly."""
        await self.registry.set_state(self.session_id, "needs_login", notice)
        await health.error(self.pool, self.tenant_id, self.session_id, notice)
        await alerts.raise_alert(self.pool, tenant_id=self.tenant_id, kind="health:logged_out",
                                 severity=alerts.CRITICAL, message=notice)
        # Nothing more to run; the worker drops this runtime and its lease.
        self.finished = True

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "telegram_connected": self.transport.connected,
            "telegram_error": self.transport.error,
            "me": self.me_info,
            "global_pause": self.paused(),
            "off_reason": self.off_reason,
            "auto_send": self.config["auto_send"],
            "tenant_id": self.tenant_id,
            # Staging: only the test chats are answered.
            "staging": self.staging_on(),
            # Kept under its old name for the panel: true once the business
            # sections of the prompt say anything at all.
            "persona_configured": bool(self.bundle and self.bundle.prompt.business_text.strip()),
        }


async def log_out_session(pool: asyncpg.Pool, session_id: str) -> bool:
    """Hard-off for an account no worker is running: take its lease (so no
    worker starts it meanwhile), connect with the stored key, log out.
    True when Telegram confirmed. The caller deletes the key either way."""
    worker_id = f"hard-off:{socket.gethostname()}"
    lease = await leasing.acquire(pool, session_id, worker_id)
    if lease is None:
        raise leasing.LeaseLost(f"session {session_id!r} is running somewhere; ask that worker instead")
    try:
        import telegram_transport

        return await telegram_transport.log_out_stored(pool, session_id)
    finally:
        await leasing.release(pool, session_id, worker_id)


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
