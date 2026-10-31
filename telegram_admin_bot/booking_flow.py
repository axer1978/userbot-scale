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

The owner ("the booker": the girl, or whoever books for her) answers with a
command or in her own words. Her words are read by the LLM into what they
mean for each booking ("yes, but tell him to bring cash", "yes 3, no 4");
what she wants him told waits on the booking (`booker_news`) for his next
reply, the extras she refuses are never offered to him again, and while he
answers a question she asked him, his messages go on to her.

The LLM only extracts what the customer asked for (a time, a cancel, a yes
to a proposed time) and what the owner's words say. Whether that time is
free, what state the booking goes to, and who is told what, is decided here.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import logging
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Optional, Sequence

import ai_responder
import audit
import availability
import booker_texts
import booking_states as bs
import booking_store
import bookings
import google_calendar
import ics
import mailer
import media
import rules
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

# A live booking starting within this long of now already answers "now?".
ASAP_WINDOW = timedelta(minutes=60)
# An outcall is under way (his messages go on to the owner) until this long
# after its end.
OUTCALL_AFTER_END = timedelta(minutes=30)
# Reading the owner's own words: the confirmed bookings she may still talk
# about (cancel one coming up, mark one that just ended), and how much of
# her chat the model sees (with the time of each message).
OWNER_LOOKBACK = timedelta(hours=12)
OWNER_LOOKAHEAD = timedelta(days=7)
OWNER_HISTORY = 30
# She asked him something: what he writes goes on to her for this long, or
# until she next acts on that booking, or it is over.
ASKED_FOR = timedelta(hours=3)
# After the bot asked her something (which booking? yes or no?), her next
# message within this long is read together with the one it asked about.
ANSWER_WINDOW = timedelta(minutes=30)
# Her words for him that no reply has carried after this long (his chat not
# answered: paused, taken over, the account off) are dropped, and she is
# told; so are words about a meeting that has ended since.
NEWS_FOR = timedelta(hours=24)
# She is told before they are dropped. While her chat can't be reached that
# is tried again this often (the admin is told the first time), and after
# DROP_GIVE_UP of trying they are dropped anyway.
DROP_RETRY = timedelta(minutes=30)
DROP_GIVE_UP = timedelta(hours=24)
# How many of the messages sent to her are remembered with their booking
# (a quote of one of them falls on that booking).
ABOUT_KEPT = 500
# Reading her words never holds her next messages (or a WhatsApp inbox)
# longer than this, waiting for a free AI slot included.
OWNER_READ_BUDGET = ai_responder.OWNER_READ_DEADLINE_SECONDS + 10
# Her commands that change a booking (and so end a question she had put to
# him about it).
CHANGES = ("yes", "no", "propose", "cancel", "done", "noshow", "ready", "in_taxi")
# A standing instruction of hers for every customer ("from now on cash
# only") becomes a proposed operator rule (rules.py): its source and author,
# and the prompt section it is filed under until the admin moves it.
BOOKER = rules.BOOKER
STANDING_SECTION = "booking"
# What his photo is stored as when it went on to her because she had asked
# him something (the photo itself is not looked at).
PHOTO_TO_HER = "[photo]"

# While one message of hers is carried out: the customers' chats whose
# reply waits until all of it is done (her words stored with the change),
# so each gets one reply that carries everything.
_held_nudges: contextvars.ContextVar[Optional[set[int]]] = contextvars.ContextVar("held_nudges", default=None)


@dataclass
class ReplyNote:
    """What the next reply in a chat must know about its bookings."""
    text: str = ""
    notice_ids: list[int] = field(default_factory=list)
    transient: list[str] = field(default_factory=list)
    reminder: Optional[tuple[int, int, datetime]] = None
    # Booking whose address (booking.address) goes out after this reply.
    address_for: Optional[int] = None
    # The owner's words for him this reply passes on: booking id -> the ids
    # of its booker_news items, cleared once the reply is out.
    her_words: dict[int, list[str]] = field(default_factory=dict)
    # The notice of each booking this reply tells, with the times it told
    # (booking id -> customer_notice, starts_at, ends_at, proposed_*): a newer
    # one set while it goes out is kept.
    notices: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def has_news(self) -> bool:
        return bool(self.notice_ids or self.transient or self.address_for or self.her_words)


@dataclass
class _Held:
    """A question the bot asked her about a booking, that her next message
    may answer: when, the id of the message that asked it, the bookings it
    named (several for "which one?"), and what of hers waits on it (her
    words, a length — repeated in it)."""
    at: datetime
    question_id: Optional[int]
    group: tuple[int, ...]
    words: Optional[bookings.OwnerCommand] = None
    had_words: bool = False


