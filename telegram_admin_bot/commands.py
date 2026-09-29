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

When Redis (Valkey) is down or unreachable:
- `dispatch()` raises `BusUnavailable`, a `CommandError`, so every caller
  that already copes with "the worker failed" copes with this too, and it
  never waits much longer than its own timeout.
- `serve()` does not end: it logs, waits (backing off up to
  SERVE_BACKOFF_MAX_SECONDS) and subscribes again, so a worker gets its
  commands again by itself once Redis is back.
- `publish_event()` gives an event up after PUBLISH_TIMEOUT_SECONDS at
  most; message handling never waits on it.
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
# On top of a dispatch's own timeout: the most it may spend reaching Redis
# at all (subscribe, publish) before the command is given up.
DISPATCH_GRACE_SECONDS = 5.0
# A live event is a nice-to-have; nothing waits on one longer than this.
PUBLISH_TIMEOUT_SECONDS = 2.0
# serve() after Redis went away: the first wait, doubling up to the maximum.
SERVE_BACKOFF_SECONDS = 1.0
SERVE_BACKOFF_MAX_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 5.0


class CommandError(RuntimeError):
    """The worker that handled this command reported a failure."""


class CommandTimeout(CommandError):
    """Nobody answered in time — almost always because no worker currently
    holds this session's lease, e.g. it needs login or crashed and hasn't
    been reassigned yet."""


class BusUnavailable(CommandError):
    """Redis (Valkey) itself could not be reached: the command was not
    delivered, or its answer was lost."""


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
        # Command handlers running in the background (serve), referenced so
        # they are not garbage-collected half way.
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    async def connect(cls, redis_url: str) -> "CommandBus":
        # A connect that hangs (host gone, packets dropped) fails in seconds
        # rather than the OS's minutes, and the periodic health check
        # notices a subscription whose connection died silently.
        return cls(aioredis.from_url(
            redis_url, decode_responses=True, socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
            socket_keepalive=True, health_check_interval=30,
        ))

    async def close(self) -> None:
        await self._redis.aclose()

    # ------------------------------------------------------------------
    # Panel side: ask whichever worker owns this session to do something.
    # ------------------------------------------------------------------

    async def dispatch(
        self, session_id: str, action: str, args: Optional[dict[str, Any]] = None,
        *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Any:
        """Run `action` on the worker holding this session. Raises
        CommandTimeout (nobody answered), BusUnavailable (Redis could not be
        reached) or CommandError (the worker reported a failure), and never
        takes much longer than `timeout`."""
        try:
            return await asyncio.wait_for(
                self._dispatch(session_id, action, args, timeout), timeout + DISPATCH_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            raise CommandTimeout(
                f"No response to {action!r} for session {session_id!r} within {timeout}s "
                "(the command bus did not respond)."
            ) from None
        except CommandError:
            raise
        except (aioredis.RedisError, OSError) as exc:
            raise BusUnavailable(
                f"The command bus is unreachable, so {action!r} did not reach session {session_id!r} "
                f"({type(exc).__name__}: {exc})."
            ) from exc

    async def _dispatch(self, session_id: str, action: str, args: Optional[dict[str, Any]], timeout: float) -> Any:
        command_id = uuid.uuid4().hex
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(_resp_channel(command_id))
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
            await _close_pubsub(pubsub, _resp_channel(command_id))

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
        incoming command for the same session.

        Losing Redis does not end it: it waits (backing off) and subscribes
        again, so commands reach this worker again once Redis is back.
        Commands published meanwhile are lost; their senders time out, which
        every caller already handles."""
        backoff = SERVE_BACKOFF_SECONDS
        failures = 0
        while not stop_event.is_set():
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(_cmd_channel(session_id))
                if failures:
                    log.info("[%s] Command bus reachable again after %d failed attempt(s).", session_id, failures)
                failures, backoff = 0, SERVE_BACKOFF_SECONDS
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
                    self._spawn(self._handle_one(session_id, command_id, action, args, handler))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log.warning("[%s] Command bus connection lost (%s: %s); subscribing again in %.0fs.",
                            session_id, type(exc).__name__, exc, backoff)
            finally:
                await _close_pubsub(pubsub, _cmd_channel(session_id))
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, SERVE_BACKOFF_MAX_SECONDS)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            await asyncio.wait_for(
                self._redis.publish(_event_channel(session_id), json.dumps(payload, default=str)),
                PUBLISH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
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


async def _close_pubsub(pubsub: Any, channel: str) -> None:
    """Unsubscribe and give the connection back, each step on its own and
    bounded, so a dead connection can neither raise nor hang here."""
    with _suppress_close_errors():
        await asyncio.wait_for(pubsub.unsubscribe(channel), CONNECT_TIMEOUT_SECONDS)
    with _suppress_close_errors():
        await asyncio.wait_for(pubsub.aclose(), CONNECT_TIMEOUT_SECONDS)
