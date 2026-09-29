"""The booking state machine: which change is allowed from which state, by whom.

Every change is a function here. It takes the booking as it is now (a row
from booking_store) and returns a `Change` describing the new state and the
columns to write, or raises `IllegalTransition`. It never writes anything:
booking_store.apply() does, with `WHERE state = <from_state>` so two changes
racing on one booking cannot both win.

States:

    requested ──submitted──▶ pending ──confirm──▶ confirmed ──mark_completed──▶ completed
        │                     │  ▲                  │  │
        │                     │  └─change_request   │  └──mark_no_show──▶ no_show
        │                     ├──propose (stays)    ├──propose / accept_proposal / reschedule (stays)
        └──decline/cancel/expire──▶ cancelled ◀─────┴──cancel

Nothing here confirms a booking on its own: `confirm` needs the owner or an
admin, and `accept_proposal` confirms only a time the other side has already
put forward in writing (the owner's new time, or the customer's reschedule
request that the owner then answers YES to).

A change the customer has not heard about yet sets `notice`; the next reply
in their chat carries it (session_runtime.booking_note).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

REQUESTED = "requested"
PENDING = "pending"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"
NO_SHOW = "no_show"
COMPLETED = "completed"
STATES = (REQUESTED, PENDING, CONFIRMED, CANCELLED, NO_SHOW, COMPLETED)
# States that hold the slot (the overlap rule in migration 0003 uses these).
LIVE = (REQUESTED, PENDING, CONFIRMED)
FINAL = (CANCELLED, NO_SHOW, COMPLETED)

OWNER = "owner"
CUSTOMER = "customer"
ADMIN = "admin"
SYSTEM = "system"
ACTORS = (OWNER, CUSTOMER, ADMIN, SYSTEM)
# Who may decide about a booking: people on the business's side.
DECIDERS = (OWNER, ADMIN)

# What the customer is owed a message about (bookings.customer_notice).
NOTICE_CONFIRMED = "confirmed"
NOTICE_DECLINED = "declined"
NOTICE_PROPOSED = "proposed"
NOTICE_RESCHEDULED = "rescheduled"
NOTICE_CANCELLED = "cancelled"
NOTICE_EXPIRED = "expired"
NOTICE_REQUESTED = "requested"
NOTICE_KEPT = "kept"


class IllegalTransition(ValueError):
    """The change is not allowed from the booking's current state, by this
    actor, or at this time."""


@dataclass(frozen=True)
class Change:
    action: str
    from_state: str
    to_state: str
    # Columns to set besides `state` (and updated_at).
    fields: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def _require(
    booking: Mapping[str, Any], action: str, states: tuple[str, ...],
    actor: str, actors: tuple[str, ...],
) -> str:
    state = booking["state"]
    if actor not in ACTORS:
        raise IllegalTransition(f"unknown actor {actor!r}")
    if state not in states:
        raise IllegalTransition(f"cannot {action} booking #{booking['number']}: it is {state}")
    if actor not in actors:
        raise IllegalTransition(f"{actor} cannot {action} a booking")
    return state


def _future(when: datetime, now: datetime, what: str) -> None:
    if when <= now:
        raise IllegalTransition(f"{what} is in the past")


_NO_PROPOSAL = {"proposed_starts_at": None, "proposed_ends_at": None, "proposed_by": None}


def submitted(booking: Mapping[str, Any], *, provider_chat_id: int, provider_message_id: Optional[int]) -> Change:
    """The request reached the owner."""
    state = _require(booking, "submit", (REQUESTED,), SYSTEM, (SYSTEM,))
    return Change("submitted", state, PENDING, {
        "provider_chat_id": provider_chat_id, "provider_message_id": provider_message_id,
    })


def confirm(booking: Mapping[str, Any], *, actor: str, now: datetime) -> Change:
    """The owner said yes to the time as it stands."""
    state = _require(booking, "confirm", (REQUESTED, PENDING), actor, DECIDERS)
    _future(booking["starts_at"], now, "the booking time")
    return Change("confirm", state, CONFIRMED, {
        **_NO_PROPOSAL, "decided_by": actor, "decided_at": now, "customer_notice": NOTICE_CONFIRMED,
    })


def decline(booking: Mapping[str, Any], *, actor: str, now: datetime, reason: str = "") -> Change:
    state = _require(booking, "decline", (REQUESTED, PENDING), actor, DECIDERS)
    return Change("decline", state, CANCELLED, {
        **_NO_PROPOSAL, "decided_by": actor, "decided_at": now, "cancelled_by": actor,
        "cancel_reason": reason or "declined", "customer_notice": NOTICE_DECLINED,
    }, reason or "declined")


def change_request(
    booking: Mapping[str, Any], *, starts_at: datetime, ends_at: datetime, blocked_until: datetime,
    now: datetime, service: Optional[str] = None,
) -> Change:
    """The customer asked for a different time before the owner answered.
    Same booking, same number; it goes back to the owner."""
    state = _require(booking, "change", (REQUESTED, PENDING), CUSTOMER, (CUSTOMER,))
    _future(starts_at, now, "the new time")
    fields: dict[str, Any] = {
        **_NO_PROPOSAL, "starts_at": starts_at, "ends_at": ends_at, "blocked_until": blocked_until,
        "provider_message_id": None, "customer_notice": NOTICE_REQUESTED,
    }
    if service is not None:
        fields["service"] = service
    return Change("change_request", state, REQUESTED, fields)


def propose(
    booking: Mapping[str, Any], *, actor: str, starts_at: datetime, ends_at: datetime, now: datetime,
) -> Change:
    """One side puts forward a different time. The owner does it instead of
    YES/NO; the customer does it to move a confirmed booking. The booking
    keeps its state and slot until the other side accepts."""
    if actor == CUSTOMER:
        state = _require(booking, "propose a new time for", (CONFIRMED,), actor, (CUSTOMER,))
    else:
        state = _require(booking, "propose a new time for", (REQUESTED, PENDING, CONFIRMED), actor, DECIDERS)
    _future(starts_at, now, "the proposed time")
    if ends_at <= starts_at:
        raise IllegalTransition("the proposed end is before its start")
    side = CUSTOMER if actor == CUSTOMER else OWNER
    fields: dict[str, Any] = {
        "proposed_starts_at": starts_at, "proposed_ends_at": ends_at, "proposed_by": side,
    }
    if side == OWNER:
        fields["customer_notice"] = NOTICE_PROPOSED
    return Change("propose", state, state, fields)


def accept_proposal(
    booking: Mapping[str, Any], *, actor: str, now: datetime, blocked_until: datetime,
) -> Change:
    """The other side takes the proposed time: the owner's proposal by the
    customer, the customer's by the owner (YES n) or an admin."""
    proposer = booking.get("proposed_by")
    if proposer is None or booking.get("proposed_starts_at") is None:
        raise IllegalTransition(f"booking #{booking['number']} has no proposed time")
    allowed = (CUSTOMER,) if proposer == OWNER else DECIDERS
    state = _require(booking, "accept the proposed time of", (REQUESTED, PENDING, CONFIRMED), actor, allowed)
    starts_at, ends_at = booking["proposed_starts_at"], booking["proposed_ends_at"]
    _future(starts_at, now, "the proposed time")
    fields = {
        **_NO_PROPOSAL, "starts_at": starts_at, "ends_at": ends_at, "blocked_until": blocked_until,
        "decided_by": actor, "decided_at": now,
        "customer_notice": NOTICE_RESCHEDULED if state == CONFIRMED else NOTICE_CONFIRMED,
    }
    action = "reschedule" if state == CONFIRMED else "accept_proposal"
    return Change(action, state, CONFIRMED, fields)


