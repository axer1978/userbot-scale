"""Bookability from opening hours, bookings and limits; free-slot suggestions."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import availability as av
from availability import Busy, Limits, Rule

TZ = "Europe/Riga"
RIGA = ZoneInfo(TZ)
MON = date(2026, 10, 5)
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=RIGA)  # Thursday before MON
WEEKDAYS = [Rule(d, time(9), time(17)) for d in range(5)]
EVERY_DAY = [Rule(d, time(9), time(17)) for d in range(7)]


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=RIGA)


def later(dt: datetime, **delta) -> datetime:
    """Real elapsed time: `+` on an aware datetime adds wall-clock time."""
    return (dt.astimezone(timezone.utc) + timedelta(**delta)).astimezone(dt.tzinfo)


def check(start, end, *, rules=WEEKDAYS, busy=(), now=NOW, limits=Limits()):
    return av.check_slot(start, end, rules=rules, busy=list(busy), tz=TZ, now=now, limits=limits)


def slots(day_from, days=1, duration=60, *, rules=WEEKDAYS, busy=(), now=NOW, limits=Limits(), limit=None):
    return av.free_slots(day_from=day_from, days=days, duration_minutes=duration, rules=rules,
                         busy=list(busy), tz=TZ, now=now, limits=limits, limit=limit)


def hours(result):
    return [(s.hour, s.minute) for s in result]


# -- check_slot: each reason ------------------------------------------------

def test_ok_inside_hours():
    result = check(at(MON, 10), at(MON, 11))
    assert result == av.SlotCheck(True, None, 0)


def test_unaligned_start_is_fine():
    assert check(at(MON, 14, 10), at(MON, 15, 10)).ok


@pytest.mark.parametrize("end_hour", [10, 9])
def test_invalid_when_end_not_after_start(end_hour):
    assert check(at(MON, 10), at(MON, end_hour)).reason == "invalid"


def test_invalid_beats_past():
    assert check(at(MON, 10), at(MON, 10), now=at(MON, 12)).reason == "invalid"


@pytest.mark.parametrize("start", [NOW, NOW - timedelta(hours=1)])
def test_past(start):
    assert check(start, start + timedelta(hours=1), rules=[]).reason == "past"


def test_too_soon():
    limits = Limits(min_notice_minutes=120)
    start = NOW + timedelta(minutes=60)
    assert check(start, start + timedelta(hours=1), rules=[], limits=limits).reason == "too_soon"
    start = NOW + timedelta(minutes=120)
    assert check(start, start + timedelta(hours=1), rules=[], limits=limits).ok


def test_too_far():
    # Real days, in UTC: Oct 1 + 30 days crosses the October clock change.
    limits = Limits(max_days_ahead=30)
    start = later(NOW, days=30, minutes=1)
    assert check(start, start + timedelta(hours=1), rules=[], limits=limits).reason == "too_far"
    start = later(NOW, days=30)
    assert check(start, later(start, hours=1), rules=[], limits=limits).ok


def test_closed_day():
    limits = Limits(closed_dates=frozenset({MON}))
    assert check(at(MON, 10), at(MON, 11), limits=limits).reason == "closed_day"


def test_closed_date_is_the_local_date():
    # 23:30 UTC on Oct 5 is already 02:30 on Oct 6 in Riga (UTC+3).
    start = datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    tue = MON + timedelta(days=1)
    assert check(start, end, rules=[], limits=Limits(closed_dates=frozenset({tue}))).reason == "closed_day"
    assert check(start, end, rules=[], limits=Limits(closed_dates=frozenset({MON}))).ok


@pytest.mark.parametrize("start,end", [
    (at(MON, 8), at(MON, 9)),                     # before opening
    (at(MON, 8, 30), at(MON, 9, 30)),             # starts before opening
    (at(MON, 16, 30), at(MON, 17, 30)),           # runs past closing
    (at(MON, 17), at(MON, 18)),                   # after closing
    (at(MON, 16), at(MON + timedelta(days=1), 10)),  # spills into the next day
    (at(date(2026, 10, 3), 10), at(date(2026, 10, 3), 11)),  # Saturday: no rule
])
def test_outside_hours(start, end):
    assert check(start, end).reason == "outside_hours"


def test_window_edges_are_inclusive():
    assert check(at(MON, 9), at(MON, 17)).ok


def test_conflict_with_overlap():
    busy = [Busy(at(MON, 10), at(MON, 11))]
    assert check(at(MON, 10, 30), at(MON, 11, 30), busy=busy).reason == "conflict"
    assert check(at(MON, 9, 30), at(MON, 10, 30), busy=busy).reason == "conflict"
    assert check(at(MON, 9), at(MON, 12), busy=busy).reason == "conflict"


def test_back_to_back_is_allowed():
    busy = [Busy(at(MON, 10), at(MON, 11))]
    assert check(at(MON, 11), at(MON, 12), busy=busy).ok
    assert check(at(MON, 9), at(MON, 10), busy=busy).ok


def test_busy_in_another_zone_compares_by_instant():
    # 07:00-08:00 UTC is 10:00-11:00 in Riga.
    busy = [Busy(datetime(2026, 10, 5, 7, tzinfo=timezone.utc), datetime(2026, 10, 5, 8, tzinfo=timezone.utc))]
    assert check(at(MON, 10, 30), at(MON, 11, 30), busy=busy).reason == "conflict"
    assert check(at(MON, 11), at(MON, 12), busy=busy).ok


# -- buffers ----------------------------------------------------------------

BUFFERED = [Rule(0, time(9), time(17), slot_minutes=60, buffer_minutes=15)]


def test_buffer_reported_from_matching_rule():
    assert check(at(MON, 10), at(MON, 11), rules=BUFFERED) == av.SlotCheck(True, None, 15)


def test_own_buffer_overlapping_next_booking_is_a_conflict():
    busy = [Busy(at(MON, 11), at(MON, 12, 15))]
    result = check(at(MON, 10), at(MON, 11), rules=BUFFERED, busy=busy)
    assert result.reason == "conflict" and result.buffer_minutes == 15
    assert check(at(MON, 9, 45), at(MON, 10, 45), rules=BUFFERED, busy=busy).ok


def test_start_inside_previous_blocked_until_is_a_conflict():
    busy = [Busy(at(MON, 10), at(MON, 11, 15))]  # 10-11 plus its 15 min buffer
    assert check(at(MON, 11), at(MON, 12), rules=BUFFERED, busy=busy).reason == "conflict"
    assert check(at(MON, 11, 15), at(MON, 12, 15), rules=BUFFERED, busy=busy).ok


def test_buffer_may_run_past_closing():
    # The buffer is cleanup time, not customer time: a 16-17 booking still fits.
    assert check(at(MON, 16), at(MON, 17), rules=BUFFERED).ok


def test_free_slots_skip_slot_blocked_by_buffer():
    busy = [Busy(at(MON, 11), at(MON, 12, 15))]
    result = slots(MON, rules=BUFFERED, busy=busy)
    assert (10, 0) not in hours(result) and (12, 0) not in hours(result)
    assert (9, 0) in hours(result) and (13, 0) in hours(result)


# -- split shifts -----------------------------------------------------------

SPLIT = [Rule(0, time(13), time(18)), Rule(0, time(9), time(12))]


def test_split_shift_booking_across_lunch_is_outside_hours():
    assert check(at(MON, 11, 30), at(MON, 13, 30), rules=SPLIT).reason == "outside_hours"
    assert check(at(MON, 12), at(MON, 13), rules=SPLIT).reason == "outside_hours"


def test_split_shift_each_window_is_bookable():
    assert check(at(MON, 11), at(MON, 12), rules=SPLIT).ok
    assert check(at(MON, 13), at(MON, 14), rules=SPLIT).ok


def test_split_shift_free_slots():
    assert hours(slots(MON, rules=SPLIT)) == [(9, 0), (10, 0), (11, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0)]


def test_split_shift_buffer_comes_from_the_matching_window():
    rules = [Rule(0, time(9), time(12), buffer_minutes=5), Rule(0, time(13), time(18), buffer_minutes=20)]
    assert check(at(MON, 9), at(MON, 10), rules=rules).buffer_minutes == 5
    assert check(at(MON, 14), at(MON, 15), rules=rules).buffer_minutes == 20


# -- no rules ---------------------------------------------------------------

def test_no_rules_enforces_only_limits_and_conflicts():
    sat_night = date(2026, 10, 3)
    assert check(at(sat_night, 3), at(sat_night, 4), rules=[]) == av.SlotCheck(True, None, 0)
    busy = [Busy(at(sat_night, 3, 30), at(sat_night, 4, 30))]
    assert check(at(sat_night, 3), at(sat_night, 4), rules=[], busy=busy).reason == "conflict"
    limits = Limits(closed_dates=frozenset({sat_night}))
    assert check(at(sat_night, 3), at(sat_night, 4), rules=[], limits=limits).reason == "closed_day"


def test_no_rules_offers_no_free_slots():
    assert slots(MON, days=7, rules=[]) == []


# -- DST (Europe/Riga) -----------------------------------------------------

SPRING = date(2026, 3, 29)  # 03:00 EET -> 04:00 EEST
FALL = date(2026, 10, 25)   # 04:00 EEST -> 03:00 EET
EARLY_NOW = datetime(2026, 3, 1, 12, tzinfo=RIGA)


@pytest.mark.parametrize("day,before,after", [(SPRING, 2, 3), (FALL, 3, 2)])
def test_dst_day_keeps_wall_clock_slots(day, before, after):
    result = slots(day - timedelta(days=1), days=2, rules=EVERY_DAY, now=EARLY_NOW)
    assert len(result) == 16
    prev, today = result[:8], result[8:]
    assert hours(prev) == hours(today) == [(h, 0) for h in range(9, 17)]
    assert all(s.date() == day - timedelta(days=1) for s in prev)
    assert all(s.date() == day for s in today)
    assert {s.utcoffset() for s in prev} == {timedelta(hours=before)}
    assert {s.utcoffset() for s in today} == {timedelta(hours=after)}
    assert all(s.tzinfo is RIGA or str(s.tzinfo) == TZ for s in result)


def test_spring_forward_window_across_the_gap():
    # 00:00-06:00 on the spring-forward night holds five real hours; the
    # nonexistent 03:00 falls onto 04:00 and is offered only once.
    result = slots(SPRING, rules=[Rule(6, time(0), time(6))], now=EARLY_NOW)
    assert hours(result) == [(0, 0), (1, 0), (2, 0), (4, 0), (5, 0)]
    utc = [s.astimezone(timezone.utc) for s in result]
    assert utc == sorted(set(utc))
    assert all((b - a) >= timedelta(hours=1) for a, b in zip(utc, utc[1:]))


def test_fall_back_window_across_the_repeat():
    result = slots(FALL, rules=[Rule(6, time(0), time(6))], now=EARLY_NOW)
    assert hours(result) == [(h, 0) for h in range(6)]
    utc = [s.astimezone(timezone.utc) for s in result]
    assert utc == sorted(set(utc))


def test_check_slot_on_nonexistent_and_ambiguous_times_does_not_crash():
    night = [Rule(6, time(0), time(6))]
    gap = datetime(2026, 3, 29, 3, 30, tzinfo=RIGA)  # does not exist on the clock
    assert check(gap, later(gap, minutes=30), rules=night, now=EARLY_NOW).ok
    for fold in (0, 1):
        repeat = datetime(2026, 10, 25, 3, 30, fold=fold, tzinfo=RIGA)
        assert check(repeat, later(repeat, minutes=30), rules=night, now=EARLY_NOW).ok


def test_dst_day_window_measured_in_real_time():
    # 09:00-17:00 is eight real hours on both change days, so an 8 h booking fits.
    for day in (SPRING, FALL):
        assert check(at(day, 9), at(day, 17), rules=EVERY_DAY, now=EARLY_NOW).ok
        assert check(at(day, 9), at(day, 17, 1), rules=EVERY_DAY, now=EARLY_NOW).reason == "outside_hours"


# -- free_slots -------------------------------------------------------------

def test_free_slots_full_day_and_days_span():
    assert hours(slots(MON)) == [(h, 0) for h in range(9, 17)]
    week = slots(MON, days=7)
    assert len(week) == 5 * 8
    assert week == sorted(week)
    assert {s.weekday() for s in week} == set(range(5))


def test_free_slots_duration_must_fit_the_window():
    assert hours(slots(MON, duration=90)) == [(h, 0) for h in range(9, 16)]


def test_free_slots_follow_slot_minutes():
    result = slots(MON, duration=30, rules=[Rule(0, time(9), time(11), slot_minutes=30)])
    assert hours(result) == [(9, 0), (9, 30), (10, 0), (10, 30)]


def test_free_slots_respect_limit():
    assert hours(slots(MON, days=7, limit=3)) == [(9, 0), (10, 0), (11, 0)]
    assert slots(MON, limit=0) == []


def test_free_slots_skip_past_and_min_notice():
    now = at(MON, 10, 30)
    result = slots(MON, now=now, limits=Limits(min_notice_minutes=60), limit=3)
    assert hours(result) == [(12, 0), (13, 0), (14, 0)]


def test_free_slots_skip_busy_closed_and_too_far():
    tue = MON + timedelta(days=1)
    busy = [Busy(at(MON, 10), at(MON, 12))]
    result = slots(MON, days=2, busy=busy, limits=Limits(closed_dates=frozenset({tue})))
    assert hours(result) == [(9, 0)] + [(h, 0) for h in range(12, 17)]
    assert slots(MON, now=NOW, limits=Limits(max_days_ahead=2)) == []


def test_free_slots_are_aware_in_tenant_zone():
    for s in slots(MON):
        assert s.utcoffset() == timedelta(hours=3)
        assert s.tzinfo is not None


# -- suggest_near -----------------------------------------------------------

def near(requested, duration=60, **kw):
    kw.setdefault("rules", WEEKDAYS)
    kw.setdefault("busy", [])
    kw.setdefault("now", NOW)
    return av.suggest_near(requested, duration, tz=TZ, **kw)


def test_suggest_near_orders_by_closeness_ties_earlier():
    busy = [Busy(at(MON, 12), at(MON, 13))]
    assert hours(near(at(MON, 12), busy=busy)) == [(11, 0), (13, 0), (10, 0)]


def test_suggest_near_excludes_the_requested_slot():
    result = near(at(MON, 12), count=4)
    assert at(MON, 12) not in result
    assert hours(result) == [(11, 0), (13, 0), (10, 0), (14, 0)]


def test_suggest_near_unaligned_request():
    assert hours(near(at(MON, 14, 10), count=2)) == [(14, 0), (15, 0)]


def test_suggest_near_rolls_into_following_days():
    fully_booked = [Busy(at(MON, 9), at(MON, 17))]
    result = near(at(MON, 12), busy=fully_booked, count=2)
    tue = MON + timedelta(days=1)
    assert result == [at(tue, 9), at(tue, 10)]


def test_suggest_near_respects_search_days():
    fully_booked = [Busy(at(MON, 9), at(MON, 17))]
    assert near(at(MON, 12), busy=fully_booked, search_days=1) == []


# -- validation and helpers -------------------------------------------------

def test_naive_datetimes_raise():
    naive = datetime(2026, 10, 5, 10)
    with pytest.raises(ValueError):
        check(naive, at(MON, 11))
    with pytest.raises(ValueError):
        check(at(MON, 10), naive)
    with pytest.raises(ValueError):
        check(at(MON, 10), at(MON, 11), now=datetime(2026, 10, 1, 8))
    with pytest.raises(ValueError):
        check(at(MON, 10), at(MON, 11), busy=[Busy(naive, at(MON, 12))])
    with pytest.raises(ValueError):
        slots(MON, now=datetime(2026, 10, 1, 8))
    with pytest.raises(ValueError):
        near(naive)
    with pytest.raises(ValueError):
        av.describe(naive, TZ)


def test_rule_from_row_accepts_time_objects_and_strings():
    as_times = av.rule_from_row({"weekday": 2, "start_time": time(9, 30), "end_time": time(18),
                                 "slot_minutes": 30, "buffer_minutes": 10})
    as_text = av.rule_from_row({"weekday": 2, "start_time": "09:30", "end_time": "18:00",
                                "slot_minutes": 30, "buffer_minutes": 10})
    with_seconds = av.rule_from_row({"weekday": 2, "start_time": "09:30:00", "end_time": "18:00:00",
                                     "slot_minutes": 30, "buffer_minutes": 10})
    assert as_times == as_text == with_seconds == Rule(2, time(9, 30), time(18), 30, 10)


def test_rule_from_row_defaults_missing_minutes():
    rule = av.rule_from_row({"weekday": 0, "start_time": "09:00", "end_time": "17:00",
                             "slot_minutes": None})
    assert rule.slot_minutes == 60 and rule.buffer_minutes == 0


@pytest.mark.parametrize("kwargs", [
    dict(weekday=7, start=time(9), end=time(17)),
    dict(weekday=0, start=time(17), end=time(9)),
    dict(weekday=0, start=time(9), end=time(9)),
    dict(weekday=0, start=time(9), end=time(17), slot_minutes=0),
    dict(weekday=0, start=time(9), end=time(17), buffer_minutes=-5),
])
def test_bad_rules_raise(kwargs):
    with pytest.raises(ValueError):
        Rule(**kwargs)


def test_describe_in_tenant_zone():
    assert av.describe(datetime(2026, 10, 2, 11, tzinfo=timezone.utc), TZ) == "Fri 02 Oct 14:00"
    assert av.describe(datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc), TZ) == "Mon 26 Oct 01:30"


def test_every_reason_is_known():
    assert set(av.REASONS) == {"past", "too_soon", "too_far", "closed_day", "outside_hours", "conflict", "invalid"}
