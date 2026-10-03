"""Linking a WhatsApp number from the panel: the panel's side of pairing.

The panel holds no WhatsApp connection. Pairing happens in the Node
`wa-gateway` service (Baileys), which the panel talks to only over the
Redis/Valkey bus, with this wire contract (v1):

- RPC on `cmd:@wa-gateway` (commands.CommandBus.dispatch):
  `pair` {session_id, pair_id, method: "qr"|"code", phone? (digits, for
  "code"), browser: [os, browser, version]} -> {started: true}, error_kind
  "busy" when the number already has a live lease or open socket;
  `pair_cancel` {pair_id} -> {cancelled: bool}.
- Events on pub/sub `wa:pair:<pair_id>`: {"type": "qr", "qr"} (rotates about
  every 20 s), {"type": "code", "code"}, {"type": "paired", "jid", "lid",
  "push_name"}, {"type": "failed", "reason"}.

`Pairings` keeps each pairing's latest state in memory (the panel is one
process, and a pairing lives minutes at most) for the browser to poll. The
channel is subscribed before `pair` is dispatched, so the first QR can't be
missed, and a background task per pairing follows it until it is paired,
fails, is cancelled or expires. What to do once a number is paired (store
its DeepSeek key, mark it active) is the caller's `on_paired`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import commands

log = logging.getLogger("wa_pairing")

GATEWAY = "@wa-gateway"
METHODS = ("qr", "code")
WAITING, QR, CODE = "waiting", "qr", "code"
PAIRED, FAILED, CANCELLED, EXPIRED = "paired", "failed", "cancelled", "expired"
FINISHED = (PAIRED, FAILED, CANCELLED, EXPIRED)

# A pairing nobody finished in this long is given up (the gateway is told to
# drop its socket). WhatsApp's own QR/code windows are shorter than this.
PAIR_TTL_SECONDS = 5 * 60
# At most this many pairings at once, across all numbers.
MAX_ACTIVE = 5
# A finished pairing stays readable this long, so a polling tab sees how it ended.
KEEP_FINISHED_SECONDS = 10 * 60
CANCEL_TIMEOUT = 5.0


def pair_channel(pair_id: str) -> str:
    return f"wa:pair:{pair_id}"


class TooManyPairings(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Pairing:
    pair_id: str
    session_id: str
    method: str
    started: float
    status: str = WAITING
    qr: Optional[str] = None
    code: Optional[str] = None
    error: Optional[str] = None
    jid: Optional[str] = None
    push_name: Optional[str] = None
    updated_at: str = field(default_factory=_utc_now)
    finished_at: Optional[float] = None
    # Held until the number is paired, so an abandoned pairing never stores
    # a key. Never part of public().
    deepseek_key: str = field(default="", repr=False)
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    def public(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "session_id": self.session_id,
            "method": self.method,
            "status": self.status,
            "qr": self.qr,
            "code": self.code,
            "error": self.error,
            "jid": self.jid,
            "push_name": self.push_name,
            "updated_at": self.updated_at,
        }


OnPaired = Callable[[Pairing, dict[str, Any]], Awaitable[None]]


class Pairings:
    """Every pairing this panel process is following, by pair_id."""

    def __init__(
        self, *, ttl: float = PAIR_TTL_SECONDS, max_active: int = MAX_ACTIVE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_active = max_active
        self._clock = clock
        self._items: dict[str, Pairing] = {}

    # ------------------------------------------------------------ reads

    def get(self, pair_id: str) -> Optional[Pairing]:
        self._sweep()
        return self._items.get(pair_id)

    def active(self) -> list[Pairing]:
        return [p for p in self._items.values() if not p.finished]

    # ---------------------------------------------------------- changes

    async def open(
        self, bus: commands.CommandBus, *, session_id: str, method: str,
        deepseek_key: str, on_paired: OnPaired,
    ) -> Pairing:
        """Subscribes to a new pairing's event channel and starts following
        it. Dispatching `pair` is the caller's next step; if that fails, it
        calls abandon(). An unfinished pairing of the same number is
        cancelled first (a closed browser tab leaves one behind)."""
        if method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        self._sweep()
        for old in [p for p in self.active() if p.session_id == session_id]:
            await self.cancel(bus, old.pair_id)
        if len(self.active()) >= self.max_active:
            raise TooManyPairings(
                f"{self.max_active} numbers are being linked already; finish or cancel one first."
            )
        pairing = Pairing(pair_id=secrets.token_hex(16), session_id=session_id, method=method,
                          started=self._clock(), deepseek_key=deepseek_key)
        stack = contextlib.AsyncExitStack()
        pubsub = await stack.enter_async_context(bus.subscribe_channel(pair_channel(pairing.pair_id)))
        self._items[pairing.pair_id] = pairing
        pairing.task = asyncio.create_task(self._follow(bus, pairing, pubsub, stack, on_paired))
        return pairing

    async def abandon(self, pair_id: str) -> None:
        """Forget a pairing whose `pair` command never got going."""
        pairing = self._items.pop(pair_id, None)
        if pairing is None:
            return
        self._finish(pairing, FAILED)
        await _stop(pairing.task)

    async def cancel(self, bus: commands.CommandBus, pair_id: str) -> Optional[Pairing]:
        """Stop following it and tell the gateway to drop the socket. None
        for an unknown pair_id; a finished pairing is returned unchanged."""
        pairing = self.get(pair_id)
        if pairing is None or pairing.finished:
            return pairing
        self._finish(pairing, CANCELLED)
        await _tell_gateway_to_cancel(bus, pair_id)
        return pairing

    async def close(self) -> None:
        """Stops every follower task (shutdown, tests). The gateway gives up
        its own sockets on its own timeout."""
        for pairing in list(self._items.values()):
            await _stop(pairing.task)
        self._items.clear()

    # --------------------------------------------------------- internals

    def _finish(self, pairing: Pairing, status: str, error: Optional[str] = None) -> None:
        pairing.status = status
        pairing.error = error
        pairing.qr = pairing.code = None
        pairing.deepseek_key = ""
        pairing.finished_at = self._clock()
        pairing.updated_at = _utc_now()

    def _sweep(self) -> None:
        now = self._clock()
        for pair_id, pairing in list(self._items.items()):
            if not pairing.finished and now - pairing.started >= self.ttl:
                # The follower normally does this itself; this covers one that died.
                self._finish(pairing, EXPIRED, "The number was not linked in time. Start again.")
            if pairing.finished and now - (pairing.finished_at or now) >= KEEP_FINISHED_SECONDS:
                del self._items[pair_id]

    async def _follow(
        self, bus: commands.CommandBus, pairing: Pairing, pubsub: Any,
        stack: contextlib.AsyncExitStack, on_paired: OnPaired,
    ) -> None:
        try:
            while not pairing.finished:
                if self._clock() - pairing.started >= self.ttl:
                    self._finish(pairing, EXPIRED, "The number was not linked in time. Start again.")
                    await _tell_gateway_to_cancel(bus, pairing.pair_id)
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None:
                    await self._apply(pairing, message.get("data"), on_paired)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] Lost the pairing channel (%s: %s).", pairing.session_id, type(exc).__name__, exc)
            if not pairing.finished:
                self._finish(pairing, FAILED, "Lost contact with the WhatsApp gateway. Start again.")
        finally:
            await stack.aclose()

    async def _apply(self, pairing: Pairing, data: Any, on_paired: OnPaired) -> None:
        try:
            event = json.loads(data) if isinstance(data, (str, bytes)) else None
        except ValueError:
            event = None
        if not isinstance(event, dict) or pairing.finished:
            return
        kind = event.get("type")
        if kind == "qr" and event.get("qr"):
            pairing.status, pairing.qr, pairing.code = QR, str(event["qr"]), None
        elif kind == "code" and event.get("code"):
            pairing.status, pairing.code, pairing.qr = CODE, str(event["code"]), None
        elif kind == "failed":
            reason = str(event.get("reason") or "WhatsApp did not link the number.")
            log.info("[%s] Pairing failed: %s", pairing.session_id, reason)
            self._finish(pairing, FAILED, reason)
            return
        elif kind == "paired":
            try:
                await on_paired(pairing, event)
            except Exception as exc:
                log.exception("[%s] Paired, but could not be activated", pairing.session_id)
                self._finish(pairing, FAILED, f"Linked, but could not be switched on: {type(exc).__name__}: {exc}")
                return
            self._finish(pairing, PAIRED)
            pairing.jid = str(event.get("jid") or "") or None
            pairing.push_name = str(event.get("push_name") or "") or None
            return
        else:
            return
        pairing.updated_at = _utc_now()


async def _tell_gateway_to_cancel(bus: commands.CommandBus, pair_id: str) -> None:
    try:
        await bus.dispatch(GATEWAY, "pair_cancel", {"pair_id": pair_id}, timeout=CANCEL_TIMEOUT)
    except commands.CommandError as exc:  # a timeout too: no gateway, nothing to cancel
        log.info("pair_cancel for %s not confirmed: %s", pair_id, exc)


async def _stop(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
