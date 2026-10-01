"""Bookings, wired to one running account: what happens when a customer
asks for a time, the owner answers, a reminder falls due or a slot frees up.

The pieces it joins:

- booking_states: which change is allowed (a person confirms, never code)
- booking_store:  the tenant's bookings, hours, waitlist, reminders in Postgres
- availability:   whether a time is free, and what is free nearby
- bookings:       the words: owner messages and commands, prompt lines

The customer is reached through the normal reply flow: a change they need
to hear about is stored on the booking (`customer_notice`) or held here for
the chat's next reply (`pending_lines`), and the reply writer is told about
it (`reply_note`). The owner is reached by text from this same account, in
the chat set as `booking.provider`.

The LLM only extracts what the customer asked for (a time, a cancel, a yes
to a proposed time). Whether that time is free, what state the booking goes
to, and who is told what, is decided here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

import ai_responder
import audit
import availability
import booking_states as bs
import booking_store
import bookings
import google_calendar
import ics
import mailer
import vision

if TYPE_CHECKING:  # pragma: no cover
    from session_runtime import SessionRuntime

log = logging.getLogger(__name__)

# Where the owner is told things, for the log and audit.
VIA_CHAT = "chat"
VIA_PAGE = "page"
VIA_OWNER = "owner"
VIA_PANEL = "panel"
VIA_SYSTEM = "system"


@dataclass
class ReplyNote:
    """What the next reply in a chat must know about its bookings."""
    text: str = ""
    notice_ids: list[int] = field(default_factory=list)
    transient: list[str] = field(default_factory=list)
    reminder: Optional[tuple[int, int, datetime]] = None

    @property
    def has_news(self) -> bool:
        return bool(self.notice_ids or self.transient)


class BookingFlow:
    PROVIDER_RETRY_SECONDS = 300
    RESUBMIT_SECONDS = 300

    def __init__(self, runtime: "SessionRuntime") -> None:
        self.rt = runtime
        self.scan_tasks: dict[int, asyncio.Task] = {}
        # News for a chat's next reply that is not a stored notice: "that
        # time is taken, offer these", a reminder, a waitlist offer.
        self.pending_lines: dict[int, list[str]] = {}
        # chat -> (booking id, minutes before, starts_at) of a reminder that
        # the next reply delivers.
        self.reminders_out: dict[int, tuple[int, int, datetime]] = {}
        # chat -> the last time asked for that was not free, for a
        # "put me on the waitlist" that names no range.
        self.last_unavailable: dict[int, tuple[datetime, datetime]] = {}
        self._provider: tuple[str, Optional[int], float] = ("", None, 0.0)
        self._calendar: Optional[google_calendar.GoogleCalendar] = None
        self._calendar_key: tuple[str, str] = ("", "")
        self._submit_attempts: dict[int, float] = {}

    # ------------------------------------------------------------ basics

    @property
    def store(self) -> booking_store.BookingStore:
        return self.rt.booking_store

    @property
    def settings(self) -> dict[str, Any]:
        return self.rt.config["booking"]

    def enabled(self) -> bool:
        return bool(self.settings.get("enabled"))

    @property
    def tz(self) -> str:
        return self.rt.config["timezone"]

    def now(self) -> datetime:
        return self.rt.utcnow()

    def local_today(self) -> date:
        return self.now().astimezone(bookings.tzinfo_for(self.tz)).date()

    def limits(self) -> availability.Limits:
        return availability.Limits(
            min_notice_minutes=self.settings["min_notice_minutes"],
            max_days_ahead=self.settings["max_days_ahead"],
            closed_dates=frozenset(date.fromisoformat(d) for d in self.settings["closed_dates"]),
        )

    def buffer_of(self, booking: dict[str, Any]) -> timedelta:
        return booking["blocked_until"] - booking["ends_at"]

    async def check(self, start: datetime, end: datetime, *, exclude_id: Optional[int] = None,
                    hours: bool = True) -> availability.SlotCheck:
        """Is [start, end) bookable? `hours=False` is for a time a person on
        the business side chose: only overlaps and the past are refused."""
        busy = await self.store.busy(start - timedelta(days=1), end + timedelta(days=1), exclude_id=exclude_id)
        if not hours:
            return availability.check_slot(start, end, rules=[], busy=busy, tz=self.tz, now=self.now(),
                                           limits=availability.Limits(max_days_ahead=3650))
        return availability.check_slot(start, end, rules=await self.store.rules(), busy=busy, tz=self.tz,
                                       now=self.now(), limits=self.limits())

    async def alternatives(self, start: datetime, minutes: int, *, exclude_id: Optional[int] = None) -> list[datetime]:
        count = self.settings["offer_alternatives"]
        if not count:
            return []
        busy = await self.store.busy(start - timedelta(days=1), start + timedelta(days=9), exclude_id=exclude_id)
        return availability.suggest_near(
            start, minutes, rules=await self.store.rules(), busy=busy, tz=self.tz, now=self.now(),
            limits=self.limits(), count=count,
        )

    def page_url(self, booking: dict[str, Any]) -> str:
        base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        return f"{base}/b/{booking['customer_token']}" if base else ""

    # ------------------------------------------------- customer messages

    async def on_customer_message(self, chat_id: int, text: str) -> None:
        """Every text a customer sends while bookings are on, before the
        reply is drafted (the draft waits for this to finish)."""
        answer = bookings.reminder_answer(text)
        if answer is not None:
            booking = await self._reminded_booking(chat_id)
            if booking is not None:
                if answer == "confirm":
                    await self.customer_action(booking, "confirm_attendance", via=VIA_CHAT)
                else:
                    await self.customer_action(booking, "cancel", via=VIA_CHAT)
                return
        self.schedule_scan(chat_id)

    async def _reminded_booking(self, chat_id: int) -> Optional[dict[str, Any]]:
        for booking in await self.store.for_chat(chat_id, (bs.CONFIRMED,)):
            last = await self.store.last_reminder(booking)
            if last is not None and last["sent_at"] is not None and booking["starts_at"] > self.now():
                return booking
        return None

    def schedule_scan(self, chat_id: int) -> None:
        self.cancel_scan(chat_id)
        self.scan_tasks[chat_id] = asyncio.create_task(self.scan(chat_id))

    def cancel_scan(self, chat_id: int) -> None:
        task = self.scan_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def wait_scan(self, chat_id: int) -> None:
        """Let a running scan finish so the reply knows its outcome."""
        task = self.scan_tasks.get(chat_id)
        if task is not None and not task.done():
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                pass

    async def scan(self, chat_id: int) -> None:
        try:
            if not self.enabled() or await self.rt.ai_limit_reason():
                return
            history = await self.rt.db.get_history_for_ai(chat_id, limit=self.settings["scan_messages"])
            if not history:
                return
            live = await self.store.for_chat(chat_id)
            arriving = self._arriving(live)
            if arriving is not None:
                async with self.rt.ai_gate():
                    arrived = await ai_responder.extract_arrival(
                        api_key=self.rt.deepseek_key, history=history, ai_config=self.rt.config["ai"],
                        client=self.rt.http_client, usage_sink=self.rt.usage_sink("arrival_check"),
                    )
                if arrived:
                    await self.on_arrival(arriving, via="text")
                    return
            entry = await self.store.waitlist_for_chat(chat_id)
            note = bookings.state_note(live)
            if entry is not None and entry["state"] == "offered":
                note = (note + "; " if note else "") + "a freed-up time was offered to them: " + bookings.describe_span(
                    entry["offered_starts_at"],
                    bookings.plus_minutes(entry["offered_starts_at"], self.settings["default_duration_minutes"]),
                    self.tz,
                )
            async with self.rt.ai_gate():
                found = await ai_responder.extract_booking(
                    api_key=self.rt.deepseek_key, history=history, tz_name=self.tz,
                    ai_config=self.rt.config["ai"], client=self.rt.http_client,
                    usage_sink=self.rt.usage_sink("booking_extract"), state_note=note,
                )
            if found:
                log.info("[%s]   booking intent in chat %s: %s", self.rt.session_id, chat_id, found.get("intent"))
                await self.act(chat_id, found, live, entry)
        except asyncio.CancelledError:
            raise
        except ai_responder.AIResponderError as exc:
            await self.rt.push_error(chat_id, f"Booking check failed: {exc}")
        except Exception as exc:
            log.exception("[%s] Booking check failed for chat %s", self.rt.session_id, chat_id)
            await self.rt.push_error(chat_id, f"Booking check failed: {type(exc).__name__}: {exc}")
        finally:
            if self.scan_tasks.get(chat_id) is asyncio.current_task():
                self.scan_tasks.pop(chat_id, None)

    async def act(self, chat_id: int, data: dict[str, Any], live: list[dict[str, Any]],
                  entry: Optional[dict[str, Any]]) -> None:
        intent = data["intent"]
        proposal = next((b for b in live if b.get("proposed_by") == bs.OWNER), None)
        request = next((b for b in live if b["state"] in (bs.REQUESTED, bs.PENDING)), None)
        confirmed = next((b for b in live if b["state"] == bs.CONFIRMED), None)
        offered = entry if entry is not None and entry["state"] == "offered" else None

        if intent == "book":
            slot = bookings.requested_slot(data, tz_name=self.tz,
                                           default_duration=self.settings["default_duration_minutes"])
            if slot is None:
                return
            start, end = slot
            if proposal is not None and proposal["proposed_starts_at"] == start:
                await self.customer_action(proposal, "accept_proposal", via=VIA_CHAT)
                return
            if offered is not None and offered["offered_starts_at"] == start:
                await self.take_waitlist_offer(chat_id, offered)
                return
            if any(b["starts_at"] == start for b in live):
                same = next(b for b in live if b["starts_at"] == start)
                if same["state"] == bs.REQUESTED:
                    await self.submit(same)
                return
            await self.request_time(chat_id, start, end, service=str(data.get("service") or ""),
                                    notes=str(data.get("notes") or ""), request=request, confirmed=confirmed)
        elif intent == "cancel":
            target = request or confirmed
            if target is not None:
                await self.customer_action(target, "cancel", via=VIA_CHAT)
        elif intent == "accept_proposal":
            if proposal is not None:
                await self.customer_action(proposal, "accept_proposal", via=VIA_CHAT)
            elif offered is not None:
                await self.take_waitlist_offer(chat_id, offered)
        elif intent == "decline_proposal":
            if proposal is not None:
                await self.customer_action(proposal, "reject_proposal", via=VIA_CHAT)
            elif offered is not None:
                await self.store.set_waitlist_state(offered["id"], "waiting", reason="they did not take the offered time")
                await self.offer_to_next(offered["offered_starts_at"], exclude_entry=offered["id"])
        elif intent == "confirm_attendance":
            if confirmed is not None:
                await self.customer_action(confirmed, "confirm_attendance", via=VIA_CHAT)
        elif intent == "waitlist":
            await self.join_waitlist(chat_id, data)

    async def request_time(
        self, chat_id: int, start: datetime, end: datetime, *, service: str, notes: str,
        request: Optional[dict[str, Any]], confirmed: Optional[dict[str, Any]],
    ) -> None:
        """The customer asked for a concrete time: a new request, a changed
        request, or a move of their confirmed booking."""
        current = request or confirmed
        result = await self.check(start, end, exclude_id=current["id"] if current else None)
        if not result.ok:
            await self.unavailable(chat_id, start, end, result.reason, exclude_id=current["id"] if current else None)
            return
        blocked = end + timedelta(minutes=result.buffer_minutes)
        now = self.now()
        try:
            if request is not None:
                change = bs.change_request(request, starts_at=start, ends_at=end, blocked_until=blocked, now=now,
                                           service=service or None)
                booking = await self.store.apply(request, change, actor=bs.CUSTOMER)
                await self.announce(booking, f"📅 Booking #{booking['number']}: the customer changed the time to "
                                             f"{bookings.describe_when(booking)}.")
                await self.submit(booking, changed=True)
            elif confirmed is not None:
                change = bs.propose(confirmed, actor=bs.CUSTOMER, starts_at=start, ends_at=end, now=now)
                booking = await self.store.apply(confirmed, change, actor=bs.CUSTOMER)
                await self.announce(booking, f"🔁 Booking #{booking['number']}: the customer asks to move it to "
                                             f"{bookings.describe_proposal(booking)}; asking the owner.")
                await self.tell_owner(booking, bookings.format_move_request(booking), remember=True)
            else:
                conversation = await self.rt.db.get_conversation(chat_id) or {}
                booking = await self.store.create(
                    chat_id=chat_id, customer_name=conversation.get("display_name") or "",
                    customer_username=conversation.get("username"), starts_at=start, ends_at=end,
                    buffer_minutes=result.buffer_minutes, tz=self.tz, service=service, notes=notes,
                )
                log.info("[%s] Booking #%s requested for %s.", self.rt.session_id, booking["number"],
                         bookings.describe_when(booking))
                await self.submit(booking)
        except booking_store.SlotTaken:
            await self.unavailable(chat_id, start, end, "conflict")
        except (bs.IllegalTransition, booking_store.StaleBooking) as exc:
            log.info("[%s]   booking request not applied: %s", self.rt.session_id, exc)

    async def unavailable(self, chat_id: int, start: datetime, end: datetime, reason: str,
                          exclude_id: Optional[int] = None) -> None:
        minutes = int((end - start).total_seconds() // 60)
        alternatives = []
        if reason in ("conflict", "outside_hours", "closed_day", "too_soon"):
            alternatives = await self.alternatives(start, minutes, exclude_id=exclude_id)
        waitlist = self.settings["waitlist_enabled"] and reason == "conflict"
        self.last_unavailable[chat_id] = (start, end)
        self.add_line(chat_id, bookings.unavailable_line(start, reason, alternatives, self.tz, waitlist))
        await self.rt.post_note(chat_id, f"📅 Asked for {availability.describe(start, self.tz)}: not available "
                                         f"({reason.replace('_', ' ')}).")

    def add_line(self, chat_id: int, line: str) -> None:
        lines = self.pending_lines.setdefault(chat_id, [])
        if line not in lines:
            lines.append(line)

    # ------------------------------------------------------- the owner

    async def provider_chat_id(self) -> Optional[int]:
        value = (self.settings.get("provider") or "").strip()
        rt = self.rt
        if not value or not rt.transport.ready:
            return None
        cached_value, cached_id, resolved_at = self._provider
        now = asyncio.get_running_loop().time()
        if cached_value == value and (cached_id is not None or now - resolved_at < self.PROVIDER_RETRY_SECONDS):
            return cached_id
        try:
            chat_id, peer = await rt.transport.resolve_owner(value)
            await rt.db.upsert_conversation(chat_id, peer.name, peer.username, peer.is_bot, peer.access_hash)
        except Exception as exc:
            log.warning("[%s] Cannot resolve the booking owner %r: %s", rt.session_id, value, type(exc).__name__)
            self._provider = (value, None, now)
            return None
        self._provider = (value, chat_id, now)
        return chat_id

    async def send_owner(self, text: str, *, reason: str) -> Optional[dict[str, Any]]:
        provider = await self.provider_chat_id()
        if provider is None:
            return None
        try:
            return await self.rt.send_as_me(provider, text, reason=reason)
        except Exception as exc:
            if not await self.rt.handle_send_failure(provider, exc):
                log.warning("[%s] Could not message the booking owner: %s", self.rt.session_id, type(exc).__name__)
            return None

    async def tell_owner(self, booking: dict[str, Any], text: str, *, remember: bool = False) -> None:
        """A message to the owner about a booking. `remember` keeps its id
        so a bare YES sent as a reply to it is unambiguous."""
        row = await self.send_owner(text, reason=f"booking #{booking['number']} update to the owner")
        if row is None:
            await self.rt.push_error(booking["chat_id"], f"Booking #{booking['number']}: could not reach the owner "
                                     "(check booking.provider). They can still answer from the panel.")
            return
        ref = self.owner_message_ref(row)
        if remember and any(ref.values()):
            await self.store.set_fields(booking["id"], **ref)

    def owner_message_ref(self, row: dict[str, Any]) -> dict[str, Any]:
        """Where the id of a message sent to the owner is kept: Telegram's
        integer id, or WhatsApp's string id in its own column."""
        if self.rt.transport.id_field == "wa_message_id":
            return {"provider_wa_message_id": row.get("wa_message_id")}
        return {"provider_message_id": row.get("telegram_id")}

    async def submit(self, booking: dict[str, Any], *, changed: bool = False) -> bool:
        """Put a requested booking to the owner. Left `requested` (and
        retried by tick) when the owner can't be reached."""
        self._submit_attempts[booking["id"]] = asyncio.get_running_loop().time()
        provider = await self.provider_chat_id()
        if provider is None:
            await self.rt.push_error(
                booking["chat_id"],
                f"Booking #{booking['number']} could not be sent: no owner is set in booking.provider, or it "
                "cannot be found. It is retried every few minutes, and can be answered from the panel.",
            )
            await self.broadcast(booking)
            return False
        try:
            row = await self.rt.send_as_me(provider, bookings.format_request(booking, changed=changed),
                                           reason="booking request to the owner")
        except Exception as exc:
            if not await self.rt.handle_send_failure(provider, exc):
                await self.rt.push_error(booking["chat_id"], f"Could not send booking #{booking['number']} to the "
                                         f"owner: {type(exc).__name__}: {exc}")
            return False
        try:
            booking = await self.store.apply(booking, bs.submitted(
                booking, provider_chat_id=provider, provider_message_id=row.get("telegram_id"),
            ), actor=bs.SYSTEM)
        except (bs.IllegalTransition, booking_store.StaleBooking):
            return False
        if row.get("wa_message_id"):
            await self.store.set_fields(booking["id"], provider_wa_message_id=row["wa_message_id"])
        self._submit_attempts.pop(booking["id"], None)
        await self.announce(booking, f"📅 Booking #{booking['number']} requested for {bookings.describe_when(booking)} "
                                     "— waiting for the owner.")
        await self.sync_calendar(None, booking)
        return True

    async def on_owner_message(self, chat_id: int, text: str, reply_to: Optional[int]) -> bool:
        """True when the owner's message was a booking command (then it gets
        no ordinary reply)."""
        cmd = bookings.parse_owner_reply(text, today=self.local_today())
        if cmd is None:
            return False
        reply = await self.owner_command(cmd, reply_to)
        if reply:
            try:
                await self.rt.send_as_me(chat_id, reply, reason="booking answer to the owner")
            except Exception as exc:
                await self.rt.handle_send_failure(chat_id, exc)
        return True

    async def owner_command(self, cmd: bookings.OwnerCommand, reply_to: Optional[int]) -> str:
        if cmd.kind == "list":
            return bookings.format_list(await self.store.awaiting_owner())
        booking = await self._target(cmd, reply_to)
        if booking is None:
            return bookings.format_which(cmd.kind if cmd.kind != "propose" else "yes")
        if cmd.error:
            return f"#{booking['number']}: {cmd.error}. " + bookings.ANSWER_HELP.format(n=booking["number"])
        now = self.now()
        try:
            if cmd.kind == "yes":
                if booking.get("proposed_by") == bs.CUSTOMER:
                    change = await self._accept(booking, bs.OWNER)
                else:
                    change = bs.confirm(booking, actor=bs.OWNER, now=now)
            elif cmd.kind == "no":
                if booking.get("proposed_by") == bs.CUSTOMER:
                    change = bs.reject_proposal(booking, actor=bs.OWNER, now=now)
                else:
                    change = bs.decline(booking, actor=bs.OWNER, now=now)
            elif cmd.kind == "propose":
                start = bookings.resolve_local(cmd, booking)
                end = bookings.plus_minutes(start, _minutes(booking))
                result = await self.check(start, end, exclude_id=booking["id"], hours=False)
                if not result.ok:
                    return f"#{booking['number']}: {availability.describe(start, self.tz)} is not possible " \
                           f"({result.reason.replace('_', ' ')})."
                change = bs.propose(booking, actor=bs.OWNER, starts_at=start, ends_at=end, now=now)
            elif cmd.kind == "cancel":
                change = bs.cancel(booking, actor=bs.OWNER, now=now, reason="cancelled by the owner")
            elif cmd.kind == "done":
                change = bs.mark_completed(booking, actor=bs.OWNER, now=now)
            else:
                change = bs.mark_no_show(booking, actor=bs.OWNER, now=now)
            updated = await self.change(booking, change, actor=bs.OWNER, via=VIA_OWNER)
        except bs.IllegalTransition as exc:
            return f"#{booking['number']}: {exc}."
        except booking_store.SlotTaken:
            return f"#{booking['number']}: that time now overlaps another booking."
        except booking_store.StaleBooking:
            return f"#{booking['number']} changed meanwhile; send LIST to see where things stand."
        return bookings.format_acknowledgement(updated, change.action)

    async def _target(self, cmd: bookings.OwnerCommand, reply_to: Optional[int]) -> Optional[dict[str, Any]]:
        if cmd.number is not None:
            return await self.store.by_number(cmd.number)
        awaiting = await self.store.awaiting_owner()
        if reply_to is not None:
            for booking in awaiting:
                if reply_to in (booking.get("provider_message_id"), booking.get("provider_wa_message_id")):
                    return booking
        if cmd.kind in ("yes", "no") and len(awaiting) == 1:
            return awaiting[0]
        return None

    async def _accept(self, booking: dict[str, Any], actor: str) -> bs.Change:
        start, end = booking["proposed_starts_at"], booking["proposed_ends_at"]
        result = await self.check(start, end, exclude_id=booking["id"], hours=False)
        if not result.ok:
            raise booking_store.SlotTaken(result.reason)
        return bs.accept_proposal(booking, actor=actor, now=self.now(), blocked_until=end + self.buffer_of(booking))

    # --------------------------------------------- customer / panel actions

    async def customer_action(self, booking: dict[str, Any], action: str, *, via: str) -> Optional[dict[str, Any]]:
        """Something the customer did: in the chat, or on their booking page."""
        now = self.now()
        try:
            if action == "cancel":
                change = bs.cancel(booking, actor=bs.CUSTOMER, now=now, reason="cancelled by the customer")
            elif action == "confirm_attendance":
                change = bs.confirm_attendance(booking, now=now)
            elif action == "accept_proposal":
                change = await self._accept(booking, bs.CUSTOMER)
            elif action == "reject_proposal":
                change = bs.reject_proposal(booking, actor=bs.CUSTOMER, now=now)
            else:
                raise ValueError(f"unknown customer action {action!r}")
            return await self.change(booking, change, actor=bs.CUSTOMER, via=via)
        except booking_store.SlotTaken:
            if action == "accept_proposal":
                self.add_line(booking["chat_id"], "NEWS TO PASS ON IN THIS REPLY: the proposed time has just been "
                              "taken by someone else. Apologise and ask what other time would suit them.")
            return None
        except (bs.IllegalTransition, booking_store.StaleBooking) as exc:
            log.info("[%s]   customer %s on #%s not applied: %s", self.rt.session_id, action, booking["number"], exc)
            if via == VIA_PAGE:
                raise
            return None

    async def admin_action(self, booking_id: int, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """A person in the panel. Raises (IllegalTransition, SlotTaken, ...)
        for the panel to show."""
        booking = await self.store.get(booking_id)
        now = self.now()
        if action == "resend":
            if booking["state"] != bs.REQUESTED:
                raise bs.IllegalTransition(f"booking #{booking['number']} is {booking['state']}, not waiting to be sent")
            await self.submit(booking)
            return booking_store.public(await self.store.get(booking_id))
        if action in ("propose", "reschedule"):
            start = datetime.fromisoformat(str(args["starts_at"]))
            if start.tzinfo is None:
                start = start.replace(tzinfo=bookings.tzinfo_for(self.tz))
            minutes = int(args.get("minutes") or _minutes(booking))
            end = bookings.plus_minutes(start, minutes)
            result = await self.check(start, end, exclude_id=booking["id"], hours=False)
            if not result.ok:
                raise booking_store.SlotTaken(f"{availability.describe(start, self.tz)} is not possible "
                                              f"({result.reason.replace('_', ' ')})")
            if action == "propose":
                change = bs.propose(booking, actor=bs.ADMIN, starts_at=start, ends_at=end, now=now)
            else:
                change = bs.reschedule(booking, actor=bs.ADMIN, starts_at=start, ends_at=end,
                                       blocked_until=end + self.buffer_of(booking), now=now)
        elif action == "confirm":
            change = (await self._accept(booking, bs.ADMIN) if booking.get("proposed_by") == bs.CUSTOMER
                      else bs.confirm(booking, actor=bs.ADMIN, now=now))
        elif action == "decline":
            change = (bs.reject_proposal(booking, actor=bs.ADMIN, now=now) if booking.get("proposed_by") == bs.CUSTOMER
                      else bs.decline(booking, actor=bs.ADMIN, now=now))
        elif action == "cancel":
            change = bs.cancel(booking, actor=bs.ADMIN, now=now, reason=str(args.get("reason") or "cancelled in the panel"))
        elif action == "complete":
            change = bs.mark_completed(booking, actor=bs.ADMIN, now=now)
        elif action == "no_show":
            change = bs.mark_no_show(booking, actor=bs.ADMIN, now=now)
        else:
            raise ValueError(f"unknown booking action {action!r}")
        updated = await self.change(booking, change, actor=bs.ADMIN, via=VIA_PANEL)
        if self.settings.get("provider"):
            await self.send_owner(bookings.format_acknowledgement(updated, change.action) + " (from the panel)",
                                  reason="panel booking change passed on to the owner")
        return booking_store.public(updated)

    # ------------------------------------------------ after every change

    async def change(self, booking: dict[str, Any], change: bs.Change, *, actor: str, via: str) -> dict[str, Any]:
        updated = await self.store.apply(booking, change, actor=actor)
        await self.after_change(booking, updated, change, actor=actor, via=via)
        return updated

    async def after_change(self, before: dict[str, Any], after: dict[str, Any], change: bs.Change, *,
                           actor: str, via: str) -> None:
        n = after["number"]
        notes = {
            "confirm": f"✅ Booking #{n} for {bookings.describe_when(after)} confirmed by the {actor}.",
            "accept_proposal": f"✅ Booking #{n} confirmed for {bookings.describe_when(after)} (proposed time taken).",
            "reschedule": f"🔁 Booking #{n} moved to {bookings.describe_when(after)}.",
            "decline": f"❌ Booking #{n} for {bookings.describe_when(after)} declined by the {actor}.",
            "reject_proposal": f"↩️ Booking #{n}: the proposed time was turned down by the {actor}.",
            "propose": (f"🕑 Booking #{n}: the {actor} proposed {bookings.describe_proposal(after)}."
                        if after.get("proposed_starts_at") else ""),
            "cancel": f"❌ Booking #{n} for {bookings.describe_when(after)} cancelled by the {actor}.",
            "expire": f"⌛ Booking #{n} for {bookings.describe_when(after)} lapsed: nobody answered before it started.",
            "mark_completed": f"Booking #{n} marked as done.",
            "mark_no_show": f"Booking #{n} marked as missed.",
            "confirm_attendance": f"👍 Booking #{n}: the customer confirmed they are coming.",
        }
        if notes.get(change.action):
            await self.announce(after, notes[change.action])
        else:
            await self.broadcast(after)

        if actor == bs.CUSTOMER:
            owner_news = {
                "cancel": "cancelled by the customer.",
                "accept_proposal": "the customer took the time you proposed — confirmed.",
                "reschedule": "the customer took the new time — moved.",
                "reject_proposal": "the customer turned down the time you proposed." + (
                    " The booking is cancelled." if after["state"] == bs.CANCELLED else " It stays as it was."),
            }.get(change.action)
            if owner_news:
                await self.tell_owner(after, bookings.format_owner_update(after, owner_news))
        elif change.action == "expire":
            await self.tell_owner(after, bookings.format_owner_update(after, "lapsed — it was not answered in time."))

        await self.sync_calendar(before, after)
        await self.email_record(before, after, change)

        moved = after["starts_at"] != before["starts_at"]
        freed = before["state"] in bs.LIVE and after["state"] not in bs.LIVE
        if (freed or moved) and before["starts_at"] > self.now():
            await self.offer_to_next(before["starts_at"])

        # The customer hears about it in their next reply. When the change
        # did not come from a message of theirs, start that reply now.
        if after.get("customer_notice") and via != VIA_CHAT:
            await self.nudge(after["chat_id"])

    async def announce(self, booking: dict[str, Any], note: str) -> None:
        await self.rt.post_note(booking["chat_id"], note)
        await self.broadcast(booking)

    async def broadcast(self, booking: dict[str, Any]) -> None:
        await self.rt.hub.broadcast({"type": "booking", "booking": booking_store.public(booking)})

    async def nudge(self, chat_id: int) -> None:
        if self.rt.paused():
            return
        conversation = await self.rt.db.get_conversation(chat_id)
        if conversation is None or self.rt.silenced(conversation):
            return
        self.rt.schedule_draft(chat_id)

    # --------------------------------------------------- the reply writer

    async def reply_note(self, chat_id: int) -> ReplyNote:
        if not self.enabled():
            return ReplyNote()
        lines: list[str] = []
        with_notice = await self.store.with_notice(chat_id)
        for booking in with_notice:
            lines.append(bookings.notice_line(booking))
        told = {b["id"] for b in with_notice}
        for booking in await self.store.for_chat(chat_id):
            if booking["id"] not in told:
                lines.append(bookings.standing_line(booking))
        transient = list(self.pending_lines.get(chat_id, []))
        lines.extend(transient)
        return ReplyNote(
            text=bookings.context_for_reply(lines), notice_ids=sorted(told), transient=transient,
            reminder=self.reminders_out.get(chat_id),
        )

    async def delivered(self, chat_id: int, note: ReplyNote) -> None:
        """The reply carrying `note` was sent or saved for approval."""
        for booking_id in note.notice_ids:
            await self.store.set_fields(booking_id, customer_notice=None)
        remaining = [line for line in self.pending_lines.get(chat_id, []) if line not in note.transient]
        if remaining:
            self.pending_lines[chat_id] = remaining
        else:
            self.pending_lines.pop(chat_id, None)
        if note.reminder is not None and self.reminders_out.get(chat_id) == note.reminder:
            booking_id, minutes, starts_at = self.reminders_out.pop(chat_id)
            booking = await self.store.get(booking_id)
            if booking["starts_at"] == starts_at:
                await self.store.mark_reminder_sent(booking, minutes)
            await self.rt.write_audit(audit.REMINDER_SENT, reason=f"{minutes} min before booking #{booking['number']}",
                                      payload={"booking": booking["number"], "minutes_before": minutes})

    # ------------------------------------------------------------ arrival

    def _arriving(self, live: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        now = self.now()
        for booking in live:
            if (booking["state"] == bs.CONFIRMED and not booking.get("instructions_sent_at")
                    and bookings.arrival_window(booking, now)):
                return booking
        return None

    def photo_check_ready(self) -> bool:
        url, key = vision.endpoint_from_env()
        return bool(self.settings["arrival_photo_check"] and self.rt.config["vision"]["enabled"]
                    and self.rt.config["vision"]["model"] and url and key)

    async def on_arrival(self, booking: dict[str, Any], *, via: str) -> None:
        if via == "text" and self.settings["arrival_requires_photo"] and self.photo_check_ready() \
                and self.rt.media_library.references():
            await self.store.set_fields(booking["id"], arrived_at=self.now())
            self.add_line(booking["chat_id"], "NEWS TO PASS ON IN THIS REPLY: they say they have arrived. Ask them "
                          "to send a photo of the entrance they are at, so the way in can be sent. " + bookings.NO_DIRECTIONS_RULE)
            await self.announce(booking, f"🚪 Booking #{booking['number']}: the customer says they have arrived; "
                                         "waiting for their photo of the entrance.")
            return
        await self.send_instructions(booking)

    async def arrival_photo(self, chat_id: int, image: bytes) -> Optional[str]:
        """A photo from a customer who may be arriving. Returns the text to
        store for it, or None when it is not an arrival photo."""
        if not self.enabled() or not self.photo_check_ready():
            return None
        booking = self._arriving(await self.store.for_chat(chat_id))
        if booking is None:
            return None
        references = []
        for item in self.rt.media_library.references():
            path = self.rt.media_library.path(item["id"])
            if path is not None:
                references.append(path.read_bytes())
        if not references:
            await self.rt.push_error(chat_id, "Arrival photo check is on, but no media item is marked as the "
                                              "entrance reference.")
            return None
        url, key = vision.endpoint_from_env()
        async with self.rt.ai_gate():
            match = await vision.compare_to_reference(
                image, references, api_url=url, api_key=key, model=self.rt.config["vision"]["model"],
                client=self.rt.http_client, usage_sink=self.rt.usage_sink("arrival_photo"),
            )
        ok = match.same_place and match.confidence >= self.settings["arrival_photo_min_confidence"]
        booking = await self.store.set_fields(booking["id"], arrival_photo_match=ok, arrived_at=self.now())
        await self.rt.write_audit(audit.ARRIVAL_PHOTO_CHECKED, reason="matches" if ok else "does not match",
                                  payload={"booking": booking["number"], "same_place": match.same_place,
                                           "confidence": round(match.confidence, 2)})
        if ok:
            await self.send_instructions(booking)
            await self.tell_owner(booking, bookings.format_owner_update(
                booking, "has arrived (their photo matches the entrance); the way in was sent."))
            return "[photo: arrival photo, matches the entrance]"
        self.add_line(chat_id, "NEWS TO PASS ON IN THIS REPLY: the photo they sent does not look like the "
                      "entrance. Ask them politely to check they are at the right door and to send another photo. "
                      + bookings.NO_DIRECTIONS_RULE)
        await self.announce(booking, f"🚪 Booking #{booking['number']}: the arrival photo does not look like the "
                                     "entrance; the way in was not sent.")
        await self.tell_owner(booking, bookings.format_owner_update(
            booking, "sent an arrival photo that does not look like the entrance — check the chat."))
        return "[photo: arrival photo, does not look like the entrance]"

    async def send_instructions(self, booking: dict[str, Any]) -> None:
        text = (self.settings.get("arrival_instructions") or "").strip()
        rt, chat_id = self.rt, booking["chat_id"]
        if not booking.get("arrived_at"):
            booking = await self.store.set_fields(booking["id"], arrived_at=self.now())
        if not text:
            await self.announce(booking, f"🚪 Booking #{booking['number']}: the customer has arrived, but no "
                                         "arrival instructions are set (booking.arrival_instructions).")
            return
        rt.cancel_draft(chat_id)
        rt.sending_chats.add(chat_id)
        try:
            await rt.send_as_me(chat_id, text, typing=True, reason="arrival instructions")
        except Exception as exc:
            if not await rt.handle_send_failure(chat_id, exc):
                await rt.push_error(chat_id, f"Could not send the arrival instructions: {type(exc).__name__}: {exc}")
            return
        finally:
            rt.sending_chats.discard(chat_id)
        booking = await self.store.set_fields(booking["id"], instructions_sent_at=self.now())
        await self.announce(booking, f"🚪 Booking #{booking['number']}: the customer has arrived — entry instructions sent.")

    # ----------------------------------------------------------- waitlist

    async def join_waitlist(self, chat_id: int, data: dict[str, Any]) -> None:
        if not self.settings["waitlist_enabled"]:
            return
        start = bookings.parse_local(str(data.get("waitlist_from") or ""), self.tz)
        end = bookings.parse_local(str(data.get("waitlist_to") or ""), self.tz)
        if start is None or end is None or end <= start:
            last = self.last_unavailable.get(chat_id)
            if last is None:
                return
            zone = bookings.tzinfo_for(self.tz)
            day = last[0].astimezone(zone).date()
            start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        if end <= self.now():
            return
        conversation = await self.rt.db.get_conversation(chat_id) or {}
        entry = await self.store.add_waitlist(chat_id=chat_id, customer_name=conversation.get("display_name") or "",
                                              wanted_from=start, wanted_to=end, service=str(data.get("service") or ""))
        span = bookings.describe_span(entry["wanted_from"], entry["wanted_to"], self.tz)
        self.add_line(chat_id, f"NEWS TO PASS ON IN THIS REPLY: they are now on the waitlist for {span}; tell them "
                      "they will hear if a time frees up. Nothing is booked yet.")
        await self.rt.post_note(chat_id, f"⏳ Added to the waitlist for {span}.")
        await self.rt.hub.broadcast({"type": "waitlist"})

    async def offer_to_next(self, start: datetime, *, exclude_entry: Optional[int] = None) -> None:
        """A slot starting at `start` may be free now: offer it to the first
        person waiting for it."""
        if not self.settings["waitlist_enabled"] or start <= self.now():
            return
        end = bookings.plus_minutes(start, self.settings["default_duration_minutes"])
        if not (await self.check(start, end)).ok:
            return
        entry = await self.store.first_in_line(start, end)
        if entry is None or entry["id"] == exclude_entry:
            return
        entry = await self.store.set_waitlist_state(entry["id"], "offered", offered_starts_at=start,
                                                    reason="a time freed up")
        self.add_line(entry["chat_id"], bookings.waitlist_offer_line(start, end, self.tz))
        await self.rt.post_note(entry["chat_id"], f"⏳ Offered the freed-up time {availability.describe(start, self.tz)} "
                                                  "from the waitlist.")
        await self.rt.hub.broadcast({"type": "waitlist"})
        await self.nudge(entry["chat_id"])

    async def take_waitlist_offer(self, chat_id: int, entry: dict[str, Any]) -> None:
        start = entry["offered_starts_at"]
        end = bookings.plus_minutes(start, self.settings["default_duration_minutes"])
        await self.store.set_waitlist_state(entry["id"], "booked", reason="took the offered time")
        await self.request_time(chat_id, start, end, service=entry.get("service") or "", notes="from the waitlist",
                                request=None, confirmed=None)

    # ------------------------------------------------------ the scheduler

    async def tick(self) -> None:
        """Run by the scheduler (scheduler.py) about once a minute. Every
        step is idempotent: running it twice, or on two workers, changes
        nothing the second time."""
        if not self.enabled():
            return
        now = self.now()
        loop_now = asyncio.get_running_loop().time()

        # One booking whose step fails (its calendar, a bad row) is logged
        # and tried again next tick; it doesn't keep the others, or the
        # later steps, from running.
        async def guarded(what: str, step) -> None:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] Booking tick: %s failed; retried next tick", self.rt.session_id, what)

        async def expire(booking: dict[str, Any]) -> None:
            try:
                await self.change(booking, bs.expire(booking, now=now), actor=bs.SYSTEM, via=VIA_SYSTEM)
            except (bs.IllegalTransition, booking_store.StaleBooking):
                pass

        for booking in await self.store.between(now - timedelta(days=7), now, states=[bs.REQUESTED, bs.PENDING]):
            if booking["starts_at"] <= now:
                await guarded(f"expiring #{booking['number']}", lambda b=booking: expire(b))

        async def remind(booking: dict[str, Any], reminder: dict[str, Any], skipped: list[int]) -> None:
            for minutes in skipped:
                await self.store.claim_reminder(booking, minutes)
            # Claimed before anything is sent: at most once, even across a
            # crash right after this line (then it is simply not sent).
            if not await self.store.claim_reminder(booking, reminder["minutes_before"]):
                return
            chat_id = booking["chat_id"]
            conversation = await self.rt.db.get_conversation(chat_id)
            why = (f"sending is off ({self.rt.off_reason})" if self.rt.paused() else
                   "unknown chat" if conversation is None else self.rt.silenced(conversation))
            if why:
                # Claimed above, so it is skipped for good: nothing is
                # replayed later.
                await self.rt.post_note(chat_id, f"⏰ Reminder for booking #{booking['number']} not sent: {why}.")
                return
            self.add_line(chat_id, bookings.reminder_line(booking, reminder.get("instruction", ""), now,
                                                          self.page_url(booking)))
            self.reminders_out[chat_id] = (booking["id"], reminder["minutes_before"], booking["starts_at"])
            await self.rt.post_note(chat_id, f"⏰ Booking #{booking['number']} is "
                                             f"{bookings.describe_until(booking['starts_at'], now)}: sending the reminder.")
            self.rt.schedule_draft(chat_id)

        for booking, reminder, skipped in await self.store.due_reminders(now, self.settings["reminders"]):
            await guarded(f"reminder for #{booking['number']}",
                          lambda b=booking, r=reminder, s=skipped: remind(b, r, s))

        async def lapse(entry: dict[str, Any]) -> None:
            await self.store.set_waitlist_state(entry["id"], "expired", reason="the offer was not taken in time")
            if entry["offered_starts_at"] > now:
                await self.offer_to_next(entry["offered_starts_at"], exclude_entry=entry["id"])

        for entry in await self.store.expired_offers(now, self.settings["waitlist_offer_hours"]):
            await guarded(f"waitlist offer {entry['id']}", lambda e=entry: lapse(e))

        unsent = [] if self.rt.paused() else await self.store.unsent()
        for booking in unsent:
            last = self._submit_attempts.get(booking["id"], 0.0)
            if loop_now - last >= self.RESUBMIT_SECONDS:
                await guarded(f"resubmitting #{booking['number']}", lambda b=booking: self.submit(b))
        # Attempts for bookings that are no longer waiting to be sent.
        waiting = {b["id"] for b in unsent}
        if not self.rt.paused():
            for booking_id in list(self._submit_attempts):
                if booking_id not in waiting:
                    self._submit_attempts.pop(booking_id, None)

    def prune_memory(self) -> None:
        """Drop per-chat hints that no longer matter (see
        SessionRuntime.prune_memory)."""
        cutoff = self.now() - timedelta(days=1)
        for chat_id, (start, _end) in list(self.last_unavailable.items()):
            if start < cutoff:
                self.last_unavailable.pop(chat_id, None)
        loop_now = asyncio.get_running_loop().time()
        for booking_id, at in list(self._submit_attempts.items()):
            if loop_now - at >= self.RESUBMIT_SECONDS:
                self._submit_attempts.pop(booking_id, None)

    # ------------------------------------------------ calendar and e-mail

    def calendar_client(self) -> Optional[google_calendar.GoogleCalendar]:
        calendar_id = self.settings.get("google_calendar_id") or ""
        key_file = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
        if not key_file and getattr(self.rt, "data_dir", None) is not None:
            default = self.rt.data_dir / "google-service-account.json"
            key_file = str(default) if default.exists() else ""
        key = (calendar_id, key_file)
        if key == self._calendar_key:
            return self._calendar
        self._calendar_key, self._calendar = key, None
        if not calendar_id or not key_file:
            return None
        try:
            self._calendar = google_calendar.GoogleCalendar(key_file, calendar_id, client=self.rt.http_client)
        except google_calendar.CalendarError as exc:
            log.error("[%s] Google Calendar disabled: %s", self.rt.session_id, exc)
        return self._calendar

    async def sync_calendar(self, before: Optional[dict[str, Any]], after: dict[str, Any]) -> None:
        """Mirror a booking into Google Calendar: tentative while pending,
        confirmed once confirmed, gone when cancelled. Failures are shown in
        the chat and never stop the booking itself."""
        calendar = self.calendar_client()
        if calendar is None:
            return
        summary = f"#{after['number']} {after.get('service') or 'Booking'} — {after.get('customer_name') or 'customer'}"
        event_id = after.get("calendar_event_id")
        try:
            if after["state"] not in bs.LIVE and after["state"] != bs.COMPLETED:
                if event_id and after["state"] != bs.NO_SHOW:
                    await calendar.delete_event(event_id)
                    await self.store.set_fields(after["id"], calendar_event_id=None)
                return
            moved = before is not None and before["starts_at"] != after["starts_at"]
            if event_id and moved:
                await calendar.delete_event(event_id)
                event_id = None
            if not event_id:
                event_id = await calendar.create_event(
                    summary=("[UNCONFIRMED] " if after["state"] != bs.CONFIRMED else "") + summary,
                    description=f"Customer: {after.get('customer_name') or ''}\nBooking #{after['number']} via Telegram",
                    start=after["starts_at"], end=after["ends_at"], tz_name=after["tz"],
                    tentative=after["state"] != bs.CONFIRMED,
                )
                await self.store.set_fields(after["id"], calendar_event_id=event_id)
            elif after["state"] == bs.CONFIRMED and (before is None or before["state"] != bs.CONFIRMED):
                await calendar.confirm_event(event_id, summary)
        except Exception as exc:
            await self.rt.push_error(after["chat_id"], f"Google Calendar: {exc}")

    async def email_record(self, before: dict[str, Any], after: dict[str, Any], change: bs.Change) -> None:
        """The e-mail record for the owner, when SMTP and owner_email are set."""
        subjects = {
            "confirm": "confirmed", "accept_proposal": "confirmed", "reschedule": "moved",
            "cancel": "cancelled", "decline": "declined", "expire": "lapsed",
        }
        what = subjects.get(change.action)
        to = (self.settings.get("owner_email") or "").strip()
        if what is None or not to:
            return
        try:
            settings = mailer.settings_from_env()
        except mailer.MailError as exc:
            await self.rt.push_error(after["chat_id"], f"E-mail not sent: {exc}")
            return
        if settings is None:
            return
        name = (self.rt.bundle.tenant["name"] if self.rt.bundle is not None else "") or "Bookings"
        when = bookings.describe_when(after)
        lines = [
            f"Booking #{after['number']} {what}.",
            "",
            f"When: {when} ({after['tz']})",
            f"Customer: {after.get('customer_name') or ''}" + (
                f" (@{after['customer_username']})" if after.get("customer_username") else ""),
        ]
        if after.get("service"):
            lines.append(f"What: {after['service']}")
        if change.action == "reschedule":
            lines.append(f"Was: {bookings.describe_when(before)}")
        if after.get("cancel_reason") and after["state"] == bs.CANCELLED:
            lines.append(f"Reason: {after['cancel_reason']}")
        try:
            attachment = ics.event_file(after, name=name, host=os.getenv("PUBLIC_HOST") or "localhost", now=self.now())
            await mailer.send(settings, to=to, subject=f"{name}: booking #{after['number']} {what} — {when}",
                              body="\n".join(lines) + "\n", ics=attachment)
        except (mailer.MailError, ValueError) as exc:
            await self.rt.push_error(after["chat_id"], f"E-mail record not sent: {exc}")


def _minutes(booking: dict[str, Any]) -> int:
    """A booking's length in real minutes (both ends are UTC from the database)."""
    return int((booking["ends_at"] - booking["starts_at"]).total_seconds() // 60)
