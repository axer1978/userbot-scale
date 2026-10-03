"""Bookings, opening hours, waitlist and reminders in Postgres, per tenant.

`BookingStore` is bound to one tenant (and the account that serves it) and
has no method that takes another tenant: every statement is scoped by
`tenant_id`, like database.Database. State changes go through `apply()`
with a `booking_states.Change`; the UPDATE only matches while the booking
is still in the state the change was computed from, so two people (or the
owner and the scheduler) acting on one booking at once cannot both win.
Every change writes a booking_events row and an audit_log row in the same
transaction.

Two live bookings of a tenant can't overlap: the exclusion constraint in
migration 0003 refuses it, which surfaces here as `SlotTaken`. The
availability check in code (availability.py) runs first and gives the
friendly answer; the constraint is the guarantee.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import asyncpg

import audit
import availability
import booking_states as bs
import crypto

log = logging.getLogger(__name__)


class SlotTaken(Exception):
    """Another live booking already holds (part of) that time."""


class StaleBooking(Exception):
    """The booking changed state since it was read; nothing was written."""


class BookingNotFound(LookupError):
    pass


# Columns apply()/set_fields() may write. Anything else is a programming
# error, not something a caller can smuggle in.
_WRITABLE = {
    "starts_at", "ends_at", "blocked_until", "service", "notes",
    "proposed_starts_at", "proposed_ends_at", "proposed_by",
    "provider_chat_id", "provider_message_id", "provider_wa_message_id", "calendar_event_id",
    "customer_notice", "cancelled_by", "cancel_reason", "decided_by", "decided_at",
    "attendance_confirmed_at", "arrived_at", "arrival_photo_match", "instructions_sent_at",
    "instructions_message_ids", "instructions_cleanup_at", "instructions_cleaned_at",
    "customer_name", "customer_username",
}


def _row(record: Optional[asyncpg.Record]) -> Optional[dict[str, Any]]:
    return dict(record) if record is not None else None


def public(booking: dict[str, Any]) -> dict[str, Any]:
    """A booking for the panel / JSON: datetimes as ISO strings."""
    out = {}
    for key, value in booking.items():
        out[key] = value.isoformat() if isinstance(value, (datetime, date)) else value
    return out


class BookingStore:
    def __init__(self, pool: asyncpg.Pool, tenant_id: int, session_id: str) -> None:
        self.pool = pool
        self.tenant_id = tenant_id
        self.session_id = session_id
        # The account's network (customer_ref namespace); set by the runtime.
        self.channel = "telegram"

    # ------------------------------------------------------------ reading

    async def get(self, booking_id: int) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND id = $2", self.tenant_id, booking_id,
        )
        if row is None:
            raise BookingNotFound(f"no booking {booking_id}")
        return dict(row)

    async def by_number(self, number: int) -> Optional[dict[str, Any]]:
        return _row(await self.pool.fetchrow(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND number = $2", self.tenant_id, number,
        ))

    async def for_chat(
        self, chat_id: int, states: Sequence[str] = bs.LIVE, *, include_past: bool = False,
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND chat_id = $2 AND state = ANY($3::text[]) "
            "AND ($4 OR ends_at > now() - interval '12 hours') ORDER BY starts_at",
            self.tenant_id, chat_id, list(states), include_past,
        )
        return [dict(r) for r in rows]

    async def with_notice(self, chat_id: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND chat_id = $2 AND customer_notice IS NOT NULL "
            "ORDER BY updated_at", self.tenant_id, chat_id,
        )
        return [dict(r) for r in rows]

    async def awaiting_owner(self) -> list[dict[str, Any]]:
        """What the owner can answer right now: new requests, and moves the
        customer asked for on confirmed bookings."""
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND ("
            "  state IN ('requested', 'pending') OR (state = 'confirmed' AND proposed_by = 'customer')"
            ") AND starts_at > now() - interval '1 day' ORDER BY number",
            self.tenant_id,
        )
        return [dict(r) for r in rows]

    async def due_cleanups(self, now: datetime) -> list[dict[str, Any]]:
        """Bookings whose arrival messages are due to be deleted."""
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND instructions_cleanup_at <= $2 "
            "AND instructions_cleaned_at IS NULL ORDER BY instructions_cleanup_at",
            self.tenant_id, now,
        )
        return [dict(r) for r in rows]

    async def unsent(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND state = 'requested' AND starts_at > now() "
            "ORDER BY number", self.tenant_id,
        )
        return [dict(r) for r in rows]

    async def between(
        self, start: datetime, end: datetime, states: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND starts_at < $3 AND ends_at > $2 "
            "AND ($4::text[] IS NULL OR state = ANY($4::text[])) ORDER BY starts_at, number",
            self.tenant_id, start, end, list(states) if states else None,
        )
        return [dict(r) for r in rows]

    async def busy(
        self, start: datetime, end: datetime, *, exclude_id: Optional[int] = None,
    ) -> list[availability.Busy]:
        """Live bookings touching [start, end), as blocked intervals. A
        pending proposal also blocks its proposed time, so nobody else is
        offered a time one side has already put forward."""
        rows = await self.pool.fetch(
            "SELECT id, starts_at, blocked_until, proposed_starts_at, proposed_ends_at, ends_at FROM bookings "
            "WHERE tenant_id = $1 AND state = ANY($2::text[]) AND id <> $5 AND ("
            "  (starts_at < $4 AND blocked_until > $3) OR "
            "  (proposed_starts_at IS NOT NULL AND proposed_starts_at < $4 AND proposed_ends_at > $3))",
            self.tenant_id, list(bs.LIVE), start, end, exclude_id or 0,
        )
        out = []
        for r in rows:
            out.append(availability.Busy(r["starts_at"], r["blocked_until"]))
            if r["proposed_starts_at"] is not None:
                buffer = r["blocked_until"] - r["ends_at"]
                out.append(availability.Busy(r["proposed_starts_at"], r["proposed_ends_at"] + buffer))
        return out

    async def events(self, booking_id: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM booking_events WHERE tenant_id = $1 AND booking_id = $2 ORDER BY id",
            self.tenant_id, booking_id,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ writing

    async def _next_number(self, con: asyncpg.Connection, at_least: int = 0) -> int:
        return await con.fetchval(
            "INSERT INTO booking_counters (tenant_id, last_number) VALUES ($1, GREATEST(1, $2)) "
            "ON CONFLICT (tenant_id) DO UPDATE SET last_number = GREATEST(booking_counters.last_number + 1, $2) "
            "RETURNING last_number",
            self.tenant_id, at_least,
        )

    async def create(
        self, *, chat_id: int, customer_name: str, customer_username: Optional[str],
        starts_at: datetime, ends_at: datetime, buffer_minutes: int, tz: str,
        service: str = "", notes: str = "", actor: str = bs.CUSTOMER, reason: str = "",
    ) -> dict[str, Any]:
        """A new request, state `requested`, with the tenant's next number."""
        blocked_until = ends_at + timedelta(minutes=buffer_minutes)
        ref = crypto.customer_ref(self.tenant_id, self.channel, chat_id)
        try:
            async with self.pool.acquire() as con, con.transaction():
                number = await self._next_number(con)
                row = await con.fetchrow(
                    "INSERT INTO bookings (tenant_id, session_id, number, chat_id, customer_ref, customer_name, "
                    "customer_username, service, notes, starts_at, ends_at, blocked_until, tz, state) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'requested') RETURNING *",
                    self.tenant_id, self.session_id, number, chat_id, ref, customer_name[:200],
                    customer_username, service[:120], notes[:500], starts_at, ends_at, blocked_until, tz,
                )
                await self._event(con, row["id"], None, bs.REQUESTED, "create", actor, reason)
                await audit.record(
                    con, tenant_id=self.tenant_id, actor=_audit_actor(actor), event=audit.BOOKING_CREATED,
                    reason=reason or "booking requested in chat",
                    payload={"booking": number, "starts_at": starts_at.isoformat(), "service": service},
                )
        except asyncpg.exceptions.ExclusionViolationError:
            raise SlotTaken("that time overlaps another booking") from None
        return dict(row)

    async def apply(self, booking: dict[str, Any], change: bs.Change, *, actor: str) -> dict[str, Any]:
        """Write a transition computed by booking_states from `booking`."""
        fields = dict(change.fields)
        unknown = set(fields) - _WRITABLE
        if unknown:
            raise ValueError(f"not writable: {sorted(unknown)}")
        sets = ["state = $4", "updated_at = now()"]
        args: list[Any] = [self.tenant_id, booking["id"], change.from_state, change.to_state]
        for key, value in fields.items():
            args.append(value)
            sets.append(f"{key} = ${len(args)}")
        try:
            async with self.pool.acquire() as con, con.transaction():
                row = await con.fetchrow(
                    f"UPDATE bookings SET {', '.join(sets)} "
                    "WHERE tenant_id = $1 AND id = $2 AND state = $3 RETURNING *",
                    *args,
                )
                if row is None:
                    raise StaleBooking(f"booking #{booking['number']} changed meanwhile")
                payload = {
                    "booking": booking["number"], "action": change.action,
                    "from": change.from_state, "to": change.to_state,
                    **{k: v for k, v in fields.items() if k in (
                        "starts_at", "ends_at", "proposed_starts_at", "cancel_reason")},
                }
                await self._event(con, booking["id"], change.from_state, change.to_state, change.action,
                                  actor, change.reason, payload)
                await audit.record(
                    con, tenant_id=self.tenant_id, actor=_audit_actor(actor), event=audit.BOOKING_CHANGED,
                    reason=change.reason or change.action, payload=payload,
                )
        except asyncpg.exceptions.ExclusionViolationError:
            raise SlotTaken("that time overlaps another booking") from None
        return dict(row)

    async def set_fields(self, booking_id: int, **fields: Any) -> dict[str, Any]:
        """Bookkeeping that is not a state change (calendar id, notice told,
        arrival noted)."""
        unknown = set(fields) - _WRITABLE
        if unknown or not fields:
            raise ValueError(f"not writable: {sorted(unknown)}")
        sets, args = ["updated_at = now()"], [self.tenant_id, booking_id]
        for key, value in fields.items():
            args.append(value)
            sets.append(f"{key} = ${len(args)}")
        row = await self.pool.fetchrow(
            f"UPDATE bookings SET {', '.join(sets)} WHERE tenant_id = $1 AND id = $2 RETURNING *", *args,
        )
        if row is None:
            raise BookingNotFound(f"no booking {booking_id}")
        return dict(row)

    async def _event(
        self, con: asyncpg.Connection, booking_id: int, from_state: Optional[str], to_state: str,
        action: str, actor: str, reason: str, payload: Optional[dict[str, Any]] = None,
    ) -> None:
        await con.execute(
            "INSERT INTO booking_events (tenant_id, booking_id, from_state, to_state, action, actor, reason, payload) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)",
            self.tenant_id, booking_id, from_state, to_state, action, actor, reason or "",
            json.dumps(payload or {}, default=str),
        )

    # ---------------------------------------------------------- reminders

    async def due_reminders(
        self, now: datetime, reminders: Sequence[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any], list[int]]]:
        """(booking, reminder, skipped) for each booking with a reminder due
        and not yet claimed. Only the latest due reminder is sent; the
        earlier ones in `skipped` (their time passed while nothing ran) are
        for the caller to claim unsent. A reminder whose time had already
        passed when the booking was confirmed is not due at all."""
        if not reminders:
            return []
        longest = max(r["minutes_before"] for r in reminders)
        rows = await self.pool.fetch(
            "SELECT * FROM bookings WHERE tenant_id = $1 AND state = 'confirmed' "
            "AND starts_at > $2::timestamptz AND starts_at <= $2::timestamptz + make_interval(mins => $3) "
            "ORDER BY starts_at",
            self.tenant_id, now, longest,
        )
        if not rows:
            return []
        claimed = {
            (r["booking_id"], r["minutes_before"], r["starts_at"])
            for r in await self.pool.fetch(
                "SELECT booking_id, minutes_before, starts_at FROM booking_reminders "
                "WHERE tenant_id = $1 AND booking_id = ANY($2::bigint[])",
                self.tenant_id, [r["id"] for r in rows],
            )
        }
        out = []
        for row in rows:
            booking = dict(row)
            confirmed_at = booking["decided_at"] or booking["created_at"]
            # Only the latest due reminder of a booking goes out: after a
            # restart that skipped the 24 h one, the 2 h one alone is sent.
            due = [
                r for r in reminders
                if booking["starts_at"] - timedelta(minutes=r["minutes_before"]) <= now
                and booking["starts_at"] - timedelta(minutes=r["minutes_before"]) >= confirmed_at
                and (booking["id"], r["minutes_before"], booking["starts_at"]) not in claimed
            ]
            if due:
                latest = min(due, key=lambda r: r["minutes_before"])
                out.append((booking, latest, [r["minutes_before"] for r in due if r is not latest]))
        return out

    async def claim_reminder(self, booking: dict[str, Any], minutes_before: int) -> bool:
        """True exactly once per booking, reminder and start time."""
        claimed = await self.pool.fetchval(
            "INSERT INTO booking_reminders (tenant_id, booking_id, minutes_before, starts_at) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING true",
            self.tenant_id, booking["id"], minutes_before, booking["starts_at"],
        )
        return bool(claimed)

    async def mark_reminder_sent(self, booking: dict[str, Any], minutes_before: int) -> None:
        await self.pool.execute(
            "UPDATE booking_reminders SET sent_at = now() "
            "WHERE tenant_id = $1 AND booking_id = $2 AND minutes_before = $3 AND starts_at = $4",
            self.tenant_id, booking["id"], minutes_before, booking["starts_at"],
        )

    async def last_reminder(self, booking: dict[str, Any]) -> Optional[dict[str, Any]]:
        return _row(await self.pool.fetchrow(
            "SELECT * FROM booking_reminders WHERE tenant_id = $1 AND booking_id = $2 AND starts_at = $3 "
            "ORDER BY claimed_at DESC LIMIT 1", self.tenant_id, booking["id"], booking["starts_at"],
        ))

    # ------------------------------------------------------- availability

    async def rule_rows(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT weekday, start_time, end_time, slot_minutes, buffer_minutes FROM availability_rules "
            "WHERE tenant_id = $1 ORDER BY weekday, start_time", self.tenant_id,
        )
        return [dict(r) for r in rows]

    async def rules(self) -> list[availability.Rule]:
        return [availability.rule_from_row(r) for r in await self.rule_rows()]

    async def save_rules(self, rows: Iterable[dict[str, Any]], *, actor: str, reason: str = "") -> list[dict[str, Any]]:
        """Replace the tenant's opening hours. Validated first (a bad row
        refuses the whole save), audited with before and after."""
        parsed = [availability.rule_from_row(r) for r in rows]
        before = await self.rule_rows()
        async with self.pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM availability_rules WHERE tenant_id = $1", self.tenant_id)
            for rule in parsed:
                await con.execute(
                    "INSERT INTO availability_rules (tenant_id, weekday, start_time, end_time, slot_minutes, "
                    "buffer_minutes) VALUES ($1, $2, $3, $4, $5, $6)",
                    self.tenant_id, rule.weekday, rule.start, rule.end, rule.slot_minutes, rule.buffer_minutes,
                )
            after = [
                {"weekday": r.weekday, "start_time": r.start, "end_time": r.end,
                 "slot_minutes": r.slot_minutes, "buffer_minutes": r.buffer_minutes} for r in parsed
            ]
            await audit.record(
                con, tenant_id=self.tenant_id, actor=_audit_actor(actor), event=audit.AVAILABILITY_CHANGED,
                reason=reason or "opening hours saved", payload={"before": before, "after": after},
            )
        return await self.rule_rows()

    # ----------------------------------------------------------- waitlist

    async def add_waitlist(
        self, *, chat_id: int, customer_name: str, wanted_from: datetime, wanted_to: datetime, service: str = "",
    ) -> dict[str, Any]:
        """One waiting entry per person: a new wish replaces their old one."""
        ref = crypto.customer_ref(self.tenant_id, self.channel, chat_id)
        async with self.pool.acquire() as con, con.transaction():
            await con.execute(
                "UPDATE waitlist SET state = 'removed' WHERE tenant_id = $1 AND customer_ref = $2 "
                "AND state IN ('waiting', 'offered')", self.tenant_id, ref,
            )
            row = await con.fetchrow(
                "INSERT INTO waitlist (tenant_id, session_id, customer_ref, chat_id, customer_name, "
                "wanted_from, wanted_to, service) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                self.tenant_id, self.session_id, ref, chat_id, customer_name[:200], wanted_from, wanted_to,
                service[:120],
            )
            await audit.record(
                con, tenant_id=self.tenant_id, actor=audit.BOT, event=audit.WAITLIST_CHANGED,
                reason="added to the waitlist",
                payload={"entry": row["id"], "from": wanted_from.isoformat(), "to": wanted_to.isoformat()},
            )
        return dict(row)

    async def waitlist(self, states: Sequence[str] = ("waiting", "offered")) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM waitlist WHERE tenant_id = $1 AND state = ANY($2::text[]) ORDER BY created_at, id",
            self.tenant_id, list(states),
        )
        return [dict(r) for r in rows]

    async def waitlist_for_chat(self, chat_id: int) -> Optional[dict[str, Any]]:
        return _row(await self.pool.fetchrow(
            "SELECT * FROM waitlist WHERE tenant_id = $1 AND chat_id = $2 AND state IN ('waiting', 'offered') "
            "ORDER BY id DESC LIMIT 1", self.tenant_id, chat_id,
        ))

    async def first_in_line(self, starts_at: datetime, ends_at: datetime) -> Optional[dict[str, Any]]:
        """The earliest waiting entry whose wished-for range covers the slot."""
        return _row(await self.pool.fetchrow(
            "SELECT * FROM waitlist WHERE tenant_id = $1 AND state = 'waiting' "
            "AND wanted_from <= $2 AND wanted_to >= $3 AND wanted_to > now() "
            "ORDER BY created_at, id LIMIT 1", self.tenant_id, starts_at, ends_at,
        ))

    async def set_waitlist_state(
        self, entry_id: int, state: str, *, offered_starts_at: Optional[datetime] = None, reason: str = "",
        actor: str = audit.BOT,
    ) -> Optional[dict[str, Any]]:
        async with self.pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                "UPDATE waitlist SET state = $3, "
                "offered_starts_at = CASE WHEN $3 = 'offered' THEN $4 ELSE offered_starts_at END, "
                "offered_at = CASE WHEN $3 = 'offered' THEN now() ELSE offered_at END "
                "WHERE tenant_id = $1 AND id = $2 RETURNING *",
                self.tenant_id, entry_id, state, offered_starts_at,
            )
            if row is not None:
                await audit.record(
                    con, tenant_id=self.tenant_id, actor=actor, event=audit.WAITLIST_CHANGED,
                    reason=reason or state, payload={"entry": entry_id, "state": state},
                )
        return _row(row)

    async def expired_offers(self, now: datetime, hours: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM waitlist WHERE tenant_id = $1 AND state = 'offered' "
            "AND (offered_at < $2::timestamptz - make_interval(hours => $3) OR offered_starts_at <= $2::timestamptz)",
            self.tenant_id, now, hours,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ legacy

    async def import_legacy_file(self, path: Path) -> int:
        """Bring a pre-platform bookings.json in, once. Numbers are kept.
        The rows are marked legacy: old data may overlap, so they are exempt
        from the overlap constraint (the availability check still sees
        them). The file is renamed afterwards so it is not imported twice."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s for import: %s", path, exc)
            return 0
        items = raw.get("bookings", []) if isinstance(raw, dict) else []
        status_map = {"pending": bs.PENDING, "confirmed": bs.CONFIRMED,
                      "declined": bs.CANCELLED, "superseded": bs.CANCELLED}
        imported = 0
        async with self.pool.acquire() as con, con.transaction():
            for item in items:
                try:
                    starts = datetime.fromisoformat(item["start"])
                    ends = datetime.fromisoformat(item["end"])
                    number, chat_id = int(item["id"]), int(item["chat_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if starts.tzinfo is None or ends <= starts:
                    continue
                status = item.get("status", "pending")
                state = status_map.get(status, bs.CANCELLED)
                if await con.fetchval(
                    "SELECT 1 FROM bookings WHERE tenant_id = $1 AND number = $2", self.tenant_id, number,
                ):
                    continue
                await self._next_number(con, at_least=number)
                decided_by = {"provider": bs.OWNER, "panel": bs.ADMIN}.get(item.get("decided_by") or "")
                cancelled_by, reason = None, None
                if status == "declined":
                    cancelled_by, reason = decided_by or bs.OWNER, "declined"
                elif status == "superseded":
                    cancelled_by, reason = bs.CUSTOMER, "replaced by a newer request"
                elif state == bs.CANCELLED:
                    cancelled_by, reason = bs.SYSTEM, f"imported with status {status}"
                notice = None
                if status in ("confirmed", "declined") and not item.get("client_notified"):
                    notice = bs.NOTICE_CONFIRMED if status == "confirmed" else bs.NOTICE_DECLINED
                row = await con.fetchrow(
                    "INSERT INTO bookings (tenant_id, session_id, number, chat_id, customer_ref, customer_name, "
                    "customer_username, service, notes, starts_at, ends_at, blocked_until, tz, state, "
                    "provider_chat_id, provider_message_id, calendar_event_id, customer_notice, cancelled_by, "
                    "cancel_reason, decided_by, decided_at, arrived_at, instructions_sent_at, legacy, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11, $12, $13, $14, $15, $16, $17, "
                    "$18, $19, $20, $21, $22, $23, TRUE, COALESCE($24, now())) RETURNING id",
                    self.tenant_id, self.session_id, number, chat_id,
                    crypto.customer_ref(self.tenant_id, self.channel, chat_id),
                    str(item.get("client_name") or "")[:200], item.get("client_username"),
                    str(item.get("title") or "")[:120], str(item.get("notes") or "")[:500],
                    starts, ends, str(item.get("timezone") or "UTC"), state,
                    item.get("provider_chat_id"), item.get("provider_message_id"), item.get("calendar_event_id"),
                    notice, cancelled_by, reason, decided_by, _ts(item.get("decided_at")),
                    _ts(item.get("arrived_at")), _ts(item.get("instructions_sent_at")), _ts(item.get("created_at")),
                )
                await self._event(con, row["id"], None, state, "import", bs.SYSTEM, "imported from bookings.json")
                imported += 1
            await audit.record(
                con, tenant_id=self.tenant_id, actor=audit.MIGRATION, event=audit.BOOKINGS_IMPORTED,
                reason="bookings.json imported", payload={"count": imported, "file": path.name},
            )
        path.rename(path.with_name(path.name + ".imported"))
        return imported


def _ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _audit_actor(actor: str) -> str:
    """booking_states actors -> audit actors. The owner answering by text
    and an admin in the panel are both people; the customer's actions are
    recorded as the bot's (it acted on what they wrote)."""
    return {bs.ADMIN: audit.ADMIN, bs.OWNER: "owner", bs.SYSTEM: audit.SYSTEM}.get(actor, audit.BOT)