@dataclass
class OwnerOutcome:
    """One command of the owner's, carried out: her line for it, the booking
    it was about (as it is now), and whether it went through — only then do
    her words for him go on."""
    line: str
    booking: Optional[dict[str, Any]] = None
    went: bool = False
    # Her line asks her something about these bookings: her next message
    # answers it, and is read together with this one.
    asks: tuple[int, ...] = ()
    # The booking itself did not change (a length that is not free), but
    # her words for him go on all the same.
    words_go: bool = False


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
        # chat -> booking id: a booking just confirmed whose address is not
        # in the chat yet (normally it went with the first reply's prices):
        # sent right after the reply that tells them.
        self.address_due: dict[int, int] = {}
        # Chats whose latest message ("I'm here") was answered by the way
        # in itself: no reply is written after it.
        self.let_in: set[int] = set()
        # The owner's messages are carried out one at a time, in order: the
        # next one is read knowing what the last one did.
        self._owner_lock = asyncio.Lock()
        # The bot's questions to her that her next message may answer, by
        # booking number; set once the question is out (answer_owner).
        self._held: dict[int, _Held] = {}
        self._pending_held: list[tuple[tuple[int, ...], Optional[bookings.OwnerCommand], bool]] = []
        # Which booking each message sent to her is about, by its id (the
        # last ABOUT_KEPT): a quote of one, or her bare YES right after it,
        # falls on that booking. Not kept over a restart: a message the bot
        # can't place makes her bare YES / NO read, never guessed.
        self._about: "collections.OrderedDict[str, int]" = collections.OrderedDict()
        # The bookings her message being handled is about (answer_owner).
        self._answer_about: set[int] = set()
        # Booking id -> (first, last) failed try at telling her its old words
        # are dropped (tick): tried again after DROP_RETRY.
        self._drop_failed: dict[int, tuple[datetime, datetime]] = {}
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

    def her_name(self) -> str:
        """booking.name, the name the customer is told. Empty: none."""
        return str(self.settings.get("name") or "").strip()

    def texts(self) -> booker_texts.Texts:
        """What the bot says to the owner: her language
        (booking.booker_language) and the account's own main messages
        (booking.booker_messages)."""
        return booker_texts.Texts.of(self.settings)

    def owner_name(self) -> str:
        """Whose booking a message to the owner is about: booking.name, else
        the account's name."""
        return self.her_name() or (self.rt.bundle.tenant["name"] if self.rt.bundle is not None else "")

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
        self.let_in.discard(chat_id)
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
            except asyncio.CancelledError:
                # The scan was replaced by a newer message's: carry on. The
                # reply itself being cancelled (the way in answered "I'm
                # here", a newer message) must stop it.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
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
                    said = "\n".join(media.latest_from_them(history)).strip()
                    await self.on_arrival(arriving, via="text", said=said)
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
            now = self.now()
            if data.get("asap") is True:
                # "Now?" again, or "ok" after it, while a time around now is
                # already with the owner: the same request, not a new time
                # every five minutes.
                current = next((b for b in live if now - bookings.JUST_PASSED <= b["starts_at"]
                                <= now + ASAP_WINDOW), None)
                if current is not None:
                    if current["state"] == bs.REQUESTED:
                        await self.submit(current)
                    return
            slot = bookings.requested_slot(data, tz_name=self.tz,
                                           default_duration=self.settings["default_duration_minutes"], now=now)
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
            outcall = data.get("outcall") is True
            notes = str(data.get("notes") or "")
            if outcall and "outcall" not in notes.lower():
                notes = ("Outcall. " + notes).strip()
            await self.request_time(chat_id, start, end, service=str(data.get("service") or ""), notes=notes,
                                    extras=" ".join(str(data.get("extras") or "").split())[:300],
                                    request=request, confirmed=confirmed)
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
        request: Optional[dict[str, Any]], confirmed: Optional[dict[str, Any]], extras: str = "",
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
                                           service=service or None, extras=extras or None)
                booking = await self.store.apply(request, change, actor=bs.CUSTOMER)
                await self.announce(booking, f"📅 Booking #{booking['number']}: the customer changed the time to "
                                             f"{bookings.describe_when(booking)}.")
                await self.submit(booking, changed=True)
            elif confirmed is not None:
                change = bs.propose(confirmed, actor=bs.CUSTOMER, starts_at=start, ends_at=end, now=now)
                booking = await self.store.apply(confirmed, change, actor=bs.CUSTOMER)
                await self.announce(booking, f"🔁 Booking #{booking['number']}: the customer asks to move it to "
                                             f"{bookings.describe_proposal(booking)}; asking the owner.")
                await self.tell_owner(booking, bookings.format_move_request(booking, name=self.owner_name(),
                                                                            t=self.texts()), remember=True)
            else:
                conversation = await self.rt.db.get_conversation(chat_id) or {}
                booking = await self.store.create(
                    chat_id=chat_id, customer_name=conversation.get("display_name") or "",
                    customer_username=conversation.get("username"), starts_at=start, ends_at=end,
                    buffer_minutes=result.buffer_minutes, tz=self.tz, service=service, notes=notes, extras=extras,
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
            # Why, when it says (not in that group, no such name): the
            # operator fixes booking.provider from this line.
            why = f"{type(exc).__name__}: {exc}" if isinstance(exc, ValueError) else type(exc).__name__
            log.warning("[%s] Cannot resolve the booking owner %r: %s", rt.session_id, value, why)
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

    async def tell_owner(self, booking: dict[str, Any], text: str, *, remember: bool = False,
                         report: bool = True) -> bool:
        """A message to the owner about a booking. `remember` keeps its id
        so a bare YES sent as a reply to it is unambiguous. Whether it went
        (one that did not is reported in his chat, unless not `report`)."""
        row = await self.send_owner(text, reason=f"booking #{booking['number']} update to the owner")
        if row is None:
            if report:
                await self.rt.push_error(booking["chat_id"], f"Booking #{booking['number']}: could not reach the "
                                         "owner (check booking.provider). They can still answer from the panel.")
            return False
        self._remember(row, booking["number"])
        ref = self.owner_message_ref(row)
        if remember and any(ref.values()):
            await self.store.set_fields(booking["id"], **ref)
        return True

    def _remember(self, row: Optional[dict[str, Any]], about: Optional[int]) -> None:
        """A message went to her about booking `about` (0 or None: several,
        or none): a quote of it, or her bare YES right after it, falls on it."""
        if row is None:
            return
        ref = row.get("wa_message_id") or row.get("telegram_id")
        if ref is not None and about:
            self._about[str(ref)] = about
            self._about.move_to_end(str(ref))
            while len(self._about) > ABOUT_KEPT:
                self._about.popitem(last=False)

    def _quoted(self, reply_to: Any) -> Optional[int]:
        """The booking a message she quoted is about, when the bot knows."""
        return self._about.get(str(reply_to)) if reply_to is not None else None

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
                f"Booking #{booking['number']} was NOT sent to the owner: no owner is set in booking.provider, "
                "or it cannot be found (a phone number only works if that person lets anyone find them by "
                "number — an @username is safest; a group only if this account is in it). It is retried "
                "every few minutes, and can be answered from the Bookings page.",
            )
            await self.broadcast(booking)
            return False
        try:
            request = bookings.format_request(booking, changed=changed, name=self.owner_name(), t=self.texts())
            row = await self.rt.send_as_me(provider, request, reason="booking request to the owner")
            self._remember(row, booking["number"])
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

    async def on_owner_message(self, chat_id: int, text: str, reply_to: Any, message_id: Optional[int] = None) -> bool:
        """The owner's message in her chat. True when it was dealt with here
        and gets no ordinary reply: a command, and — while her own words are
        read (booking.owner_free_text) — every message, so her chat never
        gets a customer's reply. Her messages are carried out one at a time,
        in the order they came. `message_id`: its stored row, so the reader
        sees her chat up to it and not the messages she sent after it."""
        async with self._owner_lock:
            held: set[int] = set()
            token = _held_nudges.set(held)
            try:
                return await self._owner_message(chat_id, text, reply_to, message_id)
            finally:
                _held_nudges.reset(token)
                self._pending_held.clear()
                self._answer_about.clear()
                # Each customer's chat she changed gets its one reply now; one
                # that can't be started does not keep the others from theirs.
                for customer_chat in sorted(held):
                    try:
                        await self.nudge(customer_chat)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("[%s] Could not start the reply in chat %s after the owner's answer",
                                      self.rt.session_id, customer_chat)

    async def _owner_message(self, chat_id: int, text: str, reply_to: Any, message_id: Optional[int]) -> bool:
        free_text = bool(self.settings.get("owner_free_text", True))
        cmd = bookings.parse_owner_reply(text, today=self.local_today(), lengths=free_text)
        if cmd is not None and cmd.kind in ("ready", "in_taxi") and not self.outcall_taxi():
            cmd = None
        if not free_text:
            # Commands only, as they always were.
            if cmd is None:
                return False
            await self.answer_owner(chat_id, [await self.owner_command(cmd, reply_to)])
            return True
        quoted = self._quoted(reply_to)
        answering, doubt, about = False, False, None
        if cmd is not None and not cmd.loose:
            cmd, read_it, answering, doubt, about = await self._strict(chat_id, cmd, reply_to, quoted, message_id)
            if not read_it:
                # A strict command never costs a model call. (A question it
                # answers too late is looked at first: carrying it out closes it.)
                lost = self._held_lost(cmd, message_id)
                await self.answer_owner(chat_id, [await self.owner_command(cmd, reply_to)] + lost)
                return True
        # Her own words ("yes, tell him to bring cash", "yes 3, no 4", "I'm
        # ready in 20 minutes"), read against what each customer asked for.
        rows = await self.owner_rows()
        reading, failed = await self.read_owner_answer(chat_id, text, reply_to, rows, quoted=quoted,
                                                       upto_id=message_id)
        if reading is None:
            await self.answer_owner(chat_id, await self._unread(cmd, text, rows, reply_to, failed, answering, doubt))
            return True
        commands = bookings.owner_commands_from(
            reading.answers, rows, now=self.now(), tz_name=self.tz, replied=self._replied(rows, reply_to, quoted),
            outcall_taxi=self.outcall_taxi(), typed=text, t=self.texts(),
        )
        # A remark about one customer is no rule for all of them.
        reading = replace(reading, standing=bookings.standing_for_all(reading.standing, rows))
        fresh = self._fresh_held(message_id)
        # What a plain YES / NO of hers is about: the bot's open question,
        # the message she quoted, the last message the bot sent her.
        focus = set(fresh) | {n for n in (quoted, about) if n}
        waiting = {b["number"] for b in rows if bookings.awaits_owner(b)}
        plan = bookings.plan_owner_answer(
            cmd, commands, standing=reading.standing, held_words=answering and self._held_words(cmd, fresh, focus),
            settled={b["number"] for b in rows if bookings.is_settled(b)}, waiting=waiting, focus=focus,
            named=bookings.named_in(text, rows),
        )
        log.info("[%s]   owner's answer read as %s%s%s", self.rt.session_id,
                 ", ".join(f"{c.kind} #{c.number or '-'}" for c in commands) or "nothing about the bookings",
                 " + a standing instruction" if reading.standing else "",
                 f"; she is asked about #{plan.ask or '-'}" if plan.ask is not None else "")
        # Every part of it, in the order she wrote it, and one answer back.
        # Nothing about any booking (thanks, small talk, a remark about him)
        # gets no answer at all.
        lines = [await self.owner_command(c, reply_to) for c in plan.commands]
        if answering:
            # Her answer to the bot's question is in: that question is over,
            # whatever became of it (a YES n the bot offered is never re-read).
            for c in plan.commands:
                if c.number is not None:
                    self._close_held(c.number)
        if plan.earlier_lost:
            lines.append(self.texts().say("earlier_not_applied"))
        if plan.ask is not None:
            # Something of hers can't be carried out as read (a number she
            # typed that the reading misses, her yes read as a no, a YES / NO
            # read as about nothing while a request waits): one question,
            # with what of hers waits on it; the rest of her message went.
            target = plan.ask or (quoted if quoted in waiting else None)
            lines.append(await self._ask_about(target, plan.ask_with, had_words=bool(cmd and cmd.loose)))
        if reading.standing:
            lines.append(await self.save_standing(chat_id, reading.standing))
        await self.answer_owner(chat_id, lines)
        return True

    async def _strict(self, chat_id: int, cmd: bookings.OwnerCommand, reply_to: Any, quoted: Optional[int],
                      message_id: Optional[int]) -> tuple[bookings.OwnerCommand, bool, bool, bool, Optional[int]]:
        """Her strict command (YES 3, NO, 3 15:30, CANCEL 3), the booking a
        quote of hers names put on it: (the command, whether it is read by
        the model after all, whether it answers the bot's question, whether
        it is read because its booking is in doubt, the booking the last
        message to her was about). Read when it answers a question the bot
        asked her (what she said with it counts); when she quoted a message
        about a booking that does not wait for her answer, or one the bot
        can't place; and a bare YES / NO when the last message the bot sent
        her is not the one request waiting (his answer about another
        booking, a notice, anything the bot can't place — also after a
        restart), or when nothing waits. LIST, READY and IN are never read."""
        if cmd.kind in ("list", "ready", "in_taxi"):
            return cmd, False, False, False, None
        awaiting = await self.store.awaiting_owner()
        waiting = {b["number"] for b in awaiting}
        if reply_to is not None and cmd.number is None:
            if any(reply_to in (b.get("provider_message_id"), b.get("provider_wa_message_id")) for b in awaiting):
                pass  # the request itself: it is the booking, as always
            elif quoted is not None and quoted in waiting:
                cmd = replace(cmd, number=quoted)
            else:
                return cmd, True, self._answers_held(cmd, message_id), True, quoted
        if self._answers_held(cmd, message_id):
            return cmd, True, True, False, None
        if cmd.number is None and reply_to is None and cmd.kind in ("yes", "no"):
            if not awaiting:
                return cmd, True, False, False, None
            if len(awaiting) == 1:
                about = await self._last_about(chat_id, awaiting)
                if about != awaiting[0]["number"]:
                    return cmd, True, False, True, about
        return cmd, False, False, False, None

    async def _last_about(self, chat_id: int, awaiting: Sequence[dict[str, Any]]) -> Optional[int]:
        """The booking the last message the bot sent her is about: a request
        waiting for her, or one the bot remembers; 0 when it can't place it
        (a notice, or from before a restart). Nothing sent to her yet: the
        one request waiting (as it always was), else None."""
        row = await self.rt.db.last_outgoing(chat_id)
        if row is None:
            return awaiting[0]["number"] if len(awaiting) == 1 else None
        ref = row.get("wa_message_id") or row.get("telegram_id")
        if ref is None:
            return 0
        for booking in awaiting:
            if ref in (booking.get("provider_message_id"), booking.get("provider_wa_message_id")):
                return booking["number"]
        return self._about.get(str(ref), 0)

    def _fresh_held(self, message_id: Optional[int]) -> dict[int, _Held]:
        """The bot's questions to her her message (`message_id`) can answer:
        asked less than ANSWER_WINDOW ago, and before she wrote it."""
        now = self.now()
        return {n: held for n, held in self._held.items()
                if now - held.at < ANSWER_WINDOW
                and (message_id is None or held.question_id is None or held.question_id < message_id)}

    def _answers_held(self, cmd: Optional[bookings.OwnerCommand], message_id: Optional[int]) -> bool:
        if cmd is None:
            return False
        fresh = self._fresh_held(message_id)
        if cmd.number is not None:
            return cmd.number in fresh
        return bool(fresh) and cmd.kind in ("yes", "no")

    @staticmethod
    def _held_words(cmd: Optional[bookings.OwnerCommand], fresh: dict[int, _Held], focus: Collection[int]) -> bool:
        """The message the bot's question to her was about carried words, a
        length or extras — of the question her command answers only."""
        if cmd is None:
            return False
        numbers = {cmd.number} if cmd.number is not None else set(focus)
        return any(fresh[n].had_words for n in numbers if n in fresh)

    def _close_held(self, number: int) -> None:
        """The bot's question about booking `number` is answered: closed, and
        with a "which one?" question the other bookings it named too."""
        held = self._held.pop(number, None)
        if held is not None and len(held.group) > 1:
            for other in held.group:
                if other in self._held and self._held[other].question_id == held.question_id:
                    self._held.pop(other)

    def _held_lost(self, cmd: bookings.OwnerCommand, message_id: Optional[int]) -> list[str]:
        """Her plain command, not read, on a booking whose question to her
        had her words and that it does not answer (asked too long ago, or
        only after she wrote it): she is told they did not go with it."""
        held = self._held.get(cmd.number) if cmd.number is not None else None
        if held is None or cmd.number in self._fresh_held(message_id) or not held.had_words:
            return []
        self._held.pop(cmd.number, None)
        return [bookings.format_held_lost(cmd.number, held.words, t=self.texts())]

    async def _unread(self, cmd: Optional[bookings.OwnerCommand], text: str, rows: Sequence[dict[str, Any]],
                      reply_to: Any, failed: str, answering: bool, doubt: bool) -> list[str]:
        """Her lines when her words could not be read. `failed`: "down" (the
        model can't be asked: down, out of budget, too slow) or "unusable"
        (it answered something that can't be used). What she typed strictly
        stands — unless it was read because its booking was in doubt (it is
        never put on the one request waiting by default). A loose command
        stands only while the model is down — a YES when nothing after it
        may turn it into something else ("ok, not today", "yes at 9", "yes 3
        hours"), a NO / CANCEL / DONE / NOSHOW with its number and no other
        time after it — and she is told the rest of her message did
        nothing. Anything else gets the commands back, or, with no booking
        on, a line that nothing was done."""
        t = self.texts()
        if cmd is not None:
            if not cmd.loose:
                stands = not (doubt and cmd.number is None)
            elif failed != "down":
                stands = False
            else:
                stands = (cmd.kind == "yes" or cmd.number is not None) and \
                    not bookings.loose_doubt(text, cmd.kind, cmd.number)
            if stands:
                lines = [await self.owner_command(cmd, reply_to)]
                if cmd.loose:
                    return lines + [t.say("read_rest_failed")]
                return lines + [t.say("read_earlier_failed")] if answering else lines
        live = [b for b in rows if b["state"] in bs.LIVE]
        if not live:
            return [t.say("read_nothing_failed")]
        waiting = [b["number"] for b in live if bookings.awaits_owner(b)]
        return [bookings.read_failed(waiting or [b["number"] for b in live], t=t)]

    async def _ask_about(self, number: Optional[int], words: Optional[bookings.OwnerCommand] = None, *,
                         had_words: bool = False) -> str:
        """Her one question when an answer of hers can't be placed: about the
        booking she named, else the only one waiting for her, else which —
        with what of hers waits on it repeated (`words`, or what already
        waited on the booking's earlier question)."""
        awaiting = await self.store.awaiting_owner()
        booking = await self.store.by_number(number) if number is not None else (
            awaiting[0] if len(awaiting) == 1 else None)
        if booking is None and not awaiting:
            return ""
        if booking is not None:
            n = booking["number"]
            held = self._held.get(n)
            words = words or (held.words if held is not None else None)
            self._pending_held.append(((n,), words, had_words or bool(held and held.had_words)))
            self._answer_about.add(n)
            question = replace(words, kind="unclear", number=n, question="", missing="", candidates=()) \
                if words is not None else bookings.OwnerCommand("unclear", n)
            return bookings.format_question(question, booking, t=self.texts())
        self._pending_held.append((tuple(b["number"] for b in awaiting), words, had_words))
        return bookings.format_which_one(awaiting, words, t=self.texts())

    async def answer_owner(self, chat_id: int, lines: Sequence[str]) -> None:
        """One message back to her for everything in hers; none when there
        is nothing to say. The questions in it are held for her next
        message, and what it is about remembered (a quote of it)."""
        text = "\n".join(line for line in lines if line)
        about = next(iter(self._answer_about)) if len(self._answer_about) == 1 else 0
        if not text:
            self._pending_held.clear()
            return
        try:
            row = await self.rt.send_as_me(chat_id, text, reason="booking answer to the owner")
        except Exception as exc:
            await self.rt.handle_send_failure(chat_id, exc)
            self._pending_held.clear()
            return
        self._remember(row, about)
        asked_at, question = self.now(), row.get("id") if row else None
        for group, words, had_words in self._pending_held:
            for number in group:
                old = self._held.get(number)
                # Asked again: what of hers waited on the earlier question
                # still waits.
                kept = words or (old.words if old is not None else None)
                carried = kept is not None and (kept.has_words or kept.minutes is not None)
                self._held[number] = _Held(asked_at, question, group, kept,
                                           had_words or carried or bool(old and old.had_words))
        self._pending_held.clear()

    async def owner_command(self, cmd: bookings.OwnerCommand, reply_to: Any) -> str:
        """One command of hers carried out, and her line for it: what was
        done, what goes on to him, the answer to what she asked."""
        try:
            outcome = await self._carry_out(cmd, reply_to)
            if outcome.booking is not None:
                self._answer_about.add(outcome.booking["number"])
                if outcome.went and cmd.kind in CHANGES:
                    # Answered by a change of hers: the question is over
                    # (words for him alone answer nothing).
                    self._close_held(outcome.booking["number"])
            if outcome.asks:
                # A question to her (which booking? yes or no?): her next
                # message answers it, and what she said here is read with it.
                carried = cmd.has_words or cmd.minutes is not None
                self._pending_held.append((outcome.asks, cmd if carried else None, carried))
            return await self._afterwards(cmd, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One part of her message going wrong does not cost her the rest.
            log.exception("[%s] The owner's %s for #%s failed", self.rt.session_id, cmd.kind, cmd.number)
            if cmd.number:
                return self.texts().say("failed_n", n=cmd.number, error=type(exc).__name__)
            return self.texts().say("failed_that", error=type(exc).__name__)

    async def _carry_out(self, cmd: bookings.OwnerCommand, reply_to: Any) -> OwnerOutcome:
        t = self.texts()
        if cmd.kind == "list":
            outcalls = await self.outcalls_under_way() if self.outcall_taxi() else []
            return OwnerOutcome(bookings.format_list(await self.store.awaiting_owner(), outcalls,
                                                     proof=self.outcall_proof(), t=t))
        if cmd.kind in ("ready", "in_taxi"):
            outcome = await self.outcall_command(cmd)
            if outcome.went and cmd.minutes and outcome.booking is not None \
                    and cmd.minutes != _minutes(outcome.booking):
                # "ready, and make it 2 hours": the step, then the length.
                length = await self._length(cmd, outcome.booking)
                outcome = replace(outcome, line=f"{outcome.line}\n{length.line}",
                                  booking=length.booking or outcome.booking)
            return outcome
        booking = None if cmd.missing == "which" else await self._target(cmd, reply_to)
        if booking is None:
            asks = cmd.candidates or tuple(b["number"] for b in await self.store.awaiting_owner())
            return OwnerOutcome(await self._which(cmd), asks=asks)
        n = booking["number"]
        if cmd.kind == "unclear":
            return OwnerOutcome(bookings.format_question(cmd, booking, t=t), booking, asks=(n,))
        if cmd.kind in ("stays", "none"):
            if not cmd.minutes or cmd.minutes == _minutes(booking):
                # Nothing about the booking changes: only her words for him,
                # or her own question about it.
                words_only = cmd.kind == "none" and (cmd.has_words or cmd.for_me)
                line = "" if words_only else t.say("stays", n=n, when=bookings.when_for(booking, t))
                if cmd.kind == "stays" and cmd.at is not None and not cmd.has_words:
                    line += " " + t.say("ready_at_note", at=f"{cmd.at:%H:%M}")
                return OwnerOutcome(line, booking, went=True)
            if booking["state"] != bs.CONFIRMED:
                # A length for a request she has not taken: her yes sets it.
                return OwnerOutcome(bookings.format_question(cmd, booking, t=t), booking, asks=(n,))
            return await self._length(cmd, booking)
        if cmd.error:
            return OwnerOutcome(f"#{n}: {t.rule_error(cmd.error)}. " + t.say("answer_help", n=n), booking)
        try:
            change = await self._change_for(cmd, booking)
            if isinstance(change, str):
                return OwnerOutcome(change, booking)
            updated = await self.change(booking, change, actor=bs.OWNER, via=VIA_OWNER)
        except bs.IllegalTransition as exc:
            return OwnerOutcome(f"#{n}: {t.rule_error(str(exc))}.", booking)
        except booking_store.SlotTaken:
            return OwnerOutcome(t.say("slot_taken", n=n), booking)
        except booking_store.StaleBooking:
            return OwnerOutcome(t.say("changed_meanwhile", n=n), booking)
        line = bookings.format_acknowledgement(updated, change.action, outcall_taxi=self.outcall_taxi(),
                                               proof=self.outcall_proof(), t=t)
        return OwnerOutcome(line, updated, went=True)

    async def _length(self, cmd: bookings.OwnerCommand, booking: dict[str, Any]) -> OwnerOutcome:
        """A new length for a confirmed booking ("make it 2 hours"). Her
        words for him go on whether or not the length is free."""
        n, t = booking["number"], self.texts()
        try:
            change = await self._change_for(replace(cmd, kind="stays"), booking)
            if isinstance(change, str):
                return OwnerOutcome(change, booking, words_go=True)
            updated = await self.change(booking, change, actor=bs.OWNER, via=VIA_OWNER)
        except bs.IllegalTransition as exc:
            return OwnerOutcome(f"#{n}: {t.rule_error(str(exc))}.", booking, words_go=True)
        except booking_store.SlotTaken:
            return OwnerOutcome(t.say("length_taken", n=n), booking, words_go=True)
        except booking_store.StaleBooking:
            return OwnerOutcome(t.say("changed_meanwhile", n=n), booking, words_go=True)
        if updated.get("customer_notice") not in (bs.NOTICE_LENGTH, bs.NOTICE_CONFIRMED, bs.NOTICE_RESCHEDULED):
            # The news he has yet to hear (she is ready, she is in the taxi,
            # a time offered) does not say when it ends now: that goes too.
            ends = bookings.describe_span(updated["starts_at"], updated["ends_at"], updated["tz"])
            length = bookings.describe_minutes(_minutes(updated))
            updated = await self.store.add_booker_news(updated["id"], bookings.booker_news_item(
                bookings.OwnerCommand("none", n, tell=f"it is now {ends} ({length})"), at=self.now()))
        line = bookings.format_acknowledgement(updated, change.action, outcall_taxi=self.outcall_taxi(),
                                               proof=self.outcall_proof(), t=t)
        return OwnerOutcome(line, updated, went=True)

    async def _change_for(self, cmd: bookings.OwnerCommand, booking: dict[str, Any]) -> "bs.Change | str":
        """The change her command makes, or her line saying why it can't be
        made (the time, or the length she gave, is not free)."""
        n, now, t = booking["number"], self.now(), self.texts()
        if cmd.kind in ("stays", "none"):
            # A new length for a confirmed booking ("make it 2 hours").
            start = booking["starts_at"]
            end = bookings.plus_minutes(start, cmd.minutes)
            if end > booking["ends_at"] and (why := await self._no_room(booking, start, end)):
                return self._not_possible(booking, start, end, why, t) + " " + \
                    t.say("length_stays", when=bookings.when_for(booking, t))
            return bs.change_length(booking, actor=bs.OWNER, ends_at=end,
                                    blocked_until=end + self.buffer_of(booking), now=now)
        if cmd.kind == "yes":
            # The time he asked for (or the move he asked for), for the
            # length she gave when she gave one.
            moving = booking.get("proposed_by") == bs.CUSTOMER
            start, asked_end = ((booking["proposed_starts_at"], booking["proposed_ends_at"]) if moving
                                else (booking["starts_at"], booking["ends_at"]))
            end = bookings.plus_minutes(start, cmd.minutes) if cmd.minutes else asked_end
            if end > asked_end and (why := await self._no_room(booking, start, end)):
                # Longer than he asked and not free: nothing is taken.
                return (self._not_possible(booking, start, end, why, t) + " "
                        + t.say("nothing_moved" if moving else "nothing_confirmed", n=n,
                                when=bookings.span_for(t, start, asked_end, booking["tz"])))
            if moving:
                return await self._accept(booking, bs.OWNER, end=end)
            if cmd.minutes:
                return bs.confirm(booking, actor=bs.OWNER, now=now, ends_at=end,
                                  blocked_until=end + self.buffer_of(booking))
            return bs.confirm(booking, actor=bs.OWNER, now=now)
        if cmd.kind == "no":
            if booking.get("proposed_by") == bs.CUSTOMER:
                return bs.reject_proposal(booking, actor=bs.OWNER, now=now)
            return bs.decline(booking, actor=bs.OWNER, now=now)
        if cmd.kind == "propose":
            start = bookings.resolve_local(cmd, booking)
            end = bookings.plus_minutes(start, cmd.minutes or _minutes(booking))
            result = await self.check(start, end, exclude_id=booking["id"], hours=False)
            if not result.ok:
                return t.say("not_possible", n=n, what=bookings.at_for(t, start, self.tz),
                             why=bookings.why_for(t, result.reason))
            return bs.propose(booking, actor=bs.OWNER, starts_at=start, ends_at=end, now=now)
        if cmd.kind == "cancel":
            return bs.cancel(booking, actor=bs.OWNER, now=now, reason="cancelled by the owner")
        if cmd.kind == "done":
            return bs.mark_completed(booking, actor=bs.OWNER, now=now)
        if cmd.kind == "noshow":
            return bs.mark_no_show(booking, actor=bs.OWNER, now=now)
        raise bs.IllegalTransition(f"{cmd.kind} is not something the owner can do to a booking")

    async def _no_room(self, booking: dict[str, Any], start: datetime, end: datetime) -> str:
        """Why [start, end) can't be this booking's time, or "" when it can:
        checked like any time a person on the business side chose (no
        opening hours: only the past and other bookings), from now on for a
        booking already under way."""
        now = self.now()
        if end <= now:
            return "past"
        # Up to the end of its buffer: that is what it holds (the overlap
        # rule in the database counts it too).
        result = await self.check(max(start, now + timedelta(minutes=1)), end + self.buffer_of(booking),
                                  exclude_id=booking["id"], hours=False)
        return "" if result.ok else result.reason

    @staticmethod
    def _not_possible(booking: dict[str, Any], start: datetime, end: datetime, why: str,
                      t: booker_texts.Texts) -> str:
        minutes = int((end - start).total_seconds() // 60)
        what = f"{bookings.span_for(t, start, end, booking['tz'])} ({t.minutes(minutes)})"
        return t.say("not_possible", n=booking["number"], what=what, why=bookings.why_for(t, why))

    async def _afterwards(self, cmd: bookings.OwnerCommand, outcome: OwnerOutcome) -> str:
        """What follows her command once it is carried out. A question she
        had put to him ends when she acts on the booking; her words for him
        go on only when it went through (a decline with "tell him I'm busy
        today" does, a yes that could not be confirmed does not) or when the
        booking was not to change anyway (her words with a length that is
        not free); a refusal of an extra holds whatever became of the rest;
        what she asked about the booking is answered from it, to her alone."""
        line, booking, t = outcome.line, outcome.booking, self.texts()
        if booking is not None and not (outcome.went or outcome.words_go) and cmd.extras_no:
            # A refusal holds whatever became of the rest: he is not offered
            # it meanwhile (he is told only once her answer goes through).
            booking = await self.store.set_fields(
                booking["id"], extras_no=bookings.extras_joined(booking.get("extras_no") or "", cmd.extras_no),
                extras_ok=_without(booking.get("extras_ok") or "", cmd.extras_no))
        if booking is not None and outcome.went and cmd.kind in CHANGES and booking.get("booker_asked_at") \
                and not cmd.ask:
            booking = await self.store.set_fields(booking["id"], booker_asked_at=None)
        if booking is not None and (outcome.went or outcome.words_go) and cmd.has_words:
            booking, waits = await self.pass_words(booking, cmd)
            line = line + bookings.passing_on_words(cmd, t=t) if line else bookings.format_words_ack(booking, cmd,
                                                                                                    t=t)
            if waits:
                line += " " + waits
        if cmd.for_me and booking is not None:
            facts = bookings.format_facts(booking, outcall_taxi=self.outcall_taxi(), proof=self.outcall_proof(),
                                          t=t)
            line = f"{line}\n{facts}" if line else facts
        return line

    async def pass_words(self, booking: dict[str, Any], cmd: bookings.OwnerCommand) -> tuple[dict[str, Any], bool]:
        """Her words for him (what to tell him, what to ask him, the extras
        she takes or refuses) wait on the booking for his next reply, which
        is started; words already waiting for him are not added twice. Her
        extras stay on the booking too: one she refused is never offered to
        him again (reply_note), unless she takes it later. Returns the
        booking, and what to tell her when it can't go to him now ("" when it
        can: his chat not answered, or quiet hours until when)."""
        now = self.now()
        fields: dict[str, Any] = {}
        if cmd.extras_ok or cmd.extras_no:
            fields["extras_ok"] = bookings.extras_joined(_without(booking["extras_ok"], cmd.extras_no),
                                                         cmd.extras_ok)
            fields["extras_no"] = bookings.extras_joined(_without(booking["extras_no"], cmd.extras_ok),
                                                         cmd.extras_no)
        if cmd.ask:
            # His answers go on to her for a while (pass_on).
            fields["booker_asked_at"] = now
        item = bookings.booker_news_item(cmd, at=now)
        if any(_same_words(waiting, item) for waiting in booking.get("booker_news") or ()):
            updated = await self.store.set_fields(booking["id"], **fields) if fields else booking
        else:
            updated = await self.store.add_booker_news(booking["id"], item, **fields)
            await self.announce(updated, f"💬 Booking #{updated['number']}: the owner's words go to him with the "
                                         f"next reply ({_words(cmd)}).")
        await self.nudge(updated["chat_id"])
        if not await self.can_reply(updated["chat_id"]):
            return updated, self.texts().say("words_wait")
        if (left := self.rt.quiet_seconds_left()) > 0:
            until = self.rt.local_now() + timedelta(seconds=left)
            return updated, bookings.words_after_quiet_hours(until, t=self.texts())
        return updated, ""

    async def _which(self, cmd: bookings.OwnerCommand) -> str:
        """No one booking could be told for her command: she is asked which."""
        t = self.texts()
        if cmd.missing == "which":
            rows = [await self.store.by_number(number) for number in cmd.candidates]
            return bookings.format_which_one([b for b in rows if b is not None], cmd, t=t)
        if cmd.kind == "unclear":
            return bookings.format_question(cmd, t=t)
        return bookings.format_which(cmd.kind if cmd.kind not in ("propose", "stays", "none") else "yes", t=t)

    async def _target(self, cmd: bookings.OwnerCommand, reply_to: Any) -> Optional[dict[str, Any]]:
        if cmd.number is not None:
            return await self.store.by_number(cmd.number)
        awaiting = await self.store.awaiting_owner()
        if reply_to is not None:
            for booking in awaiting:
                if reply_to in (booking.get("provider_message_id"), booking.get("provider_wa_message_id")):
                    return booking
            # She quoted another message the bot sent her about a booking:
            # that booking, never the one that merely waits (while her own
            # words are read; commands only works as it always did).
            quoted = self._quoted(reply_to) if self.settings.get("owner_free_text", True) else None
            if quoted is not None:
                return await self.store.by_number(quoted)
        if cmd.kind in ("yes", "no", "unclear") and len(awaiting) == 1:
            return awaiting[0]
        return None

    async def owner_rows(self) -> list[dict[str, Any]]:
        """The bookings her words may be about, by number: what waits for her
        answer, the outcalls under way, the confirmed ones from a while ago
        (cancel one, mark one that just ended) to a week ahead, and those
        over in that time ("ask him why he cancelled")."""
        now = self.now()
        open_ = {b["id"]: b for b in await self.store.awaiting_owner()}
        if self.outcall_taxi():
            open_.update((b["id"], b) for b in await self.outcalls_under_way())
        open_.update((b["id"], b) for b in await self.store.between(
            now - OWNER_LOOKBACK, now + OWNER_LOOKAHEAD, (bs.CONFIRMED, *bs.FINAL)))
        return sorted(open_.values(), key=lambda b: b["number"])

    @staticmethod
    def _replied(rows: Sequence[dict[str, Any]], reply_to: Any,
                 quoted: Optional[int] = None) -> Optional[dict[str, Any]]:
        """The booking whose message hers replies to (she quoted it): its
        request, or another message the bot sent her about it."""
        if reply_to is None:
            return None
        return next((b for b in rows if reply_to in (b.get("provider_message_id"), b.get("provider_wa_message_id"))
                     or (quoted is not None and b["number"] == quoted)), None)

    async def read_owner_answer(self, chat_id: int, text: str, reply_to: Any, rows: Sequence[dict[str, Any]], *,
                                quoted: Optional[int] = None,
                                upto_id: Optional[int] = None) -> tuple[Optional[bookings.OwnerReading], str]:
        """Her message in her own words, read by the model against the
        bookings she may mean (`rows`, owner_rows) with her recent chat up to
        it (`upto_id`: not the messages she sent after it) and when each
        message came: everything it says about each of them, and an empty
        reading when it is about none. Asked with nothing open too, so an
        instruction for every customer ("from now on cash only") is never
        lost. Returns (reading, why it is None): "down" when the model can't
        be asked (down, out of budget, no answer within OWNER_READ_BUDGET),
        "unusable" when it answered something that can't be used."""
        if not text.strip():
            return bookings.OwnerReading(), ""
        if await self.rt.ai_limit_reason():
            return None, "down"
        replied = self._replied(rows, reply_to, quoted)
        history = await self.rt.db.get_history_for_ai(chat_id, limit=OWNER_HISTORY, with_times=True,
                                                      upto_id=upto_id)
        try:
            async with asyncio.timeout(OWNER_READ_BUDGET):
                async with self.rt.ai_gate():
                    reading = await ai_responder.read_owner_answer(
                        api_key=self.rt.deepseek_key, history=history, latest=text, tz_name=self.tz,
                        now=self.now(), state_note=bookings.owner_state_note(
                            rows, outcall_taxi=self.outcall_taxi(), proof=self.outcall_proof()),
                        replying_to=replied["number"] if replied else None, ai_config=self.rt.config["ai"],
                        client=self.rt.http_client, usage_sink=self.rt.usage_sink("owner_answer"),
                    )
        except (ai_responder.AIResponderError, TimeoutError) as exc:
            log.warning("[%s] Could not read the owner's answer: %s", self.rt.session_id,
                        exc if str(exc) else type(exc).__name__)
            return None, "down"
        if reading is None:
            log.warning("[%s] The owner's answer came back unreadable from the model.", self.rt.session_id)
            return None, "unusable"
        return reading, ""

    async def save_standing(self, chat_id: int, text: str) -> str:
        """A standing instruction of hers for every customer ("from now on
        cash only"): saved as a proposed operator rule for this account,
        which the admin accepts or not (Finetune → Rules). Her line for it;
        a failure is logged and told to her, never more than that."""
        rt = self.rt
        try:
            industry_id = (rt.bundle.tenant["industry_id"] if rt.bundle is not None else
                           await rt.pool.fetchval("SELECT industry_id FROM tenants WHERE id = $1", rt.tenant_id))
            rule = await rules.create(
                rt.pool, industry_id=industry_id, tenant_id=rt.tenant_id, section=STANDING_SECTION, text=text,
                source=BOOKER, source_ref={"tenant_id": rt.tenant_id, "chat_id": chat_id}, status=rules.PROPOSED,
                created_by=BOOKER, actor=audit.OWNER,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] Could not save the owner's standing instruction: %s: %s", rt.session_id,
                        type(exc).__name__, exc)
            return self.texts().say("standing_failed", text=text)
        log.info("[%s]   the owner's standing instruction is proposed rule %s.", rt.session_id, rule["id"])
        return bookings.format_standing_saved(text, t=self.texts())

    async def _accept(self, booking: dict[str, Any], actor: str, end: Optional[datetime] = None) -> bs.Change:
        """The time the other side put forward, taken; `end`: for the length
        the owner gave."""
        start, end = booking["proposed_starts_at"], end or booking["proposed_ends_at"]
        result = await self.check(start, end, exclude_id=booking["id"], hours=False)
        if not result.ok:
            raise booking_store.SlotTaken(result.reason)
        return bs.accept_proposal(booking, actor=actor, now=self.now(), blocked_until=end + self.buffer_of(booking),
                                  ends_at=end)

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
        if action in ("ready", "in_taxi"):
            return await self.admin_outcall_step(booking, action)
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
            t = self.texts()
            await self.send_owner(bookings.format_acknowledgement(updated, change.action, outcall_taxi=self.outcall_taxi(),
                                                                  proof=self.outcall_proof(), t=t)
                                  + " " + t.say("from_panel"), reason="panel booking change passed on to the owner")
        return booking_store.public(updated)

    # ------------------------------------------------ after every change

    async def change(self, booking: dict[str, Any], change: bs.Change, *, actor: str, via: str) -> dict[str, Any]:
        updated = await self.store.apply(booking, change, actor=actor)
        if updated.get("booker_asked_at") and actor in bs.DECIDERS:
            # Decided on (by her, or in the panel): a question she had put to
            # him about it is over, and what he writes is his own again.
            updated = await self.store.set_fields(updated["id"], booker_asked_at=None)
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
            "change_length": f"⏱ Booking #{n} now lasts {bookings.describe_minutes(_minutes(after))}: "
                             f"{bookings.describe_when(after)}, changed by the {actor}.",
        }
        if notes.get(change.action):
            await self.announce(after, notes[change.action])
        else:
            await self.broadcast(after)

        t = self.texts()
        if actor == bs.CUSTOMER:
            owner_news = {
                "accept_proposal": "news_accept_proposal",
                "reschedule": "news_reschedule",
                "reject_proposal": ("news_reject_proposal_cancelled" if after["state"] == bs.CANCELLED
                                    else "news_reject_proposal_stays"),
            }.get(change.action)
            if change.action == "cancel":
                await self.tell_owner(after, bookings.format_customer_cancelled(after, name=self.owner_name(), t=t))
            elif owner_news:
                await self.tell_owner(after, bookings.format_owner_update(after, t.say(owner_news),
                                                                          name=self.owner_name(), t=t))
        elif change.action == "expire":
            await self.tell_owner(after, bookings.format_owner_update(
                after, t.say("news_lapsed"), name=self.owner_name(), t=t))

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
        """Start the reply in a customer's chat that tells them what changed."""
        held = _held_nudges.get()
        if held is not None:
            # Inside one of the owner's messages: once all of it is done.
            held.add(chat_id)
            return
        if await self.can_reply(chat_id):
            self.rt.schedule_draft(chat_id)

    async def can_reply(self, chat_id: int) -> bool:
        """A reply can be started in this customer's chat now: the account is
        not off, his chat not paused or taken over, not kept out by staging."""
        if self.rt.paused():
            return False
        conversation = await self.rt.db.get_conversation(chat_id)
        if conversation is None or self.rt.silenced(conversation):
            return False
        return not await self.rt.staged_out(chat_id, conversation.get("username"))

    # --------------------------------------------------- the reply writer

    async def reply_note(self, chat_id: int) -> ReplyNote:
        if not self.enabled():
            return ReplyNote()
        lines: list[str] = []
        with_notice = await self.store.with_notice(chat_id)
        quiet = self.quiet()
        taxi, proof = self.outcall_taxi(), self.outcall_proof()
        taxi_news = (bs.NOTICE_CONFIRMED, bs.NOTICE_READY, bs.NOTICE_IN_TAXI)
        for booking in with_notice:
            notice = booking.get("customer_notice")
            if quiet and notice == bs.NOTICE_REQUESTED:
                lines.append(bookings.quiet_request_line(booking))
            elif taxi and notice in taxi_news and bookings.is_outcall(booking) and booking["state"] == bs.CONFIRMED:
                lines.append(bookings.outcall_notice_line(
                    booking, now=self.now(), ready_minutes=self.settings["outcall_ready_minutes"],
                    has_address=bool(self.address()), proof=proof))
                # Ready: the taxi picks her up at the address, so it goes
                # again, word for word, even when he had it with the prices.
                if notice == bs.NOTICE_READY and self.address():
                    self.address_due[chat_id] = booking["id"]
                    lines.append(bookings.PICKUP_ADDRESS_LINE)
            elif notice in (bs.NOTICE_READY, bs.NOTICE_IN_TAXI):
                # Taxi steps of a booking that is no longer a confirmed
                # outcall (cancelled meanwhile), or with the steps now off.
                pass
            else:
                lines.append(bookings.notice_line(booking))
            if self.address_follows(booking) and not await self.address_in_chat(chat_id):
                self.address_due[chat_id] = booking["id"]
                lines.append(bookings.ADDRESS_FOLLOWS_LINE)
        told = {b["id"] for b in with_notice}
        standing = await self.store.for_chat(chat_id)
        for booking in standing:
            if booking["id"] in told:
                continue
            if quiet and booking["state"] in (bs.REQUESTED, bs.PENDING) and booking.get("proposed_by") != bs.OWNER:
                lines.append(bookings.quiet_request_line(booking))
            else:
                lines.append(bookings.standing_line(booking, outcall_taxi=taxi, proof=proof,
                                                    meets_outside=not self.way_in()))
        # The owner's own words for him, with whichever of his bookings
        # (in any state: "no, tell him I'm busy today"), and the extras she
        # refused him, in every reply from then on.
        her_words: dict[int, list[str]] = {}
        items: list[dict[str, Any]] = []
        now = self.now()
        for booking in await self.store.with_news(chat_id):
            # Only what is still worth saying; the tick drops the rest and
            # tells her.
            fresh = [item for item in booking["booker_news"] if item.get("id") and _news_fresh(item, booking, now)]
            if fresh:
                her_words[booking["id"]] = [item["id"] for item in fresh]
                items.extend(fresh)
        if items:
            lines.append(bookings.booker_news_line(items))
        lines.append(bookings.refused_extras_line(await self.store.refused_extras(chat_id)))
        if self.her_name() and (with_notice or standing):
            lines.append(bookings.own_name_line(self.her_name()))
        if self.address():
            lines.append(bookings.address_tag_line(self.settings.get("address_timing", "with_prices")))
        transient = list(self.pending_lines.get(chat_id, []))
        lines.extend(transient)
        return ReplyNote(
            text=bookings.context_for_reply(lines), notice_ids=sorted(told), transient=transient,
            reminder=self.reminders_out.get(chat_id), address_for=self.address_due.get(chat_id),
            her_words=her_words, notices={b["id"]: {key: b.get(key) for key in (
                "customer_notice", "starts_at", "ends_at", "proposed_starts_at", "proposed_ends_at")}
                for b in with_notice},
        )

    async def delivered(self, chat_id: int, note: ReplyNote) -> None:
        """The reply carrying `note` was sent or saved for approval."""
        for booking_id in note.notice_ids:
            if booking_id in note.notices:
                # Only the notice it told: a newer one set meanwhile stays.
                await self.store.clear_notice(booking_id, note.notices[booking_id])
            else:
                await self.store.set_fields(booking_id, customer_notice=None)
        # Only the words this reply carried: any she added meanwhile wait
        # for the next one.
        for booking_id, item_ids in note.her_words.items():
            await self.store.clear_booker_news(booking_id, item_ids)
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
        if note.address_for is not None and self.address_due.get(chat_id) == note.address_for:
            self.address_due.pop(chat_id)
            await self.send_address(await self.store.get(note.address_for))

    def quiet(self) -> bool:
        return self.enabled() and bool(self.settings.get("quiet_until_answered"))

    async def waiting_for_owner(self, chat_id: int) -> bool:
        """A time they asked for is with the owner and not answered yet: a new
        or changed request, or a move of a confirmed booking."""
        for booking in await self.store.for_chat(chat_id):
            if booking["state"] in (bs.REQUESTED, bs.PENDING) and booking.get("proposed_by") != bs.OWNER:
                return True
            if booking["state"] == bs.CONFIRMED and booking.get("proposed_by") == bs.CUSTOMER:
                return True
        return False

    async def quiet_until_answered(self, chat_id: int, history: list[dict[str, str]]) -> bool:
        """booking.quiet_until_answered: no reply to their messages while a
        time they asked for waits for the owner. Their first message still
        gets the first reply, and a message we start (the owner's answer, a
        lapsed request) is never held; nor is a reply that carries the
        owner's own words for him (session_runtime checks ReplyNote.her_words)."""
        if not self.quiet() or not history or history[-1].get("role") != "user":
            return False
        if not any(m.get("role") == "assistant" for m in history[:-1]):
            return False
        return await self.waiting_for_owner(chat_id)

    def address(self) -> str:
        """booking.address, or "" when bookings are off or none is set."""
        return (self.settings.get("address") or "").strip() if self.enabled() else ""

    def fill_address(self, text: str, *, follows: bool = False, latest: str = "") -> str:
        """A reply's [address] tag as the address itself, a message of its
        own. `follows`: the address is sent right after this reply anyway
        (a confirmation, her taxi), so the tag is only dropped. With
        booking.address_timing "when_asked" the tag only counts when their
        `latest` messages ask where she is."""
        if follows:
            return bookings.take_address_tag(text)[0]
        if self.settings.get("address_timing") == "when_asked" and not bookings.asks_where(latest):
            return bookings.take_address_tag(text)[0]
        return bookings.fill_address(text, self.address())

    async def address_in_chat(self, chat_id: int) -> bool:
        address = self.address()
        history = await self.rt.db.get_history_for_ai(chat_id, limit=200)
        return bool(address) and any(m.get("role") == "assistant" and address in (m.get("content") or "")
                                     for m in history)

    def address_follows(self, booking: dict[str, Any]) -> bool:
        """The address goes out with the news that a booking is confirmed —
        never for an outcall, where the business goes to them."""
        return (booking.get("customer_notice") == bs.NOTICE_CONFIRMED
                and bool((self.settings.get("address") or "").strip())
                and not bookings.is_outcall(booking))

    async def send_address(self, booking: dict[str, Any]) -> None:
        """booking.address, word for word, to a customer whose booking was
        just confirmed (sent after the reply that tells them)."""
        text = (self.settings.get("address") or "").strip()
        rt, chat_id = self.rt, booking["chat_id"]
        if not text or booking["state"] not in bs.LIVE:
            return
        rt.begin_sending(chat_id)
        try:
            await rt.send_as_me(chat_id, text, typing=True, reason=f"address for booking #{booking['number']}")
        except Exception as exc:
            if not await rt.handle_send_failure(chat_id, exc):
                await rt.push_error(chat_id, f"Could not send the address: {type(exc).__name__}: {exc}")
            return
        finally:
            rt.end_sending(chat_id)
        await self.announce(booking, f"📍 Booking #{booking['number']}: address sent.")

    # ------------------------------------------------------------ arrival

    def _arriving(self, live: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """The confirmed booking whose customer may be arriving now. Never
        an outcall: the business goes to them, so there is no way in to send."""
        now = self.now()
        for booking in live:
            if (booking["state"] == bs.CONFIRMED and not booking.get("instructions_sent_at")
                    and not bookings.is_outcall(booking) and bookings.arrival_window(booking, now)
                    # She meets him outside: once he has arrived there is
                    # nothing more to send.
                    and not (booking.get("arrived_at") and not self.way_in())):
                return booking
        return None

    def way_in(self) -> str:
        """booking.arrival_instructions; "" when she comes out to meet him."""
        return (self.settings.get("arrival_instructions") or "").strip()

    def answered_by_way_in(self, chat_id: int) -> bool:
        """Their latest message was answered by the arrival instructions:
        they go straight in, so nothing more is written to it."""
        return chat_id in self.let_in

    def photo_check_ready(self) -> bool:
        url, key = vision.endpoint_from_env()
        return bool(self.settings["arrival_photo_check"] and self.rt.config["vision"]["enabled"]
                    and self.rt.config["vision"]["model"] and url and key)

    async def on_arrival(self, booking: dict[str, Any], *, via: str, said: str = "") -> None:
        if via == "text" and self.settings["arrival_requires_photo"] and self.photo_check_ready() \
                and self.rt.media_library.references():
            await self.store.set_fields(booking["id"], arrived_at=self.now())
            self.add_line(booking["chat_id"], "NEWS TO PASS ON IN THIS REPLY: they say they have arrived. Ask them "
                          "to send a photo of the entrance they are at, so the way in can be sent. " + bookings.NO_DIRECTIONS_RULE)
            await self.announce(booking, f"🚪 Booking #{booking['number']}: the customer says they have arrived; "
                                         "waiting for their photo of the entrance.")
            return
        first = not booking.get("arrived_at")
        sent = await self.send_instructions(booking)
        if first and self.settings.get("arrival_to_owner", True) and self.settings.get("provider"):
            await self.tell_owner(booking, bookings.format_arrived(booking, said, way_in_sent=sent,
                                                                   name=self.owner_name(), t=self.texts()))

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
                booking, self.texts().say("news_arrived_photo_ok"), name=self.owner_name(), t=self.texts()))
            return "[photo: arrival photo, matches the entrance]"
        self.add_line(chat_id, "NEWS TO PASS ON IN THIS REPLY: the photo they sent does not look like the "
                      "entrance. Ask them politely to check they are at the right door and to send another photo. "
                      + bookings.NO_DIRECTIONS_RULE)
        await self.announce(booking, f"🚪 Booking #{booking['number']}: the arrival photo does not look like the "
                                     "entrance; the way in was not sent.")
        await self.tell_owner(booking, bookings.format_owner_update(
            booking, self.texts().say("news_arrived_photo_bad"), name=self.owner_name(), t=self.texts()))
        return "[photo: arrival photo, does not look like the entrance]"

    async def send_instructions(self, booking: dict[str, Any]) -> bool:
        """The way in (booking.arrival_instructions), once he has arrived.
        Whether it went out: none set means she comes out to meet him."""
        text = self.way_in()
        rt, chat_id = self.rt, booking["chat_id"]
        if not booking.get("arrived_at"):
            booking = await self.store.set_fields(booking["id"], arrived_at=self.now())
        if not text:
            await self.announce(booking, f"🚪 Booking #{booking['number']}: the customer has arrived; no way in is "
                                         "set (booking.arrival_instructions), so she comes out to meet him.")
            return False
        rt.cancel_draft(chat_id)
        rt.begin_sending(chat_id)
        sent: list[int] = []
        try:
            row = await rt.send_as_me(chat_id, text, typing=True, reason="arrival instructions")
            if row.get("id"):
                sent.append(row["id"])
            # Then the photos of the way in, if this network can send files.
            # One that fails is reported; the instructions are out already.
            items = rt.media_library.arrival_items() if rt.transport.can_send_files else []
            for item in items:
                try:
                    row = await rt.send_media_as_me(chat_id, item["id"], reason="arrival instructions")
                except Exception as exc:
                    await rt.push_error(chat_id, f"Could not send {media.label(item)} with the arrival "
                                                 f"instructions: {type(exc).__name__}: {exc}")
                    continue
                if row.get("id"):
                    sent.append(row["id"])
        except Exception as exc:
            if not await rt.handle_send_failure(chat_id, exc):
                await rt.push_error(chat_id, f"Could not send the arrival instructions: {type(exc).__name__}: {exc}")
            return False
        finally:
            rt.end_sending(chat_id)
        self.let_in.add(chat_id)
        minutes = int(self.settings.get("arrival_cleanup_minutes") or 0)
        fields: dict[str, Any] = {"instructions_sent_at": self.now(), "instructions_message_ids": sent}
        if minutes and sent:
            fields["instructions_cleanup_at"] = self.now() + timedelta(minutes=minutes)
        booking = await self.store.set_fields(booking["id"], **fields)
        later = f" They are deleted from the chat in {minutes} min." if minutes and sent else ""
        await self.announce(booking, f"🚪 Booking #{booking['number']}: the customer has arrived — entry "
                                     f"instructions sent.{later}")
        return True

    # How long a failed cleanup is retried before the owner is told to
    # delete the messages by hand.
    CLEANUP_GIVE_UP = timedelta(hours=6)

    async def clean_up_arrival(self, booking: dict[str, Any]) -> None:
        """Delete the arrival messages of one booking, for both sides."""
        chat_id, number = booking["chat_id"], booking["number"]
        try:
            await self.rt.delete_sent(chat_id, list(booking.get("instructions_message_ids") or []),
                                      placeholder=media.DELETED_PLACEHOLDER,
                                      reason=f"arrival instructions of booking #{number}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.now() - booking["instructions_cleanup_at"] < self.CLEANUP_GIVE_UP:
                raise  # tried again next tick
            await self.store.set_fields(booking["id"], instructions_cleaned_at=self.now())
            await self.rt.push_error(chat_id, f"Could not delete the arrival instructions of booking #{number}: "
                                              f"{type(exc).__name__}: {exc}. Delete them by hand.")
            await self.announce(booking, f"⚠️ Booking #{number}: the arrival instructions could not be deleted "
                                         "from the chat. Delete them by hand.")
            return
        await self.store.set_fields(booking["id"], instructions_cleaned_at=self.now())
        await self.rt.post_note(chat_id, f"🧹 Booking #{number}: the arrival instructions were deleted from the chat.")

    # ----------------------------------------------------------- outcalls
    #
    # booking.outcall_taxi: once the owner confirms an outcall, she gets
    # ready and sends READY n; he is asked to order a taxi to her address
    # and send its link, which goes on to her. She sends IN n once she is in
    # it (or he is told so outcall_in_taxi_minutes after the link came); he
    # is then asked for whatever is still needed to find him. Everything he
    # sends from the confirmation until the outcall is over goes on to her,
    # photos as photos.
    #
    # booking.outcall_proof: instead of waiting for READY n, he is asked
    # after the confirmation for a photo that shows he is at the place. His
    # first photo goes on to her as the proof and he is asked at once to
    # order the taxi; from there it goes on as above. READY n skips the photo.

    def outcall_taxi(self) -> bool:
        return self.enabled() and bool(self.settings.get("outcall_taxi"))

    def outcall_proof(self) -> bool:
        return self.outcall_taxi() and bool(self.settings.get("outcall_proof"))

    def wants_proof(self, booking: dict[str, Any]) -> bool:
        """His next photo is the proof: asked for, and no taxi step taken yet."""
        return self.outcall_proof() and bookings.is_outcall(booking) and not any(
            booking.get(step) for step in ("proof_at", "ready_at", "taxi_link_at", "in_taxi_at"))

    def _under_way(self, booking: dict[str, Any], now: datetime) -> bool:
        return (booking["state"] == bs.CONFIRMED and bookings.is_outcall(booking)
                and booking["ends_at"] + OUTCALL_AFTER_END > now)

    def _passing_on(self, booking: dict[str, Any], now: datetime) -> bool:
        """What he sends goes on to the owner: an outcall under way (taxi
        steps on), or an incall he has arrived for (booking.arrival_to_owner),
        until half an hour after the end."""
        if bookings.is_outcall(booking):
            return self.outcall_taxi() and self._under_way(booking, now)
        # Arrived for good: the way in went out, or there is none and she
        # comes out to meet him (not while the entrance photo is awaited).
        arrived = booking.get("instructions_sent_at") or (booking.get("arrived_at") and not self.way_in())
        return (bool(self.settings.get("arrival_to_owner", True)) and booking["state"] == bs.CONFIRMED
                and bool(arrived) and booking["ends_at"] + OUTCALL_AFTER_END > now)

    async def outcalls_under_way(self) -> list[dict[str, Any]]:
        """Confirmed outcalls that are not over yet, soonest first."""
        now = self.now()
        rows = await self.store.between(now - OUTCALL_AFTER_END, now + timedelta(days=self.settings["max_days_ahead"]),
                                        states=[bs.CONFIRMED])
        return [b for b in rows if self._under_way(b, now)]

    async def outcall_command(self, cmd: bookings.OwnerCommand) -> OwnerOutcome:
        """READY n / IN n from the owner."""
        word, t = "READY" if cmd.kind == "ready" else "IN", self.texts()
        if cmd.number is not None:
            booking = await self.store.by_number(cmd.number)
            if booking is None:
                return OwnerOutcome(t.say("no_booking", n=cmd.number))
            if not self._under_way(booking, self.now()):
                return OwnerOutcome(t.say("not_outcall", n=cmd.number), booking)
        else:
            step = "ready_at" if cmd.kind == "ready" else "in_taxi_at"
            open_ = [b for b in await self.outcalls_under_way() if not b.get(step)]
            if cmd.kind == "in_taxi":
                # The one she is ready for (or whose taxi is coming), over
                # one later today or tomorrow.
                started = [b for b in open_ if b.get("ready_at") or b.get("taxi_link_at")]
                open_ = started if len(started) == 1 else open_
            if len(open_) != 1:
                return OwnerOutcome(bookings.format_which(word.lower(), t=t))
            booking = open_[0]
        if cmd.kind == "ready":
            updated = await self.mark_ready(booking)
            if updated is None:
                return OwnerOutcome(t.say("already_ready", n=booking["number"]), booking)
            return OwnerOutcome(bookings.format_acknowledgement(updated, "ready", t=t), updated, went=True)
        updated = await self.mark_in_taxi(booking, by=bs.OWNER)
        if updated is None:
            return OwnerOutcome(t.say("already_in_taxi", n=booking["number"]), booking)
        return OwnerOutcome(bookings.format_acknowledgement(updated, "in_taxi", t=t), updated, went=True)

    async def admin_outcall_step(self, booking: dict[str, Any], action: str) -> dict[str, Any]:
        """She is ready / in the taxi, said in the panel for her (she told
        the operator another way). The owner hears it too."""
        n = booking["number"]
        if not self.outcall_taxi():
            raise bs.IllegalTransition("the outcall taxi steps are off (booking.outcall_taxi)")
        if not self._under_way(booking, self.now()):
            raise bs.IllegalTransition(f"booking #{n} is not a confirmed outcall that is still to come")
        if action == "ready":
            updated = await self.mark_ready(booking)
            if updated is None:
                raise bs.IllegalTransition(f"booking #{n} is marked ready already")
        else:
            updated = await self.mark_in_taxi(booking, by=bs.ADMIN)
            if updated is None:
                raise bs.IllegalTransition(f"the customer of #{n} already knows she is in the taxi")
        if self.settings.get("provider"):
            t = self.texts()
            await self.send_owner(bookings.format_acknowledgement(updated, action, t=t) + " " + t.say("from_panel"),
                                  reason="panel booking change passed on to the owner")
        return booking_store.public(updated)

    async def mark_ready(self, booking: dict[str, Any]) -> Optional[dict[str, Any]]:
        """She is ready: he is asked to order the taxi (the address follows
        the reply). None when she was marked ready already."""
        updated = await self.store.set_once(booking["id"], "ready_at", self.now(), customer_notice=bs.NOTICE_READY)
        if updated is None:
            return None
        await self.announce(updated, f"🚕 Booking #{updated['number']}: she is ready — asking him to order the taxi "
                                     "and send its link.")
        await self.nudge(updated["chat_id"])
        return updated

    async def mark_in_taxi(self, booking: dict[str, Any], *, by: str) -> Optional[dict[str, Any]]:
        """She is in the taxi (her IN n, or the system when it did not come
        in time): he is asked for the way to him. None when he was told
        already."""
        updated = await self.store.set_once(booking["id"], "in_taxi_at", self.now(), in_taxi_by=by,
                                            customer_notice=bs.NOTICE_IN_TAXI)
        if updated is None:
            return None
        n = updated["number"]
        if by == bs.SYSTEM:
            t = self.texts()
            minutes = self.settings["outcall_in_taxi_minutes"]
            late = " " + t.say("news_in_taxi_late", n=n, minutes=minutes) if minutes else ""
            await self.tell_owner(updated, bookings.format_owner_update(
                updated, t.say("news_told_in_taxi", late=late), name=self.owner_name(), t=t))
        await self.announce(updated, f"🚕 Booking #{n}: she is in the taxi — asking him for the address, floor, "
                                     "way in and a photo of the place.")
        await self.nudge(updated["chat_id"])
        return updated

    async def mark_proof(self, booking: dict[str, Any]) -> Optional[dict[str, Any]]:
        """His photo of the place reached her: he is asked to order the taxi
        now (her address follows the reply), as after READY n. None when the
        proof came already."""
        now = self.now()
        updated = await self.store.set_once(booking["id"], "proof_at", now, ready_at=booking.get("ready_at") or now,
                                            customer_notice=bs.NOTICE_READY)
        if updated is None:
            return None
        await self.announce(updated, f"📷 Booking #{updated['number']}: his photo of the place went to the owner — "
                                     "asking him to order the taxi and send its link.")
        await self.nudge(updated["chat_id"])
        return updated

    async def pass_on(self, chat_id: int, text: str, photo: Any = None) -> Optional[str]:
        """A customer's message while their outcall is under way, or after he
        has arrived for an incall, goes on to the owner: his taxi link with
        what to answer, a photo as the photo itself, anything else word for
        word. So does what he writes after the owner asked him something
        (answer_to_her). Returns what to store for a photo that went on,
        else None. Never raises: the reply goes on whatever happens here."""
        if not self.enabled():
            return None
        try:
            now = self.now()
            live = await self.store.for_chat(chat_id)
            booking = next((b for b in live if b["state"] == bs.CONFIRMED and self._passing_on(b, now)), None)
            if booking is None:
                # She asked him something: his answer goes to her, whatever
                # became of the booking since (she may ask after it is over).
                asked = next((b for b in await self.store.for_chat(chat_id, bs.STATES, include_past=True)
                              if self._asked(b, now)), None)
                if asked is None:
                    return None
                if photo is not None and self.photo_check_ready() and self._arriving(live) is not None:
                    # His photo of the entrance is for the arrival check
                    # (read_photo), which sends him the way in.
                    return None
                return await self.answer_to_her(asked, text, photo)
            text = (text or "").strip()
            ready = bool(booking.get("ready_at"))
            links = ([link for link in bookings.find_links(text) if bookings.is_taxi_link(link, ready=ready)]
                     if bookings.is_outcall(booking) else [])
            if links:
                booking = await self.taxi_link(booking, links[-1], text)
            if photo is not None:
                proof = self.wants_proof(booking)
                went = await self.pass_on_photo(booking, photo, "" if links else text, proof=proof)
                if proof and went:
                    await self.mark_proof(booking)
                elif proof:
                    self.add_line(chat_id, bookings.PROOF_AGAIN_LINE)
                return bookings.PHOTO_PASSED_ON
            if text and not links:
                await self.tell_owner(booking, bookings.format_passed_on(booking, text, t=self.texts()))
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[%s] Passing a message on to the owner failed (chat %s)", self.rt.session_id, chat_id)
            await self.rt.push_error(chat_id, f"Could not pass this message on to the owner: "
                                              f"{type(exc).__name__}: {exc}")
            return None

    def _asked(self, booking: dict[str, Any], now: datetime) -> bool:
        """The owner asked him something about this booking (booker_asked_at)
        less than ASKED_FOR ago: his answers go to her, until she acts on it."""
        at = booking.get("booker_asked_at")
        return at is not None and now - at < ASKED_FOR

    async def answer_to_her(self, booking: dict[str, Any], text: str, photo: Any = None) -> Optional[str]:
        """What he writes after the owner asked him something goes on to her
        word for word, a photo as the photo itself (never as the outcall's
        proof). His reply goes on as usual. Returns what to store for a
        photo that went on."""
        text = (text or "").strip()
        if photo is not None:
            return PHOTO_TO_HER if await self.pass_on_photo(booking, photo, text) else None
        if text:
            await self.tell_owner(booking, bookings.format_passed_on(booking, text, t=self.texts()))
        return None

    async def taxi_link(self, booking: dict[str, Any], link: str, text: str) -> dict[str, Any]:
        """His taxi link goes on to her. The first one starts the clock for
        telling him she is in the taxi."""
        first = booking.get("taxi_link_at") is None
        fields: dict[str, Any] = {"taxi_link": link}
        if first:
            fields["taxi_link_at"] = self.now()
        booking = await self.store.set_fields(booking["id"], **fields)
        minutes = self.settings["outcall_in_taxi_minutes"]
        # No wait set: he is told she is in the taxi at once, so no IN n.
        await self.tell_owner(booking, bookings.format_taxi_link(
            booking, text, None if booking.get("in_taxi_at") or not minutes else minutes, t=self.texts()))
        self.add_line(booking["chat_id"], bookings.TAXI_LINK_LINE)
        await self.announce(booking, f"🚕 Booking #{booking['number']}: his taxi link went on to the owner.")
        if first and not minutes:
            booking = await self.mark_in_taxi(booking, by=bs.SYSTEM) or booking
        return booking

    async def pass_on_photo(self, booking: dict[str, Any], photo: Any, caption: str, *, proof: bool = False) -> bool:
        """His photo (the place, the entrance, the hotel card) to the owner,
        as a photo. `proof`: it is his photo of the place, asked for before
        the taxi. Whether the photo itself reached her."""
        rt, n, t = self.rt, booking["number"], self.texts()
        provider = await self.provider_chat_id()
        if provider is None:
            await rt.push_error(booking["chat_id"], f"Booking #{n}: could not pass his photo on to the owner "
                                                    "(check booking.provider).")
            return False
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "photo.jpg"
            try:
                await rt.transport.download_photo(photo, path)
            except Exception as exc:
                log.warning("[%s] Could not download an outcall photo: %s", rt.session_id, type(exc).__name__)
            if not path.exists() or not path.stat().st_size:
                # WhatsApp keeps view-once photos from linked devices.
                again = t.say("photo_again_asked" if proof else "photo_again_ask")
                await self.tell_owner(booking, bookings.format_passed_on(
                    booking, (caption + "\n" if caption else "") + t.say("photo_not_fetched", again=again), t=t))
                return False
            header = (bookings.format_proof_header(booking, caption, t=t) if proof
                      else bookings.format_photo_header(booking, caption, t=t))
            header_row = await self.send_owner(header, reason=f"booking #{n}: his photo, to the owner")
            if header_row is None:
                await rt.push_error(booking["chat_id"], f"Booking #{n}: could not pass his photo on to the owner.")
                return False
            self._remember(header_row, n)
            try:
                photo_row = await rt.send_photo_as_me(provider, path, text=f"[photo from booking #{n}]",
                                                      reason=f"booking #{n}: his photo, to the owner")
                self._remember(photo_row, n)
            except Exception as exc:
                if not await rt.handle_send_failure(provider, exc):
                    await rt.push_error(booking["chat_id"], f"Booking #{n}: could not pass his photo on to the "
                                                            f"owner: {type(exc).__name__}: {exc}")
                return False
        await self.announce(booking, f"📷 Booking #{n}: his photo went on to the owner.")
        return True

    def ready_due(self, booking: dict[str, Any]) -> datetime:
        """When she is reminded to send READY n: her getting-ready time after
        the confirmation, and not before that long ahead of the start."""
        minutes = timedelta(minutes=self.settings["outcall_ready_minutes"])
        decided = booking.get("decided_at") or booking["created_at"]
        return max(decided + minutes, booking["starts_at"] - minutes)

    async def outcall_clock(self, booking: dict[str, Any], now: datetime) -> None:
        """The timed taxi steps of one outcall (the scheduler's tick): he is
        told she is in the taxi when her IN n did not come in time; she is
        reminded once to send READY n (not with the photo proof)."""
        if booking.get("in_taxi_at"):
            return
        if booking.get("taxi_link_at"):
            if now >= booking["taxi_link_at"] + timedelta(minutes=self.settings["outcall_in_taxi_minutes"]):
                await self.mark_in_taxi(booking, by=bs.SYSTEM)
            return
        if self.outcall_proof():
            # He sends the photo; she is not asked for READY n.
            return
        if booking.get("ready_at") or booking.get("ready_reminded_at") or now < self.ready_due(booking):
            return
        updated = await self.store.set_once(booking["id"], "ready_reminded_at", now)
        if updated is not None:
            await self.tell_owner(updated, bookings.format_ready_reminder(updated, name=self.owner_name(),
                                                                         t=self.texts()))

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

        async def drop_stale_words(booking: dict[str, Any]) -> None:
            stale = [item for item in booking["booker_news"] if not _news_fresh(item, booking, now)]
            if not stale:
                return
            # She is told first; only then are they dropped. Her chat out of
            # reach: tried again every DROP_RETRY (the admin told once), and
            # after DROP_GIVE_UP dropped anyway.
            failed = self._drop_failed.get(booking["id"])
            if failed is not None and now - failed[1] < DROP_RETRY:
                return
            if not await self.tell_owner(booking, bookings.format_words_dropped(booking, stale, t=self.texts()),
                                         report=failed is None):
                first = failed[0] if failed is not None else now
                if now - first < DROP_GIVE_UP:
                    self._drop_failed[booking["id"]] = (first, now)
                    return
                log.warning("[%s] Booking #%s: the owner could not be told for a day that her old words for him "
                            "were dropped; dropped anyway.", self.rt.session_id, booking["number"])
            self._drop_failed.pop(booking["id"], None)
            await self.store.clear_booker_news(booking["id"], [item["id"] for item in stale if item.get("id")])
            asked = [_news_at(item) for item in stale if item.get("ask")]
            fresh_ask = any(item.get("ask") for item in booking["booker_news"] if item not in stale)
            if asked and not fresh_ask and booking.get("booker_asked_at") \
                    and booking["booker_asked_at"] <= max(asked) + timedelta(seconds=1):
                # Her question to him that never reached him: no answer to wait for.
                await self.store.set_fields(booking["id"], booker_asked_at=None)

        if not self.rt.paused():
            # Nothing reaches her while sending is off: kept until it is on.
            for booking in await self.store.all_with_news():
                await guarded(f"dropping old words on #{booking['number']}", lambda b=booking: drop_stale_words(b))

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

        for booking in await self.store.due_cleanups(now):
            await guarded(f"deleting the arrival messages of #{booking['number']}",
                          lambda b=booking: self.clean_up_arrival(b))

        # While sending is off nothing is claimed: the steps wait for it.
        if self.outcall_taxi() and not self.rt.paused():
            for booking in await self.outcalls_under_way():
                await guarded(f"the taxi steps of #{booking['number']}",
                              lambda b=booking: self.outcall_clock(b, now))

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
            # A new time, or a new length: the event is made again.
            moved = before is not None and (before["starts_at"], before["ends_at"]) != (after["starts_at"],
                                                                                         after["ends_at"])
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
            "cancel": "cancelled", "decline": "declined", "expire": "lapsed", "change_length": "changed",
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
        if change.action in ("reschedule", "change_length"):
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


def _news_at(item: dict[str, Any]) -> datetime:
    """When she gave these words (booker_news_item's "at")."""
    try:
        at = datetime.fromisoformat(str(item.get("at") or ""))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _news_fresh(item: dict[str, Any], booking: dict[str, Any], now: datetime) -> bool:
    """Her words still worth saying: younger than NEWS_FOR, and — for a
    meeting that was on — not about one that has ended since she gave them
    (words with a decline still go with it)."""
    at = _news_at(item)
    if at == datetime.max.replace(tzinfo=timezone.utc):
        return True
    if now - at >= NEWS_FOR:
        return False
    if booking["state"] not in (bs.CONFIRMED, bs.COMPLETED, bs.NO_SHOW):
        return True
    over = booking["ends_at"] + OUTCALL_AFTER_END
    return not (at < over <= now)


def _same_words(waiting: dict[str, Any], item: dict[str, Any]) -> bool:
    """The same words for him as some already waiting (any case, spacing)."""
    def key(i: dict[str, Any]) -> tuple[Any, ...]:
        fold = lambda text: " ".join(str(text or "").split()).casefold()  # noqa: E731
        return (fold(i.get("tell")), fold(i.get("ask")), sorted(fold(x) for x in i.get("extras_ok") or ()),
                sorted(fold(x) for x in i.get("extras_no") or ()))
    return key(waiting) == key(item)


def _without(text: str, gone: Sequence[str]) -> str:
    """A comma list of extras (bookings.extras_ok / extras_no) without the
    ones in `gone`, in any case."""
    drop = {item.lower() for item in gone}
    return ", ".join(item for item in bookings.extras_list(text) if item.lower() not in drop)


def _words(cmd: bookings.OwnerCommand) -> str:
    """The owner's words for him, in short, for the note in his chat."""
    parts = [f'tell "{cmd.tell}"'] if cmd.tell else []
    if cmd.ask:
        parts.append(f'ask "{cmd.ask}"')
    if cmd.extras_ok:
        parts.append("yes to " + ", ".join(cmd.extras_ok))
    if cmd.extras_no:
        parts.append("no " + ", ".join(cmd.extras_no))
    return "; ".join(parts)