def reject_proposal(booking: Mapping[str, Any], *, actor: str, now: datetime) -> Change:
    """The proposed time is turned down. A confirmed booking stays at its
    time; a request that was never confirmed is cancelled, since the owner
    did not take the original time either."""
    proposer = booking.get("proposed_by")
    if proposer is None:
        raise IllegalTransition(f"booking #{booking['number']} has no proposed time")
    allowed = (CUSTOMER,) if proposer == OWNER else DECIDERS
    state = _require(booking, "turn down the proposed time of", (REQUESTED, PENDING, CONFIRMED), actor, allowed)
    if state == CONFIRMED:
        notice = NOTICE_KEPT if actor != CUSTOMER else None
        fields: dict[str, Any] = dict(_NO_PROPOSAL)
        if notice:
            fields["customer_notice"] = notice
        return Change("reject_proposal", state, CONFIRMED, fields)
    return Change("reject_proposal", state, CANCELLED, {
        **_NO_PROPOSAL, "cancelled_by": actor, "cancel_reason": "the proposed time was not taken",
        "customer_notice": NOTICE_CANCELLED if actor != CUSTOMER else None,
    }, "the proposed time was not taken")


def reschedule(
    booking: Mapping[str, Any], *, actor: str, starts_at: datetime, ends_at: datetime,
    blocked_until: datetime, now: datetime,
) -> Change:
    """An admin moves a confirmed booking directly (from the panel). Its own
    action, not cancel + rebook: the number, history and reminders follow."""
    state = _require(booking, "reschedule", (CONFIRMED,), actor, (ADMIN,))
    _future(starts_at, now, "the new time")
    return Change("reschedule", state, CONFIRMED, {
        **_NO_PROPOSAL, "starts_at": starts_at, "ends_at": ends_at, "blocked_until": blocked_until,
        "customer_notice": NOTICE_RESCHEDULED,
    })


