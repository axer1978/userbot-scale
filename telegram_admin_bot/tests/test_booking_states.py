"""The booking state machine: every change is allowed only from the states,
by the people and at the times booking_states says, and nothing confirms a
booking without a person."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import booking_states as bs

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=2)


def booking(state=bs.PENDING, *, starts=LATER, proposed_by=None, proposed=None):
    b = {"id": 1, "number": 7, "state": state, "starts_at": starts, "ends_at": starts + timedelta(hours=1),
         "blocked_until": starts + timedelta(hours=1), "proposed_by": proposed_by,
         "proposed_starts_at": proposed, "proposed_ends_at": proposed + timedelta(hours=1) if proposed else None}
    return b


# Every (transition, kwargs) the machine has, and the states it may start from.
def _calls():
    new = LATER + timedelta(hours=3)
    return {
        "confirm": (lambda b, a: bs.confirm(b, actor=a, now=NOW), {bs.REQUESTED, bs.PENDING}, set(bs.DECIDERS)),
        "decline": (lambda b, a: bs.decline(b, actor=a, now=NOW), {bs.REQUESTED, bs.PENDING}, set(bs.DECIDERS)),
        "cancel": (lambda b, a: bs.cancel(b, actor=a, now=NOW), set(bs.LIVE), {bs.OWNER, bs.CUSTOMER, bs.ADMIN}),
        "reschedule": (lambda b, a: bs.reschedule(b, actor=a, starts_at=new, ends_at=new + timedelta(hours=1),
                                                  blocked_until=new + timedelta(hours=1), now=NOW),
                       {bs.CONFIRMED}, {bs.ADMIN}),
        "mark_completed": (lambda b, a: bs.mark_completed(b, actor=a, now=LATER + timedelta(hours=2)),
                           {bs.CONFIRMED}, set(bs.DECIDERS)),
        "mark_no_show": (lambda b, a: bs.mark_no_show(b, actor=a, now=LATER + timedelta(hours=2)),
                         {bs.CONFIRMED}, set(bs.DECIDERS)),
    }


@pytest.mark.parametrize("name", list(_calls()))
@pytest.mark.parametrize("state", bs.STATES)
@pytest.mark.parametrize("actor", bs.ACTORS)
def test_each_change_only_from_its_states_and_by_its_people(name, state, actor):
    call, states, actors = _calls()[name]
    if state in states and actor in actors:
        change = call(booking(state), actor)
        assert change.from_state == state
        assert change.to_state in bs.STATES
    else:
        with pytest.raises(bs.IllegalTransition):
            call(booking(state), actor)


def test_only_a_person_on_the_business_side_confirms():
    for actor in (bs.CUSTOMER, bs.SYSTEM):
        with pytest.raises(bs.IllegalTransition):
            bs.confirm(booking(), actor=actor, now=NOW)
    change = bs.confirm(booking(), actor=bs.OWNER, now=NOW)
    assert change.to_state == bs.CONFIRMED
    assert change.fields["customer_notice"] == bs.NOTICE_CONFIRMED
    assert change.fields["decided_by"] == bs.OWNER


def test_a_booking_in_the_past_cannot_be_confirmed():
    with pytest.raises(bs.IllegalTransition, match="past"):
        bs.confirm(booking(starts=NOW - timedelta(minutes=1)), actor=bs.OWNER, now=NOW)


def test_submitted_is_the_system_moving_a_request_to_the_owner():
    change = bs.submitted(booking(bs.REQUESTED), provider_chat_id=9, provider_message_id=55)
    assert (change.from_state, change.to_state) == (bs.REQUESTED, bs.PENDING)
    with pytest.raises(bs.IllegalTransition):
        bs.submitted(booking(bs.PENDING), provider_chat_id=9, provider_message_id=55)


def test_the_customer_changing_a_request_sends_it_back_to_the_owner():
    new = LATER + timedelta(days=1)
    change = bs.change_request(booking(bs.PENDING), starts_at=new, ends_at=new + timedelta(hours=1),
                               blocked_until=new + timedelta(hours=1), now=NOW)
    assert change.to_state == bs.REQUESTED
    assert change.fields["starts_at"] == new and change.fields["provider_message_id"] is None
    with pytest.raises(bs.IllegalTransition):
        bs.change_request(booking(bs.CONFIRMED), starts_at=new, ends_at=new + timedelta(hours=1),
                          blocked_until=new, now=NOW)


def test_owner_proposal_then_customer_accepts_confirms_the_proposed_time():
    new = LATER + timedelta(hours=2)
    proposed = bs.propose(booking(), actor=bs.OWNER, starts_at=new, ends_at=new + timedelta(hours=1), now=NOW)
    assert proposed.to_state == bs.PENDING and proposed.fields["proposed_by"] == bs.OWNER
    assert proposed.fields["customer_notice"] == bs.NOTICE_PROPOSED
    b = booking(proposed_by=bs.OWNER, proposed=new)
    # The owner can't accept their own proposal; the customer can.
    with pytest.raises(bs.IllegalTransition):
        bs.accept_proposal(b, actor=bs.OWNER, now=NOW, blocked_until=new + timedelta(hours=1))
    change = bs.accept_proposal(b, actor=bs.CUSTOMER, now=NOW, blocked_until=new + timedelta(hours=1))
    assert change.to_state == bs.CONFIRMED
    assert change.fields["starts_at"] == new and change.fields["proposed_by"] is None


def test_customer_move_request_needs_the_owner_and_keeps_the_number():
    new = LATER + timedelta(days=1)
    change = bs.propose(booking(bs.CONFIRMED), actor=bs.CUSTOMER, starts_at=new,
                        ends_at=new + timedelta(hours=1), now=NOW)
    assert change.to_state == bs.CONFIRMED and "customer_notice" not in change.fields
    b = booking(bs.CONFIRMED, proposed_by=bs.CUSTOMER, proposed=new)
    with pytest.raises(bs.IllegalTransition):
        bs.accept_proposal(b, actor=bs.CUSTOMER, now=NOW, blocked_until=new)
    moved = bs.accept_proposal(b, actor=bs.OWNER, now=NOW, blocked_until=new + timedelta(hours=1))
    assert moved.action == "reschedule" and moved.fields["customer_notice"] == bs.NOTICE_RESCHEDULED
    kept = bs.reject_proposal(b, actor=bs.OWNER, now=NOW)
    assert kept.to_state == bs.CONFIRMED and kept.fields["customer_notice"] == bs.NOTICE_KEPT


def test_a_customer_cannot_move_a_request_that_is_not_confirmed_by_proposing():
    with pytest.raises(bs.IllegalTransition):
        bs.propose(booking(bs.PENDING), actor=bs.CUSTOMER, starts_at=LATER, ends_at=LATER + timedelta(hours=1), now=NOW)


def test_turning_down_the_owners_time_for_a_request_cancels_it():
    b = booking(proposed_by=bs.OWNER, proposed=LATER + timedelta(hours=2))
    change = bs.reject_proposal(b, actor=bs.CUSTOMER, now=NOW)
    assert change.to_state == bs.CANCELLED and change.fields["cancelled_by"] == bs.CUSTOMER


def test_nothing_to_accept_without_a_proposal():
    with pytest.raises(bs.IllegalTransition, match="no proposed time"):
        bs.accept_proposal(booking(), actor=bs.CUSTOMER, now=NOW, blocked_until=LATER)


def test_expire_only_after_the_start_and_only_unanswered():
    with pytest.raises(bs.IllegalTransition, match="not started"):
        bs.expire(booking(), now=NOW)
    change = bs.expire(booking(starts=NOW - timedelta(minutes=5)), now=NOW)
    assert change.to_state == bs.CANCELLED and change.fields["cancelled_by"] == bs.SYSTEM
    with pytest.raises(bs.IllegalTransition):
        bs.expire(booking(bs.CONFIRMED, starts=NOW - timedelta(minutes=5)), now=NOW)


def test_completion_and_no_show_only_after_the_start_and_only_by_a_person():
    with pytest.raises(bs.IllegalTransition, match="before it starts"):
        bs.mark_no_show(booking(bs.CONFIRMED), actor=bs.OWNER, now=NOW)
    with pytest.raises(bs.IllegalTransition):
        bs.mark_no_show(booking(bs.CONFIRMED, starts=NOW - timedelta(hours=2)), actor=bs.SYSTEM, now=NOW)


def test_final_states_are_final():
    for state in bs.FINAL:
        for call, _, _ in _calls().values():
            with pytest.raises(bs.IllegalTransition):
                call(booking(state), bs.ADMIN)


def test_attendance_is_confirmed_by_the_customer_for_a_future_confirmed_booking():
    change = bs.confirm_attendance(booking(bs.CONFIRMED), now=NOW)
    assert change.fields["attendance_confirmed_at"] == NOW and change.to_state == bs.CONFIRMED
    with pytest.raises(bs.IllegalTransition):
        bs.confirm_attendance(booking(bs.PENDING), now=NOW)
