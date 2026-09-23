"""Command bus + event fan-out between the (session-less) panel process and
whichever worker process actually holds a session's live SessionRuntime.

Why this exists: panel.py and manager.py are separate OS processes — and
the whole point of leasing.py is that only ONE worker, wherever it happens
to be running, may hold a given session's live Telethon connection at a
time. A request to "send this message" or "fetch contacts" has to reach
that specific worker, not just any process. Redis is the rendezvous point,
since it's the one thing every process already shares.

Two independent uses of the same connection, kept in one module because
they're the same rendezvous pattern:

- `CommandBus` — request/response RPC. The panel calls `dispatch()`, which
  publishes a command on that session's channel and waits (with a timeout)
  for a reply on a one-shot response channel. The worker that owns the
  session is the one subscribed to `serve()` on it, and is therefore the
  only one that ever answers. If no worker owns the session right now,
  `dispatch()` times out — that IS the "this session isn't running
  anywhere" signal, no separate check needed.
- `publish_event` / `subscribe_events` — one-way fan-out for live updates
  (a new message arrived, a draft is pending, a booking was confirmed).
  The worker publishes; the panel's websocket handler for that session_id
  subscribes for exactly as long as that browser tab is open and forwards
  each event over the socket. This replaces the old in-process `Hub` that
  only worked because the panel and the runtime used to be the same
  process.

Nothing here is session-state — Redis holds no data that outlives a single
command's round trip or a single websocket's lifetime. Postgres remains the
only source of truth; losing Redis loses in-flight commands and live event
delivery, never anything persisted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import redis.asyncio as aioredis

log = logging.getLogger("commands")

DEFAULT_TIMEOUT_SECONDS = 30.0


class CommandError(RuntimeError):
    """The worker that handled this command reported a failure."""


class CommandTimeout(CommandError):
    """Nobody answered in time — almost always because no worker currently
    holds this session's lease, e.g. it needs login or crashed and hasn't
    been reassigned yet."""


def _cmd_channel(session_id: str) -> str:
    return f"cmd:{session_id}"


def _resp_channel(command_id: str) -> str:
    return f"cmdresp:{command_id}"


def _event_channel(session_id: str) -> str:
    return f"events:{session_id}"


class CommandBus:
    """One instance per process, wrapping one Redis connection."""

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client

    @classmethod
    async def connect(cls, redis_url: str) -> "CommandBus":
        return cls(aioredis.from_url(redis_url, decode_responses=True))

    async def close(self) -> None:
        await self._redis.aclose()

    # ------------------------------------------------------------------
    # Panel side: ask whichever worker owns this session to do something.
    # ------------------------------------------------------------------

    async def dispatch(
        self, session_id: str, action: str, args: Optional[dict[str, Any]] = None,
        *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Any:
        command_id = uuid.uuid4().hex
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_resp_channel(command_id))
        try:
            await self._redis.publish(_cmd_channel(session_id), json.dumps({
                "command_id": command_id, "action": action, "args": args or {},
            }))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CommandTimeout(
                        f"No response to {action!r} for session {session_id!r} within "
                        f"{timeout}s — it may not be running anywhere right now."
                    )
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=min(remaining, 1.0)
                )
                if message is None:
                    continue
                payload = json.loads(message["data"])
                if payload.get("ok"):
                    return payload.get("result")
                raise CommandError(payload.get("error") or "worker reported failure")
        finally:
            with _suppress_close_errors():
                await pubsub.unsubscribe(_resp_channel(command_id))
                await pubsub.aclose()

    # ------------------------------------------------------------------
    # Worker side: answer commands for one session until told to stop.
    # ------------------------------------------------------------------

    async def serve(
        self,
        session_id: str,
        handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
        stop_event: asyncio.Event,
    ) -> None:
        """Runs until `stop_event` is set. Each command is handled in its own
        task so a slow one (e.g. an outreach send) never blocks the next
        incoming command for the same session."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_cmd_channel(session_id))
        try:
            while not stop_event.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                try:
                    payload = json.loads(message["data"])
                    command_id, action = payload["command_id"], payload["action"]
                    args = payload.get("args") or {}
                except Exception:
                    log.exception("[%s] malformed command payload", session_id)
                    continue
                asyncio.create_task(self._handle_one(session_id, command_id, action, args, handler))
        finally:
            with _suppress_close_errors():
                await pubsub.unsubscribe(_cmd_channel(session_id))
                await pubsub.aclose()

    async def _handle_one(
        self, session_id: str, command_id: str, action: str, args: dict[str, Any],
        handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
    ) -> None:
        try:
            result = await handler(action, args)
            response = {"ok": True, "result": result}
        except Exception as exc:
            log.exception("[%s] command %r failed", session_id, action)
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            await self._redis.publish(_resp_channel(command_id), json.dumps(response, default=str))
        except Exception:
            log.exception("[%s] could not publish response for %r", session_id, action)

    # ------------------------------------------------------------------
    # Event fan-out: worker -> any panel process with that session's tab open.
    # ------------------------------------------------------------------

    async def publish_event(self, session_id: str, payload: dict[str, Any]) -> None:
        try:
            await self._redis.publish(_event_channel(session_id), json.dumps(payload, default=str))
        except Exception:
            # Best-effort — a dropped live-update must never break the
            # operation that triggered it (a message still got sent/recorded
            # even if nobody's panel tab happened to be open to see it live).
            log.warning("[%s] could not publish event %r", session_id, payload.get("type"))

    @asynccontextmanager
    async def subscribe_events(self, session_id: str) -> AsyncIterator[aioredis.client.PubSub]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_event_channel(session_id))
        try:
            yield pubsub
        finally:
            with _suppress_close_errors():
                await pubsub.unsubscribe(_event_channel(session_id))
                await pubsub.aclose()


def _suppress_close_errors():
    from contextlib import suppress
    return suppress(Exception)