def cancel(booking: Mapping[str, Any], *, actor: str, now: datetime, reason: str = "") -> Change:
    state = _require(booking, "cancel", LIVE, actor, (OWNER, CUSTOMER, ADMIN))
    return Change("cancel", state, CANCELLED, {
        **_NO_PROPOSAL, "cancelled_by": actor, "cancel_reason": reason or f"cancelled by the {actor}",
        "customer_notice": NOTICE_CANCELLED,
    }, reason)


def expire(booking: Mapping[str, Any], *, now: datetime) -> Change:
    """Nobody answered before the time came. Not a decision about anyone:
    the slot is simply gone."""
    state = _require(booking, "expire", (REQUESTED, PENDING), SYSTEM, (SYSTEM,))
    if booking["starts_at"] > now:
        raise IllegalTransition(f"booking #{booking['number']} has not started yet")
    return Change("expire", state, CANCELLED, {
        **_NO_PROPOSAL, "cancelled_by": SYSTEM, "cancel_reason": "not answered before the start time",
        "customer_notice": NOTICE_EXPIRED,
    }, "not answered before the start time")


def _after_start(booking: Mapping[str, Any], now: datetime, action: str) -> None:
    if booking["starts_at"] > now:
        raise IllegalTransition(f"cannot {action} booking #{booking['number']} before it starts")


def mark_completed(booking: Mapping[str, Any], *, actor: str, now: datetime) -> Change:
    state = _require(booking, "mark completed", (CONFIRMED,), actor, DECIDERS)
    _after_start(booking, now, "complete")
    return Change("mark_completed", state, COMPLETED, {"decided_by": actor, "decided_at": now})


def mark_no_show(booking: Mapping[str, Any], *, actor: str, now: datetime) -> Change:
    """Only a person can say someone did not come (never inferred)."""
    state = _require(booking, "mark as missed", (CONFIRMED,), actor, DECIDERS)
    _after_start(booking, now, "mark as missed")
    return Change("mark_no_show", state, NO_SHOW, {"decided_by": actor, "decided_at": now})


def confirm_attendance(booking: Mapping[str, Any], *, now: datetime) -> Change:
    """The customer says they are still coming (reminder reply or the page)."""
    state = _require(booking, "confirm attendance for", (CONFIRMED,), CUSTOMER, (CUSTOMER,))
    _future(booking["starts_at"], now, "the booking")
    return Change("confirm_attendance", state, CONFIRMED, {"attendance_confirmed_at": now})
